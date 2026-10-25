"""Redis: кэш редко меняющихся данных и доступ к очереди брокера.

Схема cache-aside: читаем из Redis, при промахе идём в MongoDB и кладём результат
с TTL. При изменении данных ключ удаляется (инвалидация).

Кэш — ускорение, а не источник правды: если Redis недоступен, get_json/set_json/delete не падают
(чтение считается промахом), и API продолжает работать на MongoDB. Так «спящая» очередь или перезапуск Redis
не превращаются в ошибки для студента.
"""

import json
import logging
import time

import redis.asyncio as aioredis
from fastapi.encoders import jsonable_encoder

from app.config import get_settings

logger = logging.getLogger(__name__)

BENEFIT_TYPES_KEY = "cache:benefit_types:v2"  # v2: в записи появился priority
# Короткие таймауты: недоступный Redis не должен подвешивать запрос студента на десятки секунд
SOCKET_TIMEOUT_SEC = 3
SOCKET_CONNECT_TIMEOUT_SEC = 2


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
    options = {"decode_responses": True, "socket_timeout": SOCKET_TIMEOUT_SEC,
               "socket_connect_timeout": SOCKET_CONNECT_TIMEOUT_SEC}
    _cache = aioredis.from_url(settings.redis_cache_url, **options)
    if settings.celery_broker_url.startswith("redis"):
        _broker = aioredis.from_url(settings.celery_broker_url, **options)


async def close() -> None:
    for client in (_cache, _broker):
        if client is not None:
            await client.aclose()


def cache() -> aioredis.Redis:
    if _cache is None:
        raise RuntimeError("Redis client is not initialised")
    return _cache


# «Предохранитель»: после первой неудачи Redis считается недоступным на DOWN_COOLDOWN_SEC секунд, и остальные
# обращения в этом окне пропускаются сразу. Иначе каждое из них ждёт свой таймаут, и запрос студента
# растягивается на десятки секунд, хотя от Redis он не зависит.
DOWN_COOLDOWN_SEC = 10
_down_until = 0.0
_last_warning = 0.0


def is_down() -> bool:
    return time.monotonic() < _down_until


def queue_available() -> bool:
    """Можно ли пытаться ставить задачи в очередь: брокер на Redis недоступен — не пытаемся, подметальщик подхватит."""
    return not (_broker is not None and is_down())


def mark_down(operation: str, exc: Exception) -> None:
    global _down_until, _last_warning
    now = time.monotonic()
    _down_until = now + DOWN_COOLDOWN_SEC
    if now - _last_warning > 30:  # в лог — не чаще раза в 30 секунд
        _last_warning = now
        logger.warning("Redis is unavailable (%s: %s): continuing without cache and queue", operation, type(exc).__name__)


async def get_json(key: str):
    if is_down():
        return None
    try:
        raw = await cache().get(key)
    except Exception as exc:  # noqa: BLE001 — Redis недоступен: это промах кэша
        mark_down("get", exc)
        return None
    return None if raw is None else json.loads(raw)


async def set_json(key: str, value, ttl_sec: int) -> None:
    if is_down():
        return
    # jsonable_encoder: даты превращаются в ISO-строки, которые потом читаются обратно.
    try:
        await cache().set(key, json.dumps(jsonable_encoder(value), ensure_ascii=False), ex=ttl_sec)
    except Exception as exc:  # noqa: BLE001
        mark_down("set", exc)


async def delete(*keys: str) -> None:
    if not keys or is_down():
        return
    try:
        await cache().delete(*keys)
    except Exception as exc:  # noqa: BLE001 — устаревшая запись доживёт до TTL (секунды)
        mark_down("delete", exc)


async def queue_length() -> int:
    """Число задач, ждущих в очереди Celery (список в Redis-брокере).

    Для брокера не на Redis (например, RabbitMQ) возвращает 0 — лимит K не проверяется.
    """
    if _broker is None:
        return 0
    if is_down():
        raise ConnectionError("Redis is marked as unavailable")
    try:
        return await _broker.llen(get_settings().celery_queue)
    except Exception as exc:
        mark_down("llen", exc)
        raise
