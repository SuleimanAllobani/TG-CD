import json
import secrets
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / 'dist' / 'generated_agents'


def _copy_if_exists(src: Path, dst: Path):
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    elif src.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


class AgentSetupService:
    def __init__(self):
        DIST.mkdir(parents=True, exist_ok=True)

    def generate_identity(self):
        suffix = secrets.token_hex(3).upper()
        return f'agent_{suffix}', secrets.token_urlsafe(32)

    def build_package(self, agent_id: str, token: str, port: int = 8765, mode: str = 'direct', backend_url: str | None = None) -> Path:
        work = DIST / agent_id
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True, exist_ok=True)

        # Agent runtime files only. Do not include bot config or bot token.
        _copy_if_exists(ROOT / 'agent', work / 'agent')
        _copy_if_exists(ROOT / 'common', work / 'common')
        _copy_if_exists(ROOT / 'requirements.txt', work / 'requirements.txt')

        # Agent-only config with unique identity.
        config_dir = work / 'config'
        config_dir.mkdir(parents=True, exist_ok=True)
        public_backend = (backend_url or 'http://127.0.0.1:9000').rstrip('/')
        settings = {
            # Top-level fields are included so the generated package is easy to inspect.
            # common.config also reads the nested agent fields used by the runtime.
            'mode': mode,
            'backend_url': public_backend,
            'telegram_bot_token': '',
            'backend': {
                'enabled': mode == 'outbound',
                'base_url': public_backend,
                'public_url': public_backend,
                'bot_token': ''
            },
            'agent': {
                'id': agent_id,
                'token': token,
                'host': '0.0.0.0',
                'port': port,
                'storage_root': 'storage',
                'mode': mode,
                'backend_url': public_backend,
                'poll_interval_seconds': 5
            }
        }
        # Keep both names: settings.json is what the runtime loads, while
        # agent_settings.json is easier for testers to verify in Phase 2.5.
        (config_dir / 'settings.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')
        (config_dir / 'agent_settings.json').write_text(json.dumps(settings, indent=2), encoding='utf-8')

        scripts_dir = work / 'scripts'
        scripts_dir.mkdir(parents=True, exist_ok=True)
        self._write_agent_scripts(scripts_dir, agent_id, port)
        # Root-level installer for non-technical users.
        (work / 'install_agent_service.bat').write_text(r'''@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0scripts\install_agent_service.ps1"
pause
''', encoding='utf-8')
        self._write_readme(work, agent_id, port)

        zip_path = DIST / f'agent_setup_{agent_id}.zip'
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for p in work.rglob('*'):
                # Put install_agent_service.bat at the ZIP root after extraction.
                # Also skip bytecode caches from previous test runs.
                if '__pycache__' in p.parts or p.suffix in {'.pyc', '.pyo'}:
                    continue
                z.write(p, p.relative_to(work))
        return zip_path

    def _write_agent_scripts(self, scripts_dir: Path, agent_id: str, port: int):
        (scripts_dir / 'install_agent_service.bat').write_text(r'''@echo off
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "%~dp0install_agent_service.ps1"
pause
''', encoding='utf-8')
        (scripts_dir / 'start_agent_service.bat').write_text(f'''@echo off
powershell -ExecutionPolicy Bypass -Command "Start-Service TelegramCICDAgent_{agent_id}; Get-Service TelegramCICDAgent_{agent_id}"
pause
''', encoding='utf-8')
        (scripts_dir / 'stop_agent_service.bat').write_text(f'''@echo off
powershell -ExecutionPolicy Bypass -Command "Stop-Service TelegramCICDAgent_{agent_id}; Get-Service TelegramCICDAgent_{agent_id}"
pause
''', encoding='utf-8')
        (scripts_dir / 'set_git_token.bat').write_text(r'''@echo off
rem Store a GitHub token on THIS machine for private-repository deployments (run as Administrator).
rem Usage: set_git_token.bat --alias default [--verify owner/repo]
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Run install_agent_service.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m agent.set_git_token %*
pause
''', encoding='utf-8')
        (scripts_dir / 'uninstall_agent_service.bat').write_text(r'''@echo off
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "%~dp0uninstall_agent_service.ps1"
pause
''', encoding='utf-8')
        (scripts_dir / 'test_backend_connection.py').write_text(r'''
import json
from pathlib import Path
import requests

cfg = json.loads(Path('config/settings.json').read_text(encoding='utf-8'))
backend = cfg['agent']['backend_url'].rstrip('/')
agent_id = cfg['agent']['id']
token = cfg['agent']['token']

s = requests.Session()
s.trust_env = False
print('Testing backend without proxy:', backend)
r = s.get(backend + '/api/health', timeout=20)
print('health:', r.status_code, r.text[:500])
r.raise_for_status()
r = s.post(backend + '/api/agent/heartbeat', json={'agent_id': agent_id, 'name': agent_id, 'status': 'online'}, headers={'Authorization': 'Bearer ' + token}, timeout=20)
print('heartbeat:', r.status_code, r.text[:500])
r.raise_for_status()
print('OK: backend connection and agent token work')
''', encoding='utf-8')
        (scripts_dir / 'test_backend_connection.bat').write_text(r'''@echo off
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
)
".venv\Scripts\python.exe" -m pip install requests >nul
".venv\Scripts\python.exe" "%~dp0test_backend_connection.py"
pause
''', encoding='utf-8')

        (scripts_dir / 'install_agent_service.ps1').write_text(f'''$ErrorActionPreference = "Stop"
$ServiceName = "TelegramCICDAgent_{agent_id}"
$Port = {port}
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

function Assert-Admin {{
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    throw "Please right-click install_agent_service.bat and choose Run as Administrator."
  }}
}}

Assert-Admin
Set-Location $Root

if (!(Get-Command py -ErrorAction SilentlyContinue)) {{ throw "Python Launcher 'py' was not found. Install Python 3.12 first." }}
if (!(Test-Path ".venv")) {{ py -3.12 -m venv .venv }}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\pip.exe" install -r requirements.txt

New-Item -ItemType Directory -Force -Path ".\storage", ".\data" | Out-Null
icacls (Join-Path $Root "storage") /grant "IIS_IUSRS:(OI)(CI)RX" /T | Out-Null

# Folder for GitHub access tokens (private repositories). Only SYSTEM and Administrators can read it.
New-Item -ItemType Directory -Force -Path ".\secrets" | Out-Null
icacls (Join-Path $Root "secrets") /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# Git is required for the GitHub deployment source. ZIP deployments keep working without it.
if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {{
  Write-Host "Git was not found. Trying to install Git automatically with winget..." -ForegroundColor Yellow
  if (Get-Command winget.exe -ErrorAction SilentlyContinue) {{
    try {{ winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements | Out-Host }} catch {{ Write-Host $_ -ForegroundColor Yellow }}
    $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
  }}
  if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {{
    Write-Host "WARNING: Git is not installed. ZIP deployments work, but GitHub deployments need Git for Windows: https://git-scm.com/download/win" -ForegroundColor Yellow
  }}
}}

# Install service with NSSM. The installer tries hard to find/install NSSM automatically.
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Mode = (Get-Content ".\config\settings.json" | ConvertFrom-Json).agent.mode
$Args = if ($Mode -eq "outbound") {{ "-u -m agent.outbound_main" }} else {{ "-u -m agent.main" }}

# Direct mode needs an inbound agent API port. Outbound mode does not.
if ($Mode -ne "outbound") {{
  if (-not (Get-NetFirewallRule -DisplayName "Telegram CICD Agent $Port" -ErrorAction SilentlyContinue)) {{
    New-NetFirewallRule -DisplayName "Telegram CICD Agent $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
  }}
}}

function Find-Nssm {{
  $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
  if ($cmd) {{ return $cmd.Source }}
  $root = Get-Location
  $candidates = @(
    (Join-Path (Join-Path $root "tools") "nssm.exe"),
    (Join-Path (Join-Path $env:USERPROFILE "Downloads") "nssm.exe"),
    "C:/nssm/nssm.exe",
    "C:/nssm/win64/nssm.exe"
  )
  foreach ($c in $candidates) {{ if (Test-Path $c) {{ return $c }} }}
  return $null
}}
$NssmPath = Find-Nssm
if (!$NssmPath -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {{
  Write-Host "NSSM was not found. Trying to install NSSM automatically with winget..." -ForegroundColor Yellow
  try {{ winget install --id NSSM.NSSM -e --silent --accept-source-agreements --accept-package-agreements | Out-Host }} catch {{ Write-Host $_ -ForegroundColor Yellow }}
  $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
  $NssmPath = Find-Nssm
}}

if ($NssmPath) {{
  $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($existing) {{
    & $NssmPath stop $ServiceName 2>$null | Out-Null
    Start-Sleep -Seconds 2
    & $NssmPath remove $ServiceName confirm 2>$null | Out-Null
  }}
  & $NssmPath install $ServiceName $Python $Args
  & $NssmPath set $ServiceName AppDirectory $Root
  New-Item -ItemType Directory -Force -Path (Join-Path $Root "logs") | Out-Null
  & $NssmPath set $ServiceName AppStdout (Join-Path (Join-Path $Root "logs") "agent_stdout.log")
  & $NssmPath set $ServiceName AppStderr (Join-Path (Join-Path $Root "logs") "agent_stderr.log")
  & $NssmPath set $ServiceName AppExit Default Restart
  & $NssmPath set $ServiceName AppRestartDelay 5000
  # Clear proxy variables for the agent service. agent/outbound_main.py also uses requests with trust_env=False.
  # Include the exact backend host in NO_PROXY for tools/scripts that still honor proxy variables.
  $BackendUrl = (Get-Content ".\config\settings.json" | ConvertFrom-Json).agent.backend_url
  try {{ $BackendHost = ([System.Uri]$BackendUrl).Host }} catch {{ $BackendHost = "" }}
  $NoProxyValue = if ($BackendHost) {{ "$BackendHost,127.0.0.1,localhost,100.64.0.0/10" }} else {{ "127.0.0.1,localhost,100.64.0.0/10" }}
  & $NssmPath set $ServiceName AppEnvironmentExtra "HTTP_PROXY=" "HTTPS_PROXY=" "http_proxy=" "https_proxy=" "ALL_PROXY=" "all_proxy=" "NO_PROXY=$NoProxyValue" "no_proxy=$NoProxyValue"
  & $NssmPath start $ServiceName
}} else {{
  Write-Host "NSSM could not be found or installed automatically." -ForegroundColor Red
  Write-Host "Put nssm.exe in one of these places and rerun install_agent_service.bat:" -ForegroundColor Yellow
  Write-Host "  $(Join-Path (Join-Path $Root 'tools') 'nssm.exe')"
  Write-Host "  $(Join-Path (Join-Path $env:USERPROFILE 'Downloads') 'nssm.exe')"
  Write-Host "  C:/nssm/nssm.exe"
  exit 1
}}

Write-Host ""
Write-Host "Agent service installed and started: $ServiceName" -ForegroundColor Green
Write-Host "Mode: $Mode"
Write-Host "Direct mode listens on port $Port. Outbound mode polls backend and does not need inbound access."
Write-Host "Check service with: sc query $ServiceName"
''', encoding='utf-8')
        (scripts_dir / 'uninstall_agent_service.ps1').write_text(f'''$ServiceName = "TelegramCICDAgent_{agent_id}"
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))
function Find-Nssm {{
  $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
  if ($cmd) {{ return $cmd.Source }}
  $root = Get-Location
  $candidates = @(
    (Join-Path (Join-Path $root "tools") "nssm.exe"),
    (Join-Path (Join-Path $env:USERPROFILE "Downloads") "nssm.exe"),
    "C:/nssm/nssm.exe",
    "C:/nssm/win64/nssm.exe"
  )
  foreach ($c in $candidates) {{ if (Test-Path $c) {{ return $c }} }}
  return $null
}}
$NssmPath = Find-Nssm
if ($NssmPath -and (Get-Service $ServiceName -ErrorAction SilentlyContinue)) {{
  & $NssmPath stop $ServiceName 2>$null | Out-Null
  Start-Sleep -Seconds 2
  & $NssmPath remove $ServiceName confirm 2>$null | Out-Null
  Write-Host "Agent service removed with NSSM: $ServiceName"
}} elseif (Get-Service $ServiceName -ErrorAction SilentlyContinue) {{
  Stop-Service $ServiceName -ErrorAction SilentlyContinue
  sc.exe delete $ServiceName | Out-Host
  Write-Host "Agent service removed with sc.exe: $ServiceName"
}} else {{
  Write-Host "Agent service was not found: $ServiceName"
}}
''', encoding='utf-8')

    def _write_readme(self, work: Path, agent_id: str, port: int):
        (work / 'README_AGENT_SETUP.md').write_text(f"""# Telegram CI/CD Agent Setup

Agent ID: `{agent_id}`
Port: `{port}`

## Install on the IIS server laptop

1. Extract this ZIP on the IIS laptop, for example `C:\\CICD_Agent_{agent_id}`.
2. The installer will find NSSM from PATH, `tools\nssm.exe`, Downloads, or `C:\nssm`. If it cannot find it, put `nssm.exe` in `tools\nssm.exe` and rerun.
3. Right-click `scripts\\install_agent_service.bat`.
4. Choose **Run as Administrator**.
5. Wait until it says the service is installed and started.

## Phase 2 direct mode

If this package was generated in direct mode, find this laptop IP using `ipconfig`, then activate in Telegram:

```text
/activate_agent {agent_id} http://<THIS-LAPTOP-IP>:{port}
```

## Phase 2.5 outbound mode

If this package was generated in outbound mode, the agent connects outward to the backend automatically. You do **not** need `/activate_agent` and you do **not** need the bot laptop to reach this agent laptop directly.

Check service:

```cmd
sc query TelegramCICDAgent_{agent_id}
```

## GitHub deployments (optional)

The bot can deploy a GitHub repository (branch, tag or commit) instead of a ZIP file. This agent needs
**Git for Windows** (the installer tries to install it with winget).

Public repositories need nothing else. For **private** repositories, store a token on this machine
(never in Telegram). Create a fine-grained personal access token on GitHub limited to the repositories
you deploy, with *Contents: Read-only* and an expiry date, then run as Administrator:

```cmd
scripts\\set_git_token.bat --alias default --verify owner/private-repo
```

In Telegram choose *GitHub repository -> Private* and send the credential name (`default`).
Optional: add `"git": {{"allowed_repos": ["owner/*"]}}` to `config\\settings.json` to restrict which repositories this agent may deploy.

## Notes

- IIS actions require Administrator permissions.
- The package contains a unique agent token. Do not share it publicly.
- The installed Windows service name is `TelegramCICDAgent_{agent_id}`.
- Logs are written under `logs\\agent_stdout.log` and `logs\\agent_stderr.log` when NSSM is used.

Generated at {datetime.now(timezone.utc).isoformat()}.
""", encoding='utf-8')
