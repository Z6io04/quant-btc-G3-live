"""A fixed real-data timing diagnostic, separate from the eight return cases."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from . import compare as c

ROOT=c.ROOT

def run(data):
    spot,swap,events,funding,marks,spec=data
    sm={r[0]:r for r in spot};pm={r[0]:r for r in swap}
    startup=c.START+(8*60+55)*c.MINUTE
    recovery=startup+20*c.MINUTE
    rows=[]
    for name,core in [('F3',c.f3),('G3',c.g3)]:
        pending=None;last=None;checks=0
        for t in range(startup,c.START+2*c.DAY,c.MINUTE):
            if startup<=t<recovery:
                pending=None
                continue
            if pending and t==pending['due']:
                verdict=core.entry_signal(events,t)
                m=c.market(spec,sm[t][1],pm[t][1],marks[t][1] if t in marks else pm[t][1])
                a={'balances':{'USDT':{'cash':'25','available':'25'},'BTC':{'cash':'0','eq':'0','available':'0'}},'position':None,'fees':{'spot':'.001','swap':'.0005'}}
                plan=core.plan_entry({'capital_usdt':'25'},m,a)
                if verdict['allowed'] and plan['allowed']:
                    rows.append(dict(version=name,startup_at=startup,recovery_at=recovery,signal_at=pending['known_at'],model_execution_at=t,signal_utc=c.iso(pending['known_at']),model_execution_utc=c.iso(t),minutes_from_recovery=(t-recovery)//c.MINUTE,entry_checks=checks,notional_usd=plan['notional_usd'],fresh_signal=True,rechecked_at_execution=True,signal_frame=pending['frame'],recheck_frame=verdict['frame']))
                    break
                pending=None
            eligible=core.is_decision_time(t,last) if name=='F3' else core.entry_decision_minute(t,last,startup) is not None
            if eligible:
                last=t;checks+=1;verdict=core.entry_signal(events,t)
                if verdict['allowed']:pending={'known_at':t,'due':t+c.MINUTE,'frame':verdict['frame']}
        else:rows.append(dict(version=name,no_model_entry=True))
    assert rows[1]['signal_at']==recovery and rows[1]['model_execution_at']==recovery+c.MINUTE
    assert rows[0]['model_execution_at']==c.START+c.DAY+9*c.HOUR+c.MINUTE
    return dict(description='Hypothetical service startup on 2025-09-18 at 08:55 Beijing, disconnected immediately until 09:15, then continuously online. Uses real historical prices and settled funding. This diagnostic only demonstrates scheduling; it is not another optimized return window, actual outage or real fill.',rows=rows,passed=True)

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data-root',required=True,type=Path);args=p.parse_args()
    candidate=dict(created_utc=datetime.now(timezone.utc).isoformat(),source_sha256=c.sha(__file__),data_manifest_sha256=c.sha(args.data_root/'manifest.json'),startup_bjt='2025-09-18T08:55:00+08:00',recovery_bjt='2025-09-18T09:15:00+08:00',purpose='Predefined timing demonstration without return evaluation or parameter fitting',capital_usdt='25',versions=['F3','G3'])
    plan=ROOT/'reports/recovery-diagnostic-plan.json'
    if plan.exists():
        old=json.loads(plan.read_text())
        assert all(old[k]==v for k,v in candidate.items() if k!='created_utc')
    else:c.save(plan,candidate)
    result=run(c.data_load(args.data_root));result['plan_sha256']=c.sha(plan)
    c.save(ROOT/'reports/recovery-diagnostic.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
