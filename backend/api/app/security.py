"""Хеширование паролей (argon2) и JWT-токены."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

ACCESS = "access"
REFRESH = "refresh"

_hasher = PasswordHasher()
# Хеш-пустышка: при входе с несуществующим email тратим столько же времени,
# сколько на проверку настоящего пароля, чтобы нельзя было перебирать email по таймингу.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return password_hash is not None


def create_token(user_id: str, role: str, token_type: str) -> tuple[str, int]:
    settings = get_settings()
    if token_type == ACCESS:
        ttl = timedelta(minutes=settings.access_token_ttl_min)
    else:
        ttl = timedelta(days=settings.refresh_token_ttl_days)
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "role": role,
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, int(ttl.total_seconds())


def decode_token(token: str, expected_type: str) -> dict | None:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != expected_type:
        return None
    return payload
