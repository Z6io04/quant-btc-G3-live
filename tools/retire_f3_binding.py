#!/usr/bin/env python3
"""Retire exactly two stale F3 registrations; default check makes no writes."""
import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from g3core import config, runtime
from g3core.market import account_snapshot
from g3core.store import AccountLock


class Refuse(RuntimeError):
    """Only fixed, credential-free diagnostics reach the CLI."""


def need(ok, message):
    if not ok:
        raise Refuse(message)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Snapshot:
    path: Path
    raw: bytes
    signature: tuple

    @classmethod
    def take(cls, path):
        path = config.plain_file(path, private=True)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                 and info.st_uid == os.getuid() and not info.st_mode & 0o077
                 and info.st_size <= 65536, '登记不是受支持的私有普通文件。')
            raw = handle.read(65537)
        signature = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size,
                     hashlib.sha256(raw).hexdigest())
        return cls(path, raw, signature)

    def recheck(self):
        need(Snapshot.take(self.path) == self, '登记在检查期间发生变化；未覆盖新内容。')


def services_stopped():
    """Read both managers; unknown/unavailable manager state is not stopped."""
    for scope in ('--user', '--system'):
        for unit in ('btc-F3-live.service', 'btc-G3-live.service'):
            try:
                result = subprocess.run(
                    ['systemctl', scope, 'show', '--no-pager', unit,
                     '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID'],
                    capture_output=True, text=True, timeout=10, check=False)
                rows = result.stdout.strip().splitlines()
                fields = dict(row.split('=', 1) for row in rows)
                good = (result.returncode in (0, 1) and len(rows) == 4
                        and set(fields) == {'LoadState', 'ActiveState', 'SubState', 'MainPID'}
                        and fields['LoadState'] in ('loaded', 'not-found')
                        and fields['ActiveState'] in ('inactive', 'failed')
                        and fields['SubState'] in ('dead', 'failed') and fields['MainPID'] == '0')
            except (OSError, ValueError, subprocess.SubprocessError):
                good = False
            need(good, '无法确认用户/系统级F3与G3全部停止且MainPID为0；停止迁移。')


def flat_account(api, cfg, expected_uid=None):
    api.sync_time()
    account = account_snapshot(api, cfg)
    uid = account.get('uid')
    need(isinstance(uid, str) and uid.isdigit() and (expected_uid is None or uid == expected_uid),
         '私有账户身份不匹配。')
    need('position' in account and account['position'] is None, '账户不是明确空仓。')
    for key in ('positions', 'pending', 'algo_pending'):
        need(type(account.get(key)) is list and account[key] == [], '持仓或普通/算法委托未明确归零。')
    try:
        balances = account['balances']
        btc = [Decimal(str(balances['BTC'][k])) for k in ('cash', 'eq', 'available')]
        usdt = [Decimal(str(balances['USDT'][k])) for k in ('cash', 'available')]
        good = all(v.is_finite() and v == 0 for v in btc) and all(v.is_finite() and v >= 0 for v in usdt)
    except (KeyError, ValueError, TypeError, ArithmeticError):
        good = False
    need(good, 'BTC余额未明确归零，或余额字段缺失/无效。')
    return uid


def registrations(uid):
    config.require_uninitialized()
    own_g3 = config.plain_file(config.binding_path(uid), missing=True)
    need(not own_g3.exists(), '当前账户已经有G3登记；此工具不处理已启用G3。')
    key = config.account_key(uid)
    home = Path.home()
    paths = [home / '.local/state' / name / 'accounts' / (key + '.json')
             for name in ('btc-F3-live', 'btc-v53-live')]
    snapshots = [Snapshot.take(path) for path in paths]
    try:
        first, second = [json.loads(item.raw) for item in snapshots]
        binding = first['binding_id']
        need(isinstance(binding, str) and len(binding) == 32
             and all(c in '0123456789abcdef' for c in binding), '旧绑定ID格式错误。')
        expected = {'schema': 1, 'uid': uid, 'directory': str(home / 'btc-F3-live/var/live'),
                    'strategy': 'F3-live-1', 'binding_id': binding}
        need(type(first.get('schema')) is int and first == expected
             and type(second.get('schema')) is int
             and second == dict(expected, account=key, mode='live', phase='active'),
             '两份登记不属于同一原F3账户/目录/绑定；不修改。')
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Refuse('旧登记内容缺失或格式不合法。') from None
    return snapshots


@contextmanager
def existing_lock(path):
    """Read-only check locks an existing lock inode; it never creates files."""
    path = config.plain_file(path, missing=True)
    if not path.exists():
        yield
        return
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refuse('已有交易进程或维护工具持有锁。') from None
        yield
    finally:
        os.close(fd)


def exclusive_bytes(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    fsync_dir(path.parent)


def archive_folder():
    root = config.ROOT / 'private' / 'f3-binding-retirements'
    for part in [*reversed(root.parents), root]:
        need(not part.is_symlink(), '备份目录含符号链接。')
    root.parent.mkdir(exist_ok=True, mode=0o700)
    root.mkdir(exist_ok=True, mode=0o700)
    for path in (root.parent, root):
        info = path.stat()
        need(info.st_uid == os.getuid() and not info.st_mode & 0o077, '备份目录须为当前用户私有0700目录。')
    folder = root / uuid.uuid4().hex
    folder.mkdir(mode=0o700)
    fsync_dir(root)
    return folder


def retire(apply=False):
    need(sys.platform.startswith('linux') and os.geteuid() != 0, '仅支持Linux普通用户；不要sudo。')
    # Refuse an already-armed G3 before reading credentials or the network.
    config.require_uninitialized()
    services_stopped()
    cfg = config.load_config(require_limits=False)
    creds = config.credentials()
    api = runtime.client(cfg, creds, orders=False)
    uid = flat_account(api, cfg)
    with ExitStack() as locks:
        if apply:
            locks.enter_context(AccountLock(config.data_dir()))
            locks.enter_context(runtime.account_guard(uid))
        else:
            locks.enter_context(existing_lock(config.data_dir() / 'account.lock'))
            locks.enter_context(existing_lock(config.account_lock_path(uid)))
        services_stopped()
        snapshots = registrations(uid)
        flat_account(api, cfg, uid)
        for item in snapshots:
            item.recheck()
        if not apply:
            return {'passed': True, 'mode': 'check', 'files_changed': 0, 'orders_sent': 0,
                    'note': '只读检查通过；未退役登记，未启用或启动G3。'}
        folder = archive_folder()
        audit = {'schema': 1, 'phase': 'backing_up', 'account_hash': config.account_key(uid),
                 'files': {name: item.signature[-1] for name, item in zip(('F3.json', 'shared-F3.json'), snapshots)}}
        config.atomic_json(folder / 'audit.json', audit)
        # Both originals must be durable before either registration can change.
        for name, item in zip(('F3.json', 'shared-F3.json'), snapshots):
            exclusive_bytes(folder / name, item.raw)
        audit['phase'] = 'prepared'
        config.atomic_json(folder / 'audit.json', audit, overwrite=True)
        try:
            services_stopped()
            config.require_uninitialized()
            need(config.load_config(require_limits=False) == cfg and config.credentials() == creds,
                 '配置或密钥在检查期间变化。')
            current = registrations(uid)
            need(current == snapshots, '登记在归档期间变化。')
            flat_account(api, cfg, uid)
            for item in snapshots:
                item.recheck()
            for item in snapshots:
                item.recheck()
                item.path.unlink()
                fsync_dir(item.path.parent)
            services_stopped()
            audit['phase'] = 'retired'
            config.atomic_json(folder / 'audit.json', audit, overwrite=True)
        except BaseException:
            restored = True
            for item in snapshots:
                try:
                    if not item.path.exists() and not item.path.is_symlink():
                        exclusive_bytes(item.path, item.raw)
                    else:
                        need(Snapshot.take(item.path).raw == item.raw, '不覆盖并发修改的登记。')
                except BaseException:
                    restored = False
            audit['phase'] = 'rolled_back' if restored else 'manual_recovery_required'
            try:
                config.atomic_json(folder / 'audit.json', audit, overwrite=True)
            except BaseException:
                pass
            raise Refuse(('退役失败，原登记已恢复。' if restored else '退役失败且恢复未完整完成；不要启动交易，保留全部文件。')
                         + ' 私有备份：' + str(folder)) from None
        return {'passed': True, 'mode': 'apply', 'retired_files': 2, 'orders_sent': 0,
                'backup': str(folder), 'note': '仅退役两份旧F3登记；未改账本、启用或启动G3。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='fresh checks, archive then retire exactly two F3 registrations')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(retire(args.apply), ensure_ascii=False, indent=2))
        return 0
    except Refuse as exc:
        print(str(exc), file=sys.stderr)
    except Exception:
        # API/OS exceptions can include secrets or account identifiers.
        print('迁移检查失败；请核对本地配置、服务及私有账户，保留原登记和备份。', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
