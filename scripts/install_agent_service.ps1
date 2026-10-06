param(
  [string]$ServiceName="TelegramCICDAgent",
  [string]$AgentToken="LOCAL_TEST_123",
  [int]$Port=8765
)
$ErrorActionPreference = "Stop"
$Root=(Resolve-Path .).Path
function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Run as Administrator." }
}
Assert-Admin
if (!(Get-Command py -ErrorAction SilentlyContinue)) { throw "Python Launcher 'py' was not found. Install Python 3.12 first." }
if (!(Test-Path ".venv")) { py -3.12 -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\pip.exe" install -r requirements.txt
New-Item -ItemType Directory -Force -Path ".\storage", ".\data" | Out-Null
icacls (Join-Path $Root "storage") /grant "IIS_IUSRS:(OI)(CI)RX" /T | Out-Null
if (-not (Get-NetFirewallRule -DisplayName "Telegram CICD Agent $Port" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "Telegram CICD Agent $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
}
$env:AGENT_TOKEN=$AgentToken
$env:AGENT_PORT="$Port"
& ".\.venv\Scripts\python.exe" -m agent.windows_service --service-name $ServiceName install
& ".\.venv\Scripts\python.exe" -m agent.windows_service --service-name $ServiceName start
Write-Host "Agent service installed and started: $ServiceName" -ForegroundColor Green
