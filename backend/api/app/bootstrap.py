"""Однократная инициализация при старте (сервис init в docker-compose).

Идемпотентна — безопасно запускать при каждом старте (docker compose up, Render):
1. коллекции с валидаторами $jsonSchema (создаются или обновляются через collMod);
2. индексы;
3. миграция данных предыдущих версий (moderator -> operator, need_more_docs, processing -> ocr, priority,
   submitted_at, хэши паспорта и счёта, audit_events -> audit_logs);
4. справочник оснований (только недостающие записи — правки админа не затираются);
5. демо-учётки student@rut.ru, operator@rut.ru, admin@rut.ru — если SEED_DEMO_USERS=true;
6. первый администратор из ADMIN_EMAIL / ADMIN_PASSWORD, если админов ещё нет;
7. бакет для сканов в S3.

В облачных сервисах (MongoDB Atlas, Cloudflare R2) у технической учётки может не хватать прав на
collMod или создание бакета: это не ошибка, остаётся предупреждение в логе.
"""

import argparse
import asyncio
import logging

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import CollectionInvalid, OperationFailure, PyMongoError

from app import audit, cache, db, storage
from app.config import get_settings
from app.domain import AppStatus, Role
from app.errors import ApiError
from app.services import hashing
from app.seed import seed_benefit_types, seed_demo_users
from app.services.users import create_user

log = logging.getLogger("bootstrap")

_DATE = {"bsonType": "date"}

VALIDATORS = {
    db.APPLICATIONS: {"$and": [{"$jsonSchema": {
        "bsonType": "object",
        "required": ["schema_version", "applicant_user_id", "period", "basis", "priority", "student", "payout",
                     "form", "status", "attachments", "checks", "status_history", "created_at", "updated_at"],
        "properties": {
            "applicant_user_id": {"bsonType": "objectId"},
            "period": {"bsonType": "string", "pattern": "^[0-9]{4}-(spring|autumn)$"},
            # enum по basis намеренно убран: основания живут в справочнике application_types
            # и добавляются админом без изменения валидатора.
            "basis": {"bsonType": "string"},
            "status": {"enum": [s.value for s in AppStatus]},
            "rejection_reason": {"bsonType": ["string", "null"]},
            "priority": {"bsonType": "int", "minimum": 1},  # снимок application_types.priority
            "submitted_at": {"bsonType": ["date", "null"]},
            "student": {
                "bsonType": "object",
                "required": ["full_name", "birth_date", "institute", "group", "course"],
                "properties": {
                    "student_id": {"bsonType": ["string", "null"]},
                    "passport_hash": {"bsonType": "string"},
                    "course": {"bsonType": "int", "minimum": 1, "maximum": 6},
                },
            },
            "payout": {
                "bsonType": "object",
                "required": ["bik", "account_number"],
                "properties": {
                    "bik": {"bsonType": "string", "pattern": "^[0-9]{9}$"},
                    "account_number_hash": {"bsonType": "string"},
                },
            },
            "attachments": {
                "bsonType": "array",
                "maxItems": 12,
                "items": {
                    "bsonType": "object",
                    "required": ["attachment_id", "type", "slot", "storage"],
                    "properties": {
                        "storage": {"bsonType": "object", "required": ["bucket", "key", "sha256"]},
                    },
                },
            },
            "checks": {"bsonType": "array"},
            "status_history": {"bsonType": "array"},
            "created_at": _DATE,
            "updated_at": _DATE,
        },
    }},
        # при отказе причина обязательна и непуста
        {"$or": [{"status": {"$ne": AppStatus.rejected.value}}, {"rejection_reason": {"$regex": r"\S"}}]},
    ]},
    db.USERS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["email", "password_hash", "full_name", "role", "is_active", "created_at"],
        "properties": {
            "email": {"bsonType": "string", "pattern": "^[^@\\s]+@[^@\\s]+$"},
            "role": {"enum": [r.value for r in Role]},
            "is_active": {"bsonType": "bool"},
            "created_at": _DATE,
        },
    }},
    db.APPLICATION_TYPES: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["title", "requires", "is_active"],
        "properties": {
            "requires": {
                "bsonType": "array",
                "items": {"bsonType": "object", "required": ["slot", "any_of"]},
            },
            "is_active": {"bsonType": "bool"},
            "priority": {"bsonType": "int", "minimum": 1},
        },
    }},
    db.AUDIT_LOGS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["eventType", "actor", "resource", "timestamp"],
        "properties": {
            "eventType": {"bsonType": "string"},
            "actor": {"bsonType": "object", "required": ["type", "id"],
                      "properties": {"type": {"bsonType": "string"}, "id": {"bsonType": "string"}}},
            "resource": {"bsonType": "object", "required": ["type", "id"],
                         "properties": {"type": {"bsonType": "string"}, "id": {"bsonType": "string"}}},
            "timestamp": _DATE,
        },
    }},
}

INDEXES = {
    db.APPLICATIONS: [
        # Индексы из schema_zayavok_sppo.md, раздел 3.6
        IndexModel([("student.full_name", ASCENDING), ("student.birth_date", ASCENDING), ("period", ASCENDING)]),
        # очередь оператора: сначала приоритет основания, затем время подачи
        IndexModel([("status", ASCENDING), ("priority", ASCENDING), ("submitted_at", ASCENDING)], name="operator_queue"),
        IndexModel([("basis", ASCENDING), ("period", ASCENDING)]),
        IndexModel([("attachments.storage.sha256", ASCENDING)]),
        # «мои заявки»
        IndexModel([("applicant_user_id", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("applicant_user_id", ASCENDING), ("basis", ASCENDING), ("period", ASCENDING)]),
        # поиск дублей по HMAC-хэшам: не уникальные, один счёт законно может встретиться дважды
        IndexModel([("student.passport_hash", ASCENDING), ("period", ASCENDING)]),
        IndexModel([("payout.account_number_hash", ASCENDING), ("period", ASCENDING)]),
    ],
    db.USERS: [
        IndexModel([("email", ASCENDING)], unique=True),
        IndexModel([("role", ASCENDING), ("is_active", ASCENDING)]),
    ],
    db.AUDIT_LOGS: [
        IndexModel([("resource.id", ASCENDING), ("timestamp", ASCENDING)]),
        IndexModel([("actor.id", ASCENDING), ("timestamp", DESCENDING)]),
        IndexModel([("eventType", ASCENDING), ("timestamp", DESCENDING)]),
    ],
}

# Индексы прежних версий, которые заменены: сырой номер счёта больше не индексируется, очередь — по приоритету
OBSOLETE_INDEXES = {
    db.APPLICATIONS: ["moderation_queue", "payout.account_number_1_period_1"],
}


async def ensure_collections() -> None:
    database = db.get_db()
    for name, validator in VALIDATORS.items():
        options = {"validator": validator, "validationLevel": "strict", "validationAction": "error"}
        try:
            await database.create_collection(name, **options)
            log.info("Created collection %s", name)
        except CollectionInvalid:
            try:
                await database.command({"collMod": name, **options})
                log.info("Updated validator of %s", name)
            except OperationFailure as exc:
                log.warning("Cannot update validator of %s (%s): continuing with the existing one", name, exc.code)
        except OperationFailure as exc:
            log.warning("Cannot create collection %s (%s): it will be created on first insert", name, exc.code)
    for name, indexes in INDEXES.items():
        await database[name].create_indexes(indexes)
        log.info("Ensured %d indexes on %s", len(indexes), name)
    for name, index_names in OBSOLETE_INDEXES.items():
        for index_name in index_names:
            try:
                await database[name].drop_index(index_name)
                log.info("Dropped obsolete index %s.%s", name, index_name)
            except OperationFailure:
                pass  # такого индекса уже нет


_OCR_STATE_FROM_PROCESSING = {"$switch": {
    "branches": [
        {"case": {"$eq": ["$processing.state", "queued"]}, "then": "queued"},
        {"case": {"$eq": ["$processing.state", "running"]}, "then": "running"},
        {"case": {"$eq": ["$processing.state", "done"]}, "then": "ready_for_review"},
        {"case": {"$eq": ["$processing.state", "failed"]}, "then": "ocr_failed"},
    ],
    "default": "not_started",
}}


# Прежние названия действий журнала -> eventType плоской схемы audit_logs
_LEGACY_EVENT_TYPES = {
    "application.check_decision": audit.CHECK_SET,
    "application.ocr_restart": audit.OCR_RESTARTED,
    "attachment.download_url": audit.SCAN_VIEWED,
    "user.create": audit.USER_CREATED,
    "user.update": audit.USER_UPDATED,
    "benefit_type.create": audit.BENEFIT_TYPE_CREATED,
    "benefit_type.update": audit.BENEFIT_TYPE_UPDATED,
    "benefit_type.disable": audit.BENEFIT_TYPE_DISABLED,
}


def legacy_event_type(action: str, details: dict | None) -> str:
    """eventType для записи старого журнала audit_events (поле action)."""
    if action == "application.transition":
        details = details or {}
        return audit.TRANSITION_EVENTS.get((details.get("from"), details.get("to")), action)
    return _LEGACY_EVENT_TYPES.get(action, action)


def legacy_event_to_log(event: dict) -> dict:
    role = "operator" if event.get("actor_role") == "moderator" else event.get("actor_role", "system")
    details = event.get("details") or {}
    if "rejection_reason" not in details and details.get("to") == "rejected" and details.get("comment"):
        details = {**details, "rejection_reason": details["comment"]}  # в старом журнале причина лежала в comment
    return {
        "eventType": legacy_event_type(event.get("action", ""), details),
        "actor": {"type": role, "id": str(event.get("actor_id")), "role": role, "email": event.get("actor_email")},
        "resource": {"type": event.get("target_type", "application"), "id": str(event.get("target_id"))},
        "timestamp": event["at"],
        "details": details,
        "request_id": None,
    }


async def migrate_audit_events() -> None:
    """Переносит записи прежнего журнала audit_events в audit_logs (один раз: пока audit_logs пуст).
    Старая коллекция остаётся нетронутой."""
    database = db.get_db()
    legacy, target = database[db.AUDIT_EVENTS_LEGACY], database[db.AUDIT_LOGS]
    if await target.estimated_document_count() > 0 or await legacy.estimated_document_count() == 0:
        return
    events = [legacy_event_to_log(e) async for e in legacy.find().sort("at", 1)]
    if events:
        await target.insert_many(events)
        log.info("Migrated %d audit events: audit_events -> audit_logs", len(events))


async def backfill_hashes(*, force: bool = False) -> int:
    """Считает HMAC-хэши паспорта и счёта у заявок, где их ещё нет (force=True — у всех: после смены HASH_PEPPER)."""
    applications = db.get_db()[db.APPLICATIONS]
    query: dict = {} if force else {"$or": [
        {"student.passport.number": {"$exists": True}, "student.passport_hash": {"$exists": False}},
        {"payout.account_number": {"$exists": True}, "payout.account_number_hash": {"$exists": False}},
    ]}
    updated = 0
    async for doc in applications.find(query, {"student": 1, "payout": 1}):
        student, payout = dict(doc.get("student") or {}), dict(doc.get("payout") or {})
        hashing.apply_hashes(student, payout)
        changes = {}
        if "passport_hash" in student:
            changes["student.passport_hash"] = student["passport_hash"]
        if "account_number_hash" in payout:
            changes["payout.account_number_hash"] = payout["account_number_hash"]
        if changes:
            await applications.update_one({"_id": doc["_id"]}, {"$set": changes}, bypass_document_validation=True)
            updated += 1
    if updated:
        log.info("Hashes of passport/account computed for %d application(s)", updated)
    return updated


_bypass_validation = True


async def _update_many(collection, query: dict, update):
    """update_many для миграции. Документы старых версий не проходят новый валидатор (например, статус
    need_more_docs), а валидатор проверяет документ целиком при любом обновлении, поэтому миграция идёт с
    bypassDocumentValidation: ей положено чинить именно такие документы. У пользователя без этого права
    (кастомная роль в облаке) пробуем обычный режим."""
    global _bypass_validation
    if _bypass_validation:
        try:
            return await collection.update_many(query, update, bypass_document_validation=True)
        except OperationFailure as exc:
            if exc.code != 13:  # 13 — Unauthorized
                raise
            log.warning("No privilege to bypass document validation: migrating in the regular mode")
            _bypass_validation = False
    return await collection.update_many(query, update)


async def migrate() -> None:
    """Приводит данные прежних версий к текущей модели. Идемпотентно: условия отбирают только старые документы.

    Порядок важен: валидатор требует priority у каждой заявки, поэтому priority проставляется первым,
    иначе любое другое обновление старой заявки не пройдёт проверку схемы."""
    database = db.get_db()
    types, applications = database[db.APPLICATION_TYPES], database[db.APPLICATIONS]

    # priority: у оснований — номер бланка, у заявок — снимок приоритета их основания
    await _update_many(types, {"priority": {"$exists": False}}, [
        {"$set": {"priority": {"$ifNull": ["$priority", {"$ifNull": ["$form_no", 100]}]}}}])
    async for item in types.find({}, {"priority": 1}):
        await _update_many(applications, {"basis": item["_id"], "priority": {"$exists": False}},
                                       {"$set": {"priority": item["priority"]}})
    result = await _update_many(applications, {"priority": {"$exists": False}}, {"$set": {"priority": 100}})
    if result.modified_count:
        log.info("Set default priority for %d applications with unknown basis", result.modified_count)

    result = await _update_many(database[db.USERS], {"role": "moderator"}, {"$set": {"role": Role.operator.value}})
    if result.modified_count:
        log.info("Migrated %d users: moderator -> operator", result.modified_count)

    result = await _update_many(applications, {"status": "need_more_docs"}, [{"$set": {
        "status": AppStatus.rejected.value,
        "rejection_reason": {"$ifNull": [{"$last": "$status_history.comment"}, "Требовались дополнительные документы"]},
    }}])
    if result.modified_count:
        log.info("Migrated %d applications: need_more_docs -> rejected", result.modified_count)

    result = await _update_many(applications, {"processing": {"$exists": True}, "ocr": {"$exists": False}}, [
        {"$set": {"ocr": {
            "state": _OCR_STATE_FROM_PROCESSING, "accuracy": None, "engine": None,
            "task_id": "$processing.task_id", "updated_at": "$processing.updated_at", "error": "$processing.error",
        }}},
        {"$unset": "processing"},
    ])
    if result.modified_count:
        log.info("Migrated %d applications: processing -> ocr", result.modified_count)

    # submitted_at: время первого перехода в pending (для заявок, отправленных до появления поля)
    result = await _update_many(applications, {"submitted_at": {"$exists": False}, "status": {"$ne": "draft"}}, [
        {"$set": {"submitted_at": {"$let": {
            "vars": {"sent": {"$filter": {"input": "$status_history", "as": "h",
                                          "cond": {"$eq": ["$$h.status", "pending"]}}}},
            "in": {"$ifNull": [{"$arrayElemAt": ["$$sent.at", 0]}, "$created_at"]},
        }}}}])
    if result.modified_count:
        log.info("Set submitted_at for %d applications", result.modified_count)
    await _update_many(applications, {"submitted_at": {"$exists": False}}, {"$set": {"submitted_at": None}})

    # электронная форма: подписи студента нет, согласие фиксируется временем; родительный падеж ФИО не нужен
    await _update_many(applications, {"form.consent_pd_at": {"$exists": False}}, [
        {"$set": {"form.consent_pd_at": {"$cond": [{"$eq": ["$form.consent_pd_signed", True]}, "$created_at", None]}}}])
    await _update_many(
        applications, {"$or": [{"form.applicant_signed": {"$exists": True}}, {"form.signed_date": {"$exists": True}}]},
        {"$unset": {"form.applicant_signed": "", "form.signed_date": ""}})
    await _update_many(applications, {"student.full_name_as_written": {"$exists": True}},
                                   {"$unset": {"student.full_name_as_written": ""}})

    await backfill_hashes()
    await migrate_audit_events()


async def ensure_admin() -> None:
    settings = get_settings()
    users = db.get_db()[db.USERS]
    if await users.count_documents({"role": Role.admin}) > 0:
        log.info("Admin already exists, skipping")
        return
    if not settings.admin_email or not settings.admin_password:
        log.warning("No admin in DB and ADMIN_EMAIL/ADMIN_PASSWORD are not set")
        return
    if len(settings.admin_password) < 8:
        raise SystemExit("ADMIN_PASSWORD must be at least 8 characters")
    try:
        await create_user(settings.admin_email, settings.admin_password, "Администратор", Role.admin)
        log.info("Created first admin %s", settings.admin_email)
    except ApiError:
        log.warning("User %s exists but is not admin; promote it manually", settings.admin_email)


async def wait_for_mongo(attempts: int = 30, delay_sec: float = 2.0) -> None:
    """При первом запуске образ mongo сначала поднимает временный сервер только на localhost
    (для init-скриптов), и healthcheck может пройти раньше, чем откроется настоящий."""
    for attempt in range(1, attempts + 1):
        try:
            await db.get_db().command("ping")
            return
        except PyMongoError as exc:
            log.info("MongoDB is not ready (%s), attempt %d/%d", type(exc).__name__, attempt, attempts)
            await asyncio.sleep(delay_sec)
    raise SystemExit("MongoDB is not reachable")


async def main(rehash: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await db.connect()
    await cache.connect()
    try:
        await wait_for_mongo()
        if rehash:
            # После смены HASH_PEPPER: пересчитать хэши паспорта и счёта у всех заявок и выйти
            log.info("Recomputed hashes for %d application(s)", await backfill_hashes(force=True))
            return
        await ensure_collections()
        await migrate()
        await seed_benefit_types()
        if get_settings().seed_demo_users:
            await seed_demo_users()
        await ensure_admin()
        created = await asyncio.to_thread(storage.ensure_bucket)
        log.info("S3 bucket %s %s", get_settings().s3_bucket, "created" if created else "already exists")
    finally:
        await cache.close()
        await db.close()
    log.info("Bootstrap finished")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Инициализация MongoDB, справочника, учёток и бакета (идемпотентно)")
    parser.add_argument("--rehash", action="store_true",
                        help="пересчитать HMAC-хэши паспорта и счёта у всех заявок (после смены HASH_PEPPER)")
    asyncio.run(main(parser.parse_args().rehash))
