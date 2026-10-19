"""Redis: кэш редко меняющихся данных и доступ к очереди брокера.

Схема cache-aside: читаем из Redis, при промахе идём в MongoDB и кладём результат
с TTL. При изменении данных ключ удаляется (инвалидация).
"""

import json

import redis.asyncio as aioredis
from fastapi.encoders import jsonable_encoder

from app.config import get_settings

BENEFIT_TYPES_KEY = "cache:benefit_types:v1"


def app_status_key(app_id: str, staff: bool = False) -> str:
    # Студент и оператор видят разные поля статуса (оператор — ещё и состояние OCR),
    # поэтому кэшируются отдельно.
    return f"cache:app_status:{app_id}:{'staff' if staff else 'student'}"


def app_status_keys(app_id: str) -> list[str]:
    return [app_status_key(app_id, False), app_status_key(app_id, True)]


def idempotency_key(user_id: str, key: str) -> str:
    return f"idem:{user_id}:{key}"


_cache: aioredis.Redis | None = None
_broker: aioredis.Redis | None = None


async def connect() -> None:
    global _cache, _broker
    settings = get_settings()
    _cache = aioredis.from_url(settings.redis_cache_url, decode_responses=True)
    if settings.celery_broker_url.startswith("redis"):
        _broker = aioredis.from_url(settings.celery_broker_url, decode_responses=True)


async def close() -> None:
    for client in (_cache, _broker):
        if client is not None:
            await client.aclose()


def cache() -> aioredis.Redis:
    if _cache is None:
        raise RuntimeError("Redis client is not initialised")
    return _cache


async def get_json(key: str):
    raw = await cache().get(key)
    return None if raw is None else json.loads(raw)


async def set_json(key: str, value, ttl_sec: int) -> None:
    # jsonable_encoder: даты превращаются в ISO-строки, которые потом читаются обратно.
    await cache().set(key, json.dumps(jsonable_encoder(value), ensure_ascii=False), ex=ttl_sec)


async def delete(*keys: str) -> None:
    if keys:
        await cache().delete(*keys)


async def queue_length() -> int:
    """Число задач, ждущих в очереди Celery (список в Redis-брокере).

    Для брокера не на Redis (например, RabbitMQ) возвращает 0 — лимит K не проверяется.
    """
    if _broker is None:
        return 0
    return await _broker.llen(get_settings().celery_queue)
