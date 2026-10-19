"""Подключение к MongoDB (асинхронный драйвер PyMongo)."""

from datetime import UTC, datetime

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.config import get_settings

USERS = "users"
APPLICATIONS = "applications"
APPLICATION_TYPES = "application_types"
AUDIT_EVENTS = "audit_events"

_client: AsyncMongoClient | None = None


async def connect() -> None:
    global _client
    settings = get_settings()
    _client = AsyncMongoClient(settings.mongo_dsn, tz_aware=True, serverSelectionTimeoutMS=10000)


async def close() -> None:
    if _client is not None:
        await _client.close()


def get_db() -> AsyncDatabase:
    if _client is None:
        raise RuntimeError("MongoDB client is not initialised")
    return _client[get_settings().mongo_db]


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_oid(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None
