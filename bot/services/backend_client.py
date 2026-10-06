from __future__ import annotations

import time
from pathlib import Path
import requests


class BackendClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip('/')
        self.token = token

    def _headers(self):
        return {'Authorization': f'Bearer {self.token}'}

    def health(self):
        r = requests.get(self.base_url + '/api/health', timeout=15)
        r.raise_for_status()
        return r.json()

    def register_agent(self, agent_id: str, name: str, agent_token: str, owner_user_id: int):
        r = requests.post(
            self.base_url + '/api/bot/agents/register',
            json={'agent_id': agent_id, 'name': name, 'agent_token': agent_token, 'owner_user_id': owner_user_id, 'status': 'pending'},
            headers=self._headers(), timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def list_agents(self):
        r = requests.get(self.base_url + '/api/bot/agents', headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def agent(self, agent_id: str):
        r = requests.get(self.base_url + f'/api/bot/agents/{agent_id}', headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()


    def delete_agent(self, agent_id: str):
        r = requests.delete(self.base_url + f'/api/bot/agents/{agent_id}', headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def agent_history(self, agent_id: str, limit: int = 10):
        r = requests.get(self.base_url + f'/api/bot/agents/{agent_id}/history', params={'limit': limit}, headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def recent_jobs(self, limit: int = 10):
        r = requests.get(self.base_url + '/api/bot/jobs', params={'limit': limit}, headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def upload_package(self, user_id: int, file_path: str):
        path = Path(file_path)
        with path.open('rb') as f:
            r = requests.post(
                self.base_url + '/api/bot/packages',
                files={'file': (path.name, f, 'application/zip')},
                data={'user_id': str(user_id)},
                headers=self._headers(), timeout=120,
            )
        r.raise_for_status()
        return r.json()

    def create_deploy_job(self, payload: dict):
        r = requests.post(self.base_url + '/api/bot/jobs/deploy', json=payload, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def create_rollback_job(self, payload: dict):
        r = requests.post(self.base_url + '/api/bot/jobs/rollback', json=payload, headers=self._headers(), timeout=30)
        r.raise_for_status()
        return r.json()

    def get_job(self, job_id: str):
        r = requests.get(self.base_url + f'/api/bot/jobs/{job_id}', headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def get_job_logs(self, job_id: str):
        r = requests.get(self.base_url + f'/api/bot/jobs/{job_id}/logs', headers=self._headers(), timeout=15)
        r.raise_for_status()
        return r.json()

    def wait_for_job(self, job_id: str, timeout_seconds: int = 300, poll_seconds: int = 3):
        start = time.time()
        while time.time() - start < timeout_seconds:
            job = self.get_job(job_id)
            if job.get('status') in ('success', 'failed', 'cancelled', 'timeout'):
                return job
            time.sleep(poll_seconds)
        return self.get_job(job_id)
