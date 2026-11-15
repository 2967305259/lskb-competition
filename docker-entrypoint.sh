#!/bin/bash
set -euo pipefail
export FLASK_ENV=${FLASK_ENV:-production}
# Preparation is one process, before any worker imports the runtime application.
python -m flask --app 'app:create_app()' prepare
if [ "$FLASK_ENV" = production ]; then
    exec gunicorn --bind "0.0.0.0:${PORT:-19198}" --workers "${WEB_WORKERS:-4}" \
        --timeout 120 --access-logfile - --error-logfile - wsgi:app
fi
exec python run.py
