#!/bin/sh
# Ponto de entrada do container. ROLE escolhe o papel do serviço (padrão: api).
set -e

case "${ROLE:-api}" in
  api)
    alembic upgrade head
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'
    ;;
  worker)
    exec celery -A app.celery_app worker --loglevel=info --concurrency="${WORKER_CONCURRENCY:-4}"
    ;;
  beat)
    exec celery -A app.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule
    ;;
  *)
    echo "ROLE inválido: '$ROLE' (use api, worker ou beat)" >&2
    exit 1
    ;;
esac
