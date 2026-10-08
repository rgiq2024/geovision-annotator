@echo off
REM Start GeoVision Annotator on http://localhost:8000
cd /d %~dp0
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
pip install -q -r requirements.txt
uvicorn app:app --app-dir backend --host 0.0.0.0 --port 8000
