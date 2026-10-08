"""Explicit JSON configuration and protected local credentials; no environment input."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sqlite3
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
UNIT = 'btc-G3-live.service'
DEFAULT = {
    'version':'G3-live-1', 'mode':'live', 'origin':'https://openapi.okx.com',
    'proxy':'http://127.0.0.1:17890', 'capital_usdt':None, 'max_loss_usdt':None,
    'leverage':2, 'poll_seconds':10, 'quote_age_seconds':5, 'max_slippage_bps':3,
    'order_ttl_seconds':15, 'max_unhedged_seconds':60, 'signal_ttl_seconds':60,
    'warmup_days':16,
}


class Attention(RuntimeError):
    safe_message = True


def require(condition, message):
    if not condition:
        raise Attention(message)


def safe_error(exc):
    from .okx import OKXError
    if isinstance(exc, (Attention, OKXError)):
        return str(exc)
    if getattr(exc, 'safe_message', False):
        return str(exc)
    return type(exc).__name__ + '：本轮未完成，请核对网络和账户；不重复发送未知结果的订单。'


def plain_file(path, private=False, missing=False):
    path = Path(path).absolute()
    for part in [*reversed(path.parents),path]:
        require(not part.is_symlink(), '配置或数据路径含符号链接；请保留现场检查。')
    if missing and not path.exists():
        return path
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, '文件类型不正确。')
    if private:
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077,
                '私有文件必须属于当前用户，且权限为600。')
    return path


def atomic_json(path, value, *, overwrite=False):
    path = plain_file(path, missing=True)
    if path.exists():
        require(overwrite, '文件已存在；保留原文件，不自动覆盖。')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.parent / ('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as f:
            json.dump(value,f,ensure_ascii=False,indent=2,allow_nan=False)
            f.write('\n'); f.flush(); os.fsync(f.fileno())
        if not overwrite:
            # O_EXCL destination prevents replacing a concurrently-created identity.
            os.link(temp,path)
            temp.unlink()
        else:
            os.replace(temp,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temp.exists():temp.unlink()


def load_config(path=None, *, require_limits=True):
    cfg=json.loads(plain_file(path or ROOT/'config.json').read_text())
    require(isinstance(cfg,dict) and set(cfg)==set(DEFAULT),'config.json字段不完整或存在未知字段。')
    for name in ('version','mode','leverage','quote_age_seconds','max_slippage_bps',
                 'order_ttl_seconds','max_unhedged_seconds','signal_ttl_seconds','warmup_days'):
        require(type(cfg[name]) is type(DEFAULT[name]) and cfg[name]==DEFAULT[name],
                '本版固定G3币本位两腿规则和执行限制，不能静默更改。')
    require(type(cfg['poll_seconds']) is int and 5<=cfg['poll_seconds']<=30,'轮询间隔须为5至30秒。')
    require(cfg['origin'] in ('https://openapi.okx.com','https://www.okx.com'), '本版仅适配欧易全球站接口。')
    try:
        p=urlsplit(cfg['proxy'])
        good=(p.scheme=='http' and p.hostname in ('127.0.0.1','localhost','::1')
              and p.port is not None and p.port>0 and p.username is None and p.password is None
              and p.path in ('','/') and not p.query and not p.fragment)
    except (TypeError,ValueError):good=False
    require(good,'proxy须为本机HTTP代理，例如http://127.0.0.1:17890。')
    limits=[cfg['capital_usdt'],cfg['max_loss_usdt']]
    require(cfg['capital_usdt'] is None or
            type(cfg['capital_usdt']) in (int,float) and math.isfinite(cfg['capital_usdt'])
            and cfg['capital_usdt']>0,
            '投入资金须为有限的正数。')
    if not require_limits and any(v is None for v in limits):
        require(all(v is None or type(v) in (int,float) and math.isfinite(v) and v>0 for v in limits),
                '资金或亏损限额格式不正确。')
        return cfg
    require(all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in limits),
            '先运行setup-config填写投入资金和累计亏损限额。')
    require(cfg['max_loss_usdt']<=cfg['capital_usdt'],'亏损限额不能大于投入资金。')
    return cfg


def effective_config(cfg, state):
    """Immutable file stores initial budget; verified deposits grow ledger budget."""
    if not state.get('armed'):
        return dict(cfg)
    try:
        initial=Decimal(str(cfg['capital_usdt']))
        capital=Decimal(str(state['capital_usdt']))
        added=Decimal(str(state.get('capital_added_usdt','0')))
        good=(all(v.is_finite() for v in (initial,capital,added)) and initial>0
              and capital>0 and added>=0 and capital==initial+added)
    except (InvalidOperation,KeyError,ValueError,TypeError):
        good=False
    require(good,'当前预算与初始资金、已核对入金不一致；停止自动交易。')
    # Decimal string avoids losing cents when evaluating a persisted cashflow.
    return dict(cfg,capital_usdt=format(capital,'f'))


def credentials(path=None):
    values=json.loads(plain_file(path or ROOT/'private'/'credentials.json',private=True).read_text())
    require(isinstance(values,dict) and set(values)=={'api_key','secret_key','passphrase'},'密钥文件字段错误。')
    require(all(isinstance(v,str) and 0<len(v)<=4096 and all(33<=ord(c)<=126 for c in v)
                for v in values.values()),'密钥为空或包含不支持的字符。')
    return values


def data_dir():return ROOT/'var'/'live'


def require_uninitialized():
    """An empty database left by failed setup is not an armed trading ledger."""
    path=plain_file(data_dir()/'state.sqlite3',missing=True)
    if not path.exists():return
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        # Unknown schemas, partial histories or control requests all fail closed.
        require(db.execute('PRAGMA user_version').fetchone()[0]==1,'账本格式未知，不能重新配置。')
        empty=all(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0
                  for table in ('meta','events','intents','records','controls'))
        require(empty,'已有启用或交易记录，不能通过重设参数或密钥重置账本。')
    finally:db.close()


def code_identity(cfg):
    files=[ROOT/'launch.py']+sorted((ROOT/'g3core').rglob('*.py'))
    # Full immutable identity is recorded at arm; changing code or any config requires review.
    return {'config':cfg,'files':{str(p.relative_to(ROOT)):hashlib.sha256(plain_file(p).read_bytes()).hexdigest() for p in files}}


def account_key(uid):
    require(isinstance(uid,str) and uid.isdigit(),'欧易账户UID格式不正确。')
    return hashlib.sha256(('live:'+uid).encode()).hexdigest()


def account_lock_path(uid):
    # Share the existing v53 lock namespace to serialize any same-account operation.
    return Path.home()/'.local/state/btc-v53-live/locks'/(account_key(uid)+'.lock')


def binding_path(uid):
    return Path.home()/'.local/state/btc-G3-live/accounts'/(account_key(uid)+'.json')


def check_independent_account(uid):
    previous=Path.home()/'.local/state/btc-F3-live/accounts'/(account_key(uid)+'.json')
    require(not previous.exists() and not previous.is_symlink(),
            '该账户存在F3绑定；复用须先做受控迁移核对，本包不自动接管，也不能删除登记绕过。')
    old=Path.home()/'.local/state/btc-v53-live/accounts'/(account_key(uid)+'.json')
    if old.exists() or old.is_symlink():
        value=json.loads(plain_file(old,private=True).read_text())
        require(value.get('strategy')=='G3-live-1' and value.get('directory')==str(data_dir().resolve()),
                '该账户已绑定其他策略；G3须使用独立欧易子账户，不要删除绑定文件绕过。')


def bind_account(uid,directory):
    check_independent_account(uid)
    path=binding_path(uid)
    if path.exists():
        value=json.loads(plain_file(path,private=True).read_text())
        require(value.get('uid')==uid and value.get('directory')==str(Path(directory).resolve())
                and value.get('strategy')=='G3-live-1','账户已绑定其他目录或绑定记录不合法。')
        return value
    value={'schema':1,'uid':uid,'directory':str(Path(directory).resolve()),
           'strategy':'G3-live-1','binding_id':uuid.uuid4().hex}
    atomic_json(path,value)
    shared=Path.home()/'.local/state/btc-v53-live/accounts'/(account_key(uid)+'.json')
    # Older v53 versions check this namespace: reserve it too so a later v53
    # activation cannot reuse G3's account and mistake collateral for free funds.
    atomic_json(shared,dict(value,account=account_key(uid),mode='live',phase='active'))
    return value


def validate_binding(uid,directory,binding_id):
    check_independent_account(uid)
    value=json.loads(plain_file(binding_path(uid),private=True).read_text())
    require(value=={'schema':1,'uid':uid,'directory':str(Path(directory).resolve()),
                    'strategy':'G3-live-1','binding_id':binding_id},'账户与原账本绑定不一致；不自动重新初始化。')
    shared=Path.home()/'.local/state/btc-v53-live/accounts'/(account_key(uid)+'.json')
    expected=dict(value,account=account_key(uid),mode='live',phase='active')
    require(json.loads(plain_file(shared,private=True).read_text())==expected,'共享账户独占登记不一致。')
