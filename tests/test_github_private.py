"""Phase 2: private repositories with an agent-side token.

The authentication test spins up a real HTTP git server (git http-backend behind a Basic-auth
check), so the Authorization header that git actually sends is exercised end to end.
"""
import base64
import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent.services.deployment_service import DeploymentService
from agent.services.git_source_service import GitSourceError, GitSourceService
from agent import set_git_token
from tests.git_testutil import ListLog, file_uri_base, make_remote

TOKEN = 'github_pat_' + 'Zz9Yy8Xx7Ww6Vv5U' * 3
TOKEN_B64 = base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()
GIT_BACKEND = subprocess.run(['git', '--exec-path'], capture_output=True, text=True).stdout.strip() + '/git-http-backend'


class AuthGitServer:
    """Serves bare repos under `root` over HTTP; requires `Authorization: Basic base64(x-access-token:TOKEN)`."""

    def __init__(self, root: Path, token: str):
        outer = self
        self.seen_auth = []
        self.unauth_requests = 0
        expected = 'Basic ' + base64.b64encode(f'x-access-token:{token}'.encode()).decode()

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.0'

            def log_message(self, *a):
                pass

            def handle_any(self):
                auth = self.headers.get('Authorization')
                outer.seen_auth.append(auth)
                if auth != expected:
                    outer.unauth_requests += 1
                    self.send_response(401)
                    self.send_header('WWW-Authenticate', 'Basic realm="git"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                path, _, query = self.path.partition('?')
                length = int(self.headers.get('Content-Length') or 0)
                body = self.rfile.read(length) if length else b''
                env = {
                    'GIT_PROJECT_ROOT': str(root), 'GIT_HTTP_EXPORT_ALL': '1', 'REQUEST_METHOD': self.command,
                    'PATH_INFO': path, 'QUERY_STRING': query, 'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                    'CONTENT_LENGTH': str(len(body)), 'REMOTE_USER': 'x', 'REMOTE_ADDR': '127.0.0.1',
                    'PATH': os.environ.get('PATH', ''), 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull,
                }
                if self.headers.get('Git-Protocol'):
                    env['GIT_PROTOCOL'] = self.headers['Git-Protocol']
                if self.headers.get('Content-Encoding'):
                    env['HTTP_CONTENT_ENCODING'] = self.headers['Content-Encoding']
                p = subprocess.run([GIT_BACKEND], input=body, env=env, capture_output=True)
                head, _, payload = p.stdout.partition(b'\r\n\r\n')
                status, headers = 200, []
                for line in head.decode('latin-1').split('\r\n'):
                    k, _, v = line.partition(':')
                    if k.lower() == 'status':
                        status = int(v.strip().split()[0])
                    elif k:
                        headers.append((k, v.strip()))
                self.send_response(status)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = handle_any

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        self.base = f'http://127.0.0.1:{self.port}/'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def private_service(storage, server: AuthGitServer, settings=None):
    return GitSourceService(
        storage, settings, allowed_protocols=('https', 'http'), auth_scope=server.base,
        extra_config=[(f'url.{server.base}.insteadOf', 'https://github.com/')])


def all_file_bytes(root: Path) -> bytes:
    blob = b''
    for p in root.rglob('*'):
        if p.is_file():
            try:
                blob += p.read_bytes()
            except OSError:
                pass
    return blob


class CredentialLoadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = self.root / 'storage'

    def tearDown(self):
        self.tmp.cleanup()

    def svc(self, settings=None):
        return GitSourceService(self.storage, settings)

    def test_default_file_location_and_formats(self):
        sd = self.root / 'secrets'
        sd.mkdir()
        (sd / 'github_default.token').write_bytes(b'\xef\xbb\xbf' + TOKEN.encode() + b'\r\n')  # BOM + CRLF (Notepad)
        self.assertEqual(self.svc().load_token('default'), TOKEN)

    def test_env_var_by_alias_and_settings_override_order(self):
        with mock.patch.dict(os.environ, {'CICD_GIT_TOKEN_MY_APP': 'tok_from_alias_env_000000000000',
                                          'CUSTOM_VAR': 'tok_from_custom_env_000000000000'}):
            self.assertEqual(self.svc().load_token('my-app'), 'tok_from_alias_env_000000000000')
            s = self.svc({'credentials': {'my-app': {'token_env': 'CUSTOM_VAR'}}})
            self.assertEqual(s.load_token('my-app'), 'tok_from_custom_env_000000000000')

    def test_token_file_from_settings_relative_and_absolute(self):
        sd = self.root / 'secrets'
        sd.mkdir()
        (sd / 'special.txt').write_text('tok_relative_file_0000000000')
        self.assertEqual(self.svc({'credentials': {'x': {'token_file': 'special.txt'}}}).load_token('x'), 'tok_relative_file_0000000000')
        other = self.root / 'elsewhere.txt'
        other.write_text('tok_absolute_file_0000000000')
        self.assertEqual(self.svc({'credentials': {'x': {'token_file': str(other)}}}).load_token('x'), 'tok_absolute_file_0000000000')

    def test_missing_credential_error_is_actionable_and_has_no_secret(self):
        with self.assertRaises(GitSourceError) as c:
            self.svc().load_token('nothing')
        msg = str(c.exception)
        self.assertIn("'nothing' is not configured", msg)
        self.assertIn('agent.set_git_token', msg)

    def test_malformed_tokens_rejected(self):
        sd = self.root / 'secrets'
        sd.mkdir()
        for i, bad in enumerate(['two words', 'line1\nline2', 'tökén', 'x' * 300]):
            (sd / f'github_bad{i}.token').write_text(bad, encoding='utf-8')
            with self.assertRaises(GitSourceError, msg=bad) as c:
                self.svc().load_token(f'bad{i}')
            self.assertIn('malformed', str(c.exception))

    def test_set_git_token_tool_writes_protected_file(self):
        settings = {'agent': {'storage_root': str(self.storage)}}
        p = set_git_token.save_token(settings, 'default', TOKEN)
        self.assertEqual(p.read_text(), TOKEN)
        if os.name != 'nt':
            self.assertEqual(oct(p.stat().st_mode & 0o777), '0o600')
            self.assertEqual(oct(p.parent.stat().st_mode & 0o777), '0o700')
        self.assertEqual(GitSourceService(self.storage).load_token('default'), TOKEN)
        with self.assertRaises(ValueError):
            set_git_token.save_token(settings, 'default', 'has space')
        with self.assertRaises(ValueError):
            set_git_token.save_token(settings, 'default', '')
        self.assertEqual(p.read_text(), TOKEN)  # a rejected token does not overwrite the good one

    def test_set_git_token_cli_stdin_and_remove(self):
        cfg = self.root / 'cfg'
        cfg.mkdir()
        fake = {'agent': {'storage_root': str(self.storage)}, 'git': {}}
        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet, mock.patch('common.config.load_settings', return_value=fake), \
                mock.patch('sys.stdin', io.StringIO(TOKEN + '\n')):
            self.assertEqual(set_git_token.main(['--alias', 'ci', '--stdin']), 0)
        self.assertTrue((self.root / 'secrets' / 'github_ci.token').exists())
        with contextlib.redirect_stdout(io.StringIO()), mock.patch('common.config.load_settings', return_value=fake):
            self.assertEqual(set_git_token.main(['--alias', 'ci', '--remove']), 0)
            self.assertEqual(set_git_token.main(['--alias', '../evil', '--remove']), 2)
        self.assertFalse((self.root / 'secrets' / 'github_ci.token').exists())


class TokenHandlingTests(unittest.TestCase):
    """The token must reach git only through the child environment, scoped to github.com."""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.base = Path(cls._td.name) / 'remotes'
        make_remote(cls.base, 'acme', 'private-site')

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_token_never_in_argv_and_only_in_scoped_header_env(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = GitSourceService(Path(tmp.name) / 'storage', {'credentials': {'default': {'token_env': 'MY_GH_TOKEN'}}},
                               allowed_protocols=('https', 'file'),
                               extra_config=[(f'url.{file_uri_base(self.base)}.insteadOf', 'https://github.com/')])
        calls = []
        real_run = subprocess.run

        def spy(cmd, *a, **k):
            calls.append((list(cmd), dict(k.get('env') or {})))
            return real_run(cmd, *a, **k)

        log = ListLog()
        with mock.patch.dict(os.environ, {'MY_GH_TOKEN': TOKEN, 'CICD_GIT_TOKEN_OTHER': 'another_secret_value_0000'}), \
                mock.patch('subprocess.run', spy):
            staged = svc.stage('https://github.com/acme/private-site', None, 'default', log=log)
        staged.cleanup()
        self.assertTrue(calls)
        for cmd, env in calls:
            joined = ' '.join(cmd)
            self.assertNotIn(TOKEN, joined)
            self.assertNotIn(TOKEN_B64, joined)
            self.assertNotIn('Authorization', joined)
            self.assertNotIn('MY_GH_TOKEN', env)                       # the raw token variable is not inherited by git
            self.assertFalse([k for k in env if k.startswith('CICD_GIT_TOKEN_')])
            self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
            headers = [v for k, v in env.items() if k.startswith('GIT_CONFIG_VALUE_') and v.startswith('Authorization:')]
            self.assertEqual(headers, [f'Authorization: Basic {TOKEN_B64}'])
            keys = [v for k, v in env.items() if k.startswith('GIT_CONFIG_KEY_') and v.endswith('.extraheader')]
            self.assertEqual(keys, ['http.https://github.com/.extraheader'])  # scoped to github.com only
        self.assertNotIn(TOKEN, log.text())
        self.assertEqual(staged.info['auth'], 'token:default')

    def test_inherited_git_environment_is_filtered(self):
        svc = GitSourceService(tempfile.gettempdir())
        parent = {'GIT_TRACE': '1', 'GIT_CURL_VERBOSE': '1', 'GIT_SSL_NO_VERIFY': '1', 'GIT_DIR': '/x', 'GIT_ASKPASS': 'evil',
                  'GIT_CONFIG_COUNT': '9', 'GIT_SSL_CAINFO': '/corp-ca.pem', 'HTTPS_PROXY': 'http://proxy:3128', 'PATH': os.environ.get('PATH', '')}
        with mock.patch.dict(os.environ, parent):
            env = svc._env(None)
        for bad in ('GIT_TRACE', 'GIT_CURL_VERBOSE', 'GIT_SSL_NO_VERIFY', 'GIT_DIR', 'GIT_ASKPASS'):
            self.assertNotIn(bad, env)
        self.assertEqual(env['GIT_SSL_CAINFO'], '/corp-ca.pem')   # corporate CA still honoured
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy:3128')  # proxy still honoured
        self.assertNotEqual(env['GIT_CONFIG_COUNT'], '9')          # ours, not the parent's

    def test_error_text_is_redacted_even_if_git_echoes_the_token(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        svc = GitSourceService(Path(tmp.name) / 'storage', {'credentials': {'default': {'token_env': 'MY_GH_TOKEN'}}})
        fake = subprocess.CompletedProcess([], 128, '', f"fatal: unable to access 'https://x-access-token:{TOKEN}@github.com/a/b/': header Authorization: Basic {TOKEN_B64} {TOKEN}")
        with mock.patch.dict(os.environ, {'MY_GH_TOKEN': TOKEN}), mock.patch('subprocess.run', return_value=fake):
            with self.assertRaises(GitSourceError) as c:
                svc.stage('https://github.com/acme/site', None, 'default')
        self.assertNotIn(TOKEN, str(c.exception))
        self.assertNotIn(TOKEN_B64, str(c.exception))
        self.assertIn('GitHub source:', str(c.exception))


@unittest.skipUnless(os.path.exists(GIT_BACKEND), 'git-http-backend not available')
class PrivateRepoHttpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.remotes = Path(cls._td.name) / 'remotes'
        cls.sha = make_remote(cls.remotes, 'acme', 'private-site')
        cls.server = AuthGitServer(cls.remotes, TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls._td.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = {'credentials': {'default': {'token_env': 'MY_GH_TOKEN'}, 'wrong': {'token_env': 'WRONG_TOKEN'}}}
        self.server.seen_auth.clear()
        self.server.unauth_requests = 0

    def tearDown(self):
        self.tmp.cleanup()

    def svc(self):
        return private_service(self.root / 'storage', self.server, self.settings)

    def env(self, **extra):
        return mock.patch.dict(os.environ, {'MY_GH_TOKEN': TOKEN, 'WRONG_TOKEN': 'github_pat_wrong_wrong_wrong_wrong_0000', **extra})

    def test_correct_token_clones_private_repo(self):
        with self.env():
            s = self.svc().stage('https://github.com/acme/private-site', 'main', 'default', log=ListLog())
        try:
            self.assertEqual((s.root / 'index.html').read_text(), '<h1>v2</h1>')
            self.assertEqual(s.info['commit'], self.sha['c2'])
            self.assertEqual(s.info['auth'], 'token:default')
        finally:
            s.cleanup()
        self.assertTrue(self.server.seen_auth)
        self.assertTrue(all(a == f'Basic {TOKEN_B64}' for a in self.server.seen_auth))  # git really sent it on every request
        self.assertEqual(self.server.unauth_requests, 0)

    def test_commit_and_tag_work_with_token(self):
        with self.env():
            for ref, sha in [('v1', self.sha['c1']), (self.sha['c1'], self.sha['c1']), (self.sha['c1'][:8], self.sha['c1'])]:
                s = self.svc().stage('https://github.com/acme/private-site', ref, 'default')
                try:
                    self.assertEqual(s.info['commit'], sha, ref)
                finally:
                    s.cleanup()

    def test_no_credential_gives_friendly_error(self):
        log = ListLog()
        with self.assertRaises(GitSourceError) as c:
            self.svc().stage('https://github.com/acme/private-site', None, None, log=log)
        self.assertIn('Cannot access the repository', str(c.exception))
        self.assertIn('private', str(c.exception))

    def test_wrong_token_gives_friendly_error_without_leaking_it(self):
        with self.env(), self.assertRaises(GitSourceError) as c:
            self.svc().stage('https://github.com/acme/private-site', None, 'wrong', log=ListLog())
        self.assertIn('Cannot access the repository', str(c.exception))
        self.assertNotIn('wrong_wrong', str(c.exception))

    def test_unconfigured_alias_fails_before_any_network_call(self):
        with self.env(), self.assertRaises(GitSourceError) as c:
            self.svc().stage('https://github.com/acme/private-site', None, 'nope')
        self.assertIn('not configured', str(c.exception))
        self.assertEqual(self.server.seen_auth, [])

    def test_full_deployment_leaves_no_token_anywhere_on_disk(self):
        svc = DeploymentService(self.root, self.settings)
        svc.git = self.svc()
        svc.health.check = lambda url: (True, 'HTTP 200')
        req = dict(request_id='r', requested_by_user_id=1, agent_id='a', project_type='static', source_type='github',
                   site_name='priv', app_pool_name='pp', health_check_url='http://x/', site_port=8082,
                   repo_url='https://github.com/acme/private-site', git_ref='main', credential_ref='default')
        with self.env():
            r = svc.deploy(req)
            self.assertTrue(r['success'], r)
            # failed deployment too (unknown ref) and a rejected credential
            bad = svc.deploy(dict(req, git_ref='nope'))
            bad2 = svc.deploy(dict(req, credential_ref='wrong'))
        self.assertFalse(bad['success'])
        self.assertFalse(bad2['success'])
        self.assertEqual(r['source']['auth'], 'token:default')
        blob = all_file_bytes(self.root) + json.dumps([r, bad, bad2]).encode()
        for secret in (TOKEN, TOKEN_B64, 'wrong_wrong_wrong'):
            self.assertNotIn(secret.encode(), blob, secret)
        self.assertFalse((self.root / 'current' / 'priv' / '.git').exists())
        self.assertEqual(list((self.root / 'storage' / 'tmp').iterdir()), [])  # temp checkout removed even after failures


if __name__ == '__main__':
    unittest.main()
