@echo off
cd /d "%~dp0.."
if not exist .venv py -3.12 -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
python -u -m backend.main
