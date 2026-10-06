param([string]$AgentToken="LOCAL_TEST_123")
Write-Host "Preparing one-laptop test..."
if (!(Test-Path .\config\settings.json)) { Copy-Item .\config\settings.example.json .\config\settings.json }
python -m pip install --upgrade pip
pip install -r requirements.txt
New-Item -ItemType Directory -Force -Path .\storage, .\data | Out-Null
icacls (Resolve-Path .).Path /grant "IIS_IUSRS:(OI)(CI)RX" /T | Out-Null
Write-Host "Done. Edit config\settings.json and add your Telegram bot token."
Write-Host "Agent token for Telegram registration: $AgentToken"
