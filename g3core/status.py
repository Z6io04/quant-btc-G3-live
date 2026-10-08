"""Read local state and probe public connectivity, without loading API credentials."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import socket
import subprocess
import time
from urllib.parse import urlsplit

from .config import data_dir,UNIT,safe_error
from .okx import OKXClient
from .store import Store


def stamp(value):
    if not value:return '尚无记录'
    return datetime.fromtimestamp(int(value)/1000,timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S +08:00')


def service(name):
    try:
        p=subprocess.run(['systemctl','--user','is-active',name],capture_output=True,text=True,timeout=3)
        value=p.stdout.strip()
        return value if value in ('active','inactive','failed','activating','deactivating') else '无法读取'
    except (OSError,subprocess.TimeoutExpired):return '当前环境不能读取systemd'


def network_probe(cfg):
    p=urlsplit(cfg['proxy']); checked=int(time.time()*1000)
    value={'checked_at':checked,'proxy_service':service('mihomo-proxy.service'),
           'proxy_listening':False,'okx_reachable':False,'latency_ms':None,'error':None}
    try:
        with socket.create_connection((p.hostname,p.port),timeout=1):value['proxy_listening']=True
    except OSError:
        value['error']='本机代理端口没有接受连接。'
        return value
    start=time.monotonic()
    try:
        api=OKXClient(mode='live',origin=cfg['origin'],proxy=cfg['proxy'],timeout=4)
        value['server_time']=api.sync_time(); value['okx_reachable']=True
    except Exception as exc:value['error']=safe_error(exc)
    value['latency_ms']=round((time.monotonic()-start)*1000)
    return value


def status(cfg,probe=True):
    return {'mode':'live','service':service(UNIT),'config':cfg,
            'state':Store.read_status(data_dir()),'network':network_probe(cfg) if probe else None}


def display(result):
    s,c,net=result['state'],result['config'],result['network']; h=s.get('health',{})
    print('G3 交易执行版 — 欧易真钱交易；实际成交会改变账户资金')
    print('交易服务：'+result['service'])
    print('策略：BTC-USDT现货 + BTC-USD-SWAP币本位永续空头；独立子账户，全仓2倍设置')
    current_capital=s.get('capital_usdt',c['capital_usdt'])
    budget='未填写' if current_capital is None else str(current_capital)+' U'
    loss='未填写（启用前必填）' if c['max_loss_usdt'] is None else str(c['max_loss_usdt'])+' U'
    print('预算：%s；累计亏损停止限额：%s'%(budget,loss))
    if not s.get('armed'):
        print('尚未启用；先配置资金、密钥并检查账户。安装服务不会自动启用交易。')
    else:
        age=int(time.time()*1000)-h.get('checked_at',0)
        good=result['service']=='active' and 0<=age<=180000 and h.get('phase') not in ('error','needs_attention')
        print('运行记录：'+('近期有更新' if good else '未就绪、已停止或需要检查；以下可能是旧记录'))
        print('阶段：'+h.get('phase',s.get('phase','未知'))+'；'+h.get('message',s.get('last_decision','')))
        print('最近运行检查：'+stamp(h.get('checked_at'))+'；行情快照：'+stamp(h.get('market_time')))
        print('新开仓：'+('暂停' if s.get('paused') else '允许条件检查')+'；风控停止：'+str(s.get('halted') or '无'))
        print('初始投入：%s U；累计核对追加：%s U；配置不设固定资金上限'%(c['capital_usdt'],s.get('capital_added_usdt','0')))
        print('当前预算估算净值：%s U；累计交易盈亏估算（不含入金）：%s U'%(s.get('last_equity'),
              Decimal(str(s.get('last_equity','0')))-Decimal(str(s.get('capital_usdt','0')))))
        print('账户已核对USDT现金：%s；BTC现金：%s；币本位空头：%s 张'%(s.get('cash_usdt'),s.get('btc_balance'),s.get('contracts')))
        print('逐笔余额/持仓核对：'+('需要检查' if s.get('reconciliation_mismatch_since') else '通过（以最近时点为准）')+'；最近核对：'+stamp(s.get('last_reconciled_at')))
        print('未核对订单：%s；已完成整组退出：%s；最大回撤：%.2f%%'%(s.get('unresolved_requests',0),s.get('closed_trades',0),Decimal(s.get('max_drawdown','0'))*100))
        print('真实资金费净收支：%s BTC；合约已实现盈亏：%s BTC'%(s.get('funding_btc','0'),s.get('realized_pnl_btc','0')))
        print('实际成交费用收支（负数为收费）：'+str(s.get('fees_by_ccy',{})))
        print('组合：'+('持有两腿组合' if s.get('position') else '没有登记完整组合'))
        if s.get('op'):print('执行进度：'+str(s['op'].get('type'))+' / '+str(s['op'].get('stage'))+'；可能暂时只有一腿，查看欧易实际持仓。')
        if s.get('queued_controls'):print('等待处理的操作：'+', '.join(s['queued_controls']))
        print('记录的BTC零头：'+str(s.get('dust_btc','0'))+'；零头不等于已经卖出。')
    if net:
        print('代理服务：%s；%s 接受连接：%s'%(net['proxy_service'],c['proxy'],'是' if net['proxy_listening'] else '否'))
        print('经代理访问欧易公开接口：%s；耗时：%s毫秒'%('成功' if net['okx_reachable'] else '失败',net['latency_ms']))
        print('本次网络检查：'+stamp(net['checked_at']))
        if net['error']:print('网络提示：'+net['error'])
    print('公开联网成功不代表私有交易权限或实时持仓已核对。预算和退出规则不保证最大损失。')
