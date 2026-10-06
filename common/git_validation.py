"""Validation, secret detection and redaction helpers for the GitHub source.

Shared by the bot (to reject bad input early), the backend (to refuse payloads
that contain credentials) and the agent (the final authority before git runs).
Pure functions only; no I/O.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

GITHUB_HOST = 'github.com'


class GitInputError(ValueError):
    """User-facing validation error. Messages never echo secret-looking input."""


# ----------------------------------------------------------------------------
# Secret detection / redaction
# ----------------------------------------------------------------------------
_SECRET_PATTERNS = [
    re.compile(r'gh[pousr]_[A-Za-z0-9]{20,}'),                  # classic GitHub tokens
    re.compile(r'github_pat_[A-Za-z0-9_]{20,}'),                # fine-grained PATs
    re.compile(r'\b\d{6,}:[A-Za-z0-9_-]{30,}'),                 # Telegram bot tokens
    re.compile(r'(?i)\bauthorization\s*:\s*\S+'),               # pasted HTTP header
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}'),
    re.compile(r'(?i)\bbasic\s+[A-Za-z0-9+/=]{16,}'),
    re.compile(r'(?i)https?://[^/\s:@]+:[^/\s@]+@'),            # user:password@ in a URL
    re.compile(r'(?i)https?://[^/\s@]{20,}@'),                  # token@ in a URL
]
_URL_USERINFO = re.compile(r'(?i)(https?://)[^/\s@]+@')


def looks_like_secret(text: object) -> bool:
    """True if the text appears to contain a token, password or auth header."""
    s = str(text or '')
    return any(p.search(s) for p in _SECRET_PATTERNS)


def redact(text: object, secrets: Iterable[str] = ()) -> str:
    """Remove known secrets and anything that looks like one from text."""
    s = str(text if text is not None else '')
    for sec in secrets:
        if sec and len(sec) >= 4:
            s = s.replace(sec, '***')
    s = _URL_USERINFO.sub(r'\1***@', s)
    for p in _SECRET_PATTERNS:
        s = p.sub('***', s)
    return s


# ----------------------------------------------------------------------------
# Repository URL
# ----------------------------------------------------------------------------
_OWNER_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$')
_REPO_RE = re.compile(r'^[A-Za-z0-9._-]{1,100}$')
_URL_RE = re.compile(r'^https://(?:www\.)?github\.com/([^/\s?#]+)/([^/\s?#]+?)(?:\.git)?/?$', re.I)


def validate_repo_url(url: object) -> dict:
    """Validate a public GitHub repository URL and return its canonical parts.

    Only https://github.com/<owner>/<repo> is accepted. Credentials in the URL,
    other hosts, other schemes, extra path segments, queries and fragments are
    rejected.
    """
    raw = str(url or '').strip()
    if not raw:
        raise GitInputError('Repository URL is required.')
    if len(raw) > 300 or any(ord(c) < 32 for c in raw) or any(c.isspace() for c in raw):
        raise GitInputError('Repository URL is not valid.')
    if '@' in raw:
        raise GitInputError('Do not put credentials in the repository URL. Use the server-side token instead.')
    if raw.lower().startswith(('github.com/', 'www.github.com/')):
        raw = 'https://' + raw
    if raw.lower().startswith('http://'):
        raise GitInputError('Use https:// (plain http is not accepted).')
    m = _URL_RE.match(raw)
    if not m:
        raise GitInputError('Send a URL like https://github.com/owner/repo (only github.com repositories are supported).')
    owner, repo = m.group(1), m.group(2)
    if not _OWNER_RE.match(owner) or not _REPO_RE.match(repo) or repo.strip('.') == '' or repo.startswith('-'):
        raise GitInputError('Repository owner or name contains unsupported characters.')
    return {
        'owner': owner,
        'repo': repo,
        'full_name': f'{owner}/{repo}',
        'url': f'https://{GITHUB_HOST}/{owner}/{repo}',
    }


def repo_allowed(full_name: str, allowed: Optional[Iterable[str]]) -> bool:
    """Check owner/repo against an allowlist of 'owner/repo' or 'owner/*' entries (case-insensitive).

    An empty or missing allowlist allows everything.
    """
    entries = [str(a).strip().lower() for a in (allowed or []) if str(a).strip()]
    if not entries:
        return True
    name = full_name.lower()
    owner = name.split('/', 1)[0]
    return any(e == name or e == f'{owner}/*' or e == '*' for e in entries)


# ----------------------------------------------------------------------------
# Ref (branch / tag / commit)
# ----------------------------------------------------------------------------
_REF_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._/-]*$')
_SHA_RE = re.compile(r'^[0-9a-fA-F]{7,40}$')


def validate_ref(ref: object) -> Optional[str]:
    """Validate a branch/tag/commit. Returns None for 'use the default branch'."""
    r = str(ref or '').strip()
    if not r:
        return None
    if looks_like_secret(r):
        raise GitInputError('That does not look like a branch, tag or commit.')
    if len(r) > 200 or not _REF_RE.match(r):
        raise GitInputError('Branch/tag/commit may contain only letters, digits, . _ - / and cannot start with - or /.')
    if '..' in r or '//' in r or r.endswith(('/', '.')) or r.lower().endswith('.lock') or '/.' in r:
        raise GitInputError('Branch/tag/commit name is not valid.')
    return r


def is_commit_sha(ref: Optional[str]) -> bool:
    return bool(ref and _SHA_RE.match(ref))


# ----------------------------------------------------------------------------
# Credential alias and sub-folder
# ----------------------------------------------------------------------------
_ALIAS_RE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


def validate_credential_ref(alias: object) -> Optional[str]:
    """A credential *name* configured on the agent. Never the secret itself."""
    a = str(alias or '').strip()
    if not a:
        return None
    if looks_like_secret(a) or not _ALIAS_RE.match(a):
        raise GitInputError('Credential name may contain only letters, digits, _ and - (max 40). Never send a token here.')
    return a


_SUBDIR_PART_RE = re.compile(r'^[A-Za-z0-9._ -]{1,100}$')


def validate_subdir(subdir: object) -> Optional[str]:
    """A relative folder inside the repository to deploy. None = repository root."""
    s = str(subdir or '').strip().replace('\\', '/').strip('/')
    if not s or s == '.':
        return None
    if len(s) > 200 or ':' in s:
        raise GitInputError('Sub-folder must be a relative path inside the repository.')
    parts = s.split('/')
    for p in parts:
        if p in ('', '.', '..') or not _SUBDIR_PART_RE.match(p) or p.lower() == '.git':
            raise GitInputError('Sub-folder must be a relative path inside the repository.')
    return '/'.join(parts)


# ----------------------------------------------------------------------------
# Build preset and build target (Phase 3)
# ----------------------------------------------------------------------------
BUILD_PRESETS = ('dotnet_publish', 'npm_build')


def validate_build_preset(preset: object) -> Optional[str]:
    p = str(preset or '').strip()
    if not p:
        return None
    if p not in BUILD_PRESETS:
        raise GitInputError('Unknown build option. Choose "no build", "npm build" or ".NET publish".')
    return p


def validate_build_target(target: object, preset: Optional[str] = None) -> Optional[str]:
    """Project file (.NET) or output folder (npm), relative to the repository / chosen sub-folder."""
    t = validate_subdir(target)
    if t is None:
        return None
    if preset == 'dotnet_publish' and not t.lower().endswith(('.csproj', '.fsproj', '.vbproj')):
        raise GitInputError('Send the path of a project file ending in .csproj (for example src/Web/Web.csproj).')
    return t
