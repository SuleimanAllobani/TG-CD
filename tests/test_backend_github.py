import json
import os
import tempfile
import unittest
from unittest import mock

from backend.api.server import create_app

BOT = {'Authorization': 'Bearer test-bot-token'}


def deploy_payload(**over):
    p = dict(agent_id='agent_T1', site_name='s', app_pool_name='p', project_type='static',
             health_check_url='http://127.0.0.1:8080/', requested_by_user_id=7)
    p.update(over)
    return p


class BackendGithubJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {'CICD_BACKEND_DB': os.path.join(self.tmp.name, 'db.sqlite3'),
               'CICD_BACKEND_PACKAGE_ROOT': os.path.join(self.tmp.name, 'pkgs'),
               'CICD_BACKEND_BOT_TOKEN': 'test-bot-token'}
        with mock.patch.dict(os.environ, env):
            self.app = create_app()
        self.c = self.app.test_client()
        r = self.c.post('/api/bot/agents/register', json={'agent_id': 'agent_T1', 'agent_token': 'tok-agent', 'owner_user_id': 7}, headers=BOT)
        self.assertEqual(r.status_code, 200)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, **over):
        return self.c.post('/api/bot/jobs/deploy', json=deploy_payload(**over), headers=BOT)

    def test_zip_job_still_requires_package_id(self):
        r = self.post(source_type='uploaded_package')
        self.assertEqual(r.status_code, 400)
        self.assertIn('package_id', r.get_json()['error'])
        r = self.post()  # default source type is the ZIP flow
        self.assertEqual(r.status_code, 400)

    def test_zip_job_unchanged_when_package_given(self):
        r = self.post(package_id='pkg_1', source_type='uploaded_package')
        self.assertEqual(r.status_code, 200)
        job = r.get_json()
        self.assertEqual(job['package_id'], 'pkg_1')
        self.assertNotIn('repo_url', job['payload'])  # ZIP payload gets no GitHub keys

    def test_github_job_needs_no_package(self):
        r = self.post(source_type='github', repo_url='github.com/acme/site.git', git_ref='main', credential_ref='')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        job = r.get_json()
        self.assertEqual(job['package_id'], '')
        pl = job['payload']
        self.assertEqual(pl['repo_url'], 'https://github.com/acme/site')  # normalized
        self.assertEqual(pl['git_ref'], 'main')
        self.assertIsNone(pl['credential_ref'])

    def test_github_job_reaches_agent_and_history(self):
        job = self.post(source_type='github', repo_url='https://github.com/acme/site', credential_ref='default').get_json()
        nxt = self.c.get('/api/agent/jobs/next?agent_id=agent_T1', headers={'Authorization': 'Bearer tok-agent'}).get_json()
        self.assertEqual(nxt['job_id'], job['job_id'])
        self.assertEqual(nxt['payload']['source_type'], 'github')
        self.assertEqual(nxt['payload']['credential_ref'], 'default')
        done = self.c.post(f"/api/agent/jobs/{job['job_id']}/result", headers={'Authorization': 'Bearer tok-agent'},
                           json={'agent_id': 'agent_T1', 'success': True, 'message': 'ok', 'release_id': 'r1',
                                 'source': {'type': 'github', 'repo': 'acme/site', 'commit': 'a' * 40}})
        self.assertEqual(done.status_code, 200)
        hist = self.c.get('/api/bot/agents/agent_T1/history', headers=BOT).get_json()['jobs'][0]
        self.assertEqual(hist['result']['source']['commit'], 'a' * 40)  # visible in History / Report

    def test_github_job_requires_repo_url(self):
        r = self.post(source_type='github')
        self.assertEqual(r.status_code, 400)
        self.assertIn('repo_url', r.get_json()['error'])

    def test_credentials_are_refused_everywhere_in_payload(self):
        tok = 'ghp_' + 'A1b2C3d4' * 5
        cases = [dict(repo_url=f'https://{tok}@github.com/acme/site'),
                 dict(repo_url='https://github.com/acme/site', git_ref=tok),
                 dict(repo_url='https://github.com/acme/site', credential_ref=tok),
                 dict(repo_url='https://github.com/acme/site', notes=f'token {tok}'),
                 dict(repo_url='https://github.com/acme/site', extra_field={'x': tok})]
        for c in cases:
            r = self.post(source_type='github', **c)
            self.assertEqual(r.status_code, 400, c)
            self.assertNotIn(tok, r.get_data(as_text=True))
        # nothing was stored
        self.assertEqual(self.c.get('/api/bot/jobs', headers=BOT).get_json()['jobs'], [])

    def test_invalid_repo_or_ref_rejected(self):
        for c in [dict(repo_url='https://gitlab.com/a/b'), dict(repo_url='https://github.com/a/b', git_ref='--upload-pack=x'),
                  dict(repo_url='https://github.com/a/b', subdir='../x'), dict(repo_url='file:///etc')]:
            self.assertEqual(self.post(source_type='github', **c).status_code, 400, c)


if __name__ == '__main__':
    unittest.main()
