"""Журнал действий модераторов и администраторов (коллекция audit_events)."""

from app import db


async def log(actor: dict, action: str, target_type: str, target_id, details: dict | None = None) -> None:
    await db.get_db()[db.AUDIT_EVENTS].insert_one({
        "at": db.utcnow(),
        "actor_id": actor["_id"],
        "actor_email": actor["email"],
        "actor_role": actor["role"],
        "action": action,
        "target_type": target_type,
        "target_id": str(target_id),
        "details": details or {},
    })
