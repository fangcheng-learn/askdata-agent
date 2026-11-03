"""Owner-scoped UI metadata; analysis and evidence remain in the original stores."""
import json
import sqlite3
from pathlib import Path
from threading import RLock

class UIStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS metadata (owner TEXT, kind TEXT, id TEXT, value TEXT, PRIMARY KEY(owner,kind,id))')
        path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=20)

    def get(self, owner, kind, key):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT value FROM metadata WHERE owner=? AND kind=? AND id=?', (owner,kind,key)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, owner, kind):
        with self.lock, self.connect() as db:
            rows = db.execute('SELECT value FROM metadata WHERE owner=? AND kind=?', (owner,kind)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, owner, kind, key, value):
        with self.lock, self.connect() as db:
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?,?,?)', (owner,kind,key,json.dumps(value,ensure_ascii=False)))
        return value
