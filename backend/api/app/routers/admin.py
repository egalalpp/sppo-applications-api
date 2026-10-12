"""Администрирование: пользователи и роли, справочник оснований, журнал, статистика."""

import re

from fastapi import APIRouter, Depends, Query, Response
from pymongo import ReturnDocument

from app import audit, cache, db
from app.config import get_settings
from app.deps import require_admin
from app.domain import Role
from app.errors import ApiError, responses
from app.schemas import (
    AuditEventOut,
    BenefitTypeBase,
    BenefitTypeIn,
    BenefitTypeOut,
    Page,
    StatsOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.serializers import audit_out, benefit_type_out, user_out
from app.services import benefit_types
from app.services.users import create_user

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _users():
    return db.get_db()[db.USERS]


# ---------- Пользователи ----------

@router.get("/users", response_model=Page[UserOut], summary="Список пользователей")
async def list_users(
    role: Role | None = None,
    is_active: bool | None = None,
    q: str | None = Query(default=None, max_length=100, description="Поиск по email или ФИО"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {}
    if role:
        query["role"] = role
    if is_active is not None:
        query["is_active"] = is_active
    if q:
        pattern = {"$regex": re.escape(q), "$options": "i"}
        query["$or"] = [{"email": pattern}, {"full_name": pattern}]
    cursor = _users().find(query).sort("created_at", -1).skip(offset).limit(limit)
    items = [user_out(u) async for u in cursor]
    return {"items": items, "total": await _users().count_documents(query), "limit": limit, "offset": offset}


@router.post("/users", response_model=UserOut, status_code=201, responses=responses(409, 422),
             summary="Создать пользователя с любой ролью (например, модератора)")
async def create_user_by_admin(body: UserCreate, admin: dict = Depends(require_admin)):
    user = await create_user(body.email, body.password, body.full_name, body.role, created_by=admin["_id"])
    await audit.log(admin, "user.create", "user", user["_id"], {"email": user["email"], "role": user["role"]})
    return user_out(user)


@router.get("/users/{user_id}", response_model=UserOut, responses=responses(404), summary="Пользователь")
async def get_user(user_id: str):
    oid = db.parse_oid(user_id)
    user = await _users().find_one({"_id": oid}) if oid else None
    if user is None:
        raise ApiError(404, "Пользователь не найден")
    return user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut, responses=responses(404, 409, 422),
              summary="Сменить роль, заблокировать или переименовать пользователя")
async def update_user(user_id: str, body: UserUpdate, admin: dict = Depends(require_admin)):
    oid = db.parse_oid(user_id)
    target = await _users().find_one({"_id": oid}) if oid else None
    if target is None:
        raise ApiError(404, "Пользователь не найден")
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise ApiError(422, "Нет полей для изменения")

    loses_admin = target["role"] == Role.admin and (
        changes.get("role", Role.admin) != Role.admin or changes.get("is_active") is False
    )
    # Понизить админа может только другой активный админ, а себя — нельзя,
    # поэтому в системе всегда остаётся хотя бы один администратор.
    if loses_admin and target["_id"] == admin["_id"]:
        raise ApiError(409, "Нельзя понизить или заблокировать самого себя")

    updated = await _users().find_one_and_update(
        {"_id": target["_id"]},
        {"$set": {**changes, "updated_at": db.utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    before = {k: target.get(k) for k in changes}
    await audit.log(admin, "user.update", "user", target["_id"], {"before": before, "after": changes})
    return user_out(updated)


# ---------- Справочник оснований ----------

@router.post("/benefit-types", response_model=BenefitTypeOut, status_code=201, responses=responses(409, 422),
             summary="Добавить основание (без миграций схемы)")
async def create_benefit_type(body: BenefitTypeIn, admin: dict = Depends(require_admin)):
    coll = db.get_db()[db.APPLICATION_TYPES]
    data = body.model_dump(exclude={"code"})
    result = await coll.update_one({"_id": body.code}, {"$setOnInsert": {**data, "updated_at": db.utcnow()}},
                                   upsert=True)
    if result.upserted_id is None:
        raise ApiError(409, f"Основание {body.code} уже существует")
    await benefit_types.invalidate()
    await audit.log(admin, "benefit_type.create", "benefit_type", body.code, data)
    return benefit_type_out({"_id": body.code, **data})


@router.put("/benefit-types/{code}", response_model=BenefitTypeOut, responses=responses(404, 422),
            summary="Изменить основание (сбрасывает кэш справочника)")
async def update_benefit_type(code: str, body: BenefitTypeBase, admin: dict = Depends(require_admin)):
    coll = db.get_db()[db.APPLICATION_TYPES]
    data = body.model_dump()
    updated = await coll.find_one_and_update(
        {"_id": code}, {"$set": {**data, "updated_at": db.utcnow()}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise ApiError(404, "Основание не найдено")
    await benefit_types.invalidate()
    await audit.log(admin, "benefit_type.update", "benefit_type", code, data)
    return benefit_type_out(updated)


@router.delete("/benefit-types/{code}", status_code=204, responses=responses(404),
               summary="Отключить основание (мягкое удаление: старые заявки на него ссылаются)")
async def disable_benefit_type(code: str, admin: dict = Depends(require_admin)):
    coll = db.get_db()[db.APPLICATION_TYPES]
    result = await coll.update_one({"_id": code}, {"$set": {"is_active": False, "updated_at": db.utcnow()}})
    if result.matched_count == 0:
        raise ApiError(404, "Основание не найдено")
    await benefit_types.invalidate()
    await audit.log(admin, "benefit_type.disable", "benefit_type", code)
    return Response(status_code=204)


# ---------- Журнал и статистика ----------

@router.get("/audit-events", response_model=Page[AuditEventOut], summary="Журнал действий сотрудников")
async def list_audit_events(
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    query: dict = {}
    if actor_id:
        query["actor_id"] = db.parse_oid(actor_id)
    if action:
        query["action"] = action
    if target_type:
        query["target_type"] = target_type
    if target_id:
        query["target_id"] = target_id
    coll = db.get_db()[db.AUDIT_EVENTS]
    cursor = coll.find(query).sort("at", -1).skip(offset).limit(limit)
    items = [audit_out(e) async for e in cursor]
    return {"items": items, "total": await coll.count_documents(query), "limit": limit, "offset": offset}


async def _count_by(collection: str, field: str, match: dict) -> dict[str, int]:
    pipeline = [{"$match": match}, {"$group": {"_id": f"${field}", "n": {"$sum": 1}}}]
    cursor = await db.get_db()[collection].aggregate(pipeline)
    return {str(row["_id"]): row["n"] async for row in cursor}


@router.get("/stats", response_model=StatsOut, summary="Статистика по заявкам, пользователям и очереди")
async def stats(period: str | None = None):
    settings = get_settings()
    period = period or settings.current_period
    return {
        "period": period,
        "applications_by_status": await _count_by(db.APPLICATIONS, "status", {"period": period}),
        "applications_by_basis": await _count_by(db.APPLICATIONS, "basis", {"period": period}),
        "users_by_role": await _count_by(db.USERS, "role", {}),
        "queue_length": await cache.queue_length(),
        "max_queue_length": settings.max_queue_length,
    }
