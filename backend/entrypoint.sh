#!/bin/sh
set -e

echo "[entrypoint] waiting for database..."
python - <<'EOF'
import os, sys, time
import psycopg
host = os.environ.get("POSTGRES_HOST", "db")
port = os.environ.get("POSTGRES_PORT", "5432")
db = os.environ.get("POSTGRES_DB", "vallotton")
user = os.environ.get("POSTGRES_USER", "vallotton")
pw = os.environ.get("POSTGRES_PASSWORD", "")
for i in range(60):
    try:
        psycopg.connect(host=host, port=port, dbname=db, user=user, password=pw,
                        connect_timeout=3).close()
        sys.exit(0)
    except Exception as e:
        print(f"  db not ready ({e}); retry {i+1}/60")
        time.sleep(2)
sys.exit(1)
EOF

echo "[entrypoint] applying migrations..."
python manage.py migrate --noinput

echo "[entrypoint] collecting static files..."
python manage.py collectstatic --noinput

echo "[entrypoint] provisioning accounts from .env..."
python manage.py bootstrap_users

echo "[entrypoint] starting gunicorn..."
# One worker, on purpose.
#
# Generation runs in threads inside this process, and so does the sweeper that
# decides what a continuous run should do next. Two worker processes means two
# schedulers, each with its own view of what is running, and eventually one
# acts on the other's work. Threads give this app the concurrency it actually
# needs — it is one person, waiting on network calls — without that second view
# of the world.
exec gunicorn vallotton.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 1 \
    --threads 16 \
    --timeout 900 \
    --access-logfile - \
    --error-logfile -
