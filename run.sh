#!/usr/bin/env bash
# Start GeoVision Annotator on http://localhost:8000
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip install -q -r requirements.txt
exec uvicorn app:app --app-dir backend --host 0.0.0.0 --port "${PORT:-8000}"
