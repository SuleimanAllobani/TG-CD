"""Drives the Telegram conversation with fake Telegram objects and a fake backend.

Runs inside a temporary working directory because the bot keeps its state files
at relative paths (data/user_state.json, data/agents.json ...).
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
UID = 4242
TOKEN = 'ghp_' + 'A1b2C3d4' * 5


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **_):
        self.replies.append((text, reply_markup))

    async def delete(self):
        self.deleted = True


class FakeUpdate:
    def __init__(self, text):
        self.message = FakeMessage(text)
        self.effective_user = mock.Mock(id=UID)


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.from_user = mock.Mock(id=UID)
        self.edits = []
        self.message = FakeMessage()

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))


class FakeUpdateCb:
    def __init__(self, data):
        self.callback_query = FakeQuery(data)


class FakeBackend:
    instances = []
    result = {'success': True, 'site_name': 'MySite', 'app_pool_name': 'MyPool', 'release_id': 'rel_1',
              'source': {'type': 'github', 'repo': 'acme/deploy-tools', 'requested_ref': 'release/logs-fix', 'commit_short': 'abc1234'}}

    def __init__(self, *a, **k):
        self.uploaded = []
        self.jobs = []
        FakeBackend.instances.append(self)

    def upload_package(self, uid, path):
        self.uploaded.append(path)
        return {'package_id': 'pkg_fake'}

    def create_deploy_job(self, payload):
        self.jobs.append(payload)
        FakeBackend.last_payload = payload
        return {'job_id': 'job_fake'}

    def wait_for_job(self, job_id, timeout_seconds=300, poll_seconds=3):
        FakeBackend.last_timeout = timeout_seconds
        return {'status': 'success', 'result': FakeBackend.result}


def callback_data(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


class BotGithubFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_cwd = os.getcwd()
        cls._td = tempfile.TemporaryDirectory()
        sys.path.insert(0, str(ROOT))
        os.chdir(cls._td.name)
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '123456:TESTTOKEN_FOR_UNIT_TESTS_ONLY_000'}):
            for mod in [m for m in sys.modules if m == 'bot.main']:
                del sys.modules[mod]
            import bot.main as bm
        cls.bm = bm
        bm.BACKEND_ENABLED = True
        bm.BackendClient = FakeBackend

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls._old_cwd)
        cls._td.cleanup()

    def setUp(self):
        bm = self.bm
        bm.states.clear(UID)
        bm.agents.register(UID, 'agent_T', 'TestAgent', 'backend://http://127.0.0.1:9000', 'tok')
        FakeBackend.instances.clear()
        FakeBackend.last_payload = None

    async def cb(self, data):
        u = FakeUpdateCb(data)
        await self.bm.on_callback(u, mock.Mock(args=[]))
        return u.callback_query

    async def text(self, text):
        u = FakeUpdate(text)
        await self.bm.on_text(u, mock.Mock(args=[]))
        return u.message

    def state(self):
        return self.bm.states.get(UID)

    async def reach_github_url_step(self, ptype='static'):
        await self.cb('agent:agent_T')
        q = await self.cb(f'ptype:{ptype}')
        await self.cb('source:github')
        return q

    async def test_source_menu_has_zip_folder_and_github(self):
        q = await self.cb('agent:agent_T')
        q = await self.cb('ptype:static')
        self.assertEqual(callback_data(q.edits[-1][1]), ['source:zip', 'source:folder', 'source:github', 'cancel'])

    async def test_full_public_flow_and_payload(self):
        await self.reach_github_url_step()
        self.assertEqual(self.state()['state'], 'WAIT_REPO_URL')
        # Words like "deploy" / "logs" must not be hijacked by the menu matcher.
        m = await self.text('https://github.com/acme/deploy-tools')
        self.assertEqual(self.state()['state'], 'GH_VISIBILITY')
        self.assertEqual(self.state()['repo_url'], 'https://github.com/acme/deploy-tools')
        self.assertEqual(callback_data(m.replies[-1][1]), ['gh_vis:public', 'gh_vis:private', 'cancel'])
        await self.cb('gh_vis:public')
        self.assertEqual(self.state()['state'], 'WAIT_GIT_REF')
        await self.text('release/logs-fix')
        self.assertEqual((self.state()['state'], self.state()['git_ref']), ('WAIT_SUBDIR', 'release/logs-fix'))
        await self.text('docs')
        self.assertEqual((self.state()['state'], self.state()['subdir']), ('GH_BUILD_Q', 'docs'))
        await self.cb('gh_build:none')
        self.assertEqual(self.state()['state'], 'WAIT_SITE')
        # existing steps are untouched
        await self.text('MySite')
        await self.cb('site_exists:no')
        await self.text('8085')
        await self.text('MyPool')
        await self.cb('pool_exists:no')
        m = await self.text('http://127.0.0.1:8085/')
        summary_text = m.replies[-1][0]
        self.assertIn('Repository: https://github.com/acme/deploy-tools', summary_text)
        self.assertIn('Branch/tag/commit: release/logs-fix', summary_text)
        self.assertIn('Access: public', summary_text)
        self.assertIn('Deploy folder: docs', summary_text)
        self.assertIn('Build: none', summary_text)
        q = await self.cb('confirm:yes')
        be = FakeBackend.instances[-1]
        self.assertEqual(be.uploaded, [])  # nothing uploaded for GitHub
        p = be.jobs[0]
        self.assertEqual((p['source_type'], p['repo_url'], p['git_ref'], p['subdir'], p['credential_ref']),
                         ('github', 'https://github.com/acme/deploy-tools', 'release/logs-fix', 'docs', None))
        self.assertIsNone(p['build_preset'])
        self.assertEqual(FakeBackend.last_timeout, 600)
        final = q.message.replies[-1][0]
        self.assertIn('Source: acme/deploy-tools@release/logs-fix (abc1234)', final)

    async def test_default_branch_and_root_buttons(self):
        await self.reach_github_url_step()
        await self.text('github.com/acme/site')
        await self.cb('gh_vis:public')
        await self.cb('gh_ref:default')
        self.assertEqual((self.state()['state'], self.state()['git_ref']), ('WAIT_SUBDIR', None))
        await self.cb('gh_subdir:root')
        self.assertEqual((self.state()['state'], self.state()['subdir']), ('GH_BUILD_Q', None))

    async def test_private_flow_sends_only_credential_name(self):
        await self.reach_github_url_step()
        await self.text('https://github.com/acme/private-site')
        q = await self.cb('gh_vis:private')
        self.assertIn('never in Telegram', q.edits[-1][0])
        self.assertEqual(self.state()['state'], 'WAIT_CRED_REF')
        await self.text('default')
        self.assertEqual((self.state()['state'], self.state()['credential_ref']), ('WAIT_GIT_REF', 'default'))
        await self.cb('gh_ref:default')
        await self.cb('gh_subdir:root')
        await self.cb('gh_build:none')
        await self.text('S')
        await self.cb('site_exists:no')
        await self.text('8086')
        await self.text('P')
        await self.cb('pool_exists:no')
        m = await self.text('http://127.0.0.1:8086/')
        self.assertIn("private (token 'default' on the agent)", m.replies[-1][0])
        await self.cb('confirm:yes')
        self.assertEqual(FakeBackend.last_payload['credential_ref'], 'default')

    async def test_pasted_token_is_never_stored_and_message_deleted(self):
        await self.reach_github_url_step()
        for step_text, st in [(TOKEN, 'WAIT_REPO_URL'), (f'https://x-access-token:{TOKEN}@github.com/a/b', 'WAIT_REPO_URL')]:
            m = await self.text(step_text)
            self.assertTrue(m.deleted)
            self.assertIn('revoke it', m.replies[-1][0])
            self.assertEqual(self.state()['state'], st)
        await self.text('https://github.com/acme/site')
        await self.cb('gh_vis:private')
        m = await self.text(TOKEN)  # pasted where the credential NAME is expected
        self.assertTrue(m.deleted)
        self.assertEqual(self.state()['state'], 'WAIT_CRED_REF')
        self.assertIsNone(self.state().get('credential_ref'))
        await self.cb('gh_vis:public')
        await self.text(TOKEN)  # pasted as branch
        self.assertEqual(self.state()['state'], 'WAIT_GIT_REF')
        stored = Path('data/user_state.json').read_text()
        self.assertNotIn(TOKEN, stored)

    async def test_invalid_inputs_keep_state_and_explain(self):
        await self.reach_github_url_step()
        for bad in ['https://gitlab.com/a/b', 'not a url', 'http://github.com/a/b']:
            m = await self.text(bad)
            self.assertEqual(self.state()['state'], 'WAIT_REPO_URL')
            self.assertIn('Try again', m.replies[-1][0])
        await self.text('https://github.com/a/b')
        await self.cb('gh_vis:public')
        m = await self.text('--upload-pack=x')
        self.assertEqual(self.state()['state'], 'WAIT_GIT_REF')
        self.assertIn('Try again', m.replies[-1][0])
        await self.cb('gh_ref:default')
        m = await self.text('../secrets')
        self.assertEqual(self.state()['state'], 'WAIT_SUBDIR')

    async def finish_to_confirm(self, port='8087'):
        await self.text('S')
        await self.cb('site_exists:no')
        await self.text(port)
        await self.text('P')
        await self.cb('pool_exists:no')
        return await self.text(f'http://127.0.0.1:{port}/')

    async def test_dotnet_flow_asks_for_project_and_sends_preset(self):
        await self.reach_github_url_step('dotnet')
        self.assertEqual(self.state()['state'], 'WAIT_REPO_URL')
        await self.text('https://github.com/acme/api')
        await self.cb('gh_vis:public')
        await self.cb('gh_ref:default')
        q = await self.cb('gh_subdir:root')
        self.assertEqual(self.state()['state'], 'WAIT_BUILD_TARGET')
        self.assertIn('.csproj', q.edits[-1][0])
        m = await self.text('src/Web/Web.csproj')
        self.assertEqual((self.state()['state'], self.state()['build_target']), ('WAIT_SITE', 'src/Web/Web.csproj'))
        m = await self.finish_to_confirm()
        self.assertIn('Build: .NET publish (src/Web/Web.csproj)', m.replies[-1][0])
        await self.cb('confirm:yes')
        p = FakeBackend.last_payload
        self.assertEqual((p['build_preset'], p['build_target'], p['source_type']), ('dotnet_publish', 'src/Web/Web.csproj', 'github'))

    async def test_dotnet_target_must_be_a_project_file(self):
        await self.reach_github_url_step('dotnet')
        await self.text('https://github.com/acme/api')
        await self.cb('gh_vis:public')
        await self.cb('gh_ref:default')
        await self.cb('gh_subdir:root')
        m = await self.text('src/Web')
        self.assertEqual(self.state()['state'], 'WAIT_BUILD_TARGET')
        self.assertIn('.csproj', m.replies[-1][0])
        await self.cb('gh_target:auto')
        self.assertEqual((self.state()['state'], self.state()['build_target']), ('WAIT_SITE', None))

    async def test_static_npm_build_flow(self):
        await self.reach_github_url_step()
        await self.text('https://github.com/acme/spa')
        await self.cb('gh_vis:public')
        await self.cb('gh_ref:default')
        q = await self.cb('gh_subdir:root')
        self.assertEqual(callback_data(q.edits[-1][1]), ['gh_build:none', 'gh_build:npm', 'cancel'])
        await self.cb('gh_build:npm')
        self.assertEqual(self.state()['state'], 'WAIT_BUILD_TARGET')
        await self.text('dist')
        m = await self.finish_to_confirm('8088')
        self.assertIn('Build: npm build (dist)', m.replies[-1][0])
        await self.cb('confirm:yes')
        p = FakeBackend.last_payload
        self.assertEqual((p['build_preset'], p['build_target']), ('npm_build', 'dist'))

    async def test_unsupported_project_type_cannot_pick_github(self):
        await self.cb('agent:agent_T')
        self.bm.states.update(UID, project_type='php')
        q = await self.cb('source:github')
        self.assertIn('Static and .NET', q.edits[-1][0])
        self.assertNotEqual(self.state()['state'], 'WAIT_REPO_URL')
        self.assertIn('source:zip', callback_data(q.edits[-1][1]))

    async def test_menu_words_inside_names_and_paths_are_not_hijacked(self):
        c = self.bm._clean_button
        for text in ['MyDeployTest', r'C:\logs\site', 'help-desk', 'Status page', 'Reports2024']:
            self.assertEqual(c(text), text)
        for text in ['Deploy', 'deploy', '🚀 Deploy', '📦 Deploy ', 'My Agents', '📋 Logs', 'Help']:
            self.assertIn(c(text), ('Deploy', 'Agents', 'Logs', 'Help'))

    async def test_menu_button_still_works_inside_github_flow(self):
        await self.reach_github_url_step()
        m = await self.text('Deploy')  # exact menu label: restart the guided deployment
        self.assertEqual(self.state()['state'], 'SELECT_AGENT')

    async def test_cancel_clears_state(self):
        await self.reach_github_url_step()
        await self.cb('cancel')
        self.assertEqual(self.state()['state'], 'IDLE')

    # ---- regression: the existing ZIP and folder flows -------------------------------------------------
    def zip_state(self, **extra):
        s = dict(state='CONFIRM', agent_id='agent_T', project_type='static', source_type='uploaded_package',
                 uploaded_zip_local_path='data/bot_uploads/x.zip', site_name='S', app_pool_name='P',
                 health_check_url='http://127.0.0.1:8085/', create_site_if_missing=True, site_port=8085)
        s.update(extra)
        self.bm.states.set(UID, s)

    async def test_zip_deploy_payload_has_no_github_keys(self):
        self.zip_state()
        FakeBackend.result = {'success': True, 'site_name': 'S', 'app_pool_name': 'P', 'release_id': 'r9'}
        q = await self.cb('confirm:yes')
        be = FakeBackend.instances[-1]
        self.assertEqual(be.uploaded, ['data/bot_uploads/x.zip'])
        p = be.jobs[0]
        self.assertEqual((p['source_type'], p['package_id']), ('uploaded_package', 'pkg_fake'))
        for k in ('repo_url', 'git_ref', 'credential_ref', 'subdir', 'build_preset', 'build_target'):
            self.assertNotIn(k, p)  # old agents reject unknown keys
        self.assertEqual(FakeBackend.last_timeout, 300)
        self.assertNotIn('Source:', q.message.replies[-1][0])

    async def test_folder_deploy_still_rejected_in_backend_mode_with_same_message(self):
        self.zip_state(source_type='folder_path', folder_path='C:\\site')
        q = await self.cb('confirm:yes')
        self.assertEqual(q.edits[-1][0], 'Deployment call failed: This deployment mode supports Telegram ZIP upload only. '
                                        'Folder-path deployment is not enabled for this agent.')

    async def test_zip_summary_unchanged(self):
        self.zip_state()
        from bot.services.message_renderer import summary
        txt = summary(self.state())
        self.assertNotIn('Repository', txt)
        self.assertIn('Source: uploaded_package', txt)


if __name__ == '__main__':
    unittest.main()
