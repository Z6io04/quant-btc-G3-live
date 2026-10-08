#!/usr/bin/env python3
"""Single entry point. Only run has order permission after explicit arm-live."""
import argparse
import getpass
import json
import math
import os
import sys
import warnings

from g3core import config,runtime
from g3core.config import require,Attention,safe_error
from g3core.store import Store
from g3core.status import status,display


def interactive():
    require(sys.stdin.isatty() and sys.stdout.isatty(),'请在自己的交互终端运行，密钥不接受命令行参数或管道。')


def setup_config():
    interactive()
    config.require_uninitialized()
    cfg=config.load_config(require_limits=False)
    print('当前方案以25U为基准；本版无固定20U/25U上限，须填写独立G3子账户的实际预算。')
    print('原G3单组亏损3%、账户回撤5%仍生效；金额限额是额外停止条件，不保证实际最多亏这些。')
    capital=input('首次投入多少USDT（必须填写）：').strip()
    loss=input('累计亏损多少USDT时停止并请求退出（必须填写，充值不会自动提高此限额）：').strip()
    try:capital,loss=float(capital),float(loss)
    except ValueError:raise Attention('请填写数字，没有保存配置。') from None
    require(math.isfinite(capital) and math.isfinite(loss) and capital>0 and 0<loss<=capital,'金额须大于0，亏损限额不大于本金。')
    cfg.update(capital_usdt=capital,max_loss_usdt=loss)
    config.atomic_json(config.ROOT/'config.json',cfg,overwrite=True)
    print('已保存config.json；尚未启用交易。')


def setup_credentials():
    interactive()
    path=config.ROOT/'private'/'credentials.json'
    require(not path.exists(),'密钥文件已存在，不覆盖；已启用账户换钥需要专门维护，不能直接替换。')
    config.require_uninitialized()
    print('请输入独立G3子账户的真钱API；读取和交易权限，不启用提币。')
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        values={k:getpass.getpass(label+'：') for k,label in
                (('api_key','API Key'),('secret_key','Secret Key'),('passphrase','Passphrase'))}
    require(all(isinstance(v,str) and 0<len(v)<=4096 and all(33<=ord(c)<=126 for c in v)
                for v in values.values()),'密钥为空或含不支持字符，没有保存。')
    config.atomic_json(path,values)
    print('密钥已保存到private/credentials.json（仅当前用户可读写），没有下单。')


def main(argv=None):
    p=argparse.ArgumentParser(description='G3真钱两腿资金费策略；所有日常操作均使用此入口。')
    p.add_argument('command',choices=('setup-config','setup-credentials','check','account-check',
                                    'arm-live','run','status','pause','resume','flatten','sync-capital'))
    p.add_argument('--local',action='store_true',help='status只看本地记录，不探测网络')
    p.add_argument('--json',action='store_true',help='输出脱敏结构化状态')
    args=p.parse_args(argv)
    if args.command=='setup-config':setup_config(); return 0
    if args.command=='setup-credentials':setup_credentials(); return 0
    cfg=config.load_config(require_limits=args.command not in ('status','check'))
    if args.command=='status':
        result=status(cfg,probe=not args.local)
        if args.json:print(json.dumps(result,ensure_ascii=False,indent=2))
        else:display(result)
        return 0
    if args.command in ('pause','resume','flatten'):
        require((config.data_dir()/'state.sqlite3').is_file(),'没有已启用账本，不能提交控制指令。')
        if args.command=='flatten':
            interactive()
            require(input('这会请求真实买平币本位空头并卖出BTC现货。确认请输入 FLATTEN G3：').strip()=='FLATTEN G3','已取消退出请求。')
        s=Store(config.data_dir())
        try:
            require(s.identity() is not None and s.state().get('armed'),'缺少原启用记录。')
            s.control(args.command)
        finally:s.close()
        print('操作已排队，运行中的服务会在下一轮处理；请用status确认。resume不清除风控停止。')
        return 0
    if args.command=='sync-capital':
        interactive()
        from g3core.capital import sync_capital
        result=sync_capital(cfg,confirm=input)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    if args.command=='run':runtime.run(cfg); return 0
    if args.command=='check':result=runtime.public_check(cfg)
    elif args.command=='account-check':result=runtime.account_check(cfg)
    else:
        interactive()
        print('G3真钱账户：预算%sU，累计亏损停止%sU，全仓2倍；独立子账户。'%(cfg['capital_usdt'],cfg['max_loss_usdt']))
        print('启用后run有真实下单权限。确认账户空仓、无挂单；已有F3/v53绑定账户不可复用。')
        result=runtime.arm(cfg,input('确认请输入 ARM G3 REAL MONEY：').strip())
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('passed',True) else 1


if __name__=='__main__':
    try:sys.exit(main())
    except KeyboardInterrupt:
        print('已中断；已受理订单可能继续成交，请用status和欧易账户核对。',file=sys.stderr); sys.exit(130)
    except Exception as exc:
        print(safe_error(exc),file=sys.stderr); sys.exit(1)
