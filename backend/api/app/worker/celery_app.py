"""Celery: брокер и бэкенд результатов — Redis (db 1 и db 2).

Две очереди: applications (распознавание и проверки заявок, долгие задачи) и mail (письма),
чтобы уведомление не ждало за долгим OCR. Воркер слушает обе: -Q applications,mail.

Число параллельных обработчиков (c из модели M/M/c/K в PD.ipynb) задаётся
переменной CELERY_CONCURRENCY и/или числом реплик сервиса worker.
"""

from celery import Celery

from app.config import get_settings

settings = get_settings()

PROCESS_APPLICATION = "applications.process"
SEND_EMAIL = "notifications.send_email"

TASK_ROUTES = {
    PROCESS_APPLICATION: {"queue": settings.celery_queue},
    SEND_EMAIL: {"queue": settings.celery_mail_queue},
}

celery_app = Celery(
    "sppo",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.worker.tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.celery_queue,
    task_routes=TASK_ROUTES,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,  # задача подтверждается после выполнения: не теряется при падении воркера
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,  # длинные задачи: не забираем лишнее из очереди
    task_track_started=True,
    result_expires=24 * 3600,
    broker_connection_retry_on_startup=True,
    timezone="Europe/Moscow",
)

# API только ставит задачи. У этого экземпляра нет бэкенда результатов: иначе Redis-бэкенд
# подписывался бы на канал результата каждой отправленной задачи. Состояние задачи
# API читает через celery_app (GET /tasks/{id}) или из поля ocr в MongoDB.
producer_app = Celery("sppo-producer", broker=settings.celery_broker_url, set_as_current=False)
producer_app.conf.update(
    task_default_queue=settings.celery_queue,
    task_routes=TASK_ROUTES,
    task_serializer="json",
    accept_content=["json"],
    broker_connection_retry_on_startup=True,
    # Если брокер недоступен, отправка задачи должна провалиться за секунды, а не подвесить запрос студента:
    # заявку всё равно подхватит подметальщик (services/ocr_queue.py).
    broker_connection_timeout=2,
    task_publish_retry=True,
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.5},
    broker_transport_options={"socket_connect_timeout": 2, "socket_timeout": 3},
)
