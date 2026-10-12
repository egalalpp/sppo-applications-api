"""Заявки: создание и редактирование студентом, отправка в обработку, статус."""

from fastapi import APIRouter, Depends, Header, Query, Response
from pymongo import ReturnDocument
from starlette.concurrency import run_in_threadpool

from app import cache, db, storage
from app.config import get_settings
from app.deps import get_current_user, is_staff, require_student
from app.domain import EDITABLE_STATUSES, AppStatus, ProcessingState, can_transition
from app.errors import ApiError, responses
from app.schemas import (
    ApplicationCreate,
    ApplicationOut,
    ApplicationSummary,
    ApplicationUpdate,
    CheckOut,
    HistoryItem,
    Page,
    StatusOut,
    SubmitOut,
)
from app.serializers import SUMMARY_PROJECTION, application_out, application_summary, status_out
from app.services import applications as svc
from app.services import benefit_types

router = APIRouter(prefix="/applications", tags=["applications"])

SCHEMA_VERSION = 3  # v3: добавлены applicant_user_id и processing (см. schema_zayavok_sppo.md)
IDEMPOTENCY_TTL_SEC = 24 * 3600
# Маркер «запрос в работе» живёт недолго: если реплика упадёт посреди запроса,
# ключ не заблокируется на сутки.
IDEMPOTENCY_LOCK_SEC = 60
_PROCESSING = "processing"


def _coll():
    return db.get_db()[db.APPLICATIONS]


@router.post("", response_model=ApplicationOut, status_code=201, responses=responses(409, 422),
             summary="Создать заявку (черновик)")
async def create_application(
    body: ApplicationCreate,
    response: Response,
    user: dict = Depends(require_student),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=100,
                                         description="Повтор запроса с тем же ключом вернёт ту же заявку"),
):
    redis_key = cache.idempotency_key(str(user["_id"]), idempotency_key) if idempotency_key else None
    if redis_key:
        existing = await cache.cache().get(redis_key)
        if existing == _PROCESSING:
            raise ApiError(409, "Запрос с этим Idempotency-Key ещё обрабатывается")
        if existing:
            response.status_code = 200
            return application_out(await svc.load(existing, user))
        if not await cache.cache().set(redis_key, _PROCESSING, nx=True, ex=IDEMPOTENCY_LOCK_SEC):
            raise ApiError(409, "Запрос с этим Idempotency-Key ещё обрабатывается")

    try:
        doc = await _insert_application(body, user)
    except BaseException:  # включая отмену запроса (CancelledError)
        if redis_key:
            await cache.delete(redis_key)
        raise
    if redis_key:
        await cache.cache().set(redis_key, str(doc["_id"]), ex=IDEMPOTENCY_TTL_SEC)
    return application_out(doc)


async def _insert_application(body: ApplicationCreate, user: dict) -> dict:
    benefit_type = await benefit_types.get(body.basis)
    if benefit_type is None or not benefit_type["is_active"]:
        raise ApiError(422, f"Неизвестное или отключённое основание: {body.basis}")

    period = body.period or get_settings().current_period
    duplicate = await _coll().find_one(
        {"applicant_user_id": user["_id"], "basis": body.basis, "period": period,
         "status": {"$ne": AppStatus.rejected}},
        {"_id": 1},
    )
    if duplicate:
        raise ApiError(409, "Заявка по этому основанию в этом периоде уже есть",
                       extra={"application_id": str(duplicate["_id"])})

    now = db.utcnow()
    doc = {
        "schema_version": SCHEMA_VERSION,
        "applicant_user_id": user["_id"],
        "period": period,
        "basis": body.basis,
        "student": body.student.model_dump(mode="json"),
        "payout": body.payout.model_dump(mode="json"),
        "form": body.form.model_dump(mode="json"),
        "attachments": [],
        "checks": [],
        "status": AppStatus.draft,
        "status_history": [svc.history_entry(AppStatus.draft, user)],
        "processing": {"state": ProcessingState.idle, "task_id": None, "updated_at": None, "error": None},
        "created_at": now,
        "updated_at": now,
    }
    result = await _coll().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


@router.get("", response_model=Page[ApplicationSummary], summary="Список заявок",
            description="Студент видит только свои заявки; модератор и админ — все, с фильтрами.")
async def list_applications(
    user: dict = Depends(get_current_user),
    status: AppStatus | None = None,
    basis: str | None = None,
    period: str | None = None,
    applicant_user_id: str | None = Query(default=None, description="Только для модератора/админа"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {}
    if not is_staff(user):
        query["applicant_user_id"] = user["_id"]
    elif applicant_user_id:
        query["applicant_user_id"] = db.parse_oid(applicant_user_id)
    if status:
        query["status"] = status
    if basis:
        query["basis"] = basis
    if period:
        query["period"] = period

    cursor = _coll().find(query, SUMMARY_PROJECTION).sort("created_at", -1).skip(offset).limit(limit)
    items = [application_summary(d) async for d in cursor]
    total = await _coll().count_documents(query)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{app_id}", response_model=ApplicationOut, responses=responses(404), summary="Заявка целиком")
async def get_application(app_id: str, user: dict = Depends(get_current_user)):
    return application_out(await svc.load(app_id, user))


@router.patch("/{app_id}", response_model=ApplicationOut, responses=responses(404, 409, 422),
              summary="Изменить черновик (или заявку, по которой запросили документы)")
async def update_application(app_id: str, body: ApplicationUpdate, user: dict = Depends(require_student)):
    changes = {
        field: getattr(body, field).model_dump(mode="json")
        for field in ("student", "payout", "form")
        if getattr(body, field) is not None
    }
    if not changes:
        raise ApiError(422, "Нет полей для изменения")

    doc = await svc.load(app_id, user, {"status": 1})
    updated = await _coll().find_one_and_update(
        {"_id": doc["_id"], "applicant_user_id": user["_id"], "status": {"$in": list(EDITABLE_STATUSES)}},
        {"$set": {**changes, "updated_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise ApiError(409, f"Заявку в статусе {doc['status']} менять нельзя")
    return application_out(updated)


@router.delete("/{app_id}", status_code=204, responses=responses(404, 409), summary="Удалить черновик")
async def delete_application(app_id: str, user: dict = Depends(require_student)):
    doc = await svc.load(app_id, user, {"status": 1, "attachments": 1})
    result = await _coll().delete_one(
        {"_id": doc["_id"], "applicant_user_id": user["_id"], "status": AppStatus.draft}
    )
    if result.deleted_count == 0:
        raise ApiError(409, "Удалить можно только черновик")
    for att in doc.get("attachments", []):
        await run_in_threadpool(storage.delete_object, att["storage"]["key"])
    await svc.invalidate_status(doc["_id"])
    return Response(status_code=204)


@router.post("/{app_id}/submit", response_model=SubmitOut, status_code=202,
             responses=responses(404, 409, 422, 503),
             summary="Отправить заявку на проверку (асинхронно, через Celery)")
async def submit_application(app_id: str, user: dict = Depends(require_student)):
    doc = await svc.load(app_id, user)
    current = doc["status"]
    if not can_transition(current, AppStatus.pending, user["role"]):
        raise ApiError(409, f"Заявку в статусе {current} отправить нельзя")
    if not doc["form"].get("consent_pd_signed"):
        raise ApiError(422, "Без согласия на обработку персональных данных заявку отправить нельзя")

    # Ограниченная ёмкость K из модели M/M/c/K: при переполнении — отказ с 503.
    settings = get_settings()
    if await cache.queue_length() >= settings.max_queue_length:
        raise ApiError(503, "Очередь обработки переполнена, повторите позже", headers={"Retry-After": "60"})

    now = db.utcnow()
    result = await _coll().update_one(
        {"_id": doc["_id"], "status": current},
        {
            "$set": {
                "status": AppStatus.pending,
                "processing": {"state": ProcessingState.queued, "task_id": None, "updated_at": now, "error": None},
                "attachments.$[a].ocr.status": "queued",
                "updated_at": now,
            },
            "$push": {"status_history": svc.history_entry(AppStatus.pending, user)},
        },
        array_filters=[{"a.ocr.status": {"$ne": "done"}}],
    )
    if result.modified_count == 0:
        raise ApiError(409, "Статус заявки изменился, обновите данные")

    try:
        task_id = await svc.enqueue_processing(doc["_id"])
    except Exception:
        # Брокер недоступен: откатываем заявку в прежний статус.
        await _coll().update_one(
            {"_id": doc["_id"], "status": AppStatus.pending},
            {
                "$set": {
                    "status": current,
                    "processing": doc.get("processing"),
                    "attachments.$[a].ocr.status": "not_started",
                },
                "$pop": {"status_history": 1},
            },
            array_filters=[{"a.ocr.status": "queued"}],
        )
        await svc.invalidate_status(doc["_id"])
        raise ApiError(503, "Очередь обработки недоступна, повторите позже", headers={"Retry-After": "30"}) from None

    await _coll().update_one(
        {"_id": doc["_id"], "processing.task_id": None}, {"$set": {"processing.task_id": task_id}}
    )
    await svc.invalidate_status(doc["_id"])
    return {
        "id": app_id,
        "status": AppStatus.pending,
        "task_id": task_id,
        "status_url": f"/api/v1/applications/{app_id}/status",
    }


@router.get("/{app_id}/status", response_model=StatusOut, responses=responses(404),
            summary="Статус заявки (кэшируется в Redis на несколько секунд)")
async def get_status(app_id: str, response: Response, user: dict = Depends(get_current_user)):
    key = cache.app_status_key(app_id)
    cached = await cache.get_json(key)
    if cached and (is_staff(user) or cached["owner"] == str(user["_id"])):
        response.headers["X-Cache"] = "HIT"
        return cached["data"]

    doc = await svc.load(app_id, user, {"status": 1, "processing": 1, "updated_at": 1, "applicant_user_id": 1})
    data = status_out(doc)
    # Пока воркер работает, статус меняется быстро: держим кэш 1 с, чтобы не залипало
    # устаревшее значение, если воркер сбросил ключ между нашим чтением и записью.
    in_flight = data["processing"]["state"] in (ProcessingState.queued, ProcessingState.running)
    ttl = 1 if in_flight else get_settings().status_cache_ttl_sec
    await cache.set_json(key, {"owner": str(doc["applicant_user_id"]), "data": data}, ttl)
    response.headers["X-Cache"] = "MISS"
    return data


@router.get("/{app_id}/history", response_model=list[HistoryItem], responses=responses(404),
            summary="История смены статусов")
async def get_history(app_id: str, user: dict = Depends(get_current_user)):
    doc = await svc.load(app_id, user, {"status_history": 1})
    return doc.get("status_history", [])


@router.get("/{app_id}/checks", response_model=list[CheckOut], responses=responses(404),
            summary="Результаты проверок")
async def get_checks(app_id: str, user: dict = Depends(get_current_user)):
    doc = await svc.load(app_id, user, {"checks": 1})
    return doc.get("checks", [])
