#!/bin/sh
# Точка входа контейнера. Режим выбирается переменной RUN_MODE:
#   api    — только веб-сервер (по умолчанию)
#   worker — только воркер Celery (OCR и отправка писем)
#   all    — всё в одном контейнере: инициализация БД, воркер в фоне и веб-сервер.
#            Нужен для бесплатных тарифов, где нет отдельных фоновых воркеров (Render free).
set -e

PORT="${PORT:-8000}"

start_api() {
  exec uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --proxy-headers --forwarded-allow-ips "*"
}

run_worker() {
  exec celery -A app.worker.celery_app:celery_app worker \
    -Q "${CELERY_QUEUES:-applications,mail}" \
    --loglevel=INFO \
    --pool="${CELERY_POOL:-prefork}" \
    --concurrency="${CELERY_CONCURRENCY:-4}"
}

case "${RUN_MODE:-api}" in
  api)
    start_api
    ;;
  worker)
    run_worker
    ;;
  all)
    # Идемпотентная инициализация: коллекции, индексы, справочник, демо-учётки, бакет.
    python -m app.bootstrap
    # Воркер перезапускается, если упадёт (например, из-за нехватки памяти на OCR).
    (
      while true; do
        celery -A app.worker.celery_app:celery_app worker \
          -Q "${CELERY_QUEUES:-applications,mail}" \
          --loglevel=INFO \
          --pool="${CELERY_POOL:-threads}" \
          --concurrency="${CELERY_CONCURRENCY:-2}" || true
        echo "celery worker exited, restarting in 5s" >&2
        sleep 5
      done
    ) &
    start_api
    ;;
  *)
    echo "Unknown RUN_MODE: $RUN_MODE (expected api, worker or all)" >&2
    exit 64
    ;;
esac
