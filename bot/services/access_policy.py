"""Roles, teams and per-user limits for one agent (pure functions, no I/O).

Data lives inside the agent record of data/agents.json. Every key below is optional, so records written
by older versions keep working:

    "users":  {"<telegram id>": "owner" | "deployer" | "viewer"}      (the original key)
    "limits": {"<telegram id>": {"repos": [...] | null, "creds": [...] | null}}
    "teams":  {"<name>": {"role": "deployer"|"viewer", "repos": [...]|null, "creds": [...]|null,
                          "members": ["<telegram id>", ...]}}

Roles
    viewer    status, releases, history, logs, analyze, report
    deployer  viewer + deploy (ZIP, folder, GitHub) and rollback
    owner     deployer + delete releases, delete the agent, manage users, teams and limits

A user's effective role is the highest of their own role and the roles of the teams they belong to.
The legacy role name "member" (and any unknown name) is treated as "deployer", which is what it could do before.

Limits (GitHub deployments only)
    repos  which repositories the user may deploy ("owner/repo", "owner/*" or "*")
    creds  which credential names the user may use for private repositories
    null/missing = unrestricted, [] = nothing. Owners are never limited.
    A user's limit is the union of everything that applies to them: their own entry (if they have a direct
    role) and each team they belong to. If any of those sources is unrestricted, the user is unrestricted.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from common.git_validation import GitInputError, validate_credential_ref

ROLES = ('viewer', 'deployer', 'owner')
TEAM_ROLES = ('viewer', 'deployer')
RANK = {'viewer': 1, 'deployer': 2, 'owner': 3}

# action -> minimum role
ACTION_MIN_ROLE = {
    'view': 'viewer',
    'deploy': 'deployer',
    'rollback': 'deployer',
    'delete_release': 'owner',
    'delete_agent': 'owner',
    'manage': 'owner',
}

_TEAM_RE = re.compile(r'^[A-Za-z0-9_-]{1,30}$')
_REPO_PATTERN_RE = re.compile(r'^(?:\*|[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?:\*|[A-Za-z0-9._-]{1,100}))$')
_ID_RE = re.compile(r'^[0-9]{1,15}$')


class AccessError(ValueError):
    """User-facing validation error."""


def normalize_role(role: object) -> Optional[str]:
    r = str(role or '').strip().lower()
    if not r:
        return None
    if r in RANK:
        return r
    return 'deployer'  # legacy "member" and unknown names keep the old "can deploy" behaviour


def parse_user_id(text: object) -> str:
    s = str(text or '').strip()
    if not _ID_RE.match(s):
        raise AccessError('A Telegram user ID is a number (the person can get it with /myid).')
    return str(int(s))


def parse_team_name(text: object) -> str:
    s = str(text or '').strip()
    if not _TEAM_RE.match(s):
        raise AccessError('Team name: letters, digits, _ and - only (max 30).')
    return s


def parse_role(text: object, allowed: Iterable[str] = ROLES) -> str:
    r = str(text or '').strip().lower()
    if r not in allowed:
        raise AccessError('Role must be one of: ' + ', '.join(allowed) + '.')
    return r


def parse_scope(kind: str, text: object):
    """'all' -> None (unrestricted), 'none' -> [], otherwise a validated list."""
    raw = str(text or '').strip()
    if not raw:
        raise AccessError('Send a comma-separated list, or "all", or "none".')
    low = raw.lower()
    if low == 'all':
        return None
    if low == 'none':
        return []
    items = []
    for part in re.split(r'[,\s]+', raw):
        if not part:
            continue
        if kind == 'repos':
            if not _REPO_PATTERN_RE.match(part):
                raise AccessError(f"'{part}' is not a repository pattern. Use owner/repo, owner/* or *.")
            items.append(part.lower())
        else:
            try:
                alias = validate_credential_ref(part)
            except GitInputError as exc:
                raise AccessError(str(exc)) from None
            items.append(alias)
    if not items:
        raise AccessError('Send a comma-separated list, or "all", or "none".')
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


# ----------------------------------------------------------------------------
# Reading
# ----------------------------------------------------------------------------
def teams_of(item: dict, uid: object) -> list:
    """Names of the teams the user belongs to."""
    u = str(uid)
    return [name for name, t in (item.get('teams') or {}).items() if u in [str(m) for m in (t.get('members') or [])]]


def effective_role(item: dict, uid: object) -> Optional[str]:
    """Highest role from the user's own entry and their teams; None if they have no access."""
    u = str(uid)
    roles = []
    direct = (item.get('users') or {}).get(u)
    if direct is not None:
        roles.append(normalize_role(direct) or 'deployer')
    for name in teams_of(item, u):
        r = normalize_role((item['teams'][name]).get('role'))
        if r in TEAM_ROLES:
            roles.append(r)
    if not roles:
        return None
    return max(roles, key=lambda r: RANK[r])


def has_role(role: Optional[str], action: str) -> bool:
    need = ACTION_MIN_ROLE[action]
    return RANK.get(role or '', 0) >= RANK[need]


def can(item: dict, uid: object, action: str) -> bool:
    return has_role(effective_role(item, uid), action)


def scope(item: dict, uid: object, key: str):
    """None = unrestricted, otherwise the list of allowed patterns/names (possibly empty)."""
    u = str(uid)
    if effective_role(item, u) == 'owner':
        return None
    sources = []
    direct_role = normalize_role((item.get('users') or {}).get(u))
    if direct_role in ('deployer', 'owner'):
        # A viewer's own entry gives no deploy rights, so it must not widen what a team allows.
        sources.append(((item.get('limits') or {}).get(u) or {}).get(key))
    for name in teams_of(item, u):
        sources.append((item['teams'][name]).get(key))
    if not sources:
        return []
    if any(s is None for s in sources):
        return None
    out = []
    for s in sources:
        for v in s:
            if v not in out:
                out.append(v)
    return out


def _repo_match(full_name: str, patterns: Iterable[str]) -> bool:
    name = str(full_name or '').strip().lower()
    owner = name.split('/', 1)[0]
    return any(p == '*' or p == name or p == f'{owner}/*' for p in (str(x).lower() for x in patterns))


def repo_permitted(item: dict, uid: object, full_name: str) -> bool:
    sc = scope(item, uid, 'repos')
    return True if sc is None else _repo_match(full_name, sc)


def credential_permitted(item: dict, uid: object, alias: Optional[str]) -> bool:
    if not alias:
        return True  # public repositories use no credential
    sc = scope(item, uid, 'creds')
    return True if sc is None else str(alias) in sc


# ----------------------------------------------------------------------------
# Describing (for Telegram messages; contains no secrets)
# ----------------------------------------------------------------------------
def _fmt(sc) -> str:
    if sc is None:
        return 'all'
    return ', '.join(sc) if sc else 'none'


def describe_user(item: dict, uid: object) -> str:
    u = str(uid)
    role = effective_role(item, u) or 'no access'
    direct = (item.get('users') or {}).get(u)
    bits = [f'{u}: {role}']
    if direct is not None and normalize_role(direct) != role:
        bits.append(f'(own role {normalize_role(direct)})')
    ts = teams_of(item, u)
    if ts:
        bits.append('teams: ' + ', '.join(ts))
    if role != 'owner' and RANK.get(role or '', 0) >= RANK['deployer']:
        bits.append(f"GitHub repos: {_fmt(scope(item, u, 'repos'))}; credentials: {_fmt(scope(item, u, 'creds'))}")
    return ' | '.join(bits)


def describe_agent(item: dict) -> str:
    lines = [f"Access for agent {item.get('agent_id')}:", '']
    users = item.get('users') or {}
    members = set(users)
    for t in (item.get('teams') or {}).values():
        members.update(str(m) for m in t.get('members') or [])
    for u in sorted(members, key=lambda x: (-RANK.get(effective_role(item, x) or '', 0), int(x) if x.isdigit() else 0)):
        lines.append('- ' + describe_user(item, u))
    teams = item.get('teams') or {}
    if teams:
        lines += ['', 'Teams:']
        for name, t in teams.items():
            lines.append(f"- {name}: {normalize_role(t.get('role'))}; members: {', '.join(str(m) for m in t.get('members') or []) or 'none'}; "
                         f"GitHub repos: {_fmt(t.get('repos'))}; credentials: {_fmt(t.get('creds'))}")
    return '\n'.join(lines)


ROLE_HELP = (
    'Roles: viewer (look only), deployer (deploy and rollback), owner (everything, manages access).'
)
