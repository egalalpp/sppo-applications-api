from pymongo.errors import DuplicateKeyError

from app import db, security
from app.errors import ApiError


async def create_user(email: str, password: str, full_name: str, role: str, created_by=None) -> dict:
    now = db.utcnow()
    doc = {
        "email": email.strip().lower(),
        "password_hash": security.hash_password(password),
        "full_name": full_name.strip(),
        "role": role,
        "is_active": True,
        "created_at": now,
        "updated_at": now,
        "created_by": created_by,
    }
    try:
        result = await db.get_db()[db.USERS].insert_one(doc)
    except DuplicateKeyError:
        raise ApiError(409, "Пользователь с таким email уже существует") from None
    doc["_id"] = result.inserted_id
    return doc
