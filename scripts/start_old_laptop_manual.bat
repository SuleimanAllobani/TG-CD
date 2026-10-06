@echo off
cd /d "%~dp0.."
start "CICD Phase2.5 Backend" cmd /k "cd /d %CD% && .venv\Scripts\activate.bat && set CICD_BACKEND_HOST=0.0.0.0 && python -u -m backend.main"
start "CICD Telegram Bot" cmd /k "cd /d %CD% && .venv\Scripts\activate.bat && python -u -m bot.main"
echo Started backend and bot in separate windows.
echo Tailscale mode does not need localhost.run.
echo Make sure config\settings.json has backend.public_url = http://OLD-LAPTOP-TAILSCALE-IP:9000
echo Find old laptop Tailscale IP with: tailscale ip -4
pause
