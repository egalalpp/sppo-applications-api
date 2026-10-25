"""Работа оператора комиссии: очередь заявок, решение (одобрить / отклонить с причиной),
ручные проверки и перезапуск распознавания.

Распознавание (OCR) — подсказка оператору, а не железобетон: если оно упало, заявку всё равно
можно открыть, фото и форма на месте, а распознавание можно перезапустить кнопкой.
"""

from typing import Literal

from fastapi import APIRouter, Depends, Query
from pymongo import ReturnDocument

from app import audit, db
from app.deps import require_staff
from app.domain import (
    BLOCKING_CHECK_STATUSES,
    STAFF_TRANSITIONS,
    AppStatus,
    OcrState,
    can_transition,
)
from app.errors import ApiError, responses
from app.schemas import (
    ApplicationOut,
    ApplicationSummary,
    CheckDecisionIn,
    CheckOut,
    OcrRestartOut,
    Page,
    TransitionIn,
)
from app.serializers import SUMMARY_PROJECTION, application_out, application_summary, ocr_out
from app.services import applications as svc
from app.services import benefit_types, notifications, transitions
from app.services.ocr_queue import BrokerUnavailable, enqueue_ocr

router = APIRouter(tags=["operator"])

VIEW = {"response_model_exclude_unset": True}


def _coll():
    return db.get_db()[db.APPLICATIONS]


@router.get("/operator/queue", response_model=Page[ApplicationSummary],
            summary="Очередь оператора: по приоритету основания, затем по времени подачи",
            description="Сначала заявки с меньшим `priority` (выше приоритет), внутри одного приоритета — те, что поданы "
                        "раньше. В каждой заявке — ocr_state (ready_for_review, ocr_failed...) и ocr_accuracy, %. "
                        "Заявки с ocr_failed тоже открываются: распознавание можно перезапустить.", **VIEW)
async def operator_queue(
    user: dict = Depends(require_staff),
    status: Literal["pending", "in_review"] = "pending",
    ocr_state: OcrState | None = None,
    basis: str | None = None,
    period: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {"status": status}
    if ocr_state:
        query["ocr.state"] = ocr_state
    if basis:
        query["basis"] = basis
    if period:
        query["period"] = period
    # Индекс {status, priority, submitted_at} покрывает и фильтр по статусу, и сортировку.
    cursor = _coll().find(query, SUMMARY_PROJECTION).sort([("priority", 1), ("submitted_at", 1)]).skip(offset).limit(limit)
    items = [application_summary(d, staff=True) async for d in cursor]
    total = await _coll().count_documents(query)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/applications/{app_id}/transitions", response_model=ApplicationOut,
             responses=responses(404, 409, 422), summary="Сменить статус заявки (оператор / админ)",
             description="pending -> in_review (взять в работу), in_review -> approved | rejected | pending. "
                         "При to=rejected обязательно rejection_reason: его увидит студент. "
                         "О переходе в approved и rejected студенту уходит письмо.", **VIEW)
async def transition(app_id: str, body: TransitionIn, user: dict = Depends(require_staff)):
    doc = await svc.load(app_id, user)
    current, target = doc["status"], body.to

    if not can_transition(current, target, user["role"]):
        allowed = sorted(t for (f, t) in STAFF_TRANSITIONS if f == current)
        raise ApiError(409, f"Переход {current} -> {target} недопустим", extra={"allowed": allowed})
    if target in (AppStatus.in_review, AppStatus.approved) and svc.ocr_busy(doc):
        # Иначе воркер перезапишет проверки, которые оператор уже начал разбирать.
        raise ApiError(409, "Идёт автоматическая проверка, повторите через минуту")
    if target == AppStatus.approved:
        blocking = [c["code"] for c in doc.get("checks", []) if c["status"] in BLOCKING_CHECK_STATUSES]
        if blocking:
            raise ApiError(409, "Есть нерешённые проверки", extra={"blocking_checks": blocking})

    rejected = target == AppStatus.rejected
    updated = await transitions.apply_transition(
        doc, to=target, actor=user, expected_from=current,
        event_type=audit.TRANSITION_EVENTS[(current, target)],
        set_fields={"rejection_reason": body.rejection_reason if rejected else None},
        # В историю, которую видит студент, попадает только причина отказа
        comment=body.rejection_reason if rejected else body.comment,
        details={"rejection_reason": body.rejection_reason, "comment": body.comment},
    )
    if updated is None:
        raise ApiError(409, "Статус заявки изменился, обновите данные")

    await svc.invalidate_status(doc["_id"])
    benefit = await benefit_types.get(doc["basis"])
    await notifications.notify_status(updated, target, (benefit or {}).get("title", doc["basis"]))
    return application_out(updated, staff=True)


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

    await audit.log(user, audit.CHECK_SET, "application", app_id,
                    {"code": code, "status": body.status, "comment": body.comment})
    return next(c for c in updated["checks"] if c["code"] == code)


@router.post("/applications/{app_id}/ocr/restart", response_model=OcrRestartOut, status_code=202,
             responses=responses(404, 409, 503), summary="Перезапустить распознавание (оператор / админ)",
             description="Заново распознаёт все вложения заявки и пересчитывает автоматические проверки; "
                         "решения оператора по проверкам сохраняются. Доступно для заявок на рассмотрении, "
                         "пока распознавание не идёт (или завис дольше OCR_STALE_MINUTES).")
async def restart_ocr(app_id: str, user: dict = Depends(require_staff)):
    doc = await svc.load(app_id, user)
    if doc["status"] not in (AppStatus.pending, AppStatus.in_review):
        raise ApiError(409, f"Для заявки в статусе {doc['status']} распознавание не запускается")
    if svc.ocr_busy(doc):
        raise ApiError(409, "Распознавание уже идёт")

    try:
        # Состояние занимается атомарно (условие на прежнее состояние): двойное нажатие кнопки не создаст две задачи
        task_id = await enqueue_ocr(doc, actor=user, event_type=audit.OCR_RESTARTED)
    except BrokerUnavailable:
        raise ApiError(503, "Очередь недоступна, повторите позже", headers={"Retry-After": "30"}) from None
    if task_id is None:
        raise ApiError(409, "Распознавание уже идёт")

    fresh = await _coll().find_one({"_id": doc["_id"]}, {"ocr": 1})
    return {"id": app_id, "ocr": ocr_out(fresh)}
