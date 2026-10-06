@echo off
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -Command ".\.venv\Scripts\python.exe -m bot.windows_service --service-name TelegramCICDBot stop; .\.venv\Scripts\python.exe -m bot.windows_service --service-name TelegramCICDBot remove"
pause
