"""Справочник оснований с кэшированием в Redis (cache-aside + инвалидация)."""

from app import cache, db
from app.config import get_settings
from app.serializers import benefit_type_out


async def list_all() -> tuple[list[dict], bool]:
    """Возвращает (справочник, попали ли в кэш)."""
    cached = await cache.get_json(cache.BENEFIT_TYPES_KEY)
    if cached is not None:
        return cached, True
    cursor = db.get_db()[db.APPLICATION_TYPES].find().sort([("form_no", 1), ("_id", 1)])
    items = [benefit_type_out(doc) async for doc in cursor]
    await cache.set_json(cache.BENEFIT_TYPES_KEY, items, get_settings().benefit_types_cache_ttl_sec)
    return items, False


async def get(code: str) -> dict | None:
    items, _ = await list_all()
    return next((t for t in items if t["code"] == code), None)


async def invalidate() -> None:
    await cache.delete(cache.BENEFIT_TYPES_KEY)
