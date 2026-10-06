from datetime import datetime, timezone
from common.json_store import JsonStore
from bot.services import access_policy as ap


class AgentRegistry:
    def __init__(self):
        self.agents = JsonStore('data/agents.json')
        self.access = JsonStore('data/user_access.json')

    def _now(self):
        return datetime.now(timezone.utc).isoformat()

    def register(self, user_id, agent_id, name, base_url, token):
        """Manual registration, kept for compatibility. User becomes owner."""
        a = self.agents.read()
        existing = a.get(agent_id)
        if existing and ap.effective_role(existing, user_id) != 'owner':
            raise PermissionError('This agent ID already exists and belongs to someone else.')
        a[agent_id] = {
            'agent_id': agent_id,
            'name': name,
            'base_url': base_url.rstrip('/'),
            'token': token,
            'status': 'active',
            'owner_user_id': int(user_id),
            'users': {str(user_id): 'owner'},
            'created_at': self._now(),
            'activated_at': self._now(),
        }
        self.agents.write(a)
        self._grant_user_agent(user_id, agent_id, make_current=True)

    def create_pending(self, owner_user_id, agent_id, name, token):
        a = self.agents.read()
        if agent_id in a:
            raise PermissionError('This agent ID already exists.')
        a[agent_id] = {
            'agent_id': agent_id,
            'name': name,
            'base_url': None,
            'token': token,
            'status': 'pending_activation',
            'owner_user_id': int(owner_user_id),
            'users': {str(owner_user_id): 'owner'},
            'created_at': self._now(),
        }
        self.agents.write(a)
        self._grant_user_agent(owner_user_id, agent_id, make_current=True)

    def activate_pending(self, user_id, agent_id, base_url):
        a = self.agents.read()
        item = a.get(agent_id)
        if not item:
            raise ValueError('Unknown agent_id. Use /setup_agent first or /register_agent manually.')
        if ap.effective_role(item, user_id) != 'owner':
            raise ValueError('Only the owner can activate this agent.')
        item['base_url'] = base_url.rstrip('/')
        item['status'] = 'active'
        item['activated_at'] = self._now()
        a[agent_id] = item
        self.agents.write(a)
        self._grant_user_agent(user_id, agent_id, make_current=True)
        return item

    def _grant_user_agent(self, user_id, agent_id, make_current=False):
        ac = self.access.read()
        u = ac.setdefault(str(user_id), {'agents': [], 'current_agent': None})
        if agent_id not in u['agents']:
            u['agents'].append(agent_id)
        if make_current:
            u['current_agent'] = agent_id
        self.access.write(ac)

    def list_for_user(self, user_id):
        a = self.agents.read()
        u = self.access.read().get(str(user_id), {'agents': []})
        out = []
        for x in u.get('agents', []):
            if x in a:
                item = dict(a[x])
                item['role'] = ap.effective_role(item, user_id) or 'member'
                out.append(item)
        return out

    def current(self, user_id):
        ac = self.access.read().get(str(user_id), {})
        aid = ac.get('current_agent')
        if not aid:
            return None
        item = self.agents.read().get(aid)
        if not item:
            return None
        if ap.effective_role(item, user_id) is None:
            return None
        return item

    def get(self, agent_id):
        return self.agents.read().get(agent_id)

    def set_current(self, user_id, agent_id):
        ac = self.access.read()
        u = ac.setdefault(str(user_id), {'agents': [], 'current_agent': None})
        if agent_id not in u['agents']:
            raise ValueError('No access to agent')
        u['current_agent'] = agent_id
        self.access.write(ac)

    def role(self, user_id, agent_id):
        item = self.agents.read().get(agent_id) or {}
        return ap.effective_role(item, user_id)

    def require_role(self, user_id, agent_id, allowed_roles):
        r = self.role(user_id, agent_id)
        if r not in allowed_roles:
            raise PermissionError('You do not have permission for this action.')
        return r

    def users_for_agent(self, agent_id):
        item = self.agents.read().get(agent_id)
        if not item:
            raise ValueError('Unknown agent')
        return item.get('users', {})


    def delete(self, user_id, agent_id):
        a = self.agents.read()
        item = a.get(agent_id)
        if item and not ap.can(item, user_id, 'delete_agent'):
            raise PermissionError('Only the owner can delete this agent.')
        a.pop(agent_id, None)
        self.agents.write(a)
        ac = self.access.read()
        for uid, info in ac.items():
            info['agents'] = [x for x in info.get('agents', []) if x != agent_id]
            if info.get('current_agent') == agent_id:
                info['current_agent'] = info['agents'][0] if info.get('agents') else None
        self.access.write(ac)


    # ------------------------------------------------------------------
    # Roles, teams and limits (see bot/services/access_policy.py)
    # ------------------------------------------------------------------
    def can(self, user_id, agent_id, action) -> bool:
        item = self.agents.read().get(agent_id)
        return bool(item) and ap.can(item, user_id, action)

    def require(self, user_id, agent_id, action):
        """Raise PermissionError with a friendly message unless the user's role allows the action."""
        item = self.agents.read().get(agent_id)
        role = ap.effective_role(item, user_id) if item else None
        if role is None:
            raise PermissionError('You do not have access to this agent.')
        if not ap.has_role(role, action):
            need = ap.ACTION_MIN_ROLE[action]
            raise PermissionError(f'Your role on this agent is {role}; this action needs {need}.')
        return role

    def check_github(self, user_id, agent_id, repo_full_name, credential_ref=None):
        """Raise PermissionError if the user may not deploy this repository / use this credential."""
        item = self.agents.read().get(agent_id) or {}
        if not ap.repo_permitted(item, user_id, repo_full_name):
            raise PermissionError(f'You are not allowed to deploy {repo_full_name} on this agent. Ask the agent owner.')
        if not ap.credential_permitted(item, user_id, credential_ref):
            raise PermissionError(f"You are not allowed to use the credential '{credential_ref}' on this agent. Ask the agent owner.")

    def describe(self, agent_id) -> str:
        item = self.agents.read().get(agent_id)
        if not item:
            raise ValueError('Unknown agent')
        return ap.describe_agent(item)

    def describe_for(self, user_id, agent_id) -> str:
        item = self.agents.read().get(agent_id)
        if not item:
            raise ValueError('Unknown agent')
        return ap.describe_user(item, user_id)

    def _owner_item(self, actor_id, agent_id):
        a = self.agents.read()
        item = a.get(agent_id)
        if not item:
            raise ValueError('Unknown agent')
        self.require(actor_id, agent_id, 'manage')
        return a, item

    @staticmethod
    def _owner_count(item):
        return sum(1 for r in (item.get('users') or {}).values() if ap.normalize_role(r) == 'owner')

    def add_user(self, actor_id, agent_id, target, role='deployer'):
        target = ap.parse_user_id(target)
        role = ap.parse_role(role)
        a, item = self._owner_item(actor_id, agent_id)
        users = item.setdefault('users', {})
        if target in users:
            raise ap.AccessError(f'User {target} already has access (role {ap.normalize_role(users[target])}). Use /set_role to change it.')
        users[target] = role
        self.agents.write(a)
        self._grant_user_agent(target, agent_id, make_current=not (self.access.read().get(target) or {}).get('current_agent'))

    def set_role(self, actor_id, agent_id, target, role):
        target = ap.parse_user_id(target)
        role = ap.parse_role(role)
        a, item = self._owner_item(actor_id, agent_id)
        users = item.setdefault('users', {})
        if target not in users:
            raise ap.AccessError(f'User {target} has no direct role on this agent. Use /add_user first.')
        if ap.normalize_role(users[target]) == 'owner' and role != 'owner' and self._owner_count(item) <= 1:
            raise ap.AccessError('This is the only owner; make someone else an owner first.')
        users[target] = role
        self.agents.write(a)

    def remove_user(self, actor_id, agent_id, target):
        target = ap.parse_user_id(target)
        a, item = self._owner_item(actor_id, agent_id)
        users = item.setdefault('users', {})
        in_team = bool(ap.teams_of(item, target))
        if target not in users and not in_team:
            raise ap.AccessError(f'User {target} has no access to this agent.')
        if ap.normalize_role(users.get(target)) == 'owner' and self._owner_count(item) <= 1:
            raise ap.AccessError('This is the only owner; make someone else an owner first, or delete the agent.')
        users.pop(target, None)
        (item.get('limits') or {}).pop(target, None)
        for t in (item.get('teams') or {}).values():
            t['members'] = [m for m in t.get('members') or [] if str(m) != target]
        self.agents.write(a)
        ac = self.access.read()
        info = ac.get(target)
        if info:
            info['agents'] = [x for x in info.get('agents', []) if x != agent_id]
            if info.get('current_agent') == agent_id:
                info['current_agent'] = info['agents'][0] if info['agents'] else None
            self.access.write(ac)

    def team_create(self, actor_id, agent_id, name, role):
        name = ap.parse_team_name(name)
        role = ap.parse_role(role, ap.TEAM_ROLES)
        a, item = self._owner_item(actor_id, agent_id)
        teams = item.setdefault('teams', {})
        if name in teams:
            raise ap.AccessError(f"Team '{name}' already exists.")
        # Safe default: a new team may not deploy any GitHub repository or use any credential until the owner allows it.
        teams[name] = {'role': role, 'repos': [], 'creds': [], 'members': []}
        self.agents.write(a)

    def team_delete(self, actor_id, agent_id, name):
        name = ap.parse_team_name(name)
        a, item = self._owner_item(actor_id, agent_id)
        if name not in (item.get('teams') or {}):
            raise ap.AccessError(f"Team '{name}' does not exist.")
        del item['teams'][name]
        self.agents.write(a)

    def team_add(self, actor_id, agent_id, name, target):
        name = ap.parse_team_name(name)
        target = ap.parse_user_id(target)
        a, item = self._owner_item(actor_id, agent_id)
        team = (item.get('teams') or {}).get(name)
        if not team:
            raise ap.AccessError(f"Team '{name}' does not exist. Create it with /team_create.")
        members = [str(m) for m in team.get('members') or []]
        if target in members:
            raise ap.AccessError(f'User {target} is already in team {name}.')
        members.append(target)
        team['members'] = members
        users = item.setdefault('users', {})
        if target not in users:
            # Access to the agent comes from the team. The own entry is a viewer with NO GitHub rights,
            # so only the team's limits apply to this person.
            users[target] = 'viewer'
            item.setdefault('limits', {})[target] = {'repos': [], 'creds': []}
        self.agents.write(a)
        self._grant_user_agent(target, agent_id, make_current=not (self.access.read().get(target) or {}).get('current_agent'))

    def team_remove(self, actor_id, agent_id, name, target):
        name = ap.parse_team_name(name)
        target = ap.parse_user_id(target)
        a, item = self._owner_item(actor_id, agent_id)
        team = (item.get('teams') or {}).get(name)
        if not team:
            raise ap.AccessError(f"Team '{name}' does not exist.")
        members = [str(m) for m in team.get('members') or []]
        if target not in members:
            raise ap.AccessError(f'User {target} is not in team {name}.')
        team['members'] = [m for m in members if m != target]
        self.agents.write(a)

    def team_set_role(self, actor_id, agent_id, name, role):
        name = ap.parse_team_name(name)
        role = ap.parse_role(role, ap.TEAM_ROLES)
        a, item = self._owner_item(actor_id, agent_id)
        team = (item.get('teams') or {}).get(name)
        if not team:
            raise ap.AccessError(f"Team '{name}' does not exist.")
        team['role'] = role
        self.agents.write(a)

    def set_scope(self, actor_id, agent_id, who, key, value_text):
        """who = '<telegram id>' or 'team:<name>'; key = 'repos' | 'creds'; value_text = list | all | none."""
        if key not in ('repos', 'creds'):
            raise ap.AccessError('Unknown limit.')
        value = ap.parse_scope(key, value_text)
        a, item = self._owner_item(actor_id, agent_id)
        who = str(who or '').strip()
        if who.lower().startswith('team:'):
            name = ap.parse_team_name(who[5:])
            team = (item.get('teams') or {}).get(name)
            if not team:
                raise ap.AccessError(f"Team '{name}' does not exist.")
            team[key] = value
        else:
            target = ap.parse_user_id(who)
            users = item.setdefault('users', {})
            if target not in users:
                raise ap.AccessError(f'User {target} has no direct role on this agent. Use /add_user or /team_add first.')
            if ap.normalize_role(users[target]) == 'owner':
                raise ap.AccessError('Owners are never limited. Change the role first if you want to limit this person.')
            item.setdefault('limits', {}).setdefault(target, {'repos': None, 'creds': None})[key] = value
        self.agents.write(a)
