"""Постановка заявок в очередь распознавания и «подметальщик».

Студента очередь не касается: отправка заявки никогда не зависит от брокера. Если задачу поставить
не удалось (Redis недоступен, очередь переполнена, воркер спит), заявка остаётся в `pending` с
ocr.state = not_started, а подметальщик через минуту-другую ставит её в очередь сам.
"""

import asyncio
import logging
from datetime import timedelta

from app import audit, cache, db
from app.config import get_settings
from app.domain import OCR_IN_FLIGHT, AppStatus, OcrState
from app.services import applications as svc

logger = logging.getLogger(__name__)

SWEEP_LOCK_KEY = "lock:ocr-sweeper"
OPEN_STATUSES = [AppStatus.pending, AppStatus.in_review]


class BrokerUnavailable(Exception):
    """Не удалось поставить задачу в очередь (брокер недоступен)."""


def initial_ocr() -> dict:
    return {"state": OcrState.not_started, "accuracy": None, "engine": None, "task_id": None,
            "updated_at": None, "error": None}


async def enqueue_ocr(app: dict, *, actor: dict, event_type: str = audit.OCR_QUEUED) -> str | None:
    """Ставит заявку в очередь распознавания. Возвращает task_id.

    None — заявку уже взял кто-то другой (состояние изменилось с момента чтения app).
    BrokerUnavailable — задача не поставлена; состояние возвращено прежним, подметальщик повторит.
    Состояние «queued» занимается атомарно до отправки задачи, поэтому два вызова не создадут две задачи.
    """
    coll = db.get_db()[db.APPLICATIONS]
    previous = app.get("ocr") or initial_ocr()
    now = db.utcnow()
    claimed = await coll.find_one_and_update(
        {
            "_id": app["_id"],
            "status": {"$in": OPEN_STATUSES},
            "ocr.state": previous.get("state", OcrState.not_started),
            "ocr.updated_at": previous.get("updated_at"),
        },
        {"$set": {
            "ocr.state": OcrState.queued, "ocr.error": None, "ocr.task_id": None, "ocr.updated_at": now,
            "attachments.$[].ocr.status": "queued", "updated_at": now,
        }},
        projection={"_id": 1},
    )
    if claimed is None:
        return None
    try:
        task_id = await svc.enqueue_processing(app["_id"])
    except Exception as exc:  # noqa: BLE001 — брокер недоступен, таймаут и т.п.
        await coll.update_one(
            {"_id": app["_id"], "ocr.state": OcrState.queued, "ocr.updated_at": now},
            {"$set": {"ocr": previous, "attachments.$[].ocr.status": "not_started"}},
        )
        await svc.invalidate_status(app["_id"])
        raise BrokerUnavailable(type(exc).__name__) from exc
    await coll.update_one({"_id": app["_id"], "ocr.updated_at": now}, {"$set": {"ocr.task_id": task_id}})
    await svc.invalidate_status(app["_id"])
    await audit.log(actor, event_type, "application", app["_id"],
                    {"task_id": task_id, "previous_state": previous.get("state")})
    return task_id


async def try_enqueue_ocr(app: dict) -> bool:
    """Для отправки заявки студентом: пытается поставить в очередь, но ничего не ломает при неудаче."""
    if not cache.queue_available():  # брокер недоступен: не ждём таймауты, подметальщик подхватит
        logger.warning("Queue is unavailable, application %s waits for the sweeper", app["_id"])
        return False
    try:
        if await cache.queue_length() >= get_settings().max_queue_length:
            logger.warning("OCR queue is full, application %s waits for the sweeper", app["_id"])
            return False
        return await enqueue_ocr(app, actor=audit.system_actor("api")) is not None
    except Exception:  # noqa: BLE001 — Redis или Celery недоступны
        logger.warning("Could not queue OCR for application %s, the sweeper will retry", app["_id"], exc_info=True)
        return False


async def sweep_once() -> int:
    """Один проход подметальщика. Возвращает число заявок, поставленных в очередь.

    Берёт заявки на рассмотрении, у которых распознавание не начиналось (не удалось поставить задачу)
    или «висит» дольше OCR_STALE_MINUTES (очередь потеряна: перезапуск Redis, воркер упал).
    Очередь не переполняется: ставится не больше, чем осталось места до MAX_QUEUE_LENGTH.
    """
    settings = get_settings()
    room = settings.max_queue_length - await cache.queue_length()
    if room <= 0:
        return 0
    stale_before = db.utcnow() - timedelta(minutes=settings.ocr_stale_minutes)
    query = {
        "status": {"$in": OPEN_STATUSES},
        "$or": [
            {"ocr.state": OcrState.not_started},
            {"ocr.state": {"$in": list(OCR_IN_FLIGHT)}, "ocr.updated_at": {"$lt": stale_before}},
        ],
    }
    cursor = db.get_db()[db.APPLICATIONS].find(query, {"status": 1, "ocr": 1}).sort("updated_at", 1).limit(
        min(settings.sweep_batch_size, room))
    queued = 0
    async for app in cursor:
        try:
            if await enqueue_ocr(app, actor=audit.system_actor("sweeper")) is not None:
                queued += 1
        except BrokerUnavailable:
            logger.warning("Sweeper: broker is unavailable, will retry later")
            break
    if queued:
        logger.info("Sweeper: queued %d application(s) for OCR", queued)
    return queued


async def run_sweeper() -> None:
    """Фоновая задача API: подметает раз в sweep_interval_sec. Замок в Redis не даёт нескольким
    репликам работать одновременно (но и без него повтор безопасен: заявка занимается атомарно)."""
    interval = get_settings().sweep_interval_sec
    while True:
        try:
            if await cache.cache().set(SWEEP_LOCK_KEY, "1", nx=True, ex=max(interval - 1, 1)):
                await sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — Redis или MongoDB недоступны: пробуем в следующий раз
            logger.warning("Sweeper iteration failed", exc_info=True)
        await asyncio.sleep(interval)
