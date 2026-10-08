"""Fixed historical F3/G3 comparison using the production pure decisions.

Run from the package root: python3 -m backtesting.compare --data-root PATH.
The input directory is read-only; its complete original manifest is verified.
"""
import argparse
import bisect
import copy
import csv
import gzip
import hashlib
import inspect
import json
import multiprocessing
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal as D
from pathlib import Path
from . import reference_f3 as f3
from g3core import strategy as g3

ROOT = Path(__file__).resolve().parents[1]
MINUTE, HOUR, DAY = f3.MINUTE, f3.HOUR, f3.DAY
# Use exact timezone-qualified endpoints from the preregistered design.
DESIGN = json.loads((ROOT / 'reports/design.json').read_text())
START, END = [int(datetime.fromisoformat(x.replace('Z','+00:00')).timestamp()*1000) for x in DESIGN['window_utc']]
CAPITAL = D(DESIGN['capital_usdt'])
REQUIRED_FILES = ('recent/spot.csv.gz','recent/swap.csv.gz','recent/funding.json','recent/mark.json',
                  'recent/candles-quality.json','recent/funding-quality.json','recent/mark-quality.json',
                  'spot-spec.json','swap-spec.json','tiers.json')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def iso(t):
    return datetime.fromtimestamp(t/1000, timezone.utc).isoformat()


def data_load(root):
    manifest = json.loads((root/'manifest.json').read_text())
    for name in REQUIRED_FILES:
        expected=manifest['files'][name]
        if sha(root/name) != expected:
            raise ValueError('Historical input checksum mismatch: '+name)
    def bars(name):
        with gzip.open(root/'recent'/name, 'rt', newline='') as stream:
            return [(int(r['ts']), *[r[k] for k in ('open','high','low','close')]) for r in csv.DictReader(stream)]
    spot, swap = bars('spot.csv.gz'), bars('swap.csv.gz')
    if len(spot) != len(swap):
        raise ValueError('Mismatched minute arrays')
    for i, (s,p) in enumerate(zip(spot,swap)):
        if s[0] != p[0] or (i and s[0]-spot[i-1][0] != MINUTE):
            raise ValueError('Minute gap/unaligned instruments')
        for row in (s,p):
            o,h,l,c = map(D,row[1:])
            if l <= 0 or h < max(o,l,c) or l > min(o,h,c):
                raise ValueError('Invalid candle')
    if spot[0][0] > START-MINUTE or spot[-1][0] < END-MINUTE:
        raise ValueError('Requested window not covered')
    events = json.loads((root/'recent/funding.json').read_text(), parse_float=str)
    for row in events:
        if row.get('actual_ts',row['ts']) > row['ts']:
            raise ValueError('Settlement was moved earlier')
    normalized = f3._events(events)
    if len(normalized) != len(events):
        raise ValueError('Duplicate settlement')
    if any(b[0]-a[0] > 8*HOUR+MINUTE for a,b in zip(normalized,normalized[1:])):
        raise ValueError('Missing settlement')
    if f3.funding_frame(events,START,14) is None or normalized[-1][0] < END-8*HOUR:
        raise ValueError('Funding coverage incomplete')
    markrows=json.loads((root/'recent/mark.json').read_text(),parse_float=str)
    marks={int(r[0]):r for r in markrows}
    if len(marks)!=len(markrows) or any(t not in marks for t in range(START,END,HOUR)):
        raise ValueError('Hourly mark gaps/duplicates')
    if any(t not in marks for t,_ in normalized if START<=t<END):
        raise ValueError('Missing settlement mark proxy')
    spot_spec=json.loads((root/'spot-spec.json').read_text())['response']['data'][0]
    swap_spec=json.loads((root/'swap-spec.json').read_text())['response']['data'][0]
    tier=json.loads((root/'tiers.json').read_text())['response']['data'][0]
    if spot_spec['instId']!='BTC-USDT' or swap_spec['instId']!='BTC-USD-SWAP' or swap_spec['ctType']!='inverse' or swap_spec['ctValCcy']!='USD':
        raise ValueError('Incorrect instruments')
    spec=dict(spot_lot=spot_spec['lotSz'],spot_min=spot_spec['minSz'],spot_tick=spot_spec['tickSz'],
              spot_max=spot_spec['maxMktSz'],swap_lot=swap_spec['lotSz'],swap_min=swap_spec['minSz'],
              swap_tick=swap_spec['tickSz'],contract_usd=str(D(swap_spec['ctVal'])*D(swap_spec['ctMult'])),
              tier_max_contracts=tier['maxSz'],mmr=tier['mmr'])
    return spot,swap,events,dict(normalized),marks,spec


class FundingCache:
    """Exact memoization; signal changes only when its selected records change.

    Freshness is included separately because an 8h+1m gap can become stale
    during a minute with unchanged record indexes. Values remain production
    Decimal results; this cache neither fits parameters nor forecasts rates.
    """
    def __init__(self, events, core, sf, pf, slip):
        self.events,self.core,self.sf,self.pf,self.slip=events,core,sf,pf,slip
        self.times=[t for t,_ in core._events(events)]
        self.key=None
        self.value=None
        self.calls=0
    def get(self,t):
        cutoff=t-MINUTE
        right=bisect.bisect_right(self.times,cutoff)
        left=bisect.bisect_right(self.times,cutoff-14*DAY)
        stale=not right or cutoff-self.times[right-1]>8*HOUR
        key=(left,right,stale)
        if key!=self.key:
            self.value=self.core.entry_signal(self.events,t,self.sf,self.pf,self.slip)
            self.key=key
            self.calls+=1
        result=copy.deepcopy(self.value)
        if result['frame'] is not None:
            result['frame']['known_at']=t
        return result


def offline(t,scenario):
    if scenario not in ('monday_0855_0915_bjt_outage','daily_0855_0915_bjt_outage'):
        return False
    local=t+8*HOUR
    minute=local%DAY//MINUTE
    return 8*60+55<=minute<9*60+15 and (scenario.startswith('daily') or (local//DAY+3)%7==0)


def market(spec,sp,pp,mark):
    return dict(spec=spec,spot={'bid':str(sp),'ask':str(sp)},swap={'bid':str(pp),'ask':str(pp)},mark=str(mark))


def simulate(data,version,scenario):
    spot,swap,events,funding,marks,spec=data
    core=f3 if version=='F3' else g3
    saved_rules=dict(core.RULES)
    if scenario=='normal_cost2':
        for k in ('spot_fee','swap_fee','adverse_bps'):
            core.RULES[k]=str(D(core.RULES[k])*2)
    sf,pf,slip=[D(core.RULES[k]) for k in ('spot_fee','swap_fee','adverse_bps')]
    cache=FundingCache(events,core,sf,pf,slip)
    cfg={'capital_usdt':str(CAPITAL),'max_loss_usdt':DESIGN['model_max_loss_usdt']}
    cash=CAPITAL; coins=D(0); pos=None; pending=None; close_due=None; treasury_due=None
    cooldown=START; peak=CAPITAL; report_peak=CAPITAL; maxdd=D(0); minratio=None; maxdelta=D(0); halted=False
    rejections=Counter(); counts=Counter(); trades=[]; signals=[]; curve=[]; funding_rows=[]
    last_entry=None; last_t=None; last_market=None; offline_was=False; fees_paid=D(0); funding_value=D(0)
    def account(m):
        actual=None; eq=coins
        if pos:
            n=D(pos['notional_usd']); entry=D(pos['swap_entry']); mark=D(m['mark'])
            eq+=n*(1/mark-1/entry)
            actual={'contracts':str(-D(pos['contracts'])),'avgPx':str(entry)}
        return dict(balances={'USDT':{'cash':str(cash),'available':str(cash)},'BTC':{'cash':str(coins),'eq':str(eq),'available':str(max(D(0),coins))}},position=actual,fees={'spot':str(sf),'swap':str(pf)})
    def nav(m):
        return D(core.valuation(m,account(m)))
    def exit_position(t,m,reason):
        nonlocal cash,coins,pos,cooldown,fees_paid,close_due,treasury_due
        before=pos['entry_equity']; n=D(pos['notional_usd']); cover=core._price(m['swap']['ask'],D(spec['swap_tick']),True)
        swap_fee=n/cover*pf
        coins+=n*(1/cover-1/D(pos['swap_entry']))-swap_fee
        if coins<0:raise ValueError('Negative modeled collateral; cannot execute model liquidation')
        sold=core.round_step(coins,spec['spot_lot']); dust=coins-sold
        if sold<D(spec['spot_min']):raise ValueError('Cannot sell below minimum')
        price=core._price(m['spot']['bid'],D(spec['spot_tick']),False)
        fee=sold*price*sf
        cash+=sold*price-fee
        fees_paid+=swap_fee*D(m['spot']['bid'])+fee
        pos.update(exited_at=t,exit_utc=iso(t),exit_reason=reason,swap_exit=str(cover),spot_exit=str(price),
                   end_equity=str(cash),net_usdt=str(cash-D(before)),dust_btc_written_off=str(dust),
                   swap_exit_fee_btc=str(swap_fee),spot_exit_fee_usdt=str(fee),funding_usdt_on_receipt=str(sum(D(r['usdt_on_receipt']) for r in pos['funding_events'])))
        trades.append(pos);pos=None;coins=D(0);close_due=None;treasury_due=None;cooldown=t+24*HOUR
    try:
        first=bisect.bisect_left([r[0] for r in spot],START)
        for i in range(first,len(spot)):
            s,p=spot[i],swap[i];t=s[0]
            if t>=END:break
            prev_s,prev_p=spot[i-1],swap[i-1]
            # All risk/signal evaluation uses prices already complete at t.
            risk_mark=marks[t][1] if t%HOUR==0 else prev_p[4]
            mr=market(spec,prev_s[4],prev_p[4],risk_mark)
            mf=market(spec,s[1],p[1],marks[t][1] if t in marks else p[1])
            is_offline=offline(t,scenario)
            if is_offline and not offline_was:
                if pending: counts['entry_signal_discarded_on_disconnect']+=1
                pending=None
            offline_was=is_offline
            if pos and pos['opened_at']<t and t in funding:
                rate=funding[t]
                # Conservative ambiguous exact-close-boundary treatment.
                eligible=rate<0 or close_due is None or close_due['due']>t or is_offline
                received=D(pos['notional_usd'])/D(marks[t][1])*rate if eligible else D(0)
                coins+=received;pos['funding_btc']=str(D(pos['funding_btc'])+received)
                value=received*D(s[1]);funding_value+=value
                row=dict(ts=t,utc=iso(t),rate=str(rate),mark_proxy=str(marks[t][1]),btc=str(received),usdt_on_receipt=str(value),eligible=eligible,offline=is_offline)
                pos['funding_events'].append(row);funding_rows.append(row)
            if not is_offline:
                if close_due and close_due['due']<=t:
                    exit_position(t,mf,close_due['reason'])
                if pending and t>=pending['due']:
                    saved=pending;pending=None
                    if t>saved['due']+MINUTE:
                        rejections['expired_entry_signal']+=1
                    elif pos is None and not halted and t>=cooldown:
                        verdict=cache.get(t)
                        a=account(mf)
                        plan=core.plan_entry(dict(cfg,capital_usdt=str(min(CAPITAL,nav(mf)))),mf,a)
                        if not verdict['allowed']:rejections['recheck_'+str(verdict['reason'])]+=1
                        elif not plan['allowed']:rejections['sizing_'+str(plan['reason'])]+=1
                        else:
                            before=cash;cash-=D(plan['estimated_spot_cost']);coins=D(plan['target_collateral_btc'])
                            fees_paid+=(D(plan['estimated_spot_fee_btc'])+D(plan['estimated_swap_entry_fee_btc']))*D(s[1])
                            pos=dict(opened_at=t,entry_utc=iso(t),entry_equity=str(before),notional_usd=plan['notional_usd'],contracts=plan['contracts'],swap_entry=plan['estimated_swap_entry'],spot_entry=plan['estimated_spot_entry'],gross_btc=plan['gross_btc'],spot_fee_btc=plan['estimated_spot_fee_btc'],swap_entry_fee_btc=plan['estimated_swap_entry_fee_btc'],funding_btc='0',treasury_sold_btc='0',bad_days=0,last_daily_check=None,funding_events=[],treasury_sales=[],entry_known_at=saved['known_at'],entry_frame=saved['frame'],recheck_frame=verdict['frame'],allocation_method=plan['allocation_method'])
                            counts['opened_groups']+=1
                            signals.append(dict(kind='entry_fill',known_at=saved['known_at'],executed_at=t,utc=iso(t),notional_usd=plan['notional_usd']))
                # Reconcile/evaluate at current boundary; current open proxies the fresh
                # market after modeled fills. Otherwise use completed prior minute.
                used=mf if pos and pos['opened_at']==t else mr
                ev=core.evaluate_position(cfg,used,account(used),pos,events,t,peak_equity_usdt=str(peak))
                if ev['halt']:halted=True
                if pos:
                    pos['bad_days']=ev['bad_days'];pos['last_daily_check']=ev['last_daily_check']
                    if ev['exit_reason'] and close_due is None:
                        close_due=dict(due=t+MINUTE,reason=ev['exit_reason']);treasury_due=None
                        signals.append(dict(kind='exit_signal',known_at=t,due=t+MINUTE,reason=ev['exit_reason']))
                    elif close_due is None:
                        if treasury_due is not None and t>=treasury_due:
                            treasury_due=None
                            # Recheck constraints against fresh execution-time quote.
                            fresh=core.evaluate_position(cfg,mf,account(mf),pos,events,t,peak_equity_usdt=str(peak))
                            qty=D(fresh['treasury_btc'])
                            if qty>0 and not fresh['exit_reason'] and not halted:
                                price=core._price(mf['spot']['bid'],D(spec['spot_tick']),False);fee=qty*price*sf
                                coins-=qty;cash+=qty*price-fee;fees_paid+=fee
                                pos['treasury_sold_btc']=str(D(pos['treasury_sold_btc'])+qty)
                                pos['treasury_sales'].append(dict(ts=t,btc=str(qty),price=str(price),fee_usdt=str(fee)))
                        elif D(ev['treasury_btc'])>0:treasury_due=t+MINUTE
                elif not halted and t>=cooldown and pending is None:
                    eligible=core.is_decision_time(t,last_entry) if version=='F3' else core.entry_decision_minute(t,last_entry,START) is not None
                    if eligible:
                        last_entry=t;counts['entry_evaluations']+=1
                        verdict=cache.get(t)
                        if verdict['allowed']:
                            due=t+MINUTE if version=='F3' else core.entry_signal_due(t)
                            pending=dict(known_at=t,due=due,frame=verdict['frame']);counts['entry_signals_allowed']+=1
                            signals.append(dict(kind='entry_signal',known_at=t,due=due,frame=verdict['frame']))
                        else:rejections['signal_'+str(verdict['reason'])]+=1
            else:counts['offline_minutes']+=1
            # Monitoring metrics are omniscient reporting only; never feed offline
            # equity/peak into decisions until reconnection.
            equity=nav(mf if pos and pos['opened_at']==t else mr)
            if not is_offline:peak=max(peak,equity)
            report_peak=max(report_peak,equity)
            maxdd=max(maxdd,(report_peak-equity)/report_peak)
            if pos:
                n=D(pos['notional_usd']);mark=D(mr['mark']);eq=coins+n*(1/mark-1/D(pos['swap_entry']))
                ratio=eq*mark/n;minratio=ratio if minratio is None else min(minratio,ratio)
                maxdelta=max(maxdelta,abs((coins-n/D(pos['swap_entry']))*D(mr['spot']['bid']))/max(equity,D('1e-12')))
                counts['holding_minutes']+=1
                if ratio<=D(spec['mmr'])+pf:counts['potential_liquidation_minutes']+=1
            if t%HOUR==0:curve.append([t,str(equity)])
            last_t=t;last_market=market(spec,s[4],p[4],p[4])
        if pos:exit_position(END,last_market,'data_end')
        final=cash;report_peak=max(report_peak,final);maxdd=max(maxdd,(report_peak-final)/report_peak)
        curve.append([END,str(final)])
        if abs(sum(D(t['net_usdt']) for t in trades)-(final-CAPITAL))>D('1e-18'):
            raise ValueError('Closed trade ledger identity failed')
        summary=dict(id=version+'-'+scenario,version=version,scenario=scenario,start_utc=iso(START),end_utc_exclusive=iso(END),days=(END-START)/DAY,capital_usdt=str(CAPITAL),end_equity_usdt=str(final),net_usdt=str(final-CAPITAL),return_pct=str((final/CAPITAL-1)*100),max_drawdown_pct=str(maxdd*100),closed_groups=len(trades),treasury_sales=sum(len(x['treasury_sales']) for x in trades),opened_groups=counts['opened_groups'],account_halted=halted,holding_minutes=counts['holding_minutes'],funding_events_received=sum(r['eligible'] for r in funding_rows),funding_usdt_on_receipt=str(funding_value),fees_usdt_at_payment=str(fees_paid),minimum_collateral_ratio=str(minratio) if minratio is not None else None,max_residual_delta_pct=str(maxdelta*100),exit_reasons=dict(Counter(x['exit_reason'] for x in trades)),entry_rejections=dict(rejections),counts=dict(counts),signal_cache_actual_calculations=cache.calls)
        return dict(summary=summary,trades=trades,signals=signals,hourly_equity=curve,funding_events=funding_rows,model_checks={'closed_trade_ledger_identity':True,'no_potential_liquidation_proxy':counts['potential_liquidation_minutes']==0,'signals_delayed_and_rechecked':True},warnings=DESIGN['limitations'])
    finally:
        core.RULES.clear();core.RULES.update(saved_rules)


def parity_check(events):
    if f3.RULES!=g3.RULES:raise ValueError('Economic rules differ from F3')
    # Economic function bodies must be byte-identical; only entry scheduling differs.
    for name in ('entry_signal','plan_entry','valuation','evaluate_position','funding_frame','total_cost_rate'):
        if inspect.getsource(getattr(f3,name))!=inspect.getsource(getattr(g3,name)):
            raise ValueError('Economic implementation changed: '+name)
    cache=FundingCache(events,f3,D('.001'),D('.0005'),D('3'))
    stamps={START,START+MINUTE,END-MINUTE}
    for t,_ in f3._events(events):
        for offset in (-MINUTE,0,MINUTE,2*MINUTE,14*DAY,14*DAY+MINUTE,14*DAY+2*MINUTE):
            if START<=t+offset<END:stamps.add(t+offset)
    for t in sorted(stamps):
        if cache.get(t)!=f3.entry_signal(events,t):raise ValueError('Cached production signal mismatch')
    return {'economic_function_source_parity':True,'funding_cache_parity_points':len(stamps),'economic_rules_parity':True}


def _worker(case):
    return simulate(_SHARED_DATA,*case)


def main():
    global _SHARED_DATA
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root',type=Path,required=True)
    parser.add_argument('--freeze-only',action='store_true')
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--output',type=Path,default=ROOT/'reports/results')
    args=parser.parse_args()
    cases=[(v,s) for s in DESIGN['scenarios'] for v in DESIGN['versions']]
    paths=['backtesting/reference_f3.py','backtesting/compare.py','g3core/strategy.py','reports/design.json']
    candidate=dict(design_sha256=sha(ROOT/'reports/design.json'),source_hashes={p:sha(ROOT/p) for p in paths},data_manifest_sha256=sha(args.data_root/'manifest.json'),used_data_hashes={name:sha(args.data_root/name) for name in REQUIRED_FILES},cases=cases,execution_python=__import__('sys').executable)
    plan_path=ROOT/'reports/run-manifest.json'
    if plan_path.exists():
        existing=json.loads(plan_path.read_text())
        check=dict(candidate,cases=[list(c) for c in cases])
        if any(existing[k]!=v for k,v in check.items() if k!='execution_python'):raise ValueError('Frozen manifest changed; do not silently overwrite')
    else:save(plan_path,dict(candidate,created_utc=datetime.now(timezone.utc).isoformat(),data_root=str(args.data_root.resolve())))
    if args.freeze_only:
        print('Manifest frozen; no simulation run.');return
    started=time.monotonic();data=data_load(args.data_root);_SHARED_DATA=data;checks=parity_check(data[2]);print('Verified inputs and pure-rule parity',checks,flush=True)
    results=[]
    # fork shares immutable history in memory; each child owns scenario rule overrides.
    with multiprocessing.get_context('fork').Pool(args.workers) as pool:
        futures=[(case,pool.apply_async(_worker,(case,))) for case in cases]
        for (version,scenario),future in futures:
            result=future.get();ident=version+'-'+scenario;save(args.output/(ident+'.json'),result)
            results.append(result['summary']);print(ident,json.dumps(result['summary'],ensure_ascii=False),flush=True)
    save(args.output/'summary.json',results)
    keys=list(results[0])
    with (args.output/'summary.csv').open('w',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=keys);writer.writeheader()
        for row in results:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,dict) else v for k,v in row.items()})
    save(args.output/'validation.json',dict(checks,case_count=len(results),execution_python=__import__('sys').executable,elapsed_seconds=time.monotonic()-started,used_input_hashes_verified=True,source_manifest_preserved_verbatim=True,run_manifest_sha256=sha(plan_path),result_hashes={r['id']:sha(args.output/(r['id']+'.json')) for r in results},warning='Model checks do not validate real fills, minute marks, exchange liquidation or future performance.'))

if __name__=='__main__':main()
