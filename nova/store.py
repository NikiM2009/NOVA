import contextlib
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            self.path.touch(mode=0o600)
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS pairing (token TEXT PRIMARY KEY, expires REAL);
                CREATE TABLE IF NOT EXISTS agents (id TEXT PRIMARY KEY, token TEXT UNIQUE, name TEXT,
                    address TEXT, platform TEXT, last_seen REAL, metrics TEXT DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS servers (id TEXT PRIMARY KEY, name TEXT, agent_id TEXT,
                    software TEXT, version TEXT, port INTEGER, memory INTEGER, status TEXT,
                    metrics TEXT DEFAULT '{}', created REAL);
                CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, agent_id TEXT, server_id TEXT,
                    kind TEXT, payload TEXT, status TEXT, result TEXT, created REAL, claimed REAL);
                CREATE TABLE IF NOT EXISTS routines (id TEXT PRIMARY KEY, name TEXT, server_id TEXT,
                    action TEXT, interval_minutes INTEGER, next_run REAL, enabled INTEGER);
                CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL, level TEXT, title TEXT, detail TEXT);
                CREATE TABLE IF NOT EXISTS samples (time REAL, players INTEGER, cpu REAL, ram REAL);
                CREATE TABLE IF NOT EXISTS installations (server_id TEXT PRIMARY KEY,
                    job_id TEXT, status TEXT, error TEXT DEFAULT '');
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES ('network_secret', ?)", (secrets.token_hex(32),))
            # Recover the latest installation outcome from pre-upgrade databases.
            rows = db.execute("SELECT j.* FROM jobs j JOIN servers s ON s.id=j.server_id WHERE j.kind='install' ORDER BY j.created DESC,j.rowid DESC").fetchall()
            for row in rows:
                result = json.loads(row['result']) if row['result'] else {}
                db.execute('INSERT OR IGNORE INTO installations VALUES (?,?,?,?)',
                           (row['server_id'],row['id'],row['status'],result.get('error','')))

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def all(self, query, args=()):
        with self.db() as db:
            return [dict(row) for row in db.execute(query, args)]

    def one(self, query, args=()):
        rows = self.all(query, args)
        return rows[0] if rows else None

    def execute(self, query, args=()):
        with self.db() as db:
            db.execute(query, args)

    def setting(self, key):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row['value'] if row else None

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))

    def event(self, title, detail='', level='info'):
        self.execute("INSERT INTO events(time,level,title,detail) VALUES (?,?,?,?)", (time.time(), level, title, detail))
        self.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")

    def job(self, agent_id, server_id, kind, payload):
        job_id = secrets.token_hex(12)
        with self.db() as db:
            db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,? ,NULL,?,NULL)",
                       (job_id, agent_id, server_id, kind, json.dumps(payload), 'queued', time.time()))
            if kind=='install':
                db.execute('INSERT OR REPLACE INTO installations VALUES (?,?,?,?)',
                           (server_id,job_id,'queued',''))
        return job_id

    def seed_demo(self):
        if self.setting('demo_seeded'):
            return
        now = time.time()
        agents = [('a1', 'Atlas', '10.0.0.11', 'Linux · x86_64', 32, 64, 25.6),
                  ('a2', 'Orion', '10.0.0.12', 'Windows · x86_64', 46, 32, 18.4),
                  ('a3', 'Pi-node', '10.0.0.13', 'Linux · aarch64', 18, 8, 3.2)]
        for id_, name, address, platform, cpu, ram, used in agents:
            self.execute("INSERT OR IGNORE INTO agents VALUES (?,?,?,?,?,?,?)",
                         (id_, digest(secrets.token_hex(32)), name, address, platform, now,
                          json.dumps(dict(cpu=cpu, ram_total=ram, ram_used=used, disk=38, uptime=218430))))
        servers = [('s1','Velocity','a1','velocity','3.4.0',25565,1024,128),
                   ('s2','Lobby','a1','paper','1.21.4',25566,2048,12),
                   ('s3','Survival','a1','paper','1.21.4',25567,6144,64),
                   ('s4','Skyblock','a2','paper','1.21.4',25568,4096,38),
                   ('s5','Creative','a2','paper','1.21.4',25569,2048,14),
                   ('s6','Build','a3','paper','1.21.4',25570,2048,0)]
        for id_, name, agent, software, version, port, memory, players in servers:
            self.execute("INSERT OR IGNORE INTO servers VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (id_,name,agent,software,version,port,memory,'stopped' if id_=='s6' else 'running',
                          json.dumps(dict(players=players,tps=19.98,cpu=12 if id_=='s1' else 24,
                                          ram=round(memory*.00065,2),max_players=200)),now))
        for i in range(48):
            import math
            players = max(0, int(76 + i*.9 + 26*math.sin(i/5)))
            self.execute("INSERT INTO samples VALUES (?,?,?,?)", (now-(47-i)*1800,players,32,47.2))
        self.event('Dein Netzwerk ist bereit', 'Demodaten · 3 Agenten und 6 Instanzen')
        self.event('Survival gestartet', 'Atlas · Paper 1.21.4 · 6 GB RAM')
        self.event('Spielerrekord erreicht', '128 Spieler gleichzeitig online', 'success')
        self.set_setting('demo_seeded', '1')
