"""Independent output-ledger and causal-scheduling checks, not live safety proof."""
import argparse
import json
from decimal import Decimal as D
from pathlib import Path
from . import compare as c


def audit(folder):
    rows=json.loads((folder/'summary.json').read_text());records=[]
    for summary in rows:
        path=folder/(summary['id']+'.json');result=json.loads(path.read_text());groups=result['trades']
        checks=0
        for p in groups:
            assert p['opened_at']>=p['entry_known_at']+c.MINUTE
            assert p['opened_at']<=p['entry_known_at']+2*c.MINUTE
            assert not c.offline(p['opened_at'],summary['scenario'])
            assert p['recheck_frame']['known_at']==p['opened_at']
            assert p['entry_frame']['last_settlement']<=p['entry_known_at']-c.MINUTE
            assert p['recheck_frame']['last_settlement']<=p['opened_at']-c.MINUTE
            assert D(p['notional_usd'])==D(p['contracts'])*100
            assert p['exited_at']>=p['opened_at']
            if p['exit_reason']=='data_end':assert p['exited_at']==c.END
            else:assert not c.offline(p['exited_at'],summary['scenario'])
            n=D(p['notional_usd']);enter=D(p['swap_entry']);cover=D(p['swap_exit'])
            funding=sum((D(r['btc']) for r in p['funding_events']),D(0))
            assert abs(funding-D(p['funding_btc']))<D('1e-20')
            for r in p['funding_events']:
                assert p['opened_at']<r['ts']<=p['exited_at']
                expected=n/D(r['mark_proxy'])*D(r['rate']) if r['eligible'] else D(0)
                assert abs(expected-D(r['btc']))<D('1e-20')
            netcoins=D(p['gross_btc'])-D(p['spot_fee_btc'])-D(p['swap_entry_fee_btc'])+funding
            netcoins-=sum((D(r['btc']) for r in p['treasury_sales']),D(0))
            netcoins+=n*(1/cover-1/enter)-D(p['swap_exit_fee_btc'])
            sold=c.f3.round_step(netcoins,'0.00000001')
            assert abs(netcoins-sold-D(p['dust_btc_written_off']))<D('1e-20')
            cash=D(p['entry_equity'])-D(p['gross_btc'])*D(p['spot_entry'])
            cash+=sum((D(r['btc'])*D(r['price'])-D(r['fee_usdt']) for r in p['treasury_sales']),D(0))
            cash+=sold*D(p['spot_exit'])-D(p['spot_exit_fee_usdt'])
            assert abs(cash-D(p['end_equity']))<D('1e-15')
            checks+=11+len(p['funding_events'])*2
        assert abs(sum((D(p['net_usdt']) for p in groups),D(0))-D(summary['net_usdt']))<D('1e-15')
        assert abs(D(summary['end_equity_usdt'])-25-D(summary['net_usdt']))<D('1e-15')
        records.append(dict(id=summary['id'],closed_groups=len(groups),ledger_and_timing_assertions=checks+2,result_sha256=c.sha(path),passed=True))
    return dict(passed=True,cases=len(records),checks=records,source_sha256=c.sha(__file__),scope='Independent reconstruction of each BTC/USDT ledger from modeled executions and settlement rows; timing/data availability and outage entry checks. Does not validate unknown historical quotes, minute marks, private settlement bills or actual fills.')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--results',type=Path,default=c.ROOT/'reports/results');a=p.parse_args()
    result=audit(a.results);c.save(a.results/'independent-audit.json',result);print(json.dumps(result,indent=2))

if __name__=='__main__':main()
