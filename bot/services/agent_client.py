import requests
from common.auth import auth_headers


class AgentClient:
    def _headers(self, a):
        return auth_headers(a['token'])

    def _raise_json(self, r):
        try:
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise RuntimeError(f'{r.status_code} {detail}') from exc

    def status(self, a):
        return self._raise_json(requests.get(a['base_url'] + '/api/status', headers=self._headers(a), timeout=15))

    def site_info(self, a, site):
        return self._raise_json(requests.get(a['base_url'] + '/api/iis/site-info', params={'site_name': site}, headers=self._headers(a), timeout=15))

    def app_pool_info(self, a, pool):
        return self._raise_json(requests.get(a['base_url'] + '/api/iis/app-pool-info', params={'app_pool_name': pool}, headers=self._headers(a), timeout=15))

    def upload_package(self, a, path):
        with open(path, 'rb') as f:
            return self._raise_json(requests.post(a['base_url'] + '/api/upload-package', files={'file': f}, headers=self._headers(a), timeout=120))

    def deploy(self, a, payload):
        return self._raise_json(requests.post(a['base_url'] + '/api/deploy', json=payload, headers=self._headers(a), timeout=300))

    def rollback(self, a, site_name, app_pool_name=None):
        payload = {'site_name': site_name, 'app_pool_name': app_pool_name}
        return self._raise_json(requests.post(a['base_url'] + '/api/rollback', json=payload, headers=self._headers(a), timeout=120))

    def releases(self, a, site):
        return self._raise_json(requests.get(a['base_url'] + '/api/releases', params={'site_name': site}, headers=self._headers(a), timeout=15))

    def logs(self, a, site=None):
        return self._raise_json(requests.get(a['base_url'] + '/api/logs', params={'site_name': site} if site else {}, headers=self._headers(a), timeout=15))

    def delete_release(self, a, site_name, release_id):
        return self._raise_json(requests.delete(a['base_url'] + f'/api/releases/{site_name}/{release_id}', headers=self._headers(a), timeout=30))
