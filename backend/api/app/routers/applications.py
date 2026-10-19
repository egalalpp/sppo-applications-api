"""Заявки: создание и редактирование студентом, отправка на рассмотрение, статус.

Студент видит только данные заявки и её статус (с причиной отказа). Распознавание (OCR),
проверки и их результаты — внутренняя кухня оператора: в ответах студенту этих полей нет.
"""

from fastapi import APIRouter, Depends, Header, Query, Response
from pymongo import ReturnDocument
from starlette.concurrency import run_in_threadpool

from app import cache, db, storage
from app.config import get_settings
from app.deps import get_current_user, is_staff, require_staff, require_student
from app.domain import EDITABLE_STATUSES, OCR_IN_FLIGHT, AppStatus, OcrState, can_transition
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
from app.serializers import (
    STATUS_PROJECTION,
    SUMMARY_PROJECTION,
    application_out,
    application_summary,
    status_out,
)
from app.services import applications as svc
from app.services import benefit_types, notifications
from app.services.checks import missing_slots

router = APIRouter(prefix="/applications", tags=["applications"])

SCHEMA_VERSION = 4  # v4: роль operator, статусы без need_more_docs, rejection_reason, ocr вместо processing
IDEMPOTENCY_TTL_SEC = 24 * 3600
# Маркер «запрос в работе» живёт недолго: если реплика упадёт посреди запроса,
# ключ не заблокируется на сутки.
IDEMPOTENCY_LOCK_SEC = 60
_IN_PROGRESS = "in_progress"

# Ответы без ключей, которые не были заданы: так студенту не приходят пустые ocr/checks.
VIEW = {"response_model_exclude_unset": True}


def _coll():
    return db.get_db()[db.APPLICATIONS]


def initial_ocr() -> dict:
    return {"state": OcrState.not_started, "accuracy": None, "engine": None, "task_id": None,
            "updated_at": None, "error": None}


@router.post("", response_model=ApplicationOut, status_code=201, responses=responses(409, 422),
             summary="Создать заявку (черновик)", **VIEW)
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
        if existing == _IN_PROGRESS:
            raise ApiError(409, "Запрос с этим Idempotency-Key ещё обрабатывается")
        if existing:
            response.status_code = 200
            return application_out(await svc.load(existing, user))
        if not await cache.cache().set(redis_key, _IN_PROGRESS, nx=True, ex=IDEMPOTENCY_LOCK_SEC):
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
    student = body.student.model_dump(mode="json", exclude={"full_name_as_written"})
    doc = {
        "schema_version": SCHEMA_VERSION,
        "applicant_user_id": user["_id"],
        "period": period,
        "basis": body.basis,
        "student": student,
        "payout": body.payout.model_dump(mode="json"),
        "form": body.form.model_dump(mode="json"),
        "attachments": [],
        "checks": [],
        "status": AppStatus.draft,
        "rejection_reason": None,
        "status_history": [svc.history_entry(AppStatus.draft, user)],
        "ocr": initial_ocr(),
        "created_at": now,
        "updated_at": now,
    }
    result = await _coll().insert_one(doc)
    doc["_id"] = result.inserted_id
    return doc


@router.get("", response_model=Page[ApplicationSummary], summary="Список заявок",
            description="Студент видит только свои заявки; оператор и админ — все, с фильтрами.", **VIEW)
async def list_applications(
    user: dict = Depends(get_current_user),
    status: AppStatus | None = None,
    basis: str | None = None,
    period: str | None = None,
    applicant_user_id: str | None = Query(default=None, description="Только для оператора и админа"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {}
    staff = is_staff(user)
    if not staff:
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
    items = [application_summary(d, staff) async for d in cursor]
    total = await _coll().count_documents(query)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/{app_id}", response_model=ApplicationOut, responses=responses(404), summary="Заявка целиком", **VIEW)
async def get_application(app_id: str, user: dict = Depends(get_current_user)):
    return application_out(await svc.load(app_id, user), is_staff(user))


@router.patch("/{app_id}", response_model=ApplicationOut, responses=responses(404, 409, 422),
              summary="Изменить черновик", **VIEW)
async def update_application(app_id: str, body: ApplicationUpdate, user: dict = Depends(require_student)):
    changes = {}
    for field in ("student", "payout", "form"):
        part = getattr(body, field)
        if part is not None:
            changes[field] = part.model_dump(mode="json", exclude={"full_name_as_written"} if field == "student" else None)
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
             summary="Отправить заявку на рассмотрение",
             description="Доступно, когда заполнена форма, есть согласие на обработку ПДн и приложены все документы "
                         "из списка для выбранного основания (в интерфейсе в этот момент разблокируется кнопка). "
                         "Ответ 202: заявка принята, о результате студент узнает по статусу и из письма.")
async def submit_application(app_id: str, user: dict = Depends(require_student)):
    doc = await svc.load(app_id, user)
    current = doc["status"]
    if not can_transition(current, AppStatus.pending, user["role"]):
        raise ApiError(409, f"Заявку в статусе {current} отправить нельзя")
    if not doc["form"].get("consent_pd_signed"):
        raise ApiError(422, "Без согласия на обработку персональных данных заявку отправить нельзя")

    benefit_type = await benefit_types.get(doc["basis"])
    if benefit_type is None:
        raise ApiError(422, "Основание заявки больше не существует")
    missing = missing_slots(doc.get("attachments", []), benefit_type)
    if missing:
        raise ApiError(422, "Приложены не все обязательные документы", extra={"missing_slots": missing})

    # Ограниченная ёмкость K из модели M/M/c/K: при переполнении — отказ с 503.
    settings = get_settings()
    if await cache.queue_length() >= settings.max_queue_length:
        raise ApiError(503, "Сервис перегружен, повторите позже", headers={"Retry-After": "60"})

    now = db.utcnow()
    result = await _coll().update_one(
        {"_id": doc["_id"], "status": current},
        {
            "$set": {
                "status": AppStatus.pending,
                "ocr": {**initial_ocr(), "state": OcrState.queued, "updated_at": now},
                "attachments.$[].ocr.status": "queued",
                "updated_at": now,
            },
            "$push": {"status_history": svc.history_entry(AppStatus.pending, user)},
        },
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
                "$set": {"status": current, "ocr": doc.get("ocr") or initial_ocr(),
                         "attachments.$[a].ocr.status": "not_started"},
                "$pop": {"status_history": 1},
            },
            array_filters=[{"a.ocr.status": "queued"}],
        )
        await svc.invalidate_status(doc["_id"])
        raise ApiError(503, "Сервис временно недоступен, повторите позже", headers={"Retry-After": "30"}) from None

    await _coll().update_one({"_id": doc["_id"], "ocr.task_id": None}, {"$set": {"ocr.task_id": task_id}})
    await svc.invalidate_status(doc["_id"])
    await notifications.notify_status({**doc, "status": AppStatus.pending}, AppStatus.pending,
                                      benefit_type.get("title", doc["basis"]))
    return {"id": app_id, "status": AppStatus.pending}


@router.get("/{app_id}/status", response_model=StatusOut, responses=responses(404),
            summary="Статус заявки (кэшируется в Redis на несколько секунд)", **VIEW)
async def get_status(app_id: str, response: Response, user: dict = Depends(get_current_user)):
    staff = is_staff(user)
    key = cache.app_status_key(app_id, staff)
    cached = await cache.get_json(key)
    if cached and (staff or cached["owner"] == str(user["_id"])):
        response.headers["X-Cache"] = "HIT"
        return cached["data"]

    doc = await svc.load(app_id, user, STATUS_PROJECTION)
    data = status_out(doc, staff)
    # Пока воркер работает, состояние меняется быстро: держим кэш 1 с, чтобы не залипало
    # устаревшее значение, если воркер сбросил ключ между нашим чтением и записью.
    in_flight = (doc.get("ocr") or {}).get("state") in OCR_IN_FLIGHT
    ttl = 1 if in_flight else get_settings().status_cache_ttl_sec
    await cache.set_json(key, {"owner": str(doc["applicant_user_id"]), "data": data}, ttl)
    response.headers["X-Cache"] = "MISS"
    return data


@router.get("/{app_id}/history", response_model=list[HistoryItem], responses=responses(404),
            summary="История смены статусов", **VIEW)
async def get_history(app_id: str, user: dict = Depends(get_current_user)):
    doc = await svc.load(app_id, user, {"status_history": 1})
    history = doc.get("status_history", [])
    if is_staff(user):
        return history
    # Студенту: когда и в какой статус перешла заявка; из комментариев — только причина отказа.
    return [
        {"status": h["status"], "at": h["at"], "by_role": h.get("by_role"),
         **({"comment": h["comment"]} if h["status"] == AppStatus.rejected and h.get("comment") else {})}
        for h in history
    ]


@router.get("/{app_id}/checks", response_model=list[CheckOut], responses=responses(403, 404),
            summary="Результаты автоматических и ручных проверок (оператор и админ)")
async def get_checks(app_id: str, user: dict = Depends(require_staff)):
    doc = await svc.load(app_id, user, {"checks": 1})
    return doc.get("checks", [])
