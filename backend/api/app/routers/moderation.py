"""Работа модератора: очередь, смена статуса, ручные проверки."""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pymongo import ReturnDocument

from app import audit, db
from app.deps import require_staff
from app.domain import BLOCKING_CHECK_STATUSES, COMMENT_REQUIRED, STAFF_TRANSITIONS, AppStatus, ProcessingState, can_transition
from app.errors import ApiError, responses
from app.schemas import ApplicationOut, ApplicationSummary, CheckDecisionIn, CheckOut, Page, TransitionIn
from app.serializers import SUMMARY_PROJECTION, application_out, application_summary
from app.services import applications as svc

router = APIRouter(tags=["moderation"])


def _coll():
    return db.get_db()[db.APPLICATIONS]


@router.get("/moderation/queue", response_model=Page[ApplicationSummary],
            summary="Очередь модератора (FIFO: сначала старые заявки)")
async def moderation_queue(
    user: dict = Depends(require_staff),
    status: Literal["pending", "in_review"] = "pending",
    basis: str | None = None,
    period: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {"status": status}
    if basis:
        query["basis"] = basis
    if period:
        query["period"] = period
    # Индекс {status: 1, created_at: 1} из схемы покрывает и фильтр, и сортировку.
    cursor = _coll().find(query, SUMMARY_PROJECTION).sort("created_at", 1).skip(offset).limit(limit)
    items = [application_summary(d) async for d in cursor]
    total = await _coll().count_documents(query)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/applications/{app_id}/transitions", response_model=ApplicationOut,
             responses=responses(404, 409, 422), summary="Сменить статус заявки (модератор/админ)")
async def transition(app_id: str, body: TransitionIn, user: dict = Depends(require_staff)):
    doc = await svc.load(app_id, user)
    current, target = doc["status"], body.to

    if not can_transition(current, target, user["role"]):
        allowed = sorted(t for (f, t) in STAFF_TRANSITIONS if f == current)
        raise ApiError(409, f"Переход {current} -> {target} недопустим", extra={"allowed": allowed})
    if target in COMMENT_REQUIRED and not body.comment:
        raise ApiError(422, "Для этого статуса нужен комментарий для студента")
    processing_state = doc.get("processing", {}).get("state")
    if target == AppStatus.in_review and processing_state in (ProcessingState.queued, ProcessingState.running):
        # Иначе воркер перезапишет проверки, которые модератор уже начал разбирать.
        raise ApiError(409, "Автоматические проверки ещё выполняются")
    if target == AppStatus.approved:
        if processing_state != ProcessingState.done:
            raise ApiError(409, "Автоматические проверки ещё не завершены")
        blocking = [c["code"] for c in doc.get("checks", []) if c["status"] in BLOCKING_CHECK_STATUSES]
        if blocking:
            raise ApiError(409, "Есть нерешённые проверки", extra={"blocking_checks": blocking})

    updated = await _coll().find_one_and_update(
        {"_id": doc["_id"], "status": current},
        {
            "$set": {"status": target, "updated_at": db.utcnow()},
            "$push": {"status_history": svc.history_entry(target, user, body.comment)},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ApiError(409, "Статус заявки изменился, обновите данные")

    await audit.log(user, "application.transition", "application", app_id,
                    {"from": current, "to": target, "comment": body.comment})
    await svc.invalidate_status(doc["_id"])
    return application_out(updated)


@router.patch("/applications/{app_id}/checks/{code}", response_model=CheckOut,
              responses=responses(404, 409), summary="Ручное решение по проверке (manual_ok / manual_rejected)")
async def decide_check(app_id: str, code: str, body: CheckDecisionIn, user: dict = Depends(require_staff)):
    doc = await svc.load(app_id, user, {"status": 1, "checks": 1})
    if doc["status"] != AppStatus.in_review:
        raise ApiError(409, "Сначала возьмите заявку в работу (статус in_review)")
    if not any(c["code"] == code for c in doc.get("checks", [])):
        raise ApiError(404, f"Проверка {code} не найдена")

    now = db.utcnow()
    updated = await _coll().find_one_and_update(
        {"_id": doc["_id"], "status": AppStatus.in_review, "checks.code": code},
        {"$set": {
            "checks.$.status": body.status,
            "checks.$.by": str(user["_id"]),
            "checks.$.at": now,
            "checks.$.comment": body.comment,
            "updated_at": now,
        }},
        projection={"checks": 1},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ApiError(409, "Заявка изменилась, обновите данные")

    await audit.log(user, "application.check_decision", "application", app_id,
                    {"code": code, "status": body.status, "comment": body.comment})
    return next(c for c in updated["checks"] if c["code"] == code)
