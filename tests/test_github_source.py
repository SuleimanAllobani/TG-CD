import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.git_validation import (
    GitInputError, is_commit_sha, looks_like_secret, redact, repo_allowed, validate_credential_ref,
    validate_ref, validate_repo_url, validate_subdir,
)
from agent.services.git_source_service import GitSourceError
from agent.services.deployment_service import DeploymentService
from tests.git_testutil import ListLog, local_service, make_remote


class ValidationTests(unittest.TestCase):
    def test_repo_url_accepts_canonical_forms(self):
        for u in ['https://github.com/acme/site', 'https://github.com/acme/site.git', 'https://github.com/acme/site/',
                  'github.com/acme/site', 'https://www.github.com/Acme/my.site-1', ' https://github.com/acme/site ']:
            self.assertEqual(validate_repo_url(u)['url'].split('/')[2], 'github.com', u)
        self.assertEqual(validate_repo_url('https://github.com/acme/site.git')['full_name'], 'acme/site')

    def test_repo_url_rejects_unsafe_forms(self):
        bad = ['', 'http://github.com/acme/site', 'https://gitlab.com/acme/site', 'https://github.com/acme',
               'https://github.com/acme/site/tree/main', 'https://github.com/acme/site?x=1',
               'https://user:pass@github.com/acme/site', 'https://ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b',
               'https://github.com/-evil/site', 'https://github.com/acme/..', 'ssh://git@github.com/a/b',
               'file:///etc/passwd', 'https://github.com/acme/site\n--upload-pack=x', '--upload-pack=touch /tmp/x',
               'https://github.com.evil.com/acme/site', 'ext::sh -c id']
        for u in bad:
            with self.assertRaises(GitInputError, msg=u):
                validate_repo_url(u)

    def test_error_message_never_echoes_secret(self):
        try:
            validate_repo_url('https://user:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/a/b')
        except GitInputError as e:
            self.assertNotIn('ghp_', str(e))

    def test_ref_validation(self):
        self.assertIsNone(validate_ref(None))
        self.assertIsNone(validate_ref('  '))
        for r in ['main', 'release/1.2', 'v1.0.0', 'feature_x-1', 'abc1234', 'a' * 40]:
            self.assertEqual(validate_ref(r), r)
        for r in ['--upload-pack=x', '-main', '/abs', 'a..b', 'a b', 'x;rm -rf', 'a//b', 'trail/', 'x.lock', 'a@{1}', '$(id)', 'a\nb', 'x' * 300]:
            with self.assertRaises(GitInputError, msg=r):
                validate_ref(r)

    def test_commit_sha(self):
        self.assertTrue(is_commit_sha('abc1234'))
        self.assertTrue(is_commit_sha('a' * 40))
        self.assertFalse(is_commit_sha('abc12'))
        self.assertFalse(is_commit_sha('main'))
        self.assertFalse(is_commit_sha(None))

    def test_subdir_and_credential_ref(self):
        self.assertEqual(validate_subdir('docs'), 'docs')
        self.assertEqual(validate_subdir('\\docs\\site/'), 'docs/site')
        self.assertIsNone(validate_subdir(''))
        self.assertIsNone(validate_subdir('.'))
        self.assertEqual(validate_subdir('/docs'), 'docs')  # leading slash is treated as relative to the repo
        for s in ['../x', 'a/../b', 'C:\\x', '.git', 'a/.git/b']:
            with self.assertRaises(GitInputError, msg=s):
                validate_subdir(s)
        self.assertEqual(validate_credential_ref('default'), 'default')
        self.assertIsNone(validate_credential_ref(''))
        for a in ['ghp_abcdefghijklmnopqrstuvwxyz0123456789', 'a b', 'x' * 50, '../x']:
            with self.assertRaises(GitInputError, msg=a):
                validate_credential_ref(a)

    def test_allowlist(self):
        self.assertTrue(repo_allowed('acme/site', None))
        self.assertTrue(repo_allowed('acme/site', []))
        self.assertTrue(repo_allowed('Acme/Site', ['acme/site']))
        self.assertTrue(repo_allowed('acme/other', ['acme/*']))
        self.assertFalse(repo_allowed('evil/site', ['acme/*', 'x/y']))

    def test_secret_detection_and_redaction(self):
        tok = 'ghp_' + 'A1b2C3d4' * 5
        self.assertTrue(looks_like_secret(tok))
        self.assertTrue(looks_like_secret('github_pat_' + 'x' * 30))
        self.assertTrue(looks_like_secret('Authorization: Basic abcdefghijklmnop'))
        self.assertTrue(looks_like_secret('https://u:p@github.com/a/b'))
        self.assertFalse(looks_like_secret('https://github.com/acme/site'))
        self.assertFalse(looks_like_secret('main'))
        self.assertFalse(looks_like_secret('a' * 40))  # a commit SHA is not a secret
        out = redact(f'fatal: could not read from https://x-access-token:{tok}@github.com/a/b token={tok} custom=hunter2hunter2', ['hunter2hunter2'])
        self.assertNotIn(tok, out)
        self.assertNotIn('hunter2hunter2', out)


class GitSourceLocalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.base = Path(cls._td.name) / 'remotes'
        cls.sha = make_remote(cls.base, 'acme', 'site')

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Path(self.tmp.name) / 'storage'
        self.svc = local_service(self.storage, self.base)
        self.log = ListLog()

    def tearDown(self):
        self.tmp.cleanup()

    def stage(self, ref=None, **kw):
        return self.svc.stage('https://github.com/acme/site', ref, log=self.log, **kw)

    def test_default_branch(self):
        s = self.stage()
        try:
            self.assertEqual((s.root / 'index.html').read_text(), '<h1>v2</h1>')
            self.assertEqual(s.info['commit'], self.sha['c2'])
            self.assertEqual(s.info['ref_type'], 'default')
            self.assertEqual(s.info['requested_ref'], 'default')
            self.assertEqual(s.info['repo'], 'acme/site')
            self.assertEqual(s.info['auth'], 'anonymous')
        finally:
            s.cleanup()

    def test_branch_tag_lightweight_tag(self):
        cases = [('dev', 'branch', '<h1>dev</h1>', self.sha['dev']), ('v1', 'tag', '<h1>v1</h1>', self.sha['c1']),
                 ('light', 'tag', '<h1>v1</h1>', self.sha['c1']), ('main', 'branch', '<h1>v2</h1>', self.sha['c2'])]
        for ref, typ, html, sha in cases:
            s = self.stage(ref)
            try:
                self.assertEqual(s.info['ref_type'], typ, ref)
                self.assertEqual((s.root / 'index.html').read_text(), html, ref)
                self.assertEqual(s.info['commit'], sha, ref)  # annotated tag peeled to the commit
            finally:
                s.cleanup()

    def test_full_and_short_commit(self):
        for ref in [self.sha['c1'], self.sha['c1'][:7], self.sha['c1'][:12].upper()]:
            s = self.stage(ref)
            try:
                self.assertEqual(s.info['ref_type'], 'commit')
                self.assertEqual(s.info['commit'], self.sha['c1'])
                self.assertEqual((s.root / 'index.html').read_text(), '<h1>v1</h1>')
            finally:
                s.cleanup()

    def test_git_metadata_removed_and_clean_tree(self):
        s = self.stage()
        try:
            self.assertFalse((s.root / '.git').exists())
            self.assertFalse((s.root / '.github').exists())
            self.assertTrue((s.root / 'docs' / 'page.html').exists())
            self.assertEqual(s.info['commit_subject'], 'second commit')
        finally:
            s.cleanup()

    def test_subdir(self):
        s = self.stage(subdir='docs')
        try:
            self.assertTrue((s.root / 'page.html').exists())
            self.assertFalse((s.root / 'index.html').exists())
            self.assertEqual(s.info['subdir'], 'docs')
        finally:
            s.cleanup()
        with self.assertRaises(GitSourceError) as c:
            self.stage(subdir='nope')
        self.assertIn('not found', str(c.exception))

    def test_missing_ref_and_unknown_commit_and_repo(self):
        with self.assertRaises(GitSourceError) as c:
            self.stage('no-such-branch')
        self.assertIn("'no-such-branch' was not found", str(c.exception))
        with self.assertRaises(GitSourceError) as c:
            self.stage('deadbeefdeadbeef')
        self.assertIn('not found', str(c.exception))
        with self.assertRaises(GitSourceError) as c:
            self.svc.stage('https://github.com/acme/missing', None, log=self.log)
        self.assertTrue(str(c.exception).startswith('GitHub source: '))

    def test_invalid_input_is_rejected_before_git_runs(self):
        for url, ref in [('https://gitlab.com/a/b', None), ('https://github.com/acme/site', '--upload-pack=x'),
                         ('https://github.com/acme/site', 'a b')]:
            with self.assertRaises(GitSourceError):
                self.svc.stage(url, ref, log=self.log)

    def test_allowlist_enforced(self):
        svc = local_service(self.storage, self.base, {'allowed_repos': ['other/*']})
        with self.assertRaises(GitSourceError) as c:
            svc.stage('https://github.com/acme/site', None, log=self.log)
        self.assertIn('allowed list', str(c.exception))
        svc = local_service(self.storage, self.base, {'allowed_repos': ['acme/*']})
        svc.stage('https://github.com/acme/site', None, log=self.log).cleanup()

    def test_size_limit(self):
        svc = local_service(self.storage, self.base, {'max_repo_mb': 0})
        with self.assertRaises(GitSourceError) as c:
            svc.stage('https://github.com/acme/site', None, log=self.log)
        self.assertIn('limit', str(c.exception))

    def test_failed_stage_leaves_no_temp_folders(self):
        with self.assertRaises(GitSourceError):
            self.stage('no-such-branch')
        with self.assertRaises(GitSourceError):
            self.stage('deadbeefdeadbeef')
        tmp = self.storage / 'tmp'
        self.assertEqual(list(tmp.iterdir()) if tmp.exists() else [], [])

    def test_missing_git_executable(self):
        svc = local_service(self.storage, self.base, {'executable': '/nonexistent/git'})
        with mock.patch('shutil.which', return_value=None), mock.patch('os.path.exists', return_value=False), \
                mock.patch('pathlib.Path.exists', return_value=False), self.assertRaises(GitSourceError) as c:
            svc._git_exe()
        self.assertIn('Git is not installed', str(c.exception))


def request(**over):
    base = dict(request_id='r', requested_by_user_id=1, agent_id='a', project_type='static', source_type='github',
                site_name='gh-site', app_pool_name='gh-pool', health_check_url='http://x/', site_port=8081,
                repo_url='https://github.com/acme/site', git_ref=None, package_id=None, folder_path=None)
    base.update(over)
    return base


class DeploymentGithubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.base = Path(cls._td.name) / 'remotes'
        cls.sha = make_remote(cls.base, 'acme', 'site')

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = DeploymentService(self.root)
        self.svc.git = local_service(self.root, self.base)
        self.svc.health.check = lambda url: (True, 'HTTP 200')

    def tearDown(self):
        self.tmp.cleanup()

    def current_html(self):
        return (self.root / 'current' / 'gh-site' / 'index.html').read_text()

    def releases(self):
        return json.loads((self.root / 'releases' / 'gh-site' / 'releases.json').read_text())

    def test_success_records_source_everywhere(self):
        r = self.svc.deploy(request(git_ref='v1'))
        self.assertTrue(r['success'], r)
        self.assertEqual(self.current_html(), '<h1>v1</h1>')
        self.assertFalse((self.root / 'current' / 'gh-site' / '.git').exists())
        src = r['source']
        self.assertEqual((src['repo'], src['requested_ref'], src['ref_type'], src['commit']), ('acme/site', 'v1', 'tag', self.sha['c1']))
        entry = self.releases()[-1]
        self.assertEqual(entry['status'], 'success')
        self.assertEqual(entry['source']['commit'], self.sha['c1'])
        self.assertEqual(entry['release_id'], r['release_id'])
        self.assertTrue(any('Fetched GitHub source' in a for a in r['actions']))
        self.assertEqual(list((self.root / 'tmp').iterdir()), [])  # temp checkout removed

    def test_second_deploy_then_failed_deploy_rolls_back_to_previous_commit(self):
        self.assertTrue(self.svc.deploy(request(git_ref='v1'))['success'])
        self.assertTrue(self.svc.deploy(request(git_ref='main'))['success'])
        self.assertEqual(self.current_html(), '<h1>v2</h1>')
        self.svc.health.check = lambda url: (False, 'HTTP 500')
        r = self.svc.deploy(request(git_ref='dev'))
        self.assertFalse(r['success'])
        self.assertTrue(r['rolled_back'])
        self.assertTrue(r['failed_release_preserved'])
        self.assertEqual(r['source']['commit'], self.sha['dev'])
        self.assertEqual(self.current_html(), '<h1>v2</h1>')  # back on the last good release
        failed = [e for e in self.releases() if e['status'] == 'failed_preserved'][0]
        self.assertEqual(failed['source']['commit'], self.sha['dev'])  # failed release still says what it was

    def test_manual_rollback_returns_previous_github_release(self):
        self.svc.deploy(request(git_ref='v1'))
        self.svc.deploy(request(git_ref='main'))
        item = self.svc.releases.rollback_previous_success('gh-site')
        self.assertEqual(item['source']['commit'], self.sha['c1'])
        self.assertEqual(self.current_html(), '<h1>v1</h1>')

    def test_bad_ref_fails_cleanly_without_touching_releases_or_iis(self):
        self.assertTrue(self.svc.deploy(request(git_ref='main'))['success'])
        before = self.releases()
        r = self.svc.deploy(request(git_ref='does-not-exist'))
        self.assertFalse(r['success'])
        self.assertFalse(r['rolled_back'])
        self.assertIsNone(r['release_id'])
        self.assertIn('not found', r['message'])
        self.assertEqual(self.releases(), before)
        self.assertEqual(self.current_html(), '<h1>v2</h1>')
        self.assertEqual(r['source']['repo_url'], 'https://github.com/acme/site')

    def test_first_failure_is_preserved_not_rolled_back(self):
        self.svc.health.check = lambda url: (False, 'forced')
        r = self.svc.deploy(request(git_ref='main'))
        self.assertFalse(r['success'])
        self.assertTrue(r['first_deployment_failure'])
        self.assertTrue(r['failed_release_preserved'])
        self.assertEqual(r['source']['commit'], self.sha['c2'])

    def test_unsupported_project_type_rejected_before_any_work(self):
        r = self.svc.deploy(request(project_type='php'))
        self.assertFalse(r['success'])
        self.assertIn('static', r['message'])
        self.assertIsNone(r['release_id'])

    def test_missing_repo_url(self):
        r = self.svc.deploy(request(repo_url=None))
        self.assertFalse(r['success'])
        self.assertIn('Repository URL is required', r['message'])

    def test_credentials_in_url_never_reach_result_or_logs(self):
        tok = 'ghp_' + 'A1b2C3d4' * 5
        r = self.svc.deploy(request(repo_url=f'https://x-access-token:{tok}@github.com/acme/site'))
        blob = json.dumps(r) + Path(r['log_path']).read_text()
        self.assertFalse(r['success'])
        self.assertNotIn(tok, blob)

    def test_subdir_deploys_only_that_folder(self):
        r = self.svc.deploy(request(git_ref='main', subdir='docs'))
        self.assertTrue(r['success'], r)
        self.assertTrue((self.root / 'current' / 'gh-site' / 'page.html').exists())
        self.assertEqual(r['source']['subdir'], 'docs')


if __name__ == '__main__':
    unittest.main()
