@echo off
cd /d "%~dp0.."
powershell -ExecutionPolicy Bypass -File "%~dp0install_backend_service.ps1"
pause
