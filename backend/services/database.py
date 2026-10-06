from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT,
                token TEXT,
                owner_user_id TEXT,
                status TEXT,
                last_seen TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS packages (
                package_id TEXT PRIMARY KEY,
                original_filename TEXT,
                stored_path TEXT,
                uploaded_by_user_id TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                agent_id TEXT,
                package_id TEXT,
                requested_by_user_id TEXT,
                site_name TEXT,
                app_pool_name TEXT,
                project_type TEXT,
                health_check_url TEXT,
                payload_json TEXT,
                status TEXT,
                created_at TEXT,
                claimed_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                result_json TEXT,
                result_summary TEXT,
                error_message TEXT
            );
            CREATE TABLE IF NOT EXISTS job_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                agent_id TEXT,
                message TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT,
                action TEXT,
                target TEXT,
                details_json TEXT,
                created_at TEXT
            );
            """)
            cols = {r['name'] for r in con.execute('PRAGMA table_info(jobs)').fetchall()}
            for col, spec in {'payload_json': 'TEXT', 'started_at': 'TEXT', 'result_json': 'TEXT'}.items():
                if col not in cols:
                    con.execute(f'ALTER TABLE jobs ADD COLUMN {col} {spec}')
            cols = {r['name'] for r in con.execute('PRAGMA table_info(agents)').fetchall()}
            if 'owner_user_id' not in cols:
                con.execute('ALTER TABLE agents ADD COLUMN owner_user_id TEXT')

    def audit(self, actor: str, action: str, target: str, details: dict | None = None):
        with self.connect() as con:
            con.execute('INSERT INTO audit_logs(actor,action,target,details_json,created_at) VALUES(?,?,?,?,?)',
                        (actor, action, target, json.dumps(details or {}), now()))

    def upsert_agent(self, agent_id: str, name: str, token: str, status: str = 'offline', owner_user_id: str | None = None):
        with self.connect() as con:
            existing = con.execute('SELECT agent_id FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
            if existing:
                con.execute('UPDATE agents SET name=COALESCE(?,name), token=COALESCE(?,token), status=?, last_seen=?, owner_user_id=COALESCE(?,owner_user_id) WHERE agent_id=?',
                            (name, token, status, now(), owner_user_id, agent_id))
            else:
                con.execute('INSERT INTO agents(agent_id,name,token,owner_user_id,status,last_seen,created_at) VALUES(?,?,?,?,?,?,?)',
                            (agent_id, name, token, owner_user_id, status, now(), now()))

    def get_agent(self, agent_id: str):
        with self.connect() as con:
            row = con.execute('SELECT * FROM agents WHERE agent_id=?', (agent_id,)).fetchone()
            return dict(row) if row else None

    def list_agents(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM agents ORDER BY created_at DESC').fetchall()]

    def verify_agent(self, agent_id: str, token: str) -> bool:
        a = self.get_agent(agent_id)
        return bool(a and a.get('token') == token)

    def delete_agent(self, agent_id: str):
        with self.connect() as con:
            con.execute('DELETE FROM agents WHERE agent_id=?', (agent_id,))
            con.execute('DELETE FROM jobs WHERE agent_id=?', (agent_id,))
            con.execute('DELETE FROM job_logs WHERE agent_id=?', (agent_id,))
            return {'ok': True, 'agent_id': agent_id}

    def insert_package(self, info: dict):
        with self.connect() as con:
            con.execute('INSERT INTO packages(package_id,original_filename,stored_path,uploaded_by_user_id,created_at) VALUES(?,?,?,?,?)',
                        (info['package_id'], info['original_filename'], info['stored_path'], info['uploaded_by_user_id'], info['created_at']))

    def get_package(self, package_id: str):
        with self.connect() as con:
            row = con.execute('SELECT * FROM packages WHERE package_id=?', (package_id,)).fetchone()
            return dict(row) if row else None

    def insert_job(self, job: dict):
        with self.connect() as con:
            con.execute('''INSERT INTO jobs(job_id,agent_id,package_id,requested_by_user_id,site_name,app_pool_name,project_type,health_check_url,payload_json,status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                        (job['job_id'], job['agent_id'], job['package_id'], job.get('requested_by_user_id'), job['site_name'], job['app_pool_name'], job['project_type'], job['health_check_url'], json.dumps(job.get('payload') or {}), job['status'], job['created_at']))

    def get_job(self, job_id: str):
        with self.connect() as con:
            row = con.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d['payload'] = json.loads(d.get('payload_json') or '{}')
            d['result'] = json.loads(d.get('result_json') or '{}') if d.get('result_json') else None
            return d

    def next_job(self, agent_id: str):
        with self.connect() as con:
            row = con.execute('SELECT * FROM jobs WHERE agent_id=? AND status=? ORDER BY created_at LIMIT 1', (agent_id, 'pending')).fetchone()
            if not row:
                return None
            d = dict(row)
            d['payload'] = json.loads(d.get('payload_json') or '{}')
            return d

    def update_job_status(self, job_id: str, status: str, **fields):
        allowed = {'claimed_at', 'started_at', 'completed_at', 'result_summary', 'error_message', 'result_json'}
        keys = [k for k in fields if k in allowed]
        values = [fields[k] for k in keys]
        set_clause = ', '.join(['status=?'] + [f'{k}=?' for k in keys])
        with self.connect() as con:
            con.execute(f'UPDATE jobs SET {set_clause} WHERE job_id=?', [status] + values + [job_id])


    def list_jobs_for_agent(self, agent_id: str, limit: int = 10):
        with self.connect() as con:
            rows = con.execute(
                'SELECT * FROM jobs WHERE agent_id=? ORDER BY created_at DESC LIMIT ?',
                (agent_id, int(limit)),
            ).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d['payload'] = json.loads(d.get('payload_json') or '{}')
                d['result'] = json.loads(d.get('result_json') or '{}') if d.get('result_json') else None
                out.append(d)
            return out

    def list_recent_jobs(self, limit: int = 10):
        with self.connect() as con:
            rows = con.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?', (int(limit),)).fetchall()
            out = []
            for row in rows:
                d = dict(row)
                d['payload'] = json.loads(d.get('payload_json') or '{}')
                d['result'] = json.loads(d.get('result_json') or '{}') if d.get('result_json') else None
                out.append(d)
            return out

    def add_job_log(self, job_id: str, agent_id: str, message: str):
        with self.connect() as con:
            con.execute('INSERT INTO job_logs(job_id,agent_id,message,created_at) VALUES(?,?,?,?)', (job_id, agent_id, message, now()))

    def get_job_logs(self, job_id: str):
        with self.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM job_logs WHERE job_id=? ORDER BY id', (job_id,)).fetchall()]
