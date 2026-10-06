"""Optional build step for the GitHub source (npm build, .NET publish).

A build runs code that lives in the repository (MSBuild targets, npm scripts), on the agent machine, with the
agent's permissions. Therefore:
  * Builds are fixed presets, never free-text commands from Telegram.
  * A build only runs for repositories listed in the agent's `git.allowed_repos` (an empty list means
    "no build"; plain file deployments of any repository are still allowed).
  * The build gets a minimal environment: no agent/bot/backend tokens, no GitHub tokens, no GIT_* variables.
    (The repository is already fetched, so the token is not needed.)
  * Hard timeout; the whole process tree is killed on timeout.
  * Output is redacted and shortened before it reaches logs, results or Telegram.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common.git_validation import GitInputError, redact, repo_allowed, validate_build_target
from .git_source_service import ERROR_PREFIX, GitSourceError

DEFAULT_BUILD_TIMEOUT = 900
MAX_OUTPUT_CHARS = 400_000
DOTNET_PROJECT_EXTS = ('.csproj', '.fsproj', '.vbproj')
NPM_OUTPUT_CANDIDATES = ('dist', 'build', 'out', '_site')
_SKIP_DIRS = {'obj', 'bin', 'node_modules', '.git', '.github', 'packages'}

# Only these variables are passed to build tools (compared case-insensitively; Windows names are case-insensitive).
_ENV_PASSTHROUGH = {
    'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'TMPDIR', 'USERPROFILE', 'APPDATA',
    'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432', 'COMMONPROGRAMFILES',
    'HOME', 'USER', 'USERNAME', 'LANG', 'LC_ALL', 'OS', 'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
    'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY', 'ALL_PROXY', 'SSL_CERT_FILE', 'SSL_CERT_DIR', 'NODE_EXTRA_CA_CERTS',
    'REQUESTS_CA_BUNDLE', 'DOTNET_ROOT', 'DOTNET_ROOT(X86)',
}


class BuildError(GitSourceError):
    """Build refused or failed. Message is already redacted and prefixed."""


@dataclass
class BuiltArtifact:
    out_dir: Path
    info: dict


def _kill_tree(proc: subprocess.Popen):
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class BuildService:
    def __init__(self, storage_root, settings: Optional[dict] = None):
        self.storage_root = Path(storage_root).resolve()
        self.settings = dict(settings or {})
        self.timeout = int(self.settings.get('build_timeout_seconds', DEFAULT_BUILD_TIMEOUT))
        self.cache_root = self.storage_root / 'cache'

    # ------------------------------------------------------------------ policy
    def require_allowed(self, repo_full_name: str):
        allowed = [a for a in (self.settings.get('allowed_repos') or []) if str(a).strip()]
        if not allowed or not repo_allowed(repo_full_name, allowed):
            raise BuildError(ERROR_PREFIX + f"Builds run code from the repository, so they only run for repositories "
                             f"listed in git.allowed_repos on the agent ({repo_full_name} is not listed).")

    # --------------------------------------------------------------- environment
    def _env(self, work: Path) -> dict:
        extra = {str(n).upper() for n in (self.settings.get('build_env_passthrough') or [])}
        env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_PASSTHROUGH or k.upper() in extra}
        self.cache_root.mkdir(parents=True, exist_ok=True)
        env.update({
            'DOTNET_CLI_TELEMETRY_OPTOUT': '1', 'DOTNET_NOLOGO': '1', 'DOTNET_SKIP_FIRST_TIME_EXPERIENCE': '1',
            'DOTNET_CLI_HOME': str(work / 'dotnet_home'), 'NUGET_PACKAGES': str(self.cache_root / 'nuget'),
            'npm_config_cache': str(self.cache_root / 'npm'), 'npm_config_audit': 'false', 'npm_config_fund': 'false',
            'npm_config_update_notifier': 'false',
        })
        return env

    # ------------------------------------------------------------------- running
    def _run(self, cmd, cwd: Path, env: dict, label: str, log):
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        started = time.time()
        try:
            proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', **kwargs)
        except FileNotFoundError:
            raise BuildError(ERROR_PREFIX + f'Build failed: could not start {label} (tool not found).') from None
        try:
            out, _ = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                proc.communicate(timeout=30)
            except Exception:
                pass
            raise BuildError(ERROR_PREFIX + f'Build failed: {label} timed out after {self.timeout}s and was stopped.') from None
        out = (out or '')[-MAX_OUTPUT_CHARS:]
        seconds = round(time.time() - started, 1)
        if proc.returncode != 0:
            lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
            for ln in lines[-60:]:
                log.write('  build> ' + redact(ln)[:300])
            errs = [ln for ln in lines if re.search(r'\berror\b|ERR!|Error:', ln, re.I)]
            shown = (errs[:6] if errs else lines[-6:])
            summary = ' | '.join(redact(ln.strip())[:200] for ln in shown)[:700]
            raise BuildError(ERROR_PREFIX + f'Build failed ({label}, exit code {proc.returncode}). {summary}')
        for ln in [x for x in out.splitlines() if x.strip()][-8:]:
            log.write('  build> ' + redact(ln)[:300])
        return out, seconds

    def _version(self, cmd, cwd, env) -> str:
        try:
            p = subprocess.run([*cmd, '--version'], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=30,
                               stdin=subprocess.DEVNULL)
            return (p.stdout or '').strip().splitlines()[0][:40] if p.returncode == 0 and p.stdout.strip() else ''
        except Exception:
            return ''

    # --------------------------------------------------------------------- public
    def build(self, preset: str, root: Path, work: Path, target: Optional[str], log) -> BuiltArtifact:
        try:
            target = validate_build_target(target, preset)
        except GitInputError as e:
            raise BuildError(ERROR_PREFIX + str(e)) from None
        env = self._env(work)
        if preset == 'dotnet_publish':
            return self._dotnet_publish(Path(root), Path(work), target, env, log)
        if preset == 'npm_build':
            return self._npm_build(Path(root), Path(work), target, env, log)
        raise BuildError(ERROR_PREFIX + f"Unknown build preset '{preset}'.")

    # ---------------------------------------------------------------------- .NET
    def _dotnet_exe(self) -> str:
        configured = self.settings.get('dotnet')
        if configured and Path(configured).exists():
            return str(configured)
        found = shutil.which('dotnet')
        if found:
            return found
        for cand in (r'C:\Program Files\dotnet\dotnet.exe', r'C:\Program Files (x86)\dotnet\dotnet.exe'):
            if Path(cand).exists():
                return cand
        raise BuildError(ERROR_PREFIX + 'Build failed: the .NET SDK is not installed on the agent machine '
                         '(dotnet.exe not found). Install the .NET SDK (not only the runtime) and restart the agent service.')

    def _find_dotnet_project(self, root: Path, target: Optional[str]) -> Path:
        root = root.resolve()
        if target:
            proj = (root / target).resolve()
            if root not in proj.parents or not proj.is_file() or proj.suffix.lower() not in DOTNET_PROJECT_EXTS:
                raise BuildError(ERROR_PREFIX + f"Build failed: project file '{target}' was not found in the repository.")
            return proj
        candidates = []
        for p in root.rglob('*'):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if any(part.lower() in _SKIP_DIRS for part in rel.parts[:-1]) or len(rel.parts) > 5:
                continue
            if p.is_file() and p.suffix.lower() in DOTNET_PROJECT_EXTS:
                candidates.append(p)
        candidates = sorted(candidates)
        non_test = [c for c in candidates if not re.search(r'tests?(\.|$)', c.stem, re.I)]
        pool = non_test or candidates
        if len(pool) == 1:
            return pool[0]
        if not pool:
            raise BuildError(ERROR_PREFIX + 'Build failed: no .NET project file (.csproj) was found in the repository. '
                             'If it is in a sub-folder, choose that folder or send the project path.')
        web = []
        for c in pool:
            try:
                if re.search(r'Sdk\s*=\s*"Microsoft\.NET\.Sdk\.Web"', c.read_text(encoding='utf-8', errors='ignore')[:200_000]):
                    web.append(c)
            except OSError:
                pass
        if len(web) == 1:
            return web[0]
        names = ', '.join(str(c.relative_to(root)).replace('\\', '/') for c in pool[:10])
        raise BuildError(ERROR_PREFIX + f'Build failed: several .NET projects were found ({names}). '
                         'Send the path of the project to publish, for example src/Web/Web.csproj.')

    def _dotnet_publish(self, root: Path, work: Path, target: Optional[str], env: dict, log) -> BuiltArtifact:
        exe = self._dotnet_exe()
        proj = self._find_dotnet_project(root, target)
        out_dir = work / 'build_out'
        if out_dir.exists():
            shutil.rmtree(out_dir, ignore_errors=True)
        rel = str(proj.relative_to(root.resolve())).replace('\\', '/')
        log.write(f'Build: dotnet publish {rel} (Release)')
        version = self._version([exe], proj.parent, env)
        _, seconds = self._run([exe, 'publish', str(proj), '-c', 'Release', '-o', str(out_dir), '--nologo'],
                               proj.parent, env, 'dotnet publish', log)
        if not out_dir.is_dir() or not any(out_dir.iterdir()):
            raise BuildError(ERROR_PREFIX + 'Build failed: dotnet publish produced no output.')
        info = {'preset': 'dotnet_publish', 'target': rel, 'tool': f'dotnet {version}'.strip(), 'seconds': seconds, 'output': 'publish'}
        warnings = []
        if not (out_dir / 'web.config').exists() and not any(out_dir.glob('*.html')):
            warnings.append('The published output has no web.config or HTML file; IIS may not be able to serve it '
                            '(is this a web application, and is the ASP.NET Core Hosting Bundle installed?).')
        if warnings:
            info['warnings'] = warnings
        log.write(f'Build finished in {seconds}s')
        return BuiltArtifact(out_dir, info)

    # ----------------------------------------------------------------------- npm
    def _npm_exe(self) -> str:
        configured = self.settings.get('npm')
        if configured and Path(configured).exists():
            return str(configured)
        found = shutil.which('npm.cmd') if os.name == 'nt' else None
        found = found or shutil.which('npm')
        if found:
            return found
        raise BuildError(ERROR_PREFIX + 'Build failed: Node.js/npm is not installed on the agent machine (npm not found). '
                         'Install Node.js LTS and restart the agent service.')

    def _npm_build(self, root: Path, work: Path, target: Optional[str], env: dict, log) -> BuiltArtifact:
        npm = self._npm_exe()
        root = root.resolve()
        pkg_file = root / 'package.json'
        if not pkg_file.is_file():
            raise BuildError(ERROR_PREFIX + 'Build failed: package.json was not found (in the repository or the chosen sub-folder).')
        try:
            pkg = json.loads(pkg_file.read_text(encoding='utf-8-sig'))
        except ValueError:
            raise BuildError(ERROR_PREFIX + 'Build failed: package.json is not valid JSON.') from None
        if not isinstance((pkg.get('scripts') or {}).get('build'), str):
            raise BuildError(ERROR_PREFIX + "Build failed: package.json has no 'build' script.")
        if (root / 'yarn.lock').exists() or (root / 'pnpm-lock.yaml').exists():
            if not (root / 'package-lock.json').exists():
                raise BuildError(ERROR_PREFIX + 'Build failed: this project uses yarn or pnpm; only npm is supported '
                                 '(add a package-lock.json or use the "no build" option with a pre-built folder).')
        use_ci = (root / 'package-lock.json').exists() or (root / 'npm-shrinkwrap.json').exists()
        install = [npm, 'ci' if use_ci else 'install', '--no-audit', '--no-fund', '--loglevel=error']
        log.write(f"Build: npm {'ci' if use_ci else 'install'} + npm run build")
        _, s1 = self._run(install, root, env, f"npm {'ci' if use_ci else 'install'}", log)
        _, s2 = self._run([npm, 'run', 'build'], root, env, 'npm run build', log)
        if target:
            out_dir = (root / target).resolve()
            if root not in out_dir.parents or not out_dir.is_dir():
                raise BuildError(ERROR_PREFIX + f"Build failed: the output folder '{target}' does not exist after the build.")
        else:
            out_dir = None
            found = [root / c for c in NPM_OUTPUT_CANDIDATES if (root / c).is_dir() and any((root / c).iterdir())]
            for cand in found:
                if (cand / 'index.html').exists():
                    out_dir = cand
                    break
            out_dir = out_dir or (found[0] if found else None)
            if out_dir is None:
                raise BuildError(ERROR_PREFIX + 'Build failed: no output folder (dist, build, out) was found after the build. '
                                 'Send the output folder name.')
        if not any(out_dir.iterdir()):
            raise BuildError(ERROR_PREFIX + f"Build failed: the output folder '{out_dir.name}' is empty.")
        info = {'preset': 'npm_build', 'target': target, 'tool': f"npm {self._version([npm], root, env)}".strip(),
                'seconds': round(s1 + s2, 1), 'output': str(out_dir.relative_to(root)).replace('\\', '/')}
        if not (out_dir / 'index.html').exists():
            info['warnings'] = ['The build output has no index.html.']
        log.write(f"Build finished in {info['seconds']}s, output folder: {info['output']}")
        return BuiltArtifact(out_dir, info)
