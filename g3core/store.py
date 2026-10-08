"""Crash-safe local ledger. No exchange access and no reset operation."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


class AccountLock:
    """Lifetime process lock; scoped to the local account installation."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.handle = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / 'account.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.handle = os.fdopen(fd, 'a+')
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError('另一个 G3 进程已持有账户锁。') from None
        return self

    def __exit__(self, *args):
        self.handle.close()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser()
        if self.directory.is_symlink():
            raise ValueError('Ledger directory cannot be a symlink')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / 'state.sqlite3'
        if self.path.is_symlink():
            raise ValueError('Ledger cannot be a symlink')
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=15000')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError('Unsupported ledger schema')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, stamp INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, action_key TEXT UNIQUE NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records(namespace TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(namespace,id));
            CREATE TABLE IF NOT EXISTS controls(id INTEGER PRIMARY KEY, action TEXT NOT NULL, stamp INTEGER NOT NULL, applied INTEGER NOT NULL DEFAULT 0);
            PRAGMA user_version=1;
        ''')

    @classmethod
    def read_status(cls, directory):
        path = Path(directory).expanduser() / 'state.sqlite3'
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ValueError('Ledger cannot be a symlink')
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            row = db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            result = json.loads(row[0]) if row else {}
            result['unresolved_requests'] = db.execute("SELECT COUNT(*) FROM intents WHERE state NOT IN ('applied','expired')").fetchone()[0]
            result['queued_controls'] = [r[0] for r in db.execute('SELECT action FROM controls WHERE applied=0 ORDER BY id')]
            return result
        finally:
            db.close()

    @classmethod
    def read_identity(cls, directory):
        path=Path(directory).expanduser()/'state.sqlite3'
        if not path.exists():return None
        if path.is_symlink():raise ValueError('Ledger cannot be a symlink')
        db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
        try:
            row=db.execute("SELECT value FROM meta WHERE key='identity'").fetchone()
            return json.loads(row[0]) if row else None
        finally:db.close()

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encoded(value)))

    def state(self):
        return self.get('state', {})

    get_state = state

    def set_state(self, value):
        self.put('state', value)

    def initialize(self, identity, initial_state):
        if not isinstance(identity, dict) or not identity:
            raise ValueError('Verified account identity required')
        with self.transaction():
            existing = self.get('identity')
            if existing is not None:
                if existing != identity:
                    raise ValueError('账户或资金配置与原账本不符；禁止覆盖账本。')
                return False
            if self.state() or self.db.execute('SELECT 1 FROM intents LIMIT 1').fetchone():
                raise ValueError('Unbound ledger requires manual recovery')
            self.put('identity', identity)
            self.set_state(initial_state)
            return True

    def identity(self):
        return self.get('identity')

    def event(self, kind, data, now=None):
        self.db.execute('INSERT INTO events(stamp,kind,data) VALUES(?,?,?)', (now or int(time.time()*1000), kind, encoded(data)))

    def create_intent(self, action_key, request, context, now):
        existing = self.db.execute('SELECT data FROM intents WHERE action_key=?', (action_key,)).fetchone()
        if existing:
            return json.loads(existing[0])
        client_id = 'g3' + uuid.uuid4().hex[:30]
        request = dict(request, clOrdId=client_id)
        item = {'id':client_id, 'action_key':action_key, 'status':'prepared', 'request':request,
                'context':context, 'created_at':now, 'updated_at':now, 'result':{}}
        self.db.execute('INSERT INTO intents VALUES (?,?,?,?)', (client_id, action_key, 'prepared', encoded(item)))
        return item

    def intent(self, client_id):
        row = self.db.execute('SELECT data FROM intents WHERE id=?', (client_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def update_intent(self, item):
        self.db.execute('UPDATE intents SET state=?,data=? WHERE id=?', (item['status'], encoded(item), item['id']))

    def unresolved(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM intents WHERE state NOT IN ('applied','expired') ORDER BY rowid")]

    def intents(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM intents ORDER BY rowid')]

    def record(self, namespace, record_id, data):
        old = self.db.execute('SELECT data FROM records WHERE namespace=? AND id=?', (namespace,str(record_id))).fetchone()
        if old:
            if json.loads(old[0]) != data:
                raise ValueError('已登记的交易所记录发生变化，需人工核对。')
            return False
        self.db.execute('INSERT INTO records VALUES (?,?,?)', (namespace,str(record_id),encoded(data)))
        return True

    def records(self, namespace):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM records WHERE namespace=? ORDER BY rowid', (namespace,))]

    def control(self, action):
        if action not in ('pause','resume','flatten'):
            raise ValueError('Unknown control')
        self.db.execute('INSERT INTO controls(action,stamp) VALUES (?,?)', (action,int(time.time()*1000)))

    queue_control = control

    def controls(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM controls WHERE applied=0 ORDER BY id')]

    def apply_control(self, control_id):
        self.db.execute('UPDATE controls SET applied=1 WHERE id=?', (control_id,))

    def close(self):
        self.db.close()
