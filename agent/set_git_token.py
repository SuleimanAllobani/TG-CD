"""Store a GitHub access token on THIS (agent) machine so private repositories can be deployed.

Run on the IIS/agent server, from the agent folder, in an Administrator console:

    .venv\\Scripts\\python.exe -m agent.set_git_token --alias default
    .venv\\Scripts\\python.exe -m agent.set_git_token --alias default --verify owner/private-repo
    .venv\\Scripts\\python.exe -m agent.set_git_token --alias default --remove

The token is typed at a hidden prompt (or piped through --stdin) and written to
<agent folder>\\secrets\\github_<alias>.token, readable only by SYSTEM and Administrators.
It is never sent to the bot, the backend, Telegram or any log. Telegram only ever
carries the alias ("default").

Use a fine-grained personal access token limited to the repositories you deploy, with
"Contents: Read-only" permission and an expiry date.
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import sys
from pathlib import Path

from common.git_validation import GitInputError, validate_credential_ref

SYSTEM_SID = '*S-1-5-18'
ADMINS_SID = '*S-1-5-32-544'


def secrets_dir_for(settings: dict) -> Path:
    git_cfg = settings.get('git') or {}
    if git_cfg.get('secrets_dir'):
        return Path(git_cfg['secrets_dir'])
    return Path(settings['agent']['storage_root']).parent / 'secrets'


def token_path(settings: dict, alias: str) -> Path:
    return secrets_dir_for(settings) / f'github_{alias}.token'


def restrict_permissions(path: Path, *, is_dir: bool):
    """Owner-only on POSIX; SYSTEM + Administrators only on Windows."""
    if os.name == 'nt':
        perm = f'(OI)(CI)F' if is_dir else 'F'
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', f'{SYSTEM_SID}:{perm}', f'{ADMINS_SID}:{perm}'],
                       check=True, capture_output=True)
    else:
        os.chmod(path, 0o700 if is_dir else 0o600)


def check_token_shape(token: str):
    if not token:
        raise ValueError('The token is empty.')
    if len(token) > 255 or not token.isascii() or re.search(r'\s', token):
        raise ValueError('That does not look like a GitHub token (it contains spaces/newlines or is too long).')


def save_token(settings: dict, alias: str, token: str) -> Path:
    check_token_shape(token)
    d = secrets_dir_for(settings)
    d.mkdir(parents=True, exist_ok=True)
    restrict_permissions(d, is_dir=True)
    path = token_path(settings, alias)
    tmp = path.with_suffix('.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(token)
    restrict_permissions(tmp, is_dir=False)
    tmp.replace(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Store a GitHub token on this agent machine (never in Telegram).')
    ap.add_argument('--alias', default='default', help='credential name the bot refers to (default: default)')
    ap.add_argument('--stdin', action='store_true', help='read the token from standard input instead of a hidden prompt')
    ap.add_argument('--remove', action='store_true', help='delete the stored token for this alias')
    ap.add_argument('--verify', metavar='OWNER/REPO', help='after saving, test read access to this repository')
    args = ap.parse_args(argv)

    from common.config import load_settings
    settings = load_settings()
    try:
        alias = validate_credential_ref(args.alias) or 'default'
    except GitInputError as e:
        print(f'Invalid alias: {e}')
        return 2
    path = token_path(settings, alias)

    if args.remove:
        if path.exists():
            path.unlink()
            print(f"Removed credential '{alias}'.")
        else:
            print(f"No stored credential '{alias}'.")
        return 0

    token = sys.stdin.readline().strip() if args.stdin else getpass.getpass('GitHub token (input is hidden): ').strip()
    try:
        saved = save_token(settings, alias, token)
    except ValueError as e:
        print(f'Not saved: {e}')
        return 2
    print(f"Saved credential '{alias}' to {saved}")
    print('Only SYSTEM and Administrators can read it. If the agent service runs as another account, grant that account read access to the secrets folder.')

    if args.verify:
        from agent.services.git_source_service import GitSourceError, GitSourceService
        svc = GitSourceService(settings['agent']['storage_root'], settings.get('git'))
        try:
            staged = svc.stage(f'https://github.com/{args.verify.strip("/")}', None, alias)
            staged.cleanup()
            print(f"Verified: the token can read {args.verify}.")
        except GitSourceError as e:
            print(f'Verification failed: {e}')
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
