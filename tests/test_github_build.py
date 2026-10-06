"""Phase 3: build presets (npm build, .NET publish) for the GitHub source."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent.services.build_service import BuildError, BuildService
from agent.services.deployment_service import DeploymentService
from backend.api.server import create_app
from common.git_validation import GitInputError, validate_build_preset, validate_build_target
from tests.git_testutil import ListLog, git, local_service, write

HAVE_NPM = shutil.which('npm') is not None
HAVE_DOTNET = shutil.which('dotnet') is not None

BUILD_JS = ("const fs=require('fs');fs.mkdirSync('dist',{recursive:true});"
            "fs.writeFileSync('dist/index.html','<h1>built</h1>');"
            "fs.writeFileSync('dist/env.txt',Object.keys(process.env).join('\\n'));")
LOCK = json.dumps({'name': 'app', 'version': '1.0.0', 'lockfileVersion': 3, 'requires': True,
                   'packages': {'': {'name': 'app', 'version': '1.0.0'}}})
CSPROJ = ('<Project Sdk="Microsoft.NET.Sdk.Web"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
          '<ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>')
PROGRAM = 'var app = WebApplication.CreateBuilder(args).Build();\napp.MapGet("/", () => "hello");\napp.Run();\n'
NUGET_OFFLINE = '<configuration><packageSources><clear /></packageSources></configuration>'


def make_repo(base: Path, owner: str, repo: str, files: dict):
    """Bare repo base/owner/repo whose single commit on main holds `files` {path: text}."""
    work = Path(tempfile.mkdtemp(prefix='bw_'))
    try:
        git('init', '-q', '-b', 'main', cwd=work)
        for rel, text in files.items():
            write(work / rel, text)
        git('add', '-A', cwd=work)
        git('commit', '-q', '-m', 'init', cwd=work)
        target = base / owner / repo
        target.parent.mkdir(parents=True, exist_ok=True)
        git('clone', '-q', '--bare', str(work), str(target))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def req(**over):
    base = dict(request_id='r', requested_by_user_id=1, agent_id='a', project_type='static', source_type='github',
                site_name='bsite', app_pool_name='bpool', health_check_url='http://x/', site_port=8090,
                repo_url='https://github.com/acme/app', git_ref=None, package_id=None, folder_path=None,
                build_preset='npm_build')
    base.update(over)
    return base


class ValidationTests(unittest.TestCase):
    def test_preset(self):
        self.assertIsNone(validate_build_preset(None))
        self.assertIsNone(validate_build_preset(''))
        self.assertEqual(validate_build_preset('npm_build'), 'npm_build')
        for bad in ('rm -rf /', 'npm run evil', 'dotnet_publish; calc', 'NPM_BUILD'):
            with self.assertRaises(GitInputError):
                validate_build_preset(bad)

    def test_target(self):
        self.assertIsNone(validate_build_target('', 'npm_build'))
        self.assertEqual(validate_build_target('dist', 'npm_build'), 'dist')
        self.assertEqual(validate_build_target('src\\Web\\Web.csproj', 'dotnet_publish'), 'src/Web/Web.csproj')
        for bad, preset in [('src/Web', 'dotnet_publish'), ('../x.csproj', 'dotnet_publish'), ('..', 'npm_build'),
                            ('a/../../b', 'npm_build'), ('C:/x.csproj', 'dotnet_publish'), ('$(evil).csproj', 'dotnet_publish')]:
            with self.assertRaises(GitInputError, msg=bad):
                validate_build_target(bad, preset)


class BackendBuildValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        env = {'CICD_BACKEND_DB': os.path.join(self.tmp.name, 'db.sqlite3'),
               'CICD_BACKEND_PACKAGE_ROOT': os.path.join(self.tmp.name, 'pkgs'),
               'CICD_BACKEND_BOT_TOKEN': 'test-bot-token'}
        with mock.patch.dict(os.environ, env):
            self.app = create_app()
        self.c = self.app.test_client()
        self.h = {'Authorization': 'Bearer test-bot-token'}
        self.c.post('/api/bot/agents/register', json={'agent_id': 'agent_T1', 'agent_token': 'tok', 'owner_user_id': 7}, headers=self.h)

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, **over):
        p = dict(agent_id='agent_T1', site_name='s', app_pool_name='p', project_type='static', source_type='github',
                 health_check_url='http://127.0.0.1:8080/', requested_by_user_id=7, repo_url='https://github.com/acme/app')
        p.update(over)
        return self.c.post('/api/bot/jobs/deploy', json=p, headers=self.h)

    def test_static_npm_accepted(self):
        r = self.post(build_preset='npm_build', build_target='dist')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        pl = r.get_json()['payload']
        self.assertEqual((pl['build_preset'], pl['build_target']), ('npm_build', 'dist'))

    def test_dotnet_forces_publish_preset(self):
        r = self.post(project_type='dotnet', build_target='src/W/W.csproj')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['payload']['build_preset'], 'dotnet_publish')

    def test_plain_static_has_no_build(self):
        pl = self.post().get_json()['payload']
        self.assertIsNone(pl['build_preset'])
        self.assertIsNone(pl['build_target'])

    def test_bad_combinations_and_values_rejected(self):
        for over in [dict(build_preset='dotnet_publish'), dict(build_preset='bash -c x'),
                     dict(build_preset='npm_build', build_target='../x'),
                     dict(project_type='dotnet', build_target='src/W'), dict(project_type='php')]:
            r = self.post(**over)
            self.assertEqual(r.status_code, 400, over)

    def test_zip_job_gets_no_build_keys(self):
        r = self.c.post('/api/bot/jobs/deploy', json=dict(agent_id='agent_T1', site_name='s', app_pool_name='p', project_type='static',
                                                         health_check_url='http://x/', requested_by_user_id=7,
                                                         source_type='uploaded_package', package_id='pkg_1'), headers=self.h)
        pl = r.get_json()['payload']
        self.assertNotIn('build_preset', pl)
        self.assertNotIn('build_target', pl)


class BuildBase(unittest.TestCase):
    ALLOWED = ['acme/*']

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.base = Path(cls._td.name) / 'remotes'
        pkg = json.dumps({'name': 'app', 'version': '1.0.0', 'scripts': {'build': 'node build.js'}})
        make_repo(cls.base, 'acme', 'app', {'package.json': pkg, 'build.js': BUILD_JS, 'src/readme.txt': 'x'})
        make_repo(cls.base, 'acme', 'lockapp', {'package.json': pkg, 'package-lock.json': LOCK, 'build.js': BUILD_JS})
        make_repo(cls.base, 'acme', 'failing', {'package.json': json.dumps({'name': 'f', 'version': '1.0.0', 'scripts': {
            'build': "node -e \"console.error('Error: compile exploded ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'); process.exit(3)\""}})})
        make_repo(cls.base, 'acme', 'noscript', {'package.json': json.dumps({'name': 'n', 'version': '1.0.0'})})
        make_repo(cls.base, 'acme', 'nopkg', {'index.html': 'x'})
        make_repo(cls.base, 'acme', 'yarnapp', {'package.json': json.dumps({'name': 'y', 'version': '1.0.0', 'scripts': {'build': 'node build.js'}}),
                                                'yarn.lock': '', 'build.js': BUILD_JS})
        make_repo(cls.base, 'acme', 'slow', {'package.json': json.dumps({'name': 's', 'version': '1.0.0', 'scripts': {
            'build': 'node -e "setTimeout(function(){},60000)"'}})})
        make_repo(cls.base, 'acme', 'noout', {'package.json': json.dumps({'name': 'o', 'version': '1.0.0', 'scripts': {'build': 'node -e "0"'}})})
        make_repo(cls.base, 'acme', 'mono', {'web/package.json': pkg, 'web/build.js': BUILD_JS, 'api/readme.txt': 'x'})
        make_repo(cls.base, 'other', 'app', {'package.json': pkg, 'build.js': BUILD_JS})

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def make(self, allowed=None, **settings):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        s = {'allowed_repos': self.ALLOWED if allowed is None else allowed}
        s.update(settings)
        self.svc = DeploymentService(self.root, s)
        self.svc.git = local_service(self.root, self.base, s)
        self.svc.health.check = lambda url: (True, 'HTTP 200')
        return self.svc

    def site(self, name='bsite'):
        return self.root / 'current' / name


@unittest.skipUnless(HAVE_NPM, 'npm is not installed')
class NpmBuildTests(BuildBase):
    def test_npm_install_build_deploys_output_only(self):
        svc = self.make()
        r = svc.deploy(req())
        self.assertTrue(r['success'], r)
        self.assertEqual((self.site() / 'index.html').read_text(), '<h1>built</h1>')
        self.assertFalse((self.site() / 'package.json').exists())      # source files are not deployed
        self.assertFalse((self.site() / 'node_modules').exists())
        b = r['source']['build']
        self.assertEqual((b['preset'], b['output'], b['tool'].split()[0]), ('npm_build', 'dist', 'npm'))
        self.assertIn('Built npm_build', r['actions'])
        entry = json.loads((self.root / 'releases' / 'bsite' / 'releases.json').read_text())[-1]
        self.assertEqual(entry['source']['build']['preset'], 'npm_build')
        self.assertEqual(entry['source']['repo'], 'acme/app')
        self.assertTrue(entry['source']['commit'])

    def test_lockfile_uses_npm_ci(self):
        log = ListLog()
        svc = self.make()
        staged = svc.git.stage('https://github.com/acme/lockapp', None, None, None, log)
        try:
            out = svc.builder.build('npm_build', staged.root, staged.work, None, log)
        finally:
            staged.cleanup()
        self.assertIn('npm ci', log.text())
        self.assertEqual(out.info['output'], 'dist')

    def test_explicit_output_target(self):
        svc = self.make()
        r = svc.deploy(req(build_target='dist'))
        self.assertTrue(r['success'], r)
        self.assertEqual(r['source']['build']['target'], 'dist')

    def test_missing_output_target_fails_cleanly(self):
        svc = self.make()
        r = svc.deploy(req(build_target='nope'))
        self.assertFalse(r['success'])
        self.assertIn('does not exist after the build', r['message'])

    def test_monorepo_subdir(self):
        svc = self.make()
        r = svc.deploy(req(repo_url='https://github.com/acme/mono', subdir='web'))
        self.assertTrue(r['success'], r)
        self.assertEqual((self.site() / 'index.html').read_text(), '<h1>built</h1>')

    def test_build_environment_is_minimal(self):
        secrets = {'TELEGRAM_BOT_TOKEN': 'tg-secret-1', 'CICD_GIT_TOKEN_DEFAULT': 'ghp_secret_2', 'CICD_AGENT_TOKEN': 'agent-secret-3',
                   'GIT_ASKPASS': '/bin/true', 'MY_CUSTOM_SECRET': 'custom-4', 'GITHUB_TOKEN': 'gh-5'}
        with mock.patch.dict(os.environ, secrets):
            svc = self.make()
            r = svc.deploy(req())
        self.assertTrue(r['success'], r)
        # env.txt is written into the deployed output by the build script itself
        names = set((self.site() / 'env.txt').read_text().split())
        for k in secrets:
            self.assertNotIn(k, names)
        self.assertIn('PATH', {n.upper() for n in names})
        self.assertIn('npm_config_cache', names)

    def test_passthrough_setting_allows_named_variable(self):
        with mock.patch.dict(os.environ, {'MY_BUILD_FLAG': '1'}):
            svc = self.make(build_env_passthrough=['MY_BUILD_FLAG'])
            r = svc.deploy(req())
        self.assertIn('MY_BUILD_FLAG', set((self.site() / 'env.txt').read_text().split()))

    def test_failed_build_touches_nothing_and_is_redacted(self):
        svc = self.make()
        r = svc.deploy(req(repo_url='https://github.com/acme/failing'))
        self.assertFalse(r['success'])
        self.assertIn('Build failed', r['message'])
        self.assertIn('exit code 3', r['message'])
        self.assertNotIn('ghp_ABCDEFGHIJ', json.dumps(r))
        self.assertIsNone(r['release_id'])
        self.assertFalse(r['rolled_back'])
        self.assertFalse((self.root / 'releases' / 'bsite').exists() and any((self.root / 'releases' / 'bsite').iterdir()))
        self.assertFalse((self.root / 'current' / 'bsite').exists())
        self.assertEqual(r['source']['repo'], 'acme/failing')

    def test_failed_build_keeps_previous_release_live(self):
        svc = self.make()
        self.assertTrue(svc.deploy(req())['success'])
        r = svc.deploy(req(repo_url='https://github.com/acme/failing'))
        self.assertFalse(r['success'])
        self.assertEqual((self.site() / 'index.html').read_text(), '<h1>built</h1>')
        entries = json.loads((self.root / 'releases' / 'bsite' / 'releases.json').read_text())
        self.assertEqual([e['status'] for e in entries], ['success'])

    def test_failing_health_after_build_rolls_back(self):
        svc = self.make()
        self.assertTrue(svc.deploy(req())['success'])
        svc.health.check = lambda url: (False, 'forced')
        r = svc.deploy(req())
        self.assertFalse(r['success'])
        self.assertTrue(r['rolled_back'])
        self.assertTrue(r['failed_release_preserved'])
        self.assertEqual(r['source']['build']['preset'], 'npm_build')
        self.assertEqual((self.site() / 'index.html').read_text(), '<h1>built</h1>')

    def test_missing_script_package_and_yarn(self):
        svc = self.make()
        for repo, text in [('noscript', "no 'build' script"), ('nopkg', 'package.json was not found'),
                           ('yarnapp', 'yarn or pnpm'), ('noout', 'no output folder')]:
            r = svc.deploy(req(repo_url=f'https://github.com/acme/{repo}'))
            self.assertFalse(r['success'], repo)
            self.assertIn(text, r['message'], repo)
            self.assertIsNone(r['release_id'])

    def test_timeout_kills_build(self):
        svc = self.make(build_timeout_seconds=3)
        t = time.time()
        r = svc.deploy(req(repo_url='https://github.com/acme/slow'))
        self.assertLess(time.time() - t, 40)
        self.assertFalse(r['success'])
        self.assertIn('timed out after 3s', r['message'])

    def test_temp_folders_are_cleaned_up(self):
        svc = self.make()
        svc.deploy(req())
        svc.deploy(req(repo_url='https://github.com/acme/failing'))
        tmp = self.root / 'tmp'
        self.assertEqual(list(tmp.iterdir()) if tmp.exists() else [], [])

    def test_npm_not_installed_message(self):
        svc = self.make()
        with mock.patch('agent.services.build_service.shutil') as sh:
            sh.which.return_value = None
            r = svc.deploy(req())
        self.assertFalse(r['success'])
        self.assertIn('Node.js/npm is not installed', r['message'])


class AllowlistTests(BuildBase):
    def test_build_refused_for_repo_not_on_allowlist(self):
        svc = self.make(allowed=['someone/else'])
        r = svc.deploy(req())
        self.assertFalse(r['success'])
        self.assertIn('git.allowed_repos', r['message'])
        self.assertIsNone(r['release_id'])

    def test_empty_allowlist_means_no_builds(self):
        svc = self.make(allowed=[])
        r = svc.deploy(req())
        self.assertFalse(r['success'])
        self.assertIn('git.allowed_repos', r['message'])

    def test_allowlist_is_checked_before_anything_is_fetched(self):
        svc = self.make(allowed=['someone/else'])
        with mock.patch.object(svc.git, 'stage', side_effect=AssertionError('must not fetch')):
            r = svc.deploy(req())
        self.assertIn('git.allowed_repos', r['message'])

    def test_plain_file_deploy_still_works_with_empty_allowlist(self):
        svc = self.make(allowed=[])
        r = svc.deploy(req(build_preset=None))
        self.assertTrue(r['success'], r)
        self.assertNotIn('build', r['source'])
        self.assertTrue((self.site() / 'package.json').exists())

    def test_owner_wildcard_and_exact_match(self):
        for allowed in (['acme/*'], ['ACME/App'], ['*']):
            svc = BuildService(self.root if hasattr(self, 'root') else tempfile.gettempdir(), {'allowed_repos': allowed})
            svc.require_allowed('acme/app')

    def test_other_owner_blocked(self):
        svc = BuildService(tempfile.gettempdir(), {'allowed_repos': ['acme/*']})
        with self.assertRaises(BuildError):
            svc.require_allowed('other/app')


class CombinationTests(BuildBase):
    def test_dotnet_project_cannot_use_npm(self):
        svc = self.make()
        r = svc.deploy(req(project_type='dotnet', build_preset='npm_build'))
        self.assertFalse(r['success'])
        self.assertIn('.NET', r['message'])
        self.assertIsNone(r['release_id'])

    def test_static_cannot_use_dotnet_publish(self):
        svc = self.make()
        r = svc.deploy(req(build_preset='dotnet_publish'))
        self.assertFalse(r['success'])
        self.assertIn('static', r['message'].lower())

    def test_zip_and_folder_ignore_build_fields(self):
        svc = self.make()
        folder = self.root / 'src'
        write(folder / 'index.html', 'plain')
        r = svc.deploy(dict(request_id='z', requested_by_user_id=1, agent_id='a', project_type='static', source_type='folder_path',
                            site_name='fsite', app_pool_name='fpool', health_check_url='http://x/', site_port=8091, folder_path=str(folder)))
        self.assertTrue(r['success'], r)
        self.assertNotIn('source', r)


@unittest.skipUnless(HAVE_DOTNET, '.NET SDK is not installed')
class DotnetBuildTests(BuildBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        web = {'nuget.config': NUGET_OFFLINE, 'src/Web/Web.csproj': CSPROJ, 'src/Web/Program.cs': PROGRAM}
        make_repo(cls.base, 'acme', 'dnapp', web)
        make_repo(cls.base, 'acme', 'dntwo', {**web, 'src/Admin/Admin.csproj': CSPROJ, 'src/Admin/Program.cs': PROGRAM})
        make_repo(cls.base, 'acme', 'dntest', {**web, 'tests/Web.Tests/Web.Tests.csproj': CSPROJ.replace('Sdk.Web', 'Sdk'),
                                               'tests/Web.Tests/T.cs': 'class T {}'})
        make_repo(cls.base, 'acme', 'dnbad', {'nuget.config': NUGET_OFFLINE, 'src/Web/Web.csproj': CSPROJ,
                                              'src/Web/Program.cs': 'this is not C#'})
        make_repo(cls.base, 'acme', 'dnnone', {'readme.md': 'no project here'})

    def dn(self, **over):
        base = dict(project_type='dotnet', build_preset=None, repo_url='https://github.com/acme/dnapp')
        base.update(over)
        return req(**base)

    def test_publish_autodetect(self):
        svc = self.make()
        r = svc.deploy(self.dn())
        self.assertTrue(r['success'], r)
        site = self.site()
        self.assertTrue((site / 'Web.dll').exists())
        self.assertTrue((site / 'web.config').exists())
        self.assertFalse((site / 'Program.cs').exists())
        self.assertFalse((site / 'obj').exists())
        b = r['source']['build']
        self.assertEqual((b['preset'], b['target']), ('dotnet_publish', 'src/Web/Web.csproj'))  # auto-detected project is recorded
        self.assertTrue(b['tool'].startswith('dotnet'))

    def test_explicit_project(self):
        svc = self.make()
        r = svc.deploy(self.dn(build_target='src/Web/Web.csproj'))
        self.assertTrue(r['success'], r)
        self.assertEqual(r['source']['build']['target'], 'src/Web/Web.csproj')

    def test_wrong_target_file_fails_cleanly(self):
        svc = self.make()
        r = svc.deploy(self.dn(build_target='src/Nope/Nope.csproj'))
        self.assertFalse(r['success'])
        self.assertIn('Build failed', r['message'])
        self.assertIsNone(r['release_id'])

    def test_several_web_projects_asks_for_choice(self):
        svc = self.make()
        r = svc.deploy(self.dn(repo_url='https://github.com/acme/dntwo'))
        self.assertFalse(r['success'])
        self.assertIn('Web.csproj', r['message'])
        self.assertIn('Admin.csproj', r['message'])

    def test_test_projects_are_ignored_in_autodetect(self):
        svc = self.make()
        r = svc.deploy(self.dn(repo_url='https://github.com/acme/dntest'))
        self.assertTrue(r['success'], r)
        self.assertTrue((self.site() / 'Web.dll').exists())

    def test_compile_error_is_reported_and_touches_nothing(self):
        svc = self.make()
        r = svc.deploy(self.dn(repo_url='https://github.com/acme/dnbad'))
        self.assertFalse(r['success'])
        self.assertIn('Build failed', r['message'])
        self.assertIn('error', r['message'].lower())
        self.assertIsNone(r['release_id'])
        self.assertFalse((self.root / 'current' / 'bsite').exists())

    def test_no_project_found(self):
        svc = self.make()
        r = svc.deploy(self.dn(repo_url='https://github.com/acme/dnnone'))
        self.assertFalse(r['success'])
        self.assertIn('project', r['message'].lower())

    def test_not_allowed_repo_does_not_build(self):
        svc = self.make(allowed=['someone/else'])
        r = svc.deploy(self.dn())
        self.assertFalse(r['success'])
        self.assertIn('git.allowed_repos', r['message'])

    def test_dotnet_failing_health_rolls_back(self):
        svc = self.make()
        self.assertTrue(svc.deploy(self.dn())['success'])
        svc.health.check = lambda url: (False, 'forced')
        r = svc.deploy(self.dn())
        self.assertTrue(r['rolled_back'])
        self.assertTrue((self.site() / 'Web.dll').exists())

    def test_dotnet_missing_sdk_message(self):
        svc = self.make()
        with mock.patch('shutil.which', return_value=None), \
                mock.patch('pathlib.Path.exists', side_effect=lambda *a, **k: False):
            with self.assertRaises(Exception):
                svc.builder._dotnet_exe()


if __name__ == '__main__':
    unittest.main()
