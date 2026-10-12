"""Однократная инициализация при старте (сервис init в docker-compose).

Идемпотентна — безопасно запускать при каждом `docker compose up`:
1. коллекции с валидаторами $jsonSchema (создаются или обновляются через collMod);
2. индексы;
3. справочник оснований (только недостающие записи — правки админа не затираются);
4. первый администратор из ADMIN_EMAIL / ADMIN_PASSWORD, если админов ещё нет;
5. бакет для сканов в S3.
"""

import asyncio
import logging

from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import CollectionInvalid, PyMongoError

from app import cache, db, storage
from app.config import get_settings
from app.domain import AppStatus, Role
from app.errors import ApiError
from app.seed import BENEFIT_TYPES
from app.services.users import create_user

log = logging.getLogger("bootstrap")

_DATE = {"bsonType": "date"}

VALIDATORS = {
    db.APPLICATIONS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["schema_version", "applicant_user_id", "period", "basis", "student", "payout",
                     "form", "status", "attachments", "checks", "status_history", "created_at", "updated_at"],
        "properties": {
            "applicant_user_id": {"bsonType": "objectId"},
            "period": {"bsonType": "string", "pattern": "^[0-9]{4}-(spring|autumn)$"},
            # enum по basis намеренно убран: основания живут в справочнике application_types
            # и добавляются админом без изменения валидатора.
            "basis": {"bsonType": "string"},
            "status": {"enum": [s.value for s in AppStatus]},
            "student": {
                "bsonType": "object",
                "required": ["full_name", "full_name_as_written", "birth_date", "institute", "group", "course"],
                "properties": {
                    "student_id": {"bsonType": ["string", "null"]},
                    "course": {"bsonType": "int", "minimum": 1, "maximum": 6},
                },
            },
            "payout": {
                "bsonType": "object",
                "required": ["bik", "account_number"],
                "properties": {"bik": {"bsonType": "string", "pattern": "^[0-9]{9}$"}},
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
        },
    }},
    db.AUDIT_EVENTS: {"$jsonSchema": {
        "bsonType": "object",
        "required": ["at", "actor_id", "action", "target_type", "target_id"],
        "properties": {"at": _DATE, "actor_id": {"bsonType": "objectId"}},
    }},
}

INDEXES = {
    db.APPLICATIONS: [
        # Индексы из schema_zayavok_sppo.md, раздел 3.6
        IndexModel([("student.full_name", ASCENDING), ("student.birth_date", ASCENDING), ("period", ASCENDING)]),
        IndexModel([("status", ASCENDING), ("created_at", ASCENDING)], name="moderation_queue"),
        IndexModel([("basis", ASCENDING), ("period", ASCENDING)]),
        IndexModel([("attachments.storage.sha256", ASCENDING)]),
        # Новые: «мои заявки» и поиск дублей реквизитов
        IndexModel([("applicant_user_id", ASCENDING), ("created_at", DESCENDING)]),
        IndexModel([("applicant_user_id", ASCENDING), ("basis", ASCENDING), ("period", ASCENDING)]),
        IndexModel([("payout.account_number", ASCENDING), ("period", ASCENDING)]),
    ],
    db.USERS: [
        IndexModel([("email", ASCENDING)], unique=True),
        IndexModel([("role", ASCENDING), ("is_active", ASCENDING)]),
    ],
    db.AUDIT_EVENTS: [
        IndexModel([("at", DESCENDING)]),
        IndexModel([("actor_id", ASCENDING), ("at", DESCENDING)]),
        IndexModel([("target_type", ASCENDING), ("target_id", ASCENDING), ("at", DESCENDING)]),
    ],
}


async def ensure_collections() -> None:
    database = db.get_db()
    for name, validator in VALIDATORS.items():
        options = {"validator": validator, "validationLevel": "strict", "validationAction": "error"}
        try:
            await database.create_collection(name, **options)
            log.info("Created collection %s", name)
        except CollectionInvalid:
            await database.command({"collMod": name, **options})
            log.info("Updated validator of %s", name)
    for name, indexes in INDEXES.items():
        await database[name].create_indexes(indexes)
        log.info("Ensured %d indexes on %s", len(indexes), name)


async def seed_benefit_types() -> None:
    coll = db.get_db()[db.APPLICATION_TYPES]
    created = 0
    for item in BENEFIT_TYPES:
        data = {k: v for k, v in item.items() if k != "_id"}
        result = await coll.update_one(
            {"_id": item["_id"]}, {"$setOnInsert": {**data, "updated_at": db.utcnow()}}, upsert=True
        )
        created += result.upserted_id is not None
    await cache.delete(cache.BENEFIT_TYPES_KEY)
    log.info("Benefit types: %d created, %d already present", created, len(BENEFIT_TYPES) - created)


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


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await db.connect()
    await cache.connect()
    try:
        await wait_for_mongo()
        await ensure_collections()
        await seed_benefit_types()
        await ensure_admin()
        created = await asyncio.to_thread(storage.ensure_bucket)
        log.info("S3 bucket %s %s", get_settings().s3_bucket, "created" if created else "already exists")
    finally:
        await cache.close()
        await db.close()
    log.info("Bootstrap finished")


if __name__ == "__main__":
    asyncio.run(main())
