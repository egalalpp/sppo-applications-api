"""Смена статуса заявки: условие на текущий статус, запись в историю и в журнал аудита — атомарно."""

from pymongo import ReturnDocument

from app import audit, db
from app.services.applications import history_entry


async def apply_transition(doc: dict, *, to: str, actor: dict, event_type: str, expected_from: str | None = None,
                           set_fields: dict | None = None, comment: str | None = None,
                           details: dict | None = None) -> dict | None:
    """Переводит заявку в статус to. Возвращает обновлённый документ или None, если статус уже изменился.

    Обновление идёт с условием на текущий статус (findOneAndUpdate), поэтому из двух одновременных
    переходов выполнится один: второй получит None (роутер ответит 409). Статус, история и событие аудита
    пишутся в одной транзакции (если MongoDB — replica set, см. db.run_in_transaction): события аудита
    не бывает без перехода, и перехода без события тоже.
    """
    expected = expected_from or doc["status"]

    async def work(session):
        updated = await db.get_db()[db.APPLICATIONS].find_one_and_update(
            {"_id": doc["_id"], "status": expected},
            {
                "$set": {"status": to, "updated_at": db.utcnow(), **(set_fields or {})},
                "$push": {"status_history": history_entry(to, actor, comment)},
            },
            return_document=ReturnDocument.AFTER,
            session=session,
        )
        if updated is None:
            return None
        await audit.log(actor, event_type, "application", doc["_id"],
                        {"from": expected, "to": to, **(details or {})}, session=session)
        return updated

    return await db.run_in_transaction(work)
