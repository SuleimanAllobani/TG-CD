$ErrorActionPreference = "Stop"
$ServiceName = "TelegramCICDBot"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Please run as Administrator." }
}
Assert-Admin
Set-Location $Root
if (!(Test-Path ".\config\settings.json")) {
  Copy-Item ".\config\settings.example.json" ".\config\settings.json"
  Write-Host "Edit config\settings.json and set telegram_bot_token/backend URL, then rerun." -ForegroundColor Yellow
  exit 1
}
if (!(Get-Command py -ErrorAction SilentlyContinue)) { throw "Python Launcher 'py' was not found. Install Python 3.12 first." }
if (!(Test-Path ".venv")) { py -3.12 -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\pip.exe" install -r requirements.txt
New-Item -ItemType Directory -Force -Path ".\data", ".\dist\generated_agents", ".\logs" | Out-Null
function Find-Nssm {
  $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @(
    (Join-Path (Join-Path $Root "tools") "nssm.exe"),
    (Join-Path (Join-Path $env:USERPROFILE "Downloads") "nssm.exe"),
    "C:/nssm/nssm.exe",
    "C:/nssm/win64/nssm.exe"
  )
  foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
  return $null
}
$NssmPath = Find-Nssm
if (!$NssmPath -and (Get-Command winget.exe -ErrorAction SilentlyContinue)) {
  Write-Host "NSSM was not found. Trying winget install NSSM.NSSM..." -ForegroundColor Yellow
  try { winget install --id NSSM.NSSM -e --silent --accept-source-agreements --accept-package-agreements | Out-Host } catch { Write-Host $_ -ForegroundColor Yellow }
  $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH", "User")
  $NssmPath = Find-Nssm
}
if (!$NssmPath) { throw "NSSM was not found. Put nssm.exe in tools\\nssm.exe, Downloads, C:\\nssm\\nssm.exe, or PATH, then rerun." }
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
  & $NssmPath stop $ServiceName 2>$null | Out-Null
  Start-Sleep -Seconds 2
  & $NssmPath remove $ServiceName confirm 2>$null | Out-Null
}
& $NssmPath install $ServiceName (Join-Path $Root ".venv\Scripts\python.exe") "-u -m bot.main"
& $NssmPath set $ServiceName AppDirectory $Root
& $NssmPath set $ServiceName AppStdout (Join-Path $Root "logs\bot_stdout.log")
& $NssmPath set $ServiceName AppStderr (Join-Path $Root "logs\bot_stderr.log")
& $NssmPath set $ServiceName AppExit Default Restart
& $NssmPath set $ServiceName AppRestartDelay 5000
Write-Host "If Telegram requires VPN, set this service to run under your Windows user in services.msc > Log On." -ForegroundColor Yellow
& $NssmPath start $ServiceName
Write-Host "Bot service installed and started: $ServiceName" -ForegroundColor Green
