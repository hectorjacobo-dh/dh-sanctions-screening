#!/bin/bash
export BIND_HOST=0.0.0.0
export SINGLE_USER_MODE=false
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
