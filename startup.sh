#!/usr/bin/env bash
# Azure App Service startup (legacy single-instance deploy).
set -e
python -m alembic upgrade head
python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
