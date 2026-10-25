"""Подключение к MongoDB (асинхронный драйвер PyMongo) и транзакции."""

import inspect
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import AsyncMongoClient
from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure

from app.config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T")

USERS = "users"
APPLICATIONS = "applications"
APPLICATION_TYPES = "application_types"
AUDIT_LOGS = "audit_logs"
AUDIT_EVENTS_LEGACY = "audit_events"  # прежняя коллекция журнала; переносится в audit_logs при старте

_client: AsyncMongoClient | None = None
# None — ещё не проверяли; True — replica set или mongos (транзакции работают); False — одиночный узел
_transactions_supported: bool | None = None


async def connect() -> None:
    global _client, _transactions_supported
    _transactions_supported = None
    settings = get_settings()
    _client = AsyncMongoClient(settings.mongo_dsn, tz_aware=True, serverSelectionTimeoutMS=10000)


async def close() -> None:
    if _client is not None:
        await _client.close()


def get_db() -> AsyncDatabase:
    if _client is None:
        raise RuntimeError("MongoDB client is not initialised")
    return _client[get_settings().mongo_db]


async def _detect_transactions() -> bool:
    hello = await _client.admin.command("hello")
    return bool(hello.get("setName")) or hello.get("msg") == "isdbgrid"


async def run_in_transaction(work: Callable[[AsyncClientSession | None], Awaitable[T]]) -> T:
    """Выполняет work(session) в транзакции MongoDB и возвращает его результат.

    Транзакция нужна, чтобы смена статуса заявки и запись в журнал аудита были одним действием:
    либо произошли обе, либо ни одна. Транзакции работают на replica set (в docker-compose это replica set
    из одного узла rs0, в MongoDB Atlas — всегда). Если MongoDB запущена одиночным узлом, work вызывается
    без транзакции (session=None): записи идут последовательно, а сбой журнала аудита только пишется в лог.
    Бросить исключение внутри work значит откатить транзакцию.
    """
    global _transactions_supported
    if _client is None:
        raise RuntimeError("MongoDB client is not initialised")
    if _transactions_supported is None:
        _transactions_supported = await _detect_transactions()
        if not _transactions_supported:
            log.warning("MongoDB is not a replica set: transactions are disabled, writes are sequential")
    if not _transactions_supported:
        return await work(None)
    try:
        session = _client.start_session()
        if inspect.isawaitable(session):  # в разных версиях PyMongo start_session — корутина или обычный метод
            session = await session
        async with session:
            return await session.with_transaction(work)
    except OperationFailure as exc:
        # code 20 (IllegalOperation): «Transaction numbers are only allowed on a replica set member or mongos»
        if exc.code != 20:
            raise
        log.warning("MongoDB rejected the transaction (%s): falling back to sequential writes", exc)
        _transactions_supported = False
        return await work(None)


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_oid(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None
