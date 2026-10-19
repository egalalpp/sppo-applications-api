"""Начальные данные: справочник оснований и демо-учётки.

Ручной запуск (создаёт демо-учётки независимо от SEED_DEMO_USERS):
    docker compose exec api python -m app.seed

При старте стека вызывается из bootstrap: справочник — всегда, демо-учётки — если SEED_DEMO_USERS=true.
Всё идемпотентно: существующие записи не перезаписываются, пароли существующих учёток не меняются.
"""

import asyncio
import logging

from app import cache, db
from app.benefit_data import BENEFIT_TYPES  # noqa: F401  (реэкспорт для тестов и обратной совместимости)
from app.domain import Role
from app.services.users import create_user

log = logging.getLogger("seed")

# Известные пароли: только для разработки и демонстрации.
DEMO_USERS = [
    {"email": "student@rut.ru", "password": "student123", "full_name": "Иванов Иван Иванович",
     "role": Role.student},
    {"email": "operator@rut.ru", "password": "operator123", "full_name": "Петрова Мария Сергеевна",
     "role": Role.operator},
    {"email": "admin@rut.ru", "password": "admin123", "full_name": "Администратор системы",
     "role": Role.admin},
]


async def seed_benefit_types() -> int:
    """Добавляет недостающие основания; правки администратора не затираются."""
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
    return created


async def seed_demo_users() -> int:
    """Создаёт демо-учётки student@rut.ru, operator@rut.ru, admin@rut.ru. Возвращает число созданных."""
    users = db.get_db()[db.USERS]
    created = 0
    for spec in DEMO_USERS:
        if await users.find_one({"email": spec["email"]}, {"_id": 1}):
            continue
        await create_user(spec["email"], spec["password"], spec["full_name"], spec["role"])
        created += 1
        log.info("Created demo user %s (%s)", spec["email"], spec["role"])
    if created:
        log.warning("Demo accounts with well-known passwords exist: do not use with real data")
    return created


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    await db.connect()
    await cache.connect()
    try:
        await seed_benefit_types()
        created = await seed_demo_users()
        print(f"Готово: создано демо-учёток — {created}")
        for spec in DEMO_USERS:
            print(f"  {spec['role']:<9} {spec['email']:<18} пароль: {spec['password']}")
    finally:
        await cache.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
