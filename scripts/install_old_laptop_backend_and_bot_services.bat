@echo off
cd /d "%~dp0.."
echo Installing backend service...
powershell -ExecutionPolicy Bypass -File "%~dp0install_backend_service.ps1"
if errorlevel 1 pause && exit /b 1
echo.
echo Installing bot service...
powershell -ExecutionPolicy Bypass -File "%~dp0install_bot_service.ps1"
pause
