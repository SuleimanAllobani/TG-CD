"""GitHub source provider for the deployment pipeline.

This service only turns "repository + ref" into a clean folder of files plus a
small metadata dict. Everything after that (versioned release, IIS, health
check, rollback) is done by the existing DeploymentService code.

Security rules implemented here:
  * Only https://github.com/<owner>/<repo> URLs; optional agent-side allowlist.
  * git is run without a shell, with validated arguments and `--` separators.
  * Tokens live on the agent machine only (env var or a protected file). They
    are handed to git through the *environment* (never argv, never the URL,
    never .git/config) and are scoped to github.com.
  * Every message that can reach logs, results or Telegram is redacted.
  * `.git` and `.github` are removed from what gets deployed.
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common.git_validation import (
    GitInputError, is_commit_sha, redact, repo_allowed, validate_credential_ref,
    validate_ref, validate_repo_url, validate_subdir,
)

ERROR_PREFIX = 'GitHub source: '
DEFAULT_TIMEOUT = 300
DEFAULT_MAX_REPO_MB = 500
_REMOVE_FROM_ARTIFACT = ('.git', '.github')


class GitSourceError(RuntimeError):
    """Raised for any failure while fetching the repository. Message is already redacted."""


def _rmtree(path: Path):
    """rmtree that also works for read-only files (git pack files on Windows)."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(p)
        except Exception:
            pass
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, onerror=_onerror)
        else:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except Exception:
                pass
            path.unlink()


@dataclass
class StagedSource:
    root: Path        # folder whose contents become the release (repo root or sub-folder)
    work: Path        # temp working folder to delete when finished
    info: dict        # metadata recorded with the release

    def cleanup(self):
        try:
            _rmtree(self.work)
        except Exception:
            pass


class GitSourceService:
    def __init__(self, storage_root, settings: Optional[dict] = None, *,
                 allowed_protocols=('https',), extra_config=(), auth_scope='https://github.com/'):
        self.storage_root = Path(storage_root).resolve()
        self.settings = dict(settings or {})
        self.tmp_root = self.storage_root / 'tmp'
        self.allowed_protocols = tuple(allowed_protocols)
        self.extra_config = list(extra_config)          # (key, value) pairs; used by tests
        self.auth_scope = auth_scope
        self.timeout = int(self.settings.get('timeout_seconds', DEFAULT_TIMEOUT))
        self.max_bytes = int(self.settings.get('max_repo_mb', DEFAULT_MAX_REPO_MB)) * 1024 * 1024
        self.secrets_dir = Path(self.settings.get('secrets_dir') or (self.storage_root.parent / 'secrets'))

    # ------------------------------------------------------------------ helpers
    def _git_exe(self) -> str:
        configured = self.settings.get('executable')
        if configured and Path(configured).exists():
            return str(configured)
        found = shutil.which('git')
        if found:
            return found
        for cand in (r'C:\Program Files\Git\cmd\git.exe', r'C:\Program Files (x86)\Git\cmd\git.exe'):
            if Path(cand).exists():
                return cand
        raise GitSourceError(ERROR_PREFIX + 'Git is not installed on the agent machine (git.exe not found). '
                             'Install Git for Windows and restart the agent service.')

    def _token_env_names(self) -> set:
        names = set()
        for cfg in (self.settings.get('credentials') or {}).values():
            if isinstance(cfg, dict) and cfg.get('token_env'):
                names.add(str(cfg['token_env']))
        return names

    @staticmethod
    def _alias_env_name(alias: str) -> str:
        return 'CICD_GIT_TOKEN_' + re.sub(r'[^A-Za-z0-9]', '_', alias).upper()

    def load_token(self, alias: str) -> str:
        """Find the token for a credential alias on THIS machine. Never logged."""
        cfg = (self.settings.get('credentials') or {}).get(alias) or {}
        token = None
        if cfg.get('token_env'):
            token = os.environ.get(str(cfg['token_env']))
        if not token:
            token = os.environ.get(self._alias_env_name(alias))
        if not token:
            file_candidates = []
            if cfg.get('token_file'):
                p = Path(str(cfg['token_file']))
                file_candidates.append(p if p.is_absolute() else self.secrets_dir / p)
            file_candidates.append(self.secrets_dir / f'github_{alias}.token')
            for p in file_candidates:
                try:
                    if p.is_file():
                        token = p.read_text(encoding='utf-8-sig').strip()
                        if token:
                            break
                except OSError:
                    continue
        if not token:
            raise GitSourceError(
                ERROR_PREFIX + f"Credential '{alias}' is not configured on the agent machine. "
                "Ask the server administrator to run: python -m agent.set_git_token --alias " + alias)
        if len(token) > 255 or not token.isascii() or any(c.isspace() or ord(c) < 33 for c in token):
            raise GitSourceError(ERROR_PREFIX + f"Credential '{alias}' on the agent machine is malformed.")
        return token

    def _config_pairs(self, token: Optional[str]):
        devnull = os.devnull
        pairs = [
            ('protocol.allow', 'never'),
            *[(f'protocol.{p}.allow', 'always') for p in self.allowed_protocols],
            ('credential.helper', ''),
            ('core.symlinks', 'false'),
            ('core.autocrlf', 'false'),
            ('core.longpaths', 'true'),
            ('core.fsmonitor', 'false'),
            ('core.hooksPath', devnull),
            ('transfer.fsckObjects', 'true'),
            ('submodule.recurse', 'false'),
            ('advice.detachedHead', 'false'),
            ('gc.auto', '0'),
        ]
        if token:
            basic = base64.b64encode(f'x-access-token:{token}'.encode('ascii')).decode('ascii')
            pairs.append((f'http.{self.auth_scope}.extraheader', f'Authorization: Basic {basic}'))
        pairs.extend(self.extra_config)
        return pairs

    # GIT_* variables that are safe and sometimes required (corporate proxy / custom CA). Everything else
    # starting with GIT_ is dropped: GIT_TRACE/GIT_CURL_VERBOSE would print the auth header, GIT_DIR/GIT_CONFIG*/
    # GIT_ASKPASS/GIT_SSL_NO_VERIFY etc. could change what git does.
    _KEEP_GIT_ENV = {'GIT_SSL_CAINFO', 'GIT_SSL_CAPATH', 'GIT_SSL_CERT', 'GIT_SSL_KEY', 'GIT_SSL_VERSION'}

    @classmethod
    def _inherit(cls, name: str) -> bool:
        up = name.upper()
        if up.startswith('CICD_GIT_TOKEN_'):
            return False
        if up.startswith('GIT_'):
            return up in cls._KEEP_GIT_ENV
        return True

    def _env(self, token: Optional[str]) -> dict:
        drop_names = self._token_env_names()
        env = {k: v for k, v in os.environ.items() if self._inherit(k) and k not in drop_names}
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GCM_INTERACTIVE'] = 'never'
        pairs = self._config_pairs(token)
        env['GIT_CONFIG_COUNT'] = str(len(pairs))
        for i, (k, v) in enumerate(pairs):
            env[f'GIT_CONFIG_KEY_{i}'] = k
            env[f'GIT_CONFIG_VALUE_{i}'] = v
        return env

    def _secrets(self, token: Optional[str]):
        if not token:
            return ()
        basic = base64.b64encode(f'x-access-token:{token}'.encode('ascii')).decode('ascii')
        return (token, basic)

    def _friendly_error(self, stderr: str, secrets) -> str:
        text = redact(stderr, secrets).strip()
        low = text.lower()
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), 'unknown git error')[:300]
        if any(k in low for k in ('terminal prompts disabled', 'could not read username', 'authentication failed',
                                  'repository not found', 'invalid username', 'returned error: 403',
                                  'returned error: 401', 'returned error: 404', 'permission denied')):
            return ('Cannot access the repository. It may be private (choose a private repo with a configured '
                    'token), misspelled, or the token does not have access to it.')
        if "couldn't find remote ref" in low:
            return 'The requested branch/tag/commit was not found in the repository.'
        if any(k in low for k in ('could not resolve host', 'unable to access', 'timed out', 'connection', 'network')):
            return 'Network error while contacting GitHub: ' + first
        return first

    def _run(self, args, *, env, secrets, cwd=None, timeout=None):
        cmd = [self._git_exe(), *args]
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW
        try:
            p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=timeout or self.timeout, **kwargs)
        except subprocess.TimeoutExpired:
            raise GitSourceError(ERROR_PREFIX + f'git {args[0]} timed out after {timeout or self.timeout}s.')
        except FileNotFoundError:
            raise GitSourceError(ERROR_PREFIX + 'Git is not installed on the agent machine.')
        if p.returncode != 0:
            raise GitSourceError(ERROR_PREFIX + self._friendly_error(p.stderr or p.stdout, secrets))
        return p.stdout

    # ---------------------------------------------------------------- resolving
    def _resolve_named_ref(self, url, ref, env, secrets):
        """Return (ref_type, full_refname) for a branch/tag name, or None if it is neither."""
        heads, tags = f'refs/heads/{ref}', f'refs/tags/{ref}'
        out = self._run(['ls-remote', '--', url, heads, tags], env=env, secrets=secrets)
        names = {ln.split('\t', 1)[1].strip() for ln in out.splitlines() if '\t' in ln}
        has_branch, has_tag = heads in names, tags in names
        if has_branch:
            return 'branch', heads, (['A tag with the same name exists; the branch was used.'] if has_tag else [])
        if has_tag:
            return 'tag', tags, []
        return None

    def _resolve_default(self, url, env, secrets):
        out = self._run(['ls-remote', '--symref', '--', url, 'HEAD'], env=env, secrets=secrets)
        refname = None
        has_head = False
        for ln in out.splitlines():
            if ln.startswith('ref:'):
                refname = ln[4:].split('\t', 1)[0].strip()
            elif '\tHEAD' in ln:
                has_head = True
        if not has_head or not refname:
            raise GitSourceError(ERROR_PREFIX + 'The repository is empty or has no default branch.')
        return refname

    # ------------------------------------------------------------------ fetching
    def _fetch_ref(self, url, refname, repo_dir, env, secrets):
        repo_dir.mkdir(parents=True, exist_ok=True)
        self._run(['init', '-q', '.'], env=env, secrets=secrets, cwd=repo_dir)
        self._run(['fetch', '-q', '--no-tags', '--depth', '1', '--', url, refname], env=env, secrets=secrets, cwd=repo_dir)
        self._run(['checkout', '-q', '--detach', 'FETCH_HEAD'], env=env, secrets=secrets, cwd=repo_dir)

    def _fetch_commit(self, url, sha, repo_dir, env, secrets):
        """Check out a specific commit. Short SHAs and servers that refuse direct fetch use a blobless clone."""
        if len(sha) == 40:
            try:
                repo_dir.mkdir(parents=True, exist_ok=True)
                self._run(['init', '-q', '.'], env=env, secrets=secrets, cwd=repo_dir)
                self._run(['fetch', '-q', '--no-tags', '--depth', '1', '--', url, sha], env=env, secrets=secrets, cwd=repo_dir)
                self._run(['checkout', '-q', '--detach', 'FETCH_HEAD'], env=env, secrets=secrets, cwd=repo_dir)
                return
            except GitSourceError:
                _rmtree(repo_dir)
        try:
            self._run(['clone', '-q', '--filter=blob:none', '--no-checkout', '--', url, str(repo_dir)], env=env, secrets=secrets)
        except GitSourceError:
            _rmtree(repo_dir)
            self._run(['clone', '-q', '--no-checkout', '--', url, str(repo_dir)], env=env, secrets=secrets)
        try:
            full = self._run(['rev-parse', '--verify', '-q', f'{sha}^{{commit}}'], env=env, secrets=secrets, cwd=repo_dir).strip()
        except GitSourceError:
            raise GitSourceError(ERROR_PREFIX + 'The requested commit was not found (or the short SHA is ambiguous).')
        self._run(['checkout', '-q', '--detach', full], env=env, secrets=secrets, cwd=repo_dir)

    # ---------------------------------------------------------------- public API
    def stage(self, repo_url, ref=None, credential_ref=None, subdir=None, log=None) -> StagedSource:
        """Fetch the repository into a temp folder and return it ready for the release pipeline."""
        token = None
        secrets = ()
        work = None

        def say(msg):
            if log is not None:
                log.write(redact(msg, secrets))

        try:
            try:
                repo = validate_repo_url(repo_url)
                ref = validate_ref(ref)
                alias = validate_credential_ref(credential_ref)
                subdir = validate_subdir(subdir)
            except GitInputError as e:
                raise GitSourceError(ERROR_PREFIX + str(e)) from None
            if not repo_allowed(repo['full_name'], self.settings.get('allowed_repos')):
                raise GitSourceError(ERROR_PREFIX + f"Repository {repo['full_name']} is not in the agent's allowed list.")
            if alias:
                token = self.load_token(alias)
                secrets = self._secrets(token)
            env = self._env(token)
            url = repo['url']
            self._git_exe()

            warnings = []
            if ref is None:
                refname = self._resolve_default(url, env, secrets)
                ref_type = 'default'
            else:
                resolved = self._resolve_named_ref(url, ref, env, secrets)
                if resolved:
                    ref_type, refname, warnings = resolved
                elif is_commit_sha(ref):
                    ref_type, refname = 'commit', None
                else:
                    raise GitSourceError(ERROR_PREFIX + f"'{ref}' was not found as a branch or tag in {repo['full_name']}.")
            say(f"Fetching {repo['full_name']} ref={ref or 'default'} type={ref_type}"
                f"{' (authenticated)' if token else ' (anonymous)'}")

            self.tmp_root.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix='gitsrc_', dir=str(self.tmp_root)))
            repo_dir = work / 'repo'
            if ref_type == 'commit':
                self._fetch_commit(url, ref.lower(), repo_dir, env, secrets)
            else:
                self._fetch_ref(url, refname, repo_dir, env, secrets)

            show = self._run(['log', '-1', '--format=%H%x1f%cI%x1f%s'], env=env, secrets=secrets, cwd=repo_dir).strip()
            sha, _, rest = show.partition('\x1f')
            commit_date, _, subject = rest.partition('\x1f')
            sha = sha.strip()

            gitmodules = (repo_dir / '.gitmodules').exists()
            lfs = False
            try:
                ga = repo_dir / '.gitattributes'
                lfs = ga.is_file() and 'filter=lfs' in ga.read_text(encoding='utf-8', errors='ignore')
            except OSError:
                pass
            for name in _REMOVE_FROM_ARTIFACT:
                _rmtree(repo_dir / name)
            if gitmodules:
                warnings.append('Git submodules are not fetched.')
            if lfs:
                warnings.append('Git LFS files are not downloaded; pointer files are deployed instead.')

            total = 0
            for p in repo_dir.rglob('*'):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    pass
                if total > self.max_bytes:
                    raise GitSourceError(ERROR_PREFIX + f'The repository exceeds the {self.max_bytes // (1024 * 1024)} MB limit.')

            root = repo_dir
            if subdir:
                root = (repo_dir / subdir).resolve()
                if repo_dir.resolve() not in root.parents or not root.is_dir():
                    raise GitSourceError(ERROR_PREFIX + f"Sub-folder '{subdir}' was not found in the repository.")

            info = {
                'type': 'github',
                'repo_url': repo['url'],
                'repo': repo['full_name'],
                'requested_ref': ref or 'default',
                'ref_type': ref_type,
                'resolved_ref': refname,
                'commit': sha,
                'commit_short': sha[:7],
                'commit_subject': re.sub(r'[\x00-\x1f]', ' ', subject)[:200],
                'commit_date': commit_date.strip(),
                'subdir': subdir,
                'auth': f'token:{alias}' if token else 'anonymous',
            }
            if warnings:
                info['warnings'] = warnings
            say(f"Source fetched: {repo['full_name']}@{ref or 'default'} commit {sha[:12]}")
            for w in warnings:
                say('Warning: ' + w)
            return StagedSource(root=root, work=work, info=info)
        except GitSourceError as e:
            if work:
                _rmtree(work)
            raise GitSourceError(redact(str(e), secrets)) from None
        except Exception as e:
            if work:
                _rmtree(work)
            raise GitSourceError(ERROR_PREFIX + redact(f'{type(e).__name__}: {e}', secrets)) from None


def describe_source(info: Optional[dict]) -> str:
    """Short human label such as 'owner/repo@main (abc1234)'."""
    if not info or info.get('type') != 'github':
        return ''
    ref = info.get('requested_ref') or 'default'
    commit = info.get('commit_short')
    return f"{info.get('repo') or info.get('repo_url') or '?'}@{ref}" + (f' ({commit})' if commit else '')
