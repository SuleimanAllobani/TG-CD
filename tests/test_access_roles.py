"""Roles, teams and per-user limits: policy rules, registry changes and the Telegram commands."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot.services import access_policy as ap
from bot.services.access_policy import AccessError

ROOT = Path(__file__).resolve().parents[1]
OWNER, DEV, VIEWER, DEV2, STRANGER = 100, 200, 300, 400, 999
AGENT = 'agent_T'


class InTempDir(unittest.TestCase):
    """The bot keeps JSON state at relative paths (data/...), so run each test in its own folder."""

    def setUp(self):
        self._old = os.getcwd()
        self._td = tempfile.TemporaryDirectory()
        os.chdir(self._td.name)
        sys.path.insert(0, str(ROOT))
        from bot.services.agent_registry import AgentRegistry
        self.reg = AgentRegistry()
        self.reg.register(OWNER, AGENT, 'Main', 'backend://http://x', 'tok')

    def tearDown(self):
        os.chdir(self._old)
        self._td.cleanup()

    def item(self):
        return self.reg.get(AGENT)


class PolicyTests(unittest.TestCase):
    def test_parsers(self):
        self.assertEqual(ap.parse_user_id(' 0123 '), '123')
        for bad in ['', 'abc', '-1', '12 3', '1' * 16, '@bob']:
            with self.assertRaises(AccessError):
                ap.parse_user_id(bad)
        self.assertEqual(ap.parse_role('Deployer'), 'deployer')
        with self.assertRaises(AccessError):
            ap.parse_role('admin')
        with self.assertRaises(AccessError):
            ap.parse_role('owner', ap.TEAM_ROLES)
        for bad in ['', 'a b', 'x' * 31, '../x', 'team:x']:
            with self.assertRaises(AccessError):
                ap.parse_team_name(bad)

    def test_scope_parsing(self):
        self.assertIsNone(ap.parse_scope('repos', 'ALL'))
        self.assertEqual(ap.parse_scope('repos', 'none'), [])
        self.assertEqual(ap.parse_scope('repos', 'Acme/Site, acme/site acme/*'), ['acme/site', 'acme/*'])
        self.assertEqual(ap.parse_scope('creds', 'team-a,team_b'), ['team-a', 'team_b'])
        for bad in ['', ' ', 'justname', 'a/b/c', 'https://github.com/a/b', '../x/y', 'a/b;rm', '-a/b']:
            with self.assertRaises(AccessError, msg=bad):
                ap.parse_scope('repos', bad)
        for bad in ['bad name!', 'ghp_' + 'A1b2C3d4' * 5, '../x']:
            with self.assertRaises(AccessError, msg=bad):
                ap.parse_scope('creds', bad)

    def test_role_matrix(self):
        item = {'users': {'1': 'owner', '2': 'deployer', '3': 'viewer', '4': 'member', '5': 'weird'}}
        expect = {
            '1': dict(view=1, deploy=1, rollback=1, delete_release=1, delete_agent=1, manage=1),
            '2': dict(view=1, deploy=1, rollback=1, delete_release=0, delete_agent=0, manage=0),
            '3': dict(view=1, deploy=0, rollback=0, delete_release=0, delete_agent=0, manage=0),
            '4': dict(view=1, deploy=1, rollback=1, delete_release=0, delete_agent=0, manage=0),  # legacy "member"
            '5': dict(view=1, deploy=1, rollback=1, delete_release=0, delete_agent=0, manage=0),
            '9': dict(view=0, deploy=0, rollback=0, delete_release=0, delete_agent=0, manage=0),  # stranger
        }
        for uid, acts in expect.items():
            for act, ok in acts.items():
                self.assertEqual(ap.can(item, uid, act), bool(ok), (uid, act))

    def test_repo_matching(self):
        item = {'users': {'2': 'deployer'}, 'limits': {'2': {'repos': ['acme/site', 'corp/*'], 'creds': ['team-a']}}}
        self.assertTrue(ap.repo_permitted(item, 2, 'ACME/Site'))
        self.assertTrue(ap.repo_permitted(item, 2, 'corp/anything'))
        self.assertFalse(ap.repo_permitted(item, 2, 'acme/other'))
        self.assertFalse(ap.repo_permitted(item, 2, 'corporate/x'))
        self.assertTrue(ap.credential_permitted(item, 2, 'team-a'))
        self.assertFalse(ap.credential_permitted(item, 2, 'default'))
        self.assertTrue(ap.credential_permitted(item, 2, None))  # public repository, no credential

    def test_missing_limits_means_unrestricted_like_before(self):
        item = {'users': {'2': 'deployer'}}
        self.assertTrue(ap.repo_permitted(item, 2, 'any/thing'))
        self.assertTrue(ap.credential_permitted(item, 2, 'default'))

    def test_empty_means_nothing_and_star_means_all(self):
        item = {'users': {'2': 'deployer', '3': 'deployer'}, 'limits': {'2': {'repos': [], 'creds': []}, '3': {'repos': ['*'], 'creds': None}}}
        self.assertFalse(ap.repo_permitted(item, 2, 'a/b'))
        self.assertFalse(ap.credential_permitted(item, 2, 'x'))
        self.assertTrue(ap.repo_permitted(item, 3, 'a/b'))
        self.assertTrue(ap.credential_permitted(item, 3, 'x'))

    def test_owner_never_limited(self):
        item = {'users': {'1': 'owner'}, 'limits': {'1': {'repos': [], 'creds': []}}}
        self.assertTrue(ap.repo_permitted(item, 1, 'a/b'))
        self.assertTrue(ap.credential_permitted(item, 1, 'x'))


class RegistryTests(InTempDir):
    def test_add_user_gives_access_and_agent(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.assertEqual(self.reg.role(DEV, AGENT), 'deployer')
        self.assertEqual([a['agent_id'] for a in self.reg.list_for_user(DEV)], [AGENT])
        self.assertEqual(self.reg.current(DEV)['agent_id'], AGENT)
        self.assertEqual(self.reg.list_for_user(DEV)[0]['role'], 'deployer')

    def test_default_role_is_deployer_and_validation(self):
        self.reg.add_user(OWNER, AGENT, DEV)
        self.assertEqual(self.reg.role(DEV, AGENT), 'deployer')
        for target, role in [('abc', 'viewer'), (str(VIEWER), 'boss')]:
            with self.assertRaises(AccessError):
                self.reg.add_user(OWNER, AGENT, target, role)
        with self.assertRaises(AccessError):
            self.reg.add_user(OWNER, AGENT, DEV, 'viewer')  # already there

    def test_only_owner_can_manage(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.reg.add_user(OWNER, AGENT, VIEWER, 'viewer')
        for who in (DEV, VIEWER, STRANGER):
            for call in (lambda w=who: self.reg.add_user(w, AGENT, DEV2, 'viewer'),
                         lambda w=who: self.reg.set_role(w, AGENT, DEV, 'owner'),
                         lambda w=who: self.reg.remove_user(w, AGENT, OWNER),
                         lambda w=who: self.reg.team_create(w, AGENT, 't', 'deployer'),
                         lambda w=who: self.reg.set_scope(w, AGENT, DEV, 'repos', 'all')):
                with self.assertRaises(PermissionError):
                    call()
        self.assertEqual(self.reg.role(DEV, AGENT), 'deployer')
        self.assertIsNone(self.reg.role(DEV2, AGENT))

    def test_cannot_remove_or_demote_last_owner(self):
        with self.assertRaises(AccessError):
            self.reg.set_role(OWNER, AGENT, OWNER, 'deployer')
        with self.assertRaises(AccessError):
            self.reg.remove_user(OWNER, AGENT, OWNER)
        self.reg.add_user(OWNER, AGENT, DEV, 'owner')
        self.reg.set_role(OWNER, AGENT, OWNER, 'viewer')  # fine: DEV is also an owner
        self.assertEqual(self.reg.role(OWNER, AGENT), 'viewer')
        with self.assertRaises(PermissionError):
            self.reg.set_role(OWNER, AGENT, DEV, 'viewer')  # OWNER is no longer an owner
        with self.assertRaises(AccessError):
            self.reg.remove_user(DEV, AGENT, DEV)  # last owner now

    def test_set_role_changes_permissions(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'viewer')
        self.assertFalse(self.reg.can(DEV, AGENT, 'deploy'))
        self.reg.set_role(OWNER, AGENT, DEV, 'deployer')
        self.assertTrue(self.reg.can(DEV, AGENT, 'deploy'))
        with self.assertRaises(AccessError):
            self.reg.set_role(OWNER, AGENT, STRANGER, 'viewer')

    def test_remove_user_cleans_everything(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.reg.team_create(OWNER, AGENT, 'web', 'deployer')
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.reg.set_scope(OWNER, AGENT, DEV, 'repos', 'a/b')
        self.reg.remove_user(OWNER, AGENT, DEV)
        it = self.item()
        self.assertNotIn(str(DEV), it['users'])
        self.assertNotIn(str(DEV), it.get('limits', {}))
        self.assertEqual(it['teams']['web']['members'], [])
        self.assertIsNone(self.reg.role(DEV, AGENT))
        self.assertIsNone(self.reg.current(DEV))
        self.assertEqual(self.reg.list_for_user(DEV), [])

    def test_unknown_user_cannot_be_removed(self):
        with self.assertRaises(AccessError):
            self.reg.remove_user(OWNER, AGENT, STRANGER)

    def test_teams_are_safe_by_default(self):
        self.reg.team_create(OWNER, AGENT, 'web', 'deployer')
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.assertEqual(self.reg.role(DEV, AGENT), 'deployer')
        self.assertTrue(self.reg.can(DEV, AGENT, 'deploy'))
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/site')  # no repositories allowed yet
        self.reg.set_scope(OWNER, AGENT, 'team:web', 'repos', 'acme/site')
        self.reg.check_github(DEV, AGENT, 'acme/site')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/other')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/site', 'default')  # credential not allowed
        self.reg.set_scope(OWNER, AGENT, 'team:web', 'creds', 'team-web')
        self.reg.check_github(DEV, AGENT, 'acme/site', 'team-web')

    def test_team_added_person_gets_access_but_no_own_github_rights(self):
        self.reg.team_create(OWNER, AGENT, 'web', 'viewer')
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.assertEqual(self.item()['users'][str(DEV)], 'viewer')
        self.assertEqual(self.item()['limits'][str(DEV)], {'repos': [], 'creds': []})
        self.assertEqual(self.reg.current(DEV)['agent_id'], AGENT)
        # promoting the team later gives the role but still only the team's repositories
        self.reg.team_set_role(OWNER, AGENT, 'web', 'deployer')
        self.reg.set_scope(OWNER, AGENT, 'team:web', 'repos', 'acme/*')
        self.assertTrue(self.reg.can(DEV, AGENT, 'deploy'))
        self.reg.check_github(DEV, AGENT, 'acme/x')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'other/x')

    def test_highest_role_wins(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'viewer')
        self.reg.add_user(OWNER, AGENT, DEV2, 'deployer')
        self.reg.team_create(OWNER, AGENT, 'dep', 'deployer')
        self.reg.team_create(OWNER, AGENT, 'view', 'viewer')
        self.reg.team_add(OWNER, AGENT, 'dep', DEV)
        self.reg.team_add(OWNER, AGENT, 'view', DEV2)
        self.assertEqual(self.reg.role(DEV, AGENT), 'deployer')   # viewer + deployer team
        self.assertEqual(self.reg.role(DEV2, AGENT), 'deployer')  # deployer + viewer team
        self.assertFalse(self.reg.can(DEV2, AGENT, 'manage'))

    def test_limits_are_a_union_and_unrestricted_wins(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.reg.set_scope(OWNER, AGENT, DEV, 'repos', 'a/one')
        self.reg.team_create(OWNER, AGENT, 't1', 'deployer')
        self.reg.team_create(OWNER, AGENT, 't2', 'deployer')
        self.reg.set_scope(OWNER, AGENT, 'team:t1', 'repos', 'b/two')
        self.reg.set_scope(OWNER, AGENT, 'team:t2', 'repos', 'c/*')
        self.reg.team_add(OWNER, AGENT, 't1', DEV)
        self.reg.team_add(OWNER, AGENT, 't2', DEV)
        for ok in ('a/one', 'b/two', 'c/three'):
            self.reg.check_github(DEV, AGENT, ok)
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'a/other')
        self.reg.set_scope(OWNER, AGENT, 'team:t2', 'repos', 'all')
        self.reg.check_github(DEV, AGENT, 'anything/at-all')

    def test_viewer_entry_does_not_widen_team_limits(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'viewer')  # direct viewer entry has no limits recorded
        self.reg.team_create(OWNER, AGENT, 'web', 'deployer')
        self.reg.set_scope(OWNER, AGENT, 'team:web', 'repos', 'acme/site')
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.assertTrue(self.reg.can(DEV, AGENT, 'deploy'))
        self.reg.check_github(DEV, AGENT, 'acme/site')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'other/repo')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/site', 'default')

    def test_user_limits(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.reg.check_github(DEV, AGENT, 'any/repo', 'default')  # unrestricted until limited
        self.reg.set_scope(OWNER, AGENT, DEV, 'repos', 'acme/site,acme/api')
        self.reg.set_scope(OWNER, AGENT, DEV, 'creds', 'none')
        self.reg.check_github(DEV, AGENT, 'acme/api')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/other')
        with self.assertRaises(PermissionError):
            self.reg.check_github(DEV, AGENT, 'acme/api', 'default')
        self.reg.set_scope(OWNER, AGENT, DEV, 'repos', 'all')
        self.reg.check_github(DEV, AGENT, 'x/y')

    def test_scope_errors(self):
        with self.assertRaises(AccessError):
            self.reg.set_scope(OWNER, AGENT, OWNER, 'repos', 'a/b')       # owners are never limited
        with self.assertRaises(AccessError):
            self.reg.set_scope(OWNER, AGENT, STRANGER, 'repos', 'a/b')    # unknown user
        with self.assertRaises(AccessError):
            self.reg.set_scope(OWNER, AGENT, 'team:nope', 'repos', 'a/b')  # unknown team
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        with self.assertRaises(AccessError):
            self.reg.set_scope(OWNER, AGENT, DEV, 'repos', 'not a pattern')
        with self.assertRaises(AccessError):
            self.reg.set_scope(OWNER, AGENT, DEV, 'colors', 'all')

    def test_team_lifecycle(self):
        self.reg.team_create(OWNER, AGENT, 'web', 'deployer')
        with self.assertRaises(AccessError):
            self.reg.team_create(OWNER, AGENT, 'web', 'viewer')
        with self.assertRaises(AccessError):
            self.reg.team_create(OWNER, AGENT, 'bosses', 'owner')
        with self.assertRaises(AccessError):
            self.reg.team_add(OWNER, AGENT, 'nope', DEV)
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        with self.assertRaises(AccessError):
            self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.reg.team_remove(OWNER, AGENT, 'web', DEV)
        self.assertFalse(self.reg.can(DEV, AGENT, 'deploy'))  # left the team: back to the viewer entry
        self.assertTrue(self.reg.can(DEV, AGENT, 'view'))
        with self.assertRaises(AccessError):
            self.reg.team_remove(OWNER, AGENT, 'web', DEV)
        self.reg.team_add(OWNER, AGENT, 'web', DEV)
        self.reg.team_delete(OWNER, AGENT, 'web')
        self.assertFalse(self.reg.can(DEV, AGENT, 'deploy'))
        with self.assertRaises(AccessError):
            self.reg.team_delete(OWNER, AGENT, 'web')

    def test_legacy_records_behave_as_before(self):
        a = self.reg.agents.read()
        a[AGENT]['users'] = {str(OWNER): 'owner', str(DEV): 'member'}
        for k in ('limits', 'teams'):
            a[AGENT].pop(k, None)
        self.reg.agents.write(a)
        self.assertTrue(self.reg.can(DEV, AGENT, 'deploy'))
        self.reg.check_github(DEV, AGENT, 'any/repo', 'default')
        self.assertFalse(self.reg.can(DEV, AGENT, 'manage'))

    def test_agent_ids_cannot_be_hijacked(self):
        with self.assertRaises(PermissionError):
            self.reg.register(STRANGER, AGENT, 'Mine', 'backend://x', 'tok')
        with self.assertRaises(PermissionError):
            self.reg.create_pending(STRANGER, AGENT, 'Mine', 'tok')
        self.assertEqual(self.item()['name'], 'Main')
        self.reg.register(OWNER, AGENT, 'Renamed', 'backend://x', 'tok')  # the owner may re-register
        self.assertEqual(self.item()['name'], 'Renamed')

    def test_delete_agent_is_owner_only(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        for who in (DEV, STRANGER):
            with self.assertRaises(PermissionError):
                self.reg.delete(who, AGENT)
        self.assertIsNotNone(self.reg.get(AGENT))
        self.reg.delete(OWNER, AGENT)
        self.assertIsNone(self.reg.get(AGENT))

    def test_activate_pending_is_owner_only(self):
        self.reg.create_pending(OWNER, 'agent_P', 'Pending', 'tok')
        self.reg.add_user(OWNER, 'agent_P', DEV, 'deployer')
        with self.assertRaises(ValueError):
            self.reg.activate_pending(DEV, 'agent_P', 'http://x')
        self.reg.activate_pending(OWNER, 'agent_P', 'http://x')

    def test_describe_lists_everyone_without_secrets(self):
        self.reg.add_user(OWNER, AGENT, DEV, 'deployer')
        self.reg.team_create(OWNER, AGENT, 'web', 'viewer')
        self.reg.team_add(OWNER, AGENT, 'web', VIEWER)
        text = self.reg.describe(AGENT)
        for needle in (str(OWNER), str(DEV), str(VIEWER), 'web', 'deployer', 'viewer'):
            self.assertIn(needle, text)
        self.assertNotIn('tok', text.replace('Teams', ''))


# ---------------------------------------------------------------------------
# Telegram commands
# ---------------------------------------------------------------------------
class Msg:
    def __init__(self, text=None):
        self.text = text
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, reply_markup=None, **_):
        self.replies.append((text, reply_markup))

    async def delete(self):
        self.deleted = True

    async def reply_document(self, *a, **k):
        self.replies.append(('<document>', None))


class Upd:
    def __init__(self, uid, text=None):
        self.message = Msg(text)
        self.effective_user = mock.Mock(id=uid)


class Q:
    def __init__(self, uid, data):
        self.data = data
        self.from_user = mock.Mock(id=uid)
        self.edits = []
        self.message = Msg()

    async def answer(self, *a, **k):
        pass

    async def edit_message_text(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))


class UpdCb:
    def __init__(self, uid, data):
        self.callback_query = Q(uid, data)


class Ctx:
    def __init__(self, *args):
        self.args = list(args)
        self.bot = mock.Mock()
        self.bot.send_message = mock.AsyncMock()


class FakeBackend:
    jobs_created = []
    job_agent = AGENT

    def __init__(self, *a, **k):
        pass

    def create_deploy_job(self, payload):
        FakeBackend.jobs_created.append(payload)
        return {'job_id': 'job_x'}

    def create_rollback_job(self, payload):
        FakeBackend.jobs_created.append(('rollback', payload))
        return {'job_id': 'job_r'}

    def wait_for_job(self, job_id, timeout_seconds=300, poll_seconds=3):
        return {'status': 'success', 'result': {'success': True, 'site_name': 'S', 'app_pool_name': 'P', 'release_id': 'r1', 'message': 'ok'}}

    def get_job(self, job_id):
        return {'job_id': job_id, 'agent_id': FakeBackend.job_agent, 'status': 'success', 'site_name': 'S'}

    def get_job_logs(self, job_id):
        return {'logs': [{'message': 'log line'}]}

    def agent_history(self, agent_id, limit=10):
        return {'jobs': [{'job_id': 'job_1', 'agent_id': agent_id, 'status': 'success', 'site_name': 'S', 'app_pool_name': 'P', 'result': {}}]}


def last_text(msg_or_query):
    items = msg_or_query.replies if hasattr(msg_or_query, 'replies') else msg_or_query.edits
    return items[-1][0] if items else ''


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row] if markup else []


class BotRoleTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_cwd = os.getcwd()
        cls._td = tempfile.TemporaryDirectory()
        sys.path.insert(0, str(ROOT))
        os.chdir(cls._td.name)
        with mock.patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '123456:TESTTOKEN_FOR_UNIT_TESTS_ONLY_000'}):
            if 'bot.main' in sys.modules:
                del sys.modules['bot.main']
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
        for f in ('data/agents.json', 'data/user_access.json', 'data/user_state.json'):
            Path(f).write_text('{}', encoding='utf-8')
        bm.agents = type(bm.agents)()
        bm.states.clear(OWNER)
        for u in (OWNER, DEV, VIEWER, DEV2, STRANGER):
            bm.states.clear(u)
        bm.agents.register(OWNER, AGENT, 'Main', 'backend://http://127.0.0.1:9000', 'tok')
        bm.agents.add_user(OWNER, AGENT, DEV, 'deployer')
        bm.agents.add_user(OWNER, AGENT, VIEWER, 'viewer')
        FakeBackend.jobs_created = []
        FakeBackend.job_agent = AGENT

    async def cmd(self, fn, uid, *args):
        u = Upd(uid)
        ctx = Ctx(*args)
        await fn(u, ctx)
        return u.message, ctx

    async def cb(self, uid, data):
        u = UpdCb(uid, data)
        await self.bm.on_callback(u, Ctx())
        return u.callback_query

    async def text(self, uid, text):
        u = Upd(uid, text)
        await self.bm.on_text(u, Ctx())
        return u.message

    # ---- owner commands -------------------------------------------------
    async def test_owner_adds_user_and_person_is_notified(self):
        m, ctx = await self.cmd(self.bm.add_user, OWNER, str(DEV2), 'viewer')
        self.assertIn('Added 400', last_text(m))
        self.assertEqual(self.bm.agents.role(DEV2, AGENT), 'viewer')
        ctx.bot.send_message.assert_awaited()
        self.assertEqual(ctx.bot.send_message.await_args.kwargs['chat_id'], DEV2)

    async def test_notification_failure_does_not_break_the_command(self):
        u = Upd(OWNER)
        ctx = Ctx(str(DEV2))
        ctx.bot.send_message.side_effect = RuntimeError('user has not started the bot')
        await self.bm.add_user(u, ctx)
        self.assertEqual(self.bm.agents.role(DEV2, AGENT), 'deployer')

    async def test_usage_and_validation_messages(self):
        m, _ = await self.cmd(self.bm.add_user, OWNER)
        self.assertIn('Usage: /add_user', last_text(m))
        m, _ = await self.cmd(self.bm.add_user, OWNER, 'abc')
        self.assertIn('Telegram user ID', last_text(m))
        m, _ = await self.cmd(self.bm.add_user, OWNER, str(DEV2), 'boss')
        self.assertIn('Role must be', last_text(m))
        m, _ = await self.cmd(self.bm.set_role, OWNER, str(DEV))
        self.assertIn('Usage: /set_role', last_text(m))
        self.assertIsNone(self.bm.agents.role(DEV2, AGENT))

    async def test_non_owners_cannot_use_owner_commands(self):
        for uid in (DEV, VIEWER):
            for fn, args in [(self.bm.add_user, (str(DEV2),)), (self.bm.set_role, (str(DEV), 'owner')), (self.bm.remove_user, (str(VIEWER),)),
                             (self.bm.team_create, ('t', 'deployer')), (self.bm.allow_repos, (str(DEV), 'all')), (self.bm.allow_creds, (str(DEV), 'all'))]:
                m, _ = await self.cmd(fn, uid, *args)
                self.assertIn('Not allowed', last_text(m), (uid, fn.__name__))
        self.assertEqual(self.bm.agents.role(DEV, AGENT), 'deployer')
        self.assertIsNone(self.bm.agents.role(DEV2, AGENT))

    async def test_set_role_remove_and_last_owner_via_bot(self):
        m, _ = await self.cmd(self.bm.set_role, OWNER, str(VIEWER), 'deployer')
        self.assertEqual(self.bm.agents.role(VIEWER, AGENT), 'deployer')
        m, _ = await self.cmd(self.bm.remove_user, OWNER, str(VIEWER))
        self.assertIsNone(self.bm.agents.role(VIEWER, AGENT))
        m, _ = await self.cmd(self.bm.remove_user, OWNER, str(OWNER))
        self.assertIn('only owner', last_text(m))
        self.assertEqual(self.bm.agents.role(OWNER, AGENT), 'owner')

    async def test_team_commands_and_limits_via_bot(self):
        await self.cmd(self.bm.team_create, OWNER, 'web', 'deployer')
        m, _ = await self.cmd(self.bm.team_add, OWNER, 'web', str(DEV2))
        self.assertIn('Added 400 to team web', last_text(m))
        await self.cmd(self.bm.allow_repos, OWNER, 'team:web', 'acme/site,acme/api')
        await self.cmd(self.bm.allow_creds, OWNER, 'team:web', 'team-web')
        self.bm.agents.check_github(DEV2, AGENT, 'acme/api', 'team-web')
        with self.assertRaises(PermissionError):
            self.bm.agents.check_github(DEV2, AGENT, 'acme/zzz')
        m, _ = await self.cmd(self.bm.agent_users, OWNER)
        text = last_text(m)
        self.assertIn('web: deployer', text)
        self.assertIn('acme/site, acme/api', text)
        self.assertIn('/add_user', text)
        await self.cmd(self.bm.team_role, OWNER, 'web', 'viewer')
        self.assertFalse(self.bm.agents.can(DEV2, AGENT, 'deploy'))
        await self.cmd(self.bm.team_remove, OWNER, 'web', str(DEV2))
        await self.cmd(self.bm.team_delete, OWNER, 'web')
        self.assertNotIn('web', self.bm.agents.get(AGENT)['teams'])

    async def test_agent_users_for_non_owner_hides_owner_commands(self):
        m, _ = await self.cmd(self.bm.agent_users, DEV)
        self.assertIn(str(OWNER), last_text(m))
        self.assertNotIn('/add_user', last_text(m))
        m, _ = await self.cmd(self.bm.my_access, DEV)
        self.assertIn('200: deployer', last_text(m))
        m, _ = await self.cmd(self.bm.my_access, VIEWER)
        self.assertIn('300: viewer', last_text(m))

    # ---- viewer ---------------------------------------------------------
    async def test_viewer_cannot_deploy_roll_back_or_delete(self):
        m, _ = await self.cmd(self.bm.deploy, VIEWER)
        self.assertIn('view-only', last_text(m))
        q = await self.cb(VIEWER, 'agent:' + AGENT)
        self.assertIn('needs deployer', last_text(q))
        self.assertNotEqual(self.bm.states.get(VIEWER).get('state'), 'PROJECT_TYPE')
        m, _ = await self.cmd(self.bm.rollback, VIEWER, 'S')
        self.assertIn('Not allowed', last_text(m))
        m, _ = await self.cmd(self.bm.delete_release, VIEWER, 'S', 'rel')
        self.assertIn('Not allowed', last_text(m))
        self.assertEqual(FakeBackend.jobs_created, [])

    async def test_viewer_can_still_look(self):
        self.bm.agents.set_current(VIEWER, AGENT)
        m, _ = await self.cmd(self.bm.history, VIEWER)
        self.assertIn('Recent deployment history', last_text(m))
        m, _ = await self.cmd(self.bm.logs, VIEWER, 'job_1')
        self.assertIn('log line', last_text(m))
        m, _ = await self.cmd(self.bm.myagents, VIEWER)
        self.assertIn('viewer', last_text(m))

    async def test_viewer_zip_upload_refused(self):
        self.bm.states.set(VIEWER, {'state': 'WAIT_ZIP'})
        u = Upd(VIEWER)
        u.message.document = mock.Mock(file_name='a.zip')
        await self.bm.on_document(u, Ctx())
        self.assertIn('do not have permission', last_text(u.message))
        self.assertEqual(self.bm.states.get(VIEWER).get('state'), 'WAIT_ZIP')

    # ---- deployer and owner ----------------------------------------------
    async def test_deployer_can_rollback_but_not_delete_release(self):
        self.bm.agents.set_current(DEV, AGENT)
        m, _ = await self.cmd(self.bm.rollback, DEV, 'S')
        self.assertIn('Rollback', last_text(m))
        self.assertTrue(any(isinstance(j, tuple) and j[0] == 'rollback' for j in FakeBackend.jobs_created))
        m, _ = await self.cmd(self.bm.delete_release, DEV, 'S', 'rel')
        self.assertIn('Not allowed', last_text(m))

    async def test_agents_list_shows_delete_only_to_owner(self):
        m, _ = await self.cmd(self.bm.myagents, DEV)
        self.assertNotIn('delete_agent:' + AGENT, buttons(m.replies[-1][1]))
        m, _ = await self.cmd(self.bm.myagents, OWNER)
        self.assertIn('delete_agent:' + AGENT, buttons(m.replies[-1][1]))

    async def test_delete_agent_only_by_owner_and_nothing_changes_before_check(self):
        m, _ = await self.cmd(self.bm.delete_agent, DEV, AGENT)
        self.assertIn('needs owner', last_text(m))
        self.assertIsNotNone(self.bm.agents.get(AGENT))
        q = await self.cb(DEV, 'delete_confirm:' + AGENT)
        self.assertIn('needs owner', last_text(q))
        self.assertIsNotNone(self.bm.agents.get(AGENT))
        with mock.patch.object(self.bm, 'backend_client', mock.Mock()) as bc:
            await self.cmd(self.bm.delete_agent, DEV, AGENT)
            bc.delete_agent.assert_not_called()  # the backend row must survive too

    # ---- GitHub limits in the conversation --------------------------------
    async def reach_url_step(self, uid):
        await self.cb(uid, 'agent:' + AGENT)
        await self.cb(uid, 'ptype:static')
        await self.cb(uid, 'source:github')

    async def test_limited_deployer_is_stopped_early_on_repo_and_credential(self):
        await self.cmd(self.bm.allow_repos, OWNER, str(DEV), 'acme/site')
        await self.cmd(self.bm.allow_creds, OWNER, str(DEV), 'team-a')
        await self.reach_url_step(DEV)
        m = await self.text(DEV, 'https://github.com/acme/secret-project')
        self.assertIn('not allowed to deploy acme/secret-project', last_text(m))
        self.assertEqual(self.bm.states.get(DEV)['state'], 'WAIT_REPO_URL')
        m = await self.text(DEV, 'https://github.com/acme/site')
        self.assertEqual(self.bm.states.get(DEV)['state'], 'GH_VISIBILITY')
        await self.cb(DEV, 'gh_vis:private')
        m = await self.text(DEV, 'default')
        self.assertIn("not allowed to use the credential 'default'", last_text(m))
        self.assertEqual(self.bm.states.get(DEV)['state'], 'WAIT_CRED_REF')
        await self.text(DEV, 'team-a')
        self.assertEqual(self.bm.states.get(DEV)['state'], 'WAIT_GIT_REF')

    async def finish_flow_to_confirm(self, uid, repo='https://github.com/acme/site', cred=None):
        await self.reach_url_step(uid)
        await self.text(uid, repo)
        if cred:
            await self.cb(uid, 'gh_vis:private')
            await self.text(uid, cred)
        else:
            await self.cb(uid, 'gh_vis:public')
        await self.cb(uid, 'gh_ref:default')
        await self.cb(uid, 'gh_subdir:root')
        await self.cb(uid, 'gh_build:none')
        await self.text(uid, 'S')
        await self.cb(uid, 'site_exists:no')
        await self.text(uid, '8085')
        await self.text(uid, 'P')
        await self.cb(uid, 'pool_exists:no')
        await self.text(uid, 'http://127.0.0.1:8085/')

    async def test_allowed_deployer_completes_a_github_deploy(self):
        await self.cmd(self.bm.allow_repos, OWNER, str(DEV), 'acme/*')
        await self.finish_flow_to_confirm(DEV)
        q = await self.cb(DEV, 'confirm:yes')
        self.assertEqual(len(FakeBackend.jobs_created), 1)
        self.assertEqual(FakeBackend.jobs_created[0]['requested_by_user_id'], DEV)
        self.assertIn('Deployment completed', last_text(q.message))

    async def test_permission_withdrawn_mid_conversation_blocks_the_job(self):
        await self.cmd(self.bm.allow_repos, OWNER, str(DEV), 'acme/*')
        await self.finish_flow_to_confirm(DEV)
        await self.cmd(self.bm.allow_repos, OWNER, str(DEV), 'none')  # the owner changes the rules while the dev is answering
        q = await self.cb(DEV, 'confirm:yes')
        self.assertIn('Not allowed', last_text(q))
        self.assertEqual(FakeBackend.jobs_created, [])

    async def test_demoted_user_cannot_finish_a_started_deployment(self):
        await self.finish_flow_to_confirm(DEV)
        await self.cmd(self.bm.set_role, OWNER, str(DEV), 'viewer')
        q = await self.cb(DEV, 'confirm:yes')
        self.assertIn('Not allowed', last_text(q))
        self.assertEqual(FakeBackend.jobs_created, [])

    async def test_removed_user_cannot_finish_a_started_deployment(self):
        await self.finish_flow_to_confirm(DEV)
        await self.cmd(self.bm.remove_user, OWNER, str(DEV))
        q = await self.cb(DEV, 'confirm:yes')
        self.assertEqual(FakeBackend.jobs_created, [])
        self.assertTrue(last_text(q))

    async def test_owner_is_never_limited_and_unlimited_default_unchanged(self):
        await self.finish_flow_to_confirm(OWNER, 'https://github.com/anything/goes', cred='whatever')
        await self.cb(OWNER, 'confirm:yes')
        self.assertEqual(len(FakeBackend.jobs_created), 1)
        FakeBackend.jobs_created = []
        await self.finish_flow_to_confirm(DEV, 'https://github.com/other/repo')  # DEV has no limits set: old behaviour
        await self.cb(DEV, 'confirm:yes')
        self.assertEqual(len(FakeBackend.jobs_created), 1)

    async def test_zip_and_folder_deploys_are_not_affected_by_repo_limits(self):
        await self.cmd(self.bm.allow_repos, OWNER, str(DEV), 'none')
        await self.cb(DEV, 'agent:' + AGENT)
        q = await self.cb(DEV, 'ptype:static')
        self.assertIn('source:folder', buttons(q.edits[-1][1]))
        await self.cb(DEV, 'source:folder')
        self.assertEqual(self.bm.states.get(DEV)['state'], 'WAIT_FOLDER')

    # ---- job isolation ------------------------------------------------------
    async def test_jobs_of_other_agents_are_not_readable(self):
        FakeBackend.job_agent = 'agent_OTHER'
        self.bm.agents.set_current(DEV, AGENT)
        m, _ = await self.cmd(self.bm.logs, DEV, 'job_foreign')
        self.assertIn('does not belong to your current agent', last_text(m))
        m, _ = await self.cmd(self.bm.download_logs, DEV, 'job_foreign')
        self.assertIn('does not belong', last_text(m))
        m, _ = await self.cmd(self.bm.deployment_report, DEV, 'job_foreign')
        self.assertIn('does not belong', last_text(m))
        m, _ = await self.cmd(self.bm.analyze_job_command, DEV, 'job_foreign')
        self.assertIn('does not belong', last_text(m))

    async def test_history_of_an_agent_you_have_no_access_to_is_refused(self):
        m, _ = await self.cmd(self.bm.history, STRANGER, AGENT)
        self.assertIn('do not have access', last_text(m))
        m, _ = await self.cmd(self.bm.history, VIEWER, AGENT)
        self.assertIn('Recent deployment history', last_text(m))

    async def test_stranger_sees_nothing(self):
        m, _ = await self.cmd(self.bm.deploy, STRANGER)
        self.assertIn('No active agents', last_text(m))
        m, _ = await self.cmd(self.bm.rollback, STRANGER, 'S')
        self.assertIn('Usage', last_text(m))
        self.assertEqual(FakeBackend.jobs_created, [])


if __name__ == '__main__':
    unittest.main()
