"""Helpers for GitHub-source tests: build local bare repos that stand in for github.com.

The agent code only accepts https://github.com/<owner>/<repo>. In tests git itself
rewrites that URL to a local bare repository using `url.<base>.insteadOf`, so the
real validation + git code paths are exercised without any network access.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent.services.git_source_service import GitSourceService

GIT_ENV = {
    'GIT_AUTHOR_NAME': 'Test', 'GIT_AUTHOR_EMAIL': 'test@example.com',
    'GIT_COMMITTER_NAME': 'Test', 'GIT_COMMITTER_EMAIL': 'test@example.com',
    'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
}


def git(*args, cwd=None):
    env = {**os.environ, **GIT_ENV}
    p = subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f'git {args} failed: {p.stderr}')
    return p.stdout.strip()


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def make_remote(base: Path, owner='acme', repo='site'):
    """Create base/<owner>/<repo> (bare) with history:

      c1 (main, tag v1 annotated, tag light, branch dev starts here): index.html=v1
      c2 (main): index.html=v2
      dev: index.html=dev
    Returns dict of useful SHAs.
    """
    work = Path(tempfile.mkdtemp(prefix='work_'))
    try:
        git('init', '-q', '-b', 'main', cwd=work)
        write(work / 'index.html', '<h1>v1</h1>')
        write(work / 'docs' / 'page.html', '<p>docs</p>')
        write(work / '.github' / 'workflows' / 'ci.yml', 'name: ci')
        git('add', '-A', cwd=work)
        git('commit', '-q', '-m', 'first commit', cwd=work)
        c1 = git('rev-parse', 'HEAD', cwd=work)
        git('tag', '-a', 'v1', '-m', 'release v1', cwd=work)
        git('tag', 'light', cwd=work)
        git('branch', 'dev', cwd=work)
        write(work / 'index.html', '<h1>v2</h1>')
        git('commit', '-qam', 'second commit', cwd=work)
        c2 = git('rev-parse', 'HEAD', cwd=work)
        git('checkout', '-q', 'dev', cwd=work)
        write(work / 'index.html', '<h1>dev</h1>')
        git('commit', '-qam', 'dev commit', cwd=work)
        c3 = git('rev-parse', 'HEAD', cwd=work)
        git('checkout', '-q', 'main', cwd=work)  # default branch of the bare clone must be main
        target = base / owner / repo
        target.parent.mkdir(parents=True, exist_ok=True)
        git('clone', '-q', '--bare', str(work), str(target))
        git('config', 'uploadpack.allowAnySHA1InWant', 'true', cwd=target)
        git('config', 'uploadpack.allowFilter', 'true', cwd=target)
        return {'c1': c1, 'c2': c2, 'dev': c3, 'path': target}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def file_uri_base(path: Path) -> str:
    """file:///.../ URI (works on Windows too) with a trailing slash."""
    return Path(path).resolve().as_uri().rstrip('/') + '/'


def local_service(storage_root, remotes_base: Path, settings=None, **kw):
    """GitSourceService whose https://github.com/ URLs are redirected to local bare repos."""
    extra = [(f'url.{file_uri_base(remotes_base)}.insteadOf', 'https://github.com/')]
    return GitSourceService(storage_root, settings, allowed_protocols=('https', 'file'), extra_config=extra, **kw)


class ListLog:
    def __init__(self):
        self.lines = []

    def write(self, msg):
        self.lines.append(msg)

    def text(self):
        return '\n'.join(self.lines)
