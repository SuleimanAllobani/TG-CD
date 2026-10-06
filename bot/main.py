import tempfile
import uuid
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from common.config import load_settings
from bot.state.user_state_store import UserStateStore
from bot.services.agent_registry import AgentRegistry
from bot.services import access_policy as ap
from bot.services.agent_client import AgentClient
from bot.services.agent_setup_service import AgentSetupService
from bot.services.message_renderer import summary, deploy_result_text, source_label
from common.git_validation import (GitInputError, looks_like_secret, validate_build_target, validate_credential_ref, validate_ref,
                                   validate_repo_url, validate_subdir)
from bot.services.backend_client import BackendClient
from ai.diagnostic_analyzer import analyze_job as analyze_job_rules, format_analysis, format_report

settings = load_settings()
TOKEN = settings.get('telegram_bot_token')
states = UserStateStore()
agents = AgentRegistry()
client = AgentClient()
setup_service = AgentSetupService()
UPLOAD_DIR = Path(settings.get('bot_upload_dir', 'data/bot_uploads'))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKEND_CFG = settings.get('backend', {})
BACKEND_PUBLIC_URL = (BACKEND_CFG.get('public_url') or BACKEND_CFG.get('base_url', 'http://127.0.0.1:9000')).rstrip('/')
BACKEND_ENABLED = bool(BACKEND_CFG.get('enabled'))
backend_client = BackendClient(BACKEND_CFG.get('base_url', 'http://127.0.0.1:9000'), BACKEND_CFG.get('bot_token', 'phase25-bot-token')) if BACKEND_ENABLED else None


def main_menu():
    return ReplyKeyboardMarkup(
        [
            ['Deploy', 'Agents'],
            ['Status', 'Releases'],
            ['Logs', 'History'],
            ['Analyze', 'Report'],
            ['Setup Agent', 'Agent Users'],
            ['Help'],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder='Choose an action...'
    )


def _clean_button(text: str) -> str:
    """Normalize old and new menu labels.

    The UI now shows clean labels without icons, but this still accepts older
    persistent keyboard labels that may remain cached in a Telegram chat.
    """
    text = (text or '').strip()
    labels = ['Deploy', 'Agents', 'My Agents', 'Status', 'Releases', 'Logs',
              'History', 'Analyze', 'Report', 'Setup Agent', 'Agent Users', 'Help']
    for label in labels:
        if label.lower() == text.lower():
            return 'Agents' if label == 'My Agents' else label
    for label in labels:
        low = text.lower()
        if label.lower() in low:
            # Old keyboards prefixed the label with an icon. Accept the label only when nothing but
            # symbols/spaces surrounds it, so site names or paths that merely contain a menu word
            # (for example "MyDeployTest" or "C:\\logs\\site") are not hijacked.
            rest = low.replace(label.lower(), '', 1)
            if not any(ch.isalnum() for ch in rest):
                return 'Agents' if label == 'My Agents' else label
    return text

def command_hint(text: str) -> str:
    text = _clean_button(text)
    mapping = {
        'Deploy': '/deploy',
        'Agents': '/myagents',
        'Status': '/status',
        'Releases': '/releases',
        'Logs': '/logs',
        'History': '/history',
        'Analyze': '/analyze_job',
        'Report': '/deployment_report',
        'Setup Agent': '/setup_agent',
        'Agent Users': '/agent_users',
        'Help': '/help',
    }
    return mapping.get(text, text)


MENU_BUTTONS = {
    'Deploy', 'Agents', 'Status', 'Releases', 'Logs', 'History',
    'Analyze', 'Report', 'Setup Agent', 'Agent Users', 'Help'
}


async def route_menu_button(update, context, text: str):
    """Make persistent keyboard buttons behave like real commands from any state."""
    uid = update.effective_user.id
    text = _clean_button(text)

    if text == 'Deploy':
        states.clear(uid)
        await deploy(update, context)
        return True
    if text == 'Agents':
        states.clear(uid)
        await myagents(update, context)
        return True
    if text == 'Status':
        states.clear(uid)
        await status(update, context)
        return True
    if text == 'Releases':
        states.clear(uid)
        await releases(update, context)
        return True
    if text == 'Logs':
        states.clear(uid)
        await logs(update, context)
        return True
    if text == 'History':
        states.clear(uid)
        await history(update, context)
        return True
    if text == 'Analyze':
        states.clear(uid)
        await choose_analysis_job(update, context)
        return True
    if text == 'Report':
        states.clear(uid)
        await choose_report_job(update, context)
        return True
    if text == 'Setup Agent':
        states.set(uid, {'state': 'WAIT_SETUP_AGENT_NAME'})
        await update.message.reply_text(
            'Setup Agent\n\nEnter a friendly name for this server. Example: Main-Laptop-Agent',
            reply_markup=main_menu()
        )
        return True
    if text == 'Agent Users':
        states.clear(uid)
        await agent_users(update, context)
        return True
    if text == 'Help':
        states.clear(uid)
        await start(update, context)
        return True
    return False

# ---------------------------------------------------------------------------
# GitHub source flow (additional source next to ZIP upload and folder path)
# ---------------------------------------------------------------------------
GITHUB_TEXT_STATES = {'WAIT_REPO_URL', 'WAIT_CRED_REF', 'WAIT_GIT_REF', 'WAIT_SUBDIR', 'WAIT_BUILD_TARGET'}
GITHUB_JOB_TIMEOUT_SECONDS = 600

GITHUB_URL_PROMPT = (
    'Send the GitHub repository URL.\n'
    'Example: https://github.com/owner/repo\n\n'
    'Never send passwords or access tokens in this chat.'
)
GITHUB_PRIVATE_PROMPT = (
    'Private repositories use an access token that is stored on the agent server, never in Telegram.\n\n'
    'Send the credential NAME configured on the agent (letters/digits, for example: default).\n'
    'If none exists yet, ask the server administrator to run on the agent machine:\n'
    'python -m agent.set_git_token --alias default'
)
GITHUB_REF_PROMPT = (
    'Which branch, tag or commit should be deployed?\n'
    'Send a name (for example main, v1.2.0 or a commit hash), or tap the button to use the default branch.'
)
GITHUB_SUBDIR_PROMPT = (
    'Deploy the whole repository, or only one folder of it?\n'
    'Send a folder path such as docs or public, or tap the button for the repository root.'
)
GITHUB_BUILD_Q_PROMPT = (
    'Does this repository need a build before deployment?\n\n'
    '"No build" deploys the files exactly as they are in the repository.\n'
    '"npm build" runs npm ci (or npm install) and npm run build on the agent, then deploys the build output folder.\n'
    'Builds run code from the repository, so they only work for repositories the agent administrator has allowed.'
)
GITHUB_DOTNET_TARGET_PROMPT = (
    'Which project should be published?\n'
    'Send the path of the .csproj file inside the repository (for example src/Web/Web.csproj), '
    'or tap the button to auto-detect (works when there is one web project).\n'
    'The agent needs the .NET SDK and the ASP.NET Core Hosting Bundle installed.'
)
GITHUB_NPM_TARGET_PROMPT = (
    'Which folder contains the build output?\n'
    'Send a folder such as dist or build, or tap the button to auto-detect (dist, build, out, _site).'
)
GITHUB_SECRET_WARNING = (
    'That message looks like a password or access token, so I did not use or store it'
    ' (and removed it from the chat where possible).\n\n'
    'If it was a real token, revoke it on GitHub now and create a new one. '
    'Tokens must be configured on the agent server, not sent in Telegram.'
)


def source_menu():
    """Choose-source buttons. ZIP and folder entries are unchanged; GitHub is an additional row."""
    return kb([[('Upload ZIP here', 'source:zip')], [('Folder path on agent', 'source:folder')],
               [('GitHub repository', 'source:github')], [('Cancel', 'cancel')]])


def _is_exact_menu_button(text: str) -> bool:
    labels = {m.lower() for m in MENU_BUTTONS} | {'my agents'}
    return (text or '').strip().lower() in labels



def after_subdir_step(uid):
    """Next question after the sub-folder is chosen. Returns (text, markup) and sets the state."""
    ptype = states.get(uid).get('project_type')
    if ptype == 'dotnet':
        states.update(uid, state='WAIT_BUILD_TARGET', build_preset='dotnet_publish', build_target=None)
        return GITHUB_DOTNET_TARGET_PROMPT, kb([[('Auto-detect project', 'gh_target:auto')], [('Cancel', 'cancel')]])
    states.update(uid, state='GH_BUILD_Q', build_preset=None, build_target=None)
    return GITHUB_BUILD_Q_PROMPT, kb([[('No build', 'gh_build:none'), ('npm build', 'gh_build:npm')], [('Cancel', 'cancel')]])


def _check_github_scope(uid, repo_full_name, credential_ref=None):
    """Early, friendly version of the repository/credential limits (run_deploy checks again)."""
    aid = states.get(uid).get('agent_id')
    if aid:
        agents.check_github(uid, aid, repo_full_name, credential_ref)


async def github_text_step(update, context, st: str, raw: str):
    """Handle the free-text steps of the GitHub flow.

    Uses the RAW text on purpose: the generic menu matcher (_clean_button) does substring
    matching and would hijack repo URLs/branches that contain words like 'deploy' or 'logs'.
    """
    uid = update.effective_user.id
    if looks_like_secret(raw):
        try:
            await update.message.delete()
        except Exception:
            pass
        await update.message.reply_text(GITHUB_SECRET_WARNING)
        return
    try:
        if st == 'WAIT_REPO_URL':
            repo = validate_repo_url(raw)
            _check_github_scope(uid, repo['full_name'])
            states.update(uid, state='GH_VISIBILITY', repo_url=repo['url'])
            await update.message.reply_text(
                f"Repository: {repo['full_name']}\n\nIs this repository public or private?",
                reply_markup=kb([[('Public', 'gh_vis:public'), ('Private', 'gh_vis:private')], [('Cancel', 'cancel')]]))
        elif st == 'WAIT_CRED_REF':
            alias = validate_credential_ref(raw)
            if not alias:
                raise GitInputError('Send a credential name such as default.')
            _check_github_scope(uid, validate_repo_url(states.get(uid).get('repo_url'))['full_name'], alias)
            states.update(uid, state='WAIT_GIT_REF', credential_ref=alias)
            await update.message.reply_text(GITHUB_REF_PROMPT, reply_markup=kb([[('Default branch', 'gh_ref:default')], [('Cancel', 'cancel')]]))
        elif st == 'WAIT_GIT_REF':
            ref = validate_ref(raw)
            states.update(uid, state='WAIT_SUBDIR', git_ref=ref)
            await update.message.reply_text(GITHUB_SUBDIR_PROMPT, reply_markup=kb([[('Repository root', 'gh_subdir:root')], [('Cancel', 'cancel')]]))
        elif st == 'WAIT_SUBDIR':
            sub = validate_subdir(raw)
            states.update(uid, subdir=sub)
            text, markup = after_subdir_step(uid)
            await update.message.reply_text(text, reply_markup=markup)
        elif st == 'WAIT_BUILD_TARGET':
            preset = states.get(uid).get('build_preset')
            target = validate_build_target(raw, preset)
            states.update(uid, state='WAIT_SITE', build_target=target)
            await update.message.reply_text('Enter IIS site name:')
    except GitInputError as e:
        await update.message.reply_text(f'{e}\n\nTry again, or use Cancel.')
    except PermissionError as e:
        await update.message.reply_text(f'{e}\n\nSend something else, or use Cancel.')


def kb(rows):
    return InlineKeyboardMarkup([[InlineKeyboardButton(t, callback_data=d) for t, d in row] for row in rows])


def current_agent_or_none(user_id):
    return agents.current(user_id)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        'Deployment control panel ready.\n\n'
        'Use the menu below to deploy applications, manage agents, view history, and generate diagnostic reports.\n\n'
        'Common actions:\n'
        '/setup_agent - create an agent installer package\n'
        '/deploy - start a guided deployment\n'
        '/status - check the selected agent\n'
        '/history - view recent deployment jobs\n'
        '/analyze_job - choose a deployment to analyze\n'
        '/deployment_report - choose a deployment report\n'
        '/my_access - show your role on the selected agent\n'
        '/agent_users - who has access (owners can add people and teams)',
        reply_markup=main_menu()
    )


async def myid(update, context):
    await update.message.reply_text(f"Your Telegram user ID is:\n{update.effective_user.id}")


async def setup_agent(update, context):
    """Generate an outbound agent package.

    The user only provides a friendly name. The bot uses:
    - backend.base_url for its own backend API calls
    - backend.public_url for the generated outbound agent package

    This keeps the user experience simple and avoids asking users to type tunnel/cloud URLs.
    """
    uid = update.effective_user.id
    args = list(context.args)
    name = ' '.join(args).strip() if args else None
    if not name:
        # Guided flow: the persistent menu button can start setup without exposing command syntax.
        states.set(uid, {'state': 'WAIT_SETUP_AGENT_NAME'})
        await update.message.reply_text(
            'Setup Agent\n\nEnter a friendly name for this server. Example: Main-Laptop-Agent',
            reply_markup=main_menu()
        )
        return

    if BACKEND_ENABLED and not BACKEND_PUBLIC_URL:
        await update.message.reply_text(
            'Backend public URL is not configured. Ask the administrator to set backend.public_url in config/settings.json.',
            reply_markup=main_menu()
        )
        return

    agent_id, token = setup_service.generate_identity()
    mode = 'outbound' if BACKEND_ENABLED else 'direct'
    package_backend_url = BACKEND_PUBLIC_URL
    agents.create_pending(uid, agent_id, name, token)

    if BACKEND_ENABLED:
        try:
            # IMPORTANT: register through the local/private backend base_url used by the bot,
            # not through the public tunnel URL. The public URL is only for the agent package.
            backend_client.register_agent(agent_id, name, token, uid)
        except Exception as e:
            await update.message.reply_text(
                'Backend registration failed. Make sure the backend is running and backend.base_url is correct.\n\n'
                f'Details: {e}',
                reply_markup=main_menu()
            )
            return

    zip_path = setup_service.build_package(agent_id, token, port=8765, mode=mode, backend_url=package_backend_url)
    if BACKEND_ENABLED:
        # Outbound mode: agent does not expose an API to the bot. It connects outward to the backend.
        agents.activate_pending(uid, agent_id, f"backend://{BACKEND_CFG.get('base_url', 'http://127.0.0.1:9000').rstrip('/')}")
        text = (
            "Agent package created successfully.\n\n"
            f"Agent name: {name}\n"
            "Status: Waiting for installation\n\n"
            "Install on the IIS server:\n"
            "1. Download the attached ZIP file.\n"
            "2. Extract it to a folder such as C:\\CICD_AGENT.\n"
            "3. Right-click install_agent_service.bat and choose Run as administrator.\n"
            "4. After installation, return here and select Status.\n\n"
            "The agent will connect automatically after the service starts."
        )
    else:
        text = (
            "Agent setup package generated.\n\n"
            f"Agent ID: {agent_id}\n"
            f"Name: {name}\n"
            "Role: Owner\n"
            "Status: Pending activation\n\n"
            "Download this ZIP on the IIS/agent laptop, extract it, then right-click:\n"
            "install_agent_service.bat → Run as Administrator\n\n"
            "After installation, come back here and run:\n"
            f"/activate_agent {agent_id} http://<AGENT-LAPTOP-IP>:8765"
        )
    await update.message.reply_text(text, reply_markup=main_menu())
    await update.message.reply_document(document=open(zip_path, 'rb'), filename=zip_path.name)


async def activate_agent(update, context):
    if len(context.args) < 2:
        await update.message.reply_text('Usage: /activate_agent <agent_id> <agent_url>\nExample: /activate_agent agent_ABC123 http://192.168.1.55:8765')
        return
    uid = update.effective_user.id
    agent_id, base_url = context.args[0], context.args[1]
    try:
        item = agents.activate_pending(uid, agent_id, base_url)
        r = client.status(item)
        await update.message.reply_text(
            "Agent activated successfully.\n\n"
            f"Agent: {agent_id}\n"
            f"URL: {base_url}\n"
            f"Status: {r.get('status')}\n\n"
            "You can now run /use_agent and /deploy."
        )
    except Exception as e:
        await update.message.reply_text(
            "Agent activation failed.\n\n"
            f"Reason: {e}\n\n"
            "Check that the agent service is running, the IP address is correct, and Windows Firewall allows port 8765."
        )


def _cur_agent_for_manage(uid):
    a = agents.current(uid)
    if not a:
        raise ValueError('No current agent. Use /myagents and /use_agent first.')
    return a


async def _manage(update, context, usage, fn, ok_text, notify=None):
    """Run an owner-only access command with uniform error handling."""
    uid = update.effective_user.id
    args = list(context.args or [])
    try:
        a = _cur_agent_for_manage(uid)
        agents.require(uid, a['agent_id'], 'manage')
        if not args:
            await update.message.reply_text(usage)
            return
        fn(uid, a['agent_id'], *args)
        await update.message.reply_text(ok_text(a, args))
        if notify:
            await notify(context, a, args)
    except TypeError:
        await update.message.reply_text(usage)
    except PermissionError as e:
        await update.message.reply_text('Not allowed: ' + str(e))
    except ValueError as e:  # AccessError is a ValueError
        await update.message.reply_text(str(e))


async def add_user(update, context):
    async def notify(ctx, a, args):
        try:
            role = (args[1] if len(args) > 1 else 'deployer').lower()
            await ctx.bot.send_message(chat_id=int(args[0]), text=f"You were given access to agent {a['agent_id']} ({a.get('name')}) as {role}. Open Agents to select it.")
        except Exception:
            pass  # the person may not have started the bot yet; the access is saved anyway
    await _manage(update, context, 'Usage: /add_user <telegram_id> [viewer|deployer|owner]\nThe person can get their ID with /myid. Default role: deployer.\n' + ap.ROLE_HELP,
                  lambda uid, aid, target, role='deployer': agents.add_user(uid, aid, target, role),
                  lambda a, args: f"Added {args[0]} to agent {a['agent_id']} as {(args[1] if len(args) > 1 else 'deployer').lower()}.\nUse /allow_repos and /allow_creds to limit which GitHub repositories and credential names they may use.", notify)


async def set_role(update, context):
    await _manage(update, context, 'Usage: /set_role <telegram_id> <viewer|deployer|owner>',
                  lambda uid, aid, target, role: agents.set_role(uid, aid, target, role),
                  lambda a, args: f"{args[0]} is now {args[1].lower()} on agent {a['agent_id']}.")


async def remove_user(update, context):
    await _manage(update, context, 'Usage: /remove_user <telegram_id>',
                  lambda uid, aid, target: agents.remove_user(uid, aid, target),
                  lambda a, args: f"Removed {args[0]} from agent {a['agent_id']} (including their team memberships).")


async def team_create(update, context):
    await _manage(update, context, 'Usage: /team_create <name> <viewer|deployer>\nA new team cannot deploy any GitHub repository until you allow repositories with /allow_repos team:<name> ...',
                  lambda uid, aid, name, role: agents.team_create(uid, aid, name, role),
                  lambda a, args: f"Team {args[0]} created as {args[1].lower()}. Add people with /team_add {args[0]} <telegram_id>. It has no GitHub repositories yet: /allow_repos team:{args[0]} owner/repo")


async def team_delete(update, context):
    await _manage(update, context, 'Usage: /team_delete <name>',
                  lambda uid, aid, name: agents.team_delete(uid, aid, name),
                  lambda a, args: f"Team {args[0]} deleted. Its members keep any role they have of their own (people added through the team stay as viewers).")


async def team_add(update, context):
    await _manage(update, context, 'Usage: /team_add <team> <telegram_id>',
                  lambda uid, aid, team, target: agents.team_add(uid, aid, team, target),
                  lambda a, args: f"Added {args[1]} to team {args[0]}.")


async def team_remove(update, context):
    await _manage(update, context, 'Usage: /team_remove <team> <telegram_id>',
                  lambda uid, aid, team, target: agents.team_remove(uid, aid, team, target),
                  lambda a, args: f"Removed {args[1]} from team {args[0]}.")


async def team_role(update, context):
    await _manage(update, context, 'Usage: /team_role <team> <viewer|deployer>',
                  lambda uid, aid, team, role: agents.team_set_role(uid, aid, team, role),
                  lambda a, args: f"Team {args[0]} is now {args[1].lower()}.")


async def allow_repos(update, context):
    await _manage(update, context, 'Usage: /allow_repos <telegram_id|team:NAME> <owner/repo,owner/*|all|none>\nLimits which GitHub repositories that person or team may deploy on this agent.',
                  lambda uid, aid, who, *rest: agents.set_scope(uid, aid, who, 'repos', ' '.join(rest)),
                  lambda a, args: f"GitHub repositories for {args[0]}: {' '.join(args[1:])}")


async def allow_creds(update, context):
    await _manage(update, context, 'Usage: /allow_creds <telegram_id|team:NAME> <name1,name2|all|none>\nLimits which private-repository credential names that person or team may use.',
                  lambda uid, aid, who, *rest: agents.set_scope(uid, aid, who, 'creds', ' '.join(rest)),
                  lambda a, args: f"Credential names for {args[0]}: {' '.join(args[1:])}")


async def my_access(update, context):
    uid = update.effective_user.id
    a = agents.current(uid)
    if not a:
        await update.message.reply_text('No current agent. Use /myagents and /use_agent first.')
        return
    await update.message.reply_text(f"Agent {a['agent_id']}\nYou: {agents.describe_for(uid, a['agent_id'])}\n\n{ap.ROLE_HELP}")


async def agent_users(update, context):
    a = agents.current(update.effective_user.id)
    if not a:
        await update.message.reply_text('No current agent. Use /myagents and /use_agent first.')
        return
    try:
        agents.require(update.effective_user.id, a['agent_id'], 'view')
        text = agents.describe(a['agent_id'])
        if agents.can(update.effective_user.id, a['agent_id'], 'manage'):
            text += ('\n\n' + ap.ROLE_HELP + '\nOwner commands: /add_user /set_role /remove_user /team_create /team_add /team_remove /team_role /team_delete /allow_repos /allow_creds')
        await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text('Could not show users: ' + str(e))


async def register_agent(update, context):
    if len(context.args) < 4:
        await update.message.reply_text('Usage: /register_agent <id> <name> <url> <token>')
        return
    try:
        agents.register(update.effective_user.id, context.args[0], context.args[1], context.args[2], context.args[3])
    except PermissionError as e:
        await update.message.reply_text('Not allowed: ' + str(e))
        return
    await update.message.reply_text(f'Agent registered and selected: {context.args[0]}')


async def myagents(update, context):
    lst = agents.list_for_user(update.effective_user.id)
    if not lst:
        await update.message.reply_text('No agents registered. Use Setup Agent or /setup_agent to create an installer package.', reply_markup=main_menu())
        return
    cur = agents.current(update.effective_user.id)
    lines = [' Your agents:\n']
    rows = []
    for a in lst:
        mark = '* ' if cur and cur.get('agent_id') == a['agent_id'] else ''
        aid = a['agent_id']
        lines.append(f"{mark}{aid} - {a.get('name')} - {a.get('status','active')} - {a.get('role')} - {a.get('base_url') or 'not activated'}")
        row = [(f"Use {aid}", 'use_agent:' + aid)]
        if ap.has_role(a.get('role'), 'delete_agent'):
            row.append((f"Delete {aid}", 'delete_agent:' + aid))
        rows.append(row)
    await update.message.reply_text('\n'.join(lines), reply_markup=kb(rows + [[('Close', 'cancel')]]))

async def use_agent(update, context):
    if not context.args:
        await update.message.reply_text('Usage: /use_agent <agent_id>')
        return
    try:
        agents.set_current(update.effective_user.id, context.args[0])
        await update.message.reply_text(f'Selected agent: {context.args[0]}', reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Could not select agent: ' + str(e))


async def status(update, context):
    a = current_agent_or_none(update.effective_user.id)
    if not a:
        await update.message.reply_text('Register/select an agent first.')
        return
    try:
        if BACKEND_ENABLED and str(a.get('base_url','')).startswith('backend://'):
            bc = BackendClient(str(a.get('base_url')).replace('backend://', ''), BACKEND_CFG.get('bot_token', 'phase25-bot-token'))
            r = bc.agent(a['agent_id'])
            await update.message.reply_text(f"Agent status\nAgent: {a['agent_id']}\nStatus: {r.get('status')}\nLast seen: {r.get('last_seen')}")
        else:
            r = client.status(a)
            await update.message.reply_text(f"Agent is online\nAgent: {a['agent_id']}\nStatus: {r.get('status')}\nStorage: {r.get('storage_root')}")
    except Exception as e:
        await update.message.reply_text('Status failed: ' + str(e))


async def deploy(update, context):
    uid = update.effective_user.id
    active = [a for a in agents.list_for_user(uid) if a.get('status') == 'active']
    lst = [a for a in active if ap.has_role(a.get('role'), 'deploy')]
    if active and not lst:
        await update.message.reply_text('You have view-only access (viewer) on your agents, so you cannot deploy. Ask an agent owner for the deployer role.')
        return
    if not lst:
        await update.message.reply_text('No active agents. Use Setup Agent to create an installer package, then install it on the IIS server.')
        return
    states.set(uid, {'state': 'SELECT_AGENT'})
    await update.message.reply_text('Select target agent:', reply_markup=kb([[(a['name'], 'agent:' + a['agent_id'])] for a in lst] + [[('Cancel', 'cancel')]]))


async def on_callback(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    if data == 'cancel':
        states.clear(uid)
        await q.edit_message_text('Cancelled.')
        return
    if data.startswith('use_agent:'):
        aid = data.split(':', 1)[1]
        try:
            agents.set_current(uid, aid)
            states.clear(uid)
            await q.edit_message_text(f'Selected agent: {aid}')
        except Exception as e:
            await q.edit_message_text('Could not select agent: ' + str(e))
        return
    if data.startswith('delete_agent:'):
        aid = data.split(':', 1)[1]
        await q.edit_message_text(f'Delete old agent {aid}?', reply_markup=kb([[('Yes, delete', 'delete_confirm:' + aid), ('Cancel', 'cancel')]]))
        return
    if data.startswith('delete_confirm:'):
        aid = data.split(':', 1)[1]
        try:
            await delete_agent_by_id(q, uid, aid)
        except Exception as e:
            await q.edit_message_text('Delete failed: ' + str(e))
        return
    if data.startswith('analyze_job:'):
        job_id = data.split(':', 1)[1]
        try:
            a = agents.current(uid)
            job, logs = await _fetch_job_and_logs(job_id, a)
            analysis = analyze_job_rules(job, logs)
            await q.edit_message_text(format_analysis(analysis, job))
        except Exception as e:
            await q.edit_message_text('Analysis failed: ' + str(e))
        return
    if data.startswith('report_job:'):
        job_id = data.split(':', 1)[1]
        try:
            a = agents.current(uid)
            job, logs = await _fetch_job_and_logs(job_id, a)
            await q.edit_message_text(format_report(job, logs))
        except Exception as e:
            await q.edit_message_text('Report failed: ' + str(e))
        return
    if data.startswith('logs_job:'):
        job_id = data.split(':', 1)[1]
        try:
            a = agents.current(uid)
            job, logs = await _fetch_job_and_logs(job_id, a)
            if not logs:
                await q.edit_message_text('No logs found for this deployment.')
                return
            text = '\n'.join([str(x.get('message') or x) for x in logs])[-3500:]
            await q.edit_message_text(text or 'No logs found for this deployment.')
        except Exception as e:
            await q.edit_message_text('Logs failed: ' + str(e))
        return
    if data.startswith('agent:'):
        aid = data.split(':', 1)[1]
        try:
            agents.require(uid, aid, 'deploy')
        except PermissionError as e:
            await q.edit_message_text(str(e))
            return
        agents.set_current(uid, aid)
        states.update(uid, state='PROJECT_TYPE', agent_id=aid)
        await q.edit_message_text('Select project type:', reply_markup=kb([[('Static', 'ptype:static'), ('.NET', 'ptype:dotnet')], [('Cancel', 'cancel')]]))
        return
    if data.startswith('ptype:'):
        states.update(uid, state='SOURCE_TYPE', project_type=data.split(':')[1])
        await q.edit_message_text('Choose deployment source:', reply_markup=source_menu())
        return
    if data == 'source:github':
        if states.get(uid).get('project_type') not in ('static', 'dotnet'):
            await q.edit_message_text('GitHub deployment supports Static and .NET projects.\n\nChoose another source:', reply_markup=source_menu())
            return
        states.update(uid, state='WAIT_REPO_URL', source_type='github', repo_url=None, git_ref=None, credential_ref=None, subdir=None,
                      build_preset=None, build_target=None)
        await q.edit_message_text(GITHUB_URL_PROMPT)
        return
    if data.startswith('gh_vis:'):
        if data.endswith('private'):
            states.update(uid, state='WAIT_CRED_REF')
            await q.edit_message_text(GITHUB_PRIVATE_PROMPT)
        else:
            states.update(uid, state='WAIT_GIT_REF', credential_ref=None)
            await q.edit_message_text(GITHUB_REF_PROMPT, reply_markup=kb([[('Default branch', 'gh_ref:default')], [('Cancel', 'cancel')]]))
        return
    if data == 'gh_ref:default':
        states.update(uid, state='WAIT_SUBDIR', git_ref=None)
        await q.edit_message_text(GITHUB_SUBDIR_PROMPT, reply_markup=kb([[('Repository root', 'gh_subdir:root')], [('Cancel', 'cancel')]]))
        return
    if data == 'gh_subdir:root':
        states.update(uid, subdir=None)
        text, markup = after_subdir_step(uid)
        await q.edit_message_text(text, reply_markup=markup)
        return
    if data.startswith('gh_build:'):
        if data.endswith('npm'):
            states.update(uid, state='WAIT_BUILD_TARGET', build_preset='npm_build', build_target=None)
            await q.edit_message_text(GITHUB_NPM_TARGET_PROMPT, reply_markup=kb([[('Auto-detect folder', 'gh_target:auto')], [('Cancel', 'cancel')]]))
        else:
            states.update(uid, state='WAIT_SITE', build_preset=None, build_target=None)
            await q.edit_message_text('Enter IIS site name:')
        return
    if data == 'gh_target:auto':
        states.update(uid, state='WAIT_SITE', build_target=None)
        await q.edit_message_text('Enter IIS site name:')
        return
    if data == 'source:zip':
        states.update(uid, state='WAIT_ZIP', source_type='uploaded_package')
        await q.edit_message_text('Send the deployment ZIP file now.')
        return
    if data == 'source:folder':
        states.update(uid, state='WAIT_FOLDER', source_type='folder_path')
        await q.edit_message_text('Enter folder path accessible by the agent machine:')
        return
    if data.startswith('site_exists:'):
        yes = data.endswith('yes')
        states.update(uid, site_exists_expected=yes, create_site_if_missing=not yes)
        if yes:
            states.update(uid, state='WAIT_POOL')
            await q.edit_message_text('Enter application pool name:')
        else:
            states.update(uid, state='WAIT_PORT')
            await q.edit_message_text('Enter port for new IIS site, for example 8085:')
        return
    if data.startswith('pool_exists:'):
        yes = data.endswith('yes')
        states.update(uid, state='WAIT_HEALTH', app_pool_exists_expected=yes, create_app_pool_if_missing=not yes, app_pool_runtime='', app_pool_pipeline_mode='Integrated')
        await q.edit_message_text('Enter health check URL, for example http://127.0.0.1:8085/index.html:')
        return
    if data == 'confirm:yes':
        await q.edit_message_text('Deploying...')
        await run_deploy(q, uid)
        return
    if data == 'confirm:no':
        states.clear(uid)
        await q.edit_message_text('Deployment cancelled.')
        return


async def on_document(update, context):
    uid = update.effective_user.id
    s = states.get(uid)
    if s.get('state') != 'WAIT_ZIP':
        return
    cur = agents.current(uid)
    if not cur or not agents.can(uid, cur['agent_id'], 'deploy'):
        await update.message.reply_text('You do not have permission to deploy on this agent.')
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith('.zip'):
        await update.message.reply_text('Please send a .zip file.')
        return
    folder = UPLOAD_DIR / str(uid)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / doc.file_name
    f = await context.bot.get_file(doc.file_id)
    await f.download_to_drive(path)
    states.update(uid, state='WAIT_SITE', uploaded_zip_local_path=str(path))
    await update.message.reply_text('ZIP received. Enter IIS site name:')


async def on_text(update, context):
    uid = update.effective_user.id
    text = update.message.text.strip()
    raw_text = text
    st_raw = states.get(uid).get('state')
    if st_raw in GITHUB_TEXT_STATES and not _is_exact_menu_button(raw_text):
        await github_text_step(update, context, st_raw, raw_text)
        return
    text = _clean_button(text)
    s = states.get(uid)
    st = s.get('state')
    if text in MENU_BUTTONS:
        if await route_menu_button(update, context, text):
            return
    if not st or st == 'IDLE':
        # Persistent bottom menu buttons behave like commands, so users do not need to remember slash commands.
        if text == 'Deploy':
            await deploy(update, context)
            return
        if text == 'Agents':
            await myagents(update, context)
            return
        if text == 'Status':
            await status(update, context)
            return
        if text == 'Logs':
            await logs(update, context)
            return
        if text == 'History':
            await history(update, context)
            return
        if text == 'Analyze':
            await choose_analysis_job(update, context)
            return
        if text == 'Report':
            await choose_report_job(update, context)
            return
        if text == 'Setup Agent':
            states.set(uid, {'state': 'WAIT_SETUP_AGENT_NAME'})
            await update.message.reply_text('Setup Agent\n\nEnter a friendly name for this server. Example: Main-Laptop-Agent', reply_markup=main_menu())
            return
        if text == 'Agent Users':
            await agent_users(update, context)
            return
        if text == 'Help':
            await start(update, context)
            return
        if text == 'Releases':
            await releases(update, context)
            return
        mapped = command_hint(text)
        if mapped.startswith('/'):
            await update.message.reply_text(f'Use command: {mapped}', reply_markup=main_menu())
        else:
            await update.message.reply_text(mapped, reply_markup=main_menu())
        return
    if st == 'WAIT_SETUP_AGENT_NAME':
        name = text.strip()
        if not name or name.startswith('/'):
            await update.message.reply_text('Please enter a friendly server name, for example: Main-Laptop-Agent', reply_markup=main_menu())
            return
        states.clear(uid)
        context.args = name.split()
        await setup_agent(update, context)
        return
    if st == 'WAIT_FOLDER':
        states.update(uid, state='WAIT_SITE', folder_path=text)
        await update.message.reply_text('Enter IIS site name:')
        return
    if st == 'WAIT_SITE':
        states.update(uid, state='SITE_EXISTS', site_name=text)
        await update.message.reply_text('Does this IIS site already exist?', reply_markup=kb([[('Yes', 'site_exists:yes'), ('No, create it', 'site_exists:no')], [('Cancel', 'cancel')]]))
        return
    if st == 'WAIT_PORT':
        try:
            port = int(text)
            assert 1 <= port <= 65535
        except Exception:
            await update.message.reply_text('Enter a valid port number.')
            return
        states.update(uid, state='WAIT_POOL', site_port=port, site_host=None, site_protocol='http')
        await update.message.reply_text('Enter application pool name:')
        return
    if st == 'WAIT_POOL':
        states.update(uid, state='POOL_EXISTS', app_pool_name=text)
        await update.message.reply_text('Does this app pool already exist?', reply_markup=kb([[('Yes', 'pool_exists:yes'), ('No, create it', 'pool_exists:no')], [('Cancel', 'cancel')]]))
        return
    if st == 'WAIT_HEALTH':
        if not (text.startswith('http://') or text.startswith('https://')):
            await update.message.reply_text('Health URL must start with http:// or https://')
            return
        states.update(uid, state='CONFIRM', health_check_url=text)
        await update.message.reply_text(summary(states.get(uid)), reply_markup=kb([[('Deploy', 'confirm:yes'), ('Cancel', 'confirm:no')]]))
        return


async def run_deploy(q, uid):
    s = states.get(uid)
    a = agents.current(uid)
    try:
        # Authoritative permission check (the earlier steps only give friendly early feedback).
        if not a:
            raise PermissionError('You do not have access to an agent. Select one first.')
        agents.require(uid, a['agent_id'], 'deploy')
        if s.get('source_type') == 'github':
            agents.check_github(uid, a['agent_id'], validate_repo_url(s.get('repo_url'))['full_name'], s.get('credential_ref'))
        payload = {
            'request_id': 'req_' + uuid.uuid4().hex[:10],
            'requested_by_user_id': uid,
            'agent_id': a['agent_id'],
            'project_type': s['project_type'],
            'source_type': s['source_type'],
            'package_id': None,
            'folder_path': s.get('folder_path'),
            'site_name': s['site_name'],
            'site_exists_expected': s.get('site_exists_expected', False),
            'create_site_if_missing': s.get('create_site_if_missing', True),
            'site_port': s.get('site_port', 80),
            'site_host': s.get('site_host'),
            'site_protocol': 'http',
            'app_pool_name': s['app_pool_name'],
            'app_pool_exists_expected': s.get('app_pool_exists_expected', False),
            'create_app_pool_if_missing': s.get('create_app_pool_if_missing', True),
            'app_pool_runtime': s.get('app_pool_runtime', ''),
            'app_pool_pipeline_mode': s.get('app_pool_pipeline_mode', 'Integrated'),
            'health_check_url': s['health_check_url'],
            'notes': 'created by Telegram bot',
        }
        if s['source_type'] == 'github':
            # GitHub-only keys. ZIP/folder payloads stay exactly as before (older agents reject unknown keys).
            # Only a credential NAME is sent; the token itself never leaves the agent machine.
            payload.update({'repo_url': s.get('repo_url'), 'git_ref': s.get('git_ref'),
                            'credential_ref': s.get('credential_ref'), 'subdir': s.get('subdir'),
                            'build_preset': s.get('build_preset'), 'build_target': s.get('build_target')})
        if BACKEND_ENABLED and str(a.get('base_url','')).startswith('backend://'):
            if s['source_type'] not in ('uploaded_package', 'github'):
                raise ValueError('This deployment mode supports Telegram ZIP upload only. Folder-path deployment is not enabled for this agent.')
            bc = BackendClient(str(a.get('base_url')).replace('backend://', ''), BACKEND_CFG.get('bot_token', 'phase25-bot-token'))
            if s['source_type'] == 'uploaded_package':
                up = bc.upload_package(uid, s['uploaded_zip_local_path'])
                payload['package_id'] = up['package_id']
                payload['source_type'] = 'uploaded_package'
            job = bc.create_deploy_job(payload)
            await q.edit_message_text(
                f"Deployment job created.\n\nJob: {job['job_id']}\nAgent: {a['agent_id']}\nStatus: pending\n\nWaiting for outbound agent to pick it up..."
            )
            final = bc.wait_for_job(job['job_id'], timeout_seconds=GITHUB_JOB_TIMEOUT_SECONDS if s['source_type'] == 'github' else 300, poll_seconds=3)
            states.clear(uid)
            result = final.get('result') or {}
            if final.get('status') == 'success':
                await q.message.reply_text('Deployment completed successfully.\n\n' + deploy_result_text(result), reply_markup=main_menu())
            elif final.get('status') == 'failed':
                await q.message.reply_text('Deployment failed.\n\n' + deploy_result_text(result), reply_markup=main_menu())
            else:
                await q.message.reply_text(f"Deployment job did not finish in time.\nJob: {job['job_id']}\nStatus: {final.get('status')}", reply_markup=main_menu())
            return

        package_id = None
        if s['source_type'] == 'uploaded_package':
            up = client.upload_package(a, s['uploaded_zip_local_path'])
            package_id = up['package_id']
        payload['package_id'] = package_id
        r = client.deploy(a, payload)
        states.clear(uid)
        await q.edit_message_text(deploy_result_text(r))
    except PermissionError as e:
        states.clear(uid)
        await q.edit_message_text('Not allowed: ' + str(e))
    except Exception as e:
        await q.edit_message_text('Deployment call failed: ' + str(e))


async def siteinfo(update, context):
    a = agents.current(update.effective_user.id)
    if not a or not context.args:
        await update.message.reply_text('Usage: /siteinfo <site_name>')
        return
    try:
        r = client.site_info(a, context.args[0])
        await update.message.reply_text(
            f"Site: {r.get('site_name')}\nExists: {r.get('exists')}\nPath: {r.get('physical_path')}\nApp pool: {r.get('app_pool')}\nState: {r.get('state')}"
        )
    except Exception as e:
        await update.message.reply_text('Site info failed: ' + str(e))


async def releases(update, context):
    """Show releases/job history.

    In outbound backend mode the bot cannot query the agent HTTP API directly, so this
    view uses backend job history instead of the old /api/releases direct-agent endpoint.
    """
    a = agents.current(update.effective_user.id)
    if not a:
        await update.message.reply_text('No current agent. Open Agents and select an agent first.', reply_markup=main_menu())
        return
    try:
        if BACKEND_ENABLED and str(a.get('base_url', '')).startswith('backend://'):
            site_filter = context.args[0] if context.args else None
            bc = _backend_client_for_agent(a)
            jobs = bc.agent_history(a['agent_id'], limit=20).get('jobs', [])
            if site_filter:
                jobs = [j for j in jobs if str(j.get('site_name') or '').lower() == site_filter.lower()]
            if not jobs:
                msg = 'No releases found.' if not site_filter else f'No releases found for site {site_filter}.'
                await update.message.reply_text(msg, reply_markup=main_menu())
                return
            lines = ['Recent releases']
            if site_filter:
                lines[0] += f' for {site_filter}'
            for j in jobs[:10]:
                result = j.get('result') or {}
                status = str(j.get('status') or 'unknown').upper()
                site = j.get('site_name') or '-'
                release_id = result.get('release_id') or '-'
                job_id = j.get('job_id') or '-'
                message = j.get('result_summary') or result.get('message') or result.get('error') or ''
                if len(message) > 100:
                    message = message[:97] + '...'
                src = source_label(result.get('source'))
                src_line = f'Source: {src}\n' if src else ''
                lines.append(f'\n{status} | {site}\nRelease: {release_id}\n{src_line}Job: {job_id}\n{message}')
            await update.message.reply_text('\n'.join(lines)[:3900], reply_markup=main_menu())
            return

        if not context.args:
            await update.message.reply_text('Usage: /releases <site_name>', reply_markup=main_menu())
            return
        items = client.releases(a, context.args[0]).get('releases', [])
        if not items:
            await update.message.reply_text('No releases found.', reply_markup=main_menu())
            return
        await update.message.reply_text('\n'.join([f"{x.get('release_id')} - {x.get('status')}" for x in items])[:3500], reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Releases failed: ' + str(e), reply_markup=main_menu())


async def rollback(update, context):
    a = agents.current(update.effective_user.id)
    if not a or not context.args:
        await update.message.reply_text('Usage: /rollback <site_name> [app_pool_name]', reply_markup=main_menu())
        return
    try:
        agents.require(update.effective_user.id, a['agent_id'], 'rollback')
        site_name = context.args[0]
        app_pool_name = context.args[1] if len(context.args) > 1 else None
        if BACKEND_ENABLED and str(a.get('base_url','')).startswith('backend://'):
            bc = BackendClient(str(a.get('base_url')).replace('backend://', ''), BACKEND_CFG.get('bot_token', 'phase25-bot-token'))
            job = bc.create_rollback_job({
                'agent_id': a['agent_id'],
                'requested_by_user_id': update.effective_user.id,
                'site_name': site_name,
                'app_pool_name': app_pool_name,
            })
            await update.message.reply_text(
                f"Rollback job created.\n\nJob: {job['job_id']}\nAgent: {a['agent_id']}\nWaiting for outbound agent...",
                reply_markup=main_menu()
            )
            final = bc.wait_for_job(job['job_id'], timeout_seconds=180, poll_seconds=3)
            result = final.get('result') or {}
            prefix = 'Success' if final.get('status') == 'success' else 'Failed'
            await update.message.reply_text(
                f"{prefix} Rollback {final.get('status')}\n\n" + (result.get('message') or result.get('error') or str(result)),
                reply_markup=main_menu()
            )
            return
        r = client.rollback(a, site_name, app_pool_name)
        await update.message.reply_text(('Success: ' if r.get('success') else 'Failed: ') + r.get('message', r.get('error', str(r))), reply_markup=main_menu())
    except PermissionError as e:
        await update.message.reply_text('Not allowed: ' + str(e), reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Rollback failed: ' + str(e), reply_markup=main_menu())


async def delete_release(update, context):
    a = agents.current(update.effective_user.id)
    if not a or len(context.args) < 2:
        await update.message.reply_text('Usage: /delete_release <site_name> <release_id>')
        return
    try:
        agents.require(update.effective_user.id, a['agent_id'], 'delete_release')
        r = client.delete_release(a, context.args[0], context.args[1])
        await update.message.reply_text('Release deleted.' if r.get('success') else 'Delete failed: ' + str(r))
    except PermissionError as e:
        await update.message.reply_text('Not allowed: ' + str(e))
    except Exception as e:
        await update.message.reply_text('Delete failed: ' + str(e))


async def logs(update, context):
    a = agents.current(update.effective_user.id)
    if not a:
        await update.message.reply_text('No current agent. Open Agents and select an agent first.', reply_markup=main_menu())
        return
    try:
        if BACKEND_ENABLED and str(a.get('base_url', '')).startswith('backend://'):
            # /logs with no job id opens a picker. /logs job_xxx shows that job's logs.
            if not context.args:
                await choose_logs_job(update, context)
                return
            job_id = context.args[0]
            bc = _backend_client_for_agent(a)
            _ensure_job_in_agent(bc.get_job(job_id), a)
            payload = bc.get_job_logs(job_id)
            items = payload.get('logs', [])
            if not items:
                await update.message.reply_text('No logs found for this deployment.', reply_markup=main_menu())
                return
            txt = '\n'.join([str(x.get('message') or x) for x in items])
            await update.message.reply_text(txt[-3500:], reply_markup=main_menu())
            return

        site = context.args[0] if context.args else None
        txt = client.logs(a, site).get('logs', '') or 'No logs.'
        await update.message.reply_text(txt[-3500:], reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Logs failed: ' + str(e), reply_markup=main_menu())


async def download_logs(update, context):
    a = agents.current(update.effective_user.id)
    if not a:
        await update.message.reply_text('No current agent. Open Agents and select an agent first.', reply_markup=main_menu())
        return
    try:
        if BACKEND_ENABLED and str(a.get('base_url', '')).startswith('backend://'):
            if not context.args:
                await update.message.reply_text('Usage: /download_logs <job_id>', reply_markup=main_menu())
                return
            bc = _backend_client_for_agent(a)
            _ensure_job_in_agent(bc.get_job(context.args[0]), a)
            items = bc.get_job_logs(context.args[0]).get('logs', [])
            txt = '\n'.join([str(x.get('message') or x) for x in items]) or 'No logs.'
            filename = f'{context.args[0]}_logs.txt'
        else:
            site = context.args[0] if context.args else None
            txt = client.logs(a, site).get('logs', '') or 'No logs.'
            filename = f'{site or "agent"}_logs.txt'
        with tempfile.NamedTemporaryFile('w+', suffix='.txt', delete=False, encoding='utf-8') as f:
            f.write(txt)
            name = f.name
        await update.message.reply_document(document=open(name, 'rb'), filename=filename)
    except Exception as e:
        await update.message.reply_text('Download logs failed: ' + str(e), reply_markup=main_menu())


async def delete_agent_by_id(q_or_update, uid: int, aid: str):
    # Only the owner may delete an agent; check BEFORE anything is removed.
    if agents.get(aid) is not None:
        agents.require(uid, aid, 'delete_agent')
    # Remove from backend first when possible, then local bot registry.
    try:
        if BACKEND_ENABLED and backend_client:
            backend_client.delete_agent(aid)
    except Exception:
        # Continue with local cleanup even if backend row is already gone.
        pass
    agents.delete(uid, aid)
    states.clear(uid)
    msg = f'Deleted agent: {aid}'
    if hasattr(q_or_update, 'edit_message_text'):
        await q_or_update.edit_message_text(msg)
    else:
        await q_or_update.message.reply_text(msg, reply_markup=main_menu())


async def delete_agent(update, context):
    if not context.args:
        await update.message.reply_text('Usage: /delete_agent <agent_id>\nTip: Agents also shows Delete buttons.', reply_markup=main_menu())
        return
    try:
        await delete_agent_by_id(update, update.effective_user.id, context.args[0])
    except PermissionError as e:
        await update.message.reply_text(str(e), reply_markup=main_menu())


async def cancel(update, context):
    states.clear(update.effective_user.id)
    await update.message.reply_text('Cancelled current action.', reply_markup=main_menu())


async def history(update, context):
    if not BACKEND_ENABLED:
        await update.message.reply_text('History is available when backend mode is enabled.')
        return
    try:
        aid = context.args[0] if context.args else None
        if not aid:
            a = agents.current(update.effective_user.id)
            if not a:
                await update.message.reply_text('No current agent. Use /myagents and /use_agent first.')
                return
            aid = a['agent_id']
            base_url = str(a.get('base_url', '')).replace('backend://', '') if str(a.get('base_url', '')).startswith('backend://') else BACKEND_CFG.get('base_url')
        else:
            agents.require(update.effective_user.id, aid, 'view')
            base_url = BACKEND_CFG.get('base_url')
        bc = BackendClient(base_url, BACKEND_CFG.get('bot_token', 'phase25-bot-token'))
        jobs = bc.agent_history(aid, limit=10).get('jobs', [])
        if not jobs:
            await update.message.reply_text(f'No history found for agent {aid}.', reply_markup=main_menu())
            return
        lines = [f'Recent deployment history for {aid}:']
        for j in jobs:
            result = j.get('result') or {}
            status = j.get('status')
            site = j.get('site_name')
            pool = j.get('app_pool_name')
            job_id = j.get('job_id')
            release = result.get('release_id') or '-'
            requested_by = j.get('requested_by_user_id') or '-'
            summary_text = j.get('result_summary') or result.get('message') or ''
            if len(summary_text) > 120:
                summary_text = summary_text[:117] + '...'
            src = source_label(result.get('source'))
            src_line = f'Source: {src}\n' if src else ''
            lines.append(f'\n{status.upper()} | {site} | pool: {pool}\nJob: {job_id} | Release: {release}\n{src_line}By: {requested_by}\n{summary_text}')
        await update.message.reply_text('\n'.join(lines)[:3900], reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('History failed: ' + str(e), reply_markup=main_menu())



def _backend_client_for_agent(a: dict | None = None) -> BackendClient:
    """Return the bot-facing backend client for backend jobs."""
    if a and str(a.get('base_url', '')).startswith('backend://'):
        base_url = str(a.get('base_url')).replace('backend://', '')
    else:
        base_url = BACKEND_CFG.get('base_url', 'http://127.0.0.1:9000')
    return BackendClient(base_url, BACKEND_CFG.get('bot_token', 'phase25-bot-token'))


async def _fetch_job_and_logs(job_id: str, a: dict | None = None):
    if not BACKEND_ENABLED:
        raise RuntimeError('Analysis requires backend mode.')
    if not a:
        raise PermissionError('No current agent. Open Agents and select an agent first.')
    bc = _backend_client_for_agent(a)
    job = bc.get_job(job_id)
    _ensure_job_in_agent(job, a)
    logs = bc.get_job_logs(job_id).get('logs', [])
    return job, logs


def _ensure_job_in_agent(job: dict, a: dict | None):
    """A user may only read jobs of the agent they currently have selected (and have access to)."""
    if not a or (job or {}).get('agent_id') != a.get('agent_id'):
        raise PermissionError('That job does not belong to your current agent.')


def _short_job_label(job: dict) -> str:
    status = str(job.get('status') or 'unknown').upper()
    site = job.get('site_name') or 'unknown site'
    job_id = job.get('job_id') or ''
    short_id = job_id.replace('job_', '')[:8] if job_id else '-'

    if status == 'SUCCESS':
        marker = 'Success'
    elif status == 'FAILED':
        marker = 'Failed'
    elif status == 'RUNNING':
        marker = 'Running'
    elif status == 'PENDING':
        marker = 'Pending'
    else:
        marker = status.title()

    return f"{marker} | {site} | {short_id}"


async def _recent_jobs_for_current_agent(uid: int, limit: int = 8):
    a = agents.current(uid)
    if not a:
        raise RuntimeError('No current agent. Open Agents and select an agent first.')

    bc = _backend_client_for_agent(a)
    jobs = bc.agent_history(a['agent_id'], limit=limit).get('jobs', [])
    return a, jobs


async def choose_analysis_job(update, context):
    try:
        uid = update.effective_user.id
        a, jobs = await _recent_jobs_for_current_agent(uid, limit=8)

        if not jobs:
            await update.message.reply_text(
                'No deployments found for the selected agent yet.',
                reply_markup=main_menu()
            )
            return

        rows = []
        for job in jobs:
            job_id = job.get('job_id')
            if job_id:
                rows.append([(_short_job_label(job), f'analyze_job:{job_id}')])

        rows.append([('Cancel', 'cancel')])

        await update.message.reply_text(
            'Choose a deployment to analyze:',
            reply_markup=kb(rows)
        )
    except Exception as e:
        await update.message.reply_text('Could not load deployments: ' + str(e), reply_markup=main_menu())


async def choose_report_job(update, context):
    try:
        uid = update.effective_user.id
        a, jobs = await _recent_jobs_for_current_agent(uid, limit=8)

        if not jobs:
            await update.message.reply_text(
                'No deployments found for the selected agent yet.',
                reply_markup=main_menu()
            )
            return

        rows = []
        for job in jobs:
            job_id = job.get('job_id')
            if job_id:
                rows.append([(_short_job_label(job), f'report_job:{job_id}')])

        rows.append([('Cancel', 'cancel')])

        await update.message.reply_text(
            'Choose a deployment report:',
            reply_markup=kb(rows)
        )
    except Exception as e:
        await update.message.reply_text('Could not load deployments: ' + str(e), reply_markup=main_menu())


async def choose_logs_job(update, context):
    """Show recent jobs and let the user choose which logs to view."""
    try:
        uid = update.effective_user.id
        a, jobs = await _recent_jobs_for_current_agent(uid, limit=8)
        if not jobs:
            await update.message.reply_text('No deployments found for the selected agent yet.', reply_markup=main_menu())
            return
        rows = []
        for job in jobs:
            job_id = job.get('job_id')
            rows.append([(_short_job_label(job), f'logs_job:{job_id}')])
        rows.append([('Cancel', 'cancel')])
        await update.message.reply_text('Choose deployment logs:', reply_markup=kb(rows))
    except Exception as e:
        await update.message.reply_text('Could not load deployments: ' + str(e), reply_markup=main_menu())


async def _latest_failed_job_for_current_agent(uid: int):
    a = agents.current(uid)
    if not a:
        raise RuntimeError('No current agent. Use /myagents and /use_agent first.')
    bc = _backend_client_for_agent(a)
    jobs = bc.agent_history(a['agent_id'], limit=20).get('jobs', [])
    for job in jobs:
        if str(job.get('status', '')).lower() == 'failed':
            logs = bc.get_job_logs(job['job_id']).get('logs', [])
            return job, logs
    raise RuntimeError('No failed job found for the current agent. Use /history to see recent jobs.')


async def analyze_failure(update, context):
    """Analyze the latest failed job for the selected/current agent."""
    try:
        job, logs = await _latest_failed_job_for_current_agent(update.effective_user.id)
        analysis = analyze_job_rules(job, logs)
        await update.message.reply_text(format_analysis(analysis, job), reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Analysis failed: ' + str(e), reply_markup=main_menu())


async def analyze_job_command(update, context):
    """Analyze a specific job id. Also used by /recommend_fix."""
    if not context.args:
        await choose_analysis_job(update, context)
        return
    try:
        a = agents.current(update.effective_user.id)
        job, logs = await _fetch_job_and_logs(context.args[0], a)
        analysis = analyze_job_rules(job, logs)
        await update.message.reply_text(format_analysis(analysis, job), reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Job analysis failed: ' + str(e), reply_markup=main_menu())


async def deployment_report(update, context):
    """Generate a concise deployment report for a selected job.

    If no job_id is provided, show a recent-job picker instead of guessing.
    """
    try:
        if not context.args:
            await choose_report_job(update, context)
            return

        a = agents.current(update.effective_user.id)
        if not a:
            await update.message.reply_text(
                'No current agent. Open Agents and select an agent first.',
                reply_markup=main_menu()
            )
            return

        bc = _backend_client_for_agent(a)
        job = bc.get_job(context.args[0])
        _ensure_job_in_agent(job, a)
        logs = bc.get_job_logs(job['job_id']).get('logs', [])
        await update.message.reply_text(format_report(job, logs), reply_markup=main_menu())

    except Exception as e:
        await update.message.reply_text('Report failed: ' + str(e), reply_markup=main_menu())

async def backend_status(update, context):
    if not BACKEND_ENABLED:
        await update.message.reply_text('Backend mode is disabled in config/settings.json')
        return
    try:
        r = backend_client.health()
        await update.message.reply_text(f"Backend online\nLocal URL: {BACKEND_CFG.get('base_url')}\nAgent/Public URL: {BACKEND_PUBLIC_URL}\nService: {r.get('service')}", reply_markup=main_menu())
    except Exception as e:
        await update.message.reply_text('Backend check failed: ' + str(e))

def main():
    if not TOKEN or TOKEN.startswith('PUT_'):
        raise SystemExit('Set telegram_bot_token in config/settings.json or TELEGRAM_BOT_TOKEN env var')
    app = (Application.builder().token(TOKEN).connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30).build())
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', start))
    app.add_handler(CommandHandler('myid', myid))
    app.add_handler(CommandHandler('setup_agent', setup_agent))
    app.add_handler(CommandHandler('activate_agent', activate_agent))
    app.add_handler(CommandHandler('agent_users', agent_users))
    app.add_handler(CommandHandler('my_access', my_access))
    app.add_handler(CommandHandler('add_user', add_user))
    app.add_handler(CommandHandler('set_role', set_role))
    app.add_handler(CommandHandler('remove_user', remove_user))
    app.add_handler(CommandHandler('team_create', team_create))
    app.add_handler(CommandHandler('team_delete', team_delete))
    app.add_handler(CommandHandler('team_add', team_add))
    app.add_handler(CommandHandler('team_remove', team_remove))
    app.add_handler(CommandHandler('team_role', team_role))
    app.add_handler(CommandHandler('allow_repos', allow_repos))
    app.add_handler(CommandHandler('allow_creds', allow_creds))
    app.add_handler(CommandHandler('register_agent', register_agent))
    app.add_handler(CommandHandler('myagents', myagents))
    app.add_handler(CommandHandler('use_agent', use_agent))
    app.add_handler(CommandHandler('status', status))
    app.add_handler(CommandHandler('backend_status', backend_status))
    app.add_handler(CommandHandler('history', history))
    app.add_handler(CommandHandler('analyze_failure', analyze_failure))
    app.add_handler(CommandHandler('analyze_job', analyze_job_command))
    app.add_handler(CommandHandler('deployment_report', deployment_report))
    app.add_handler(CommandHandler('recommend_fix', analyze_job_command))
    app.add_handler(CommandHandler('delete_agent', delete_agent))
    app.add_handler(CommandHandler('cancel', cancel))
    app.add_handler(CommandHandler('deploy', deploy))
    app.add_handler(CommandHandler('siteinfo', siteinfo))
    app.add_handler(CommandHandler('releases', releases))
    app.add_handler(CommandHandler('rollback', rollback))
    app.add_handler(CommandHandler('delete_release', delete_release))
    app.add_handler(CommandHandler('logs', logs))
    app.add_handler(CommandHandler('download_logs', download_logs))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.run_polling(poll_interval=1, timeout=30, drop_pending_updates=True)


if __name__ == '__main__':
    main()
