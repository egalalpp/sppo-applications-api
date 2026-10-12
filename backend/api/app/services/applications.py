"""Общая логика работы с заявками для роутеров."""

from bson import ObjectId
from starlette.concurrency import run_in_threadpool

from app import cache, db
from app.deps import is_staff
from app.errors import ApiError
from app.worker.celery_app import PROCESS_APPLICATION, producer_app


async def load(app_id: str, user: dict, projection: dict | None = None) -> dict:
    """Загружает заявку с проверкой доступа.

    Студент видит только свои заявки. На чужую отвечаем 404, а не 403:
    так нельзя узнать, существует ли заявка с таким id.
    """
    oid = db.parse_oid(app_id)
    query: dict = {"_id": oid}
    if not is_staff(user):
        query["applicant_user_id"] = user["_id"]
    doc = await db.get_db()[db.APPLICATIONS].find_one(query, projection) if oid else None
    if doc is None:
        raise ApiError(404, "Заявка не найдена")
    return doc


def history_entry(status: str, user: dict, comment: str | None = None) -> dict:
    return {
        "status": status,
        "at": db.utcnow(),
        "by": str(user["_id"]),
        "by_role": user["role"],
        "comment": comment,
    }


async def invalidate_status(app_id: ObjectId | str) -> None:
    await cache.delete(cache.app_status_key(str(app_id)))


async def enqueue_processing(app_id: ObjectId) -> str:
    """Ставит заявку в очередь Celery. send_task синхронный, поэтому — в пуле потоков."""
    result = await run_in_threadpool(
        producer_app.send_task, PROCESS_APPLICATION, args=[str(app_id)]
    )
    return result.id
