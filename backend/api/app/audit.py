"""Журнал аудита: коллекция audit_logs, плоская схема, одно событие — один документ.

    {
      "eventType": "claim_rejected",
      "actor":     {"type": "operator", "id": "<users._id>", "role": "operator", "email": "..."},
      "resource":  {"type": "application", "id": "<applications._id>"},
      "timestamp": ISODate(...),
      "details":   {"from": "in_review", "to": "rejected", "rejection_reason": "..."},
      "request_id": "7c2e..."
    }

Журнал пишется из кода (API и воркера), а не через Change Streams: только из кода известно, кто совершил
действие, а запись можно сделать в одной транзакции со сменой статуса (см. services/transitions.py).
"""

import logging

from app import db
from app.context import request_id_var

logger = logging.getLogger(__name__)

# Типы событий (раздел 7.1 TDD)
CLAIM_CREATED = "claim_created"
CLAIM_SUBMITTED = "claim_submitted"
CLAIM_WITHDRAWN = "claim_withdrawn"
CLAIM_REVIEW_STARTED = "claim_review_started"
CLAIM_RELEASED = "claim_released"
CLAIM_APPROVED = "claim_approved"
CLAIM_REJECTED = "claim_rejected"
CLAIM_REOPENED = "claim_reopened"
OCR_QUEUED = "ocr_queued"
OCR_DONE = "ocr_done"
OCR_FAILED = "ocr_failed"
OCR_RESTARTED = "ocr_restarted"
CHECK_SET = "check_set"
ATTACHMENT_UPLOADED = "attachment_uploaded"
ATTACHMENT_REMOVED = "attachment_removed"
OPERATOR_REASSIGNED = "operator_reassigned"
SCAN_VIEWED = "scan_viewed"
# Администрирование (в TDD отдельно не перечислено)
USER_CREATED = "user_created"
USER_UPDATED = "user_updated"
BENEFIT_TYPE_CREATED = "benefit_type_created"
BENEFIT_TYPE_UPDATED = "benefit_type_updated"
BENEFIT_TYPE_DISABLED = "benefit_type_disabled"

# Переход статуса -> тип события (кто и откуда, в details)
TRANSITION_EVENTS = {
    ("draft", "pending"): CLAIM_SUBMITTED,
    ("pending", "draft"): CLAIM_WITHDRAWN,
    ("pending", "in_review"): CLAIM_REVIEW_STARTED,
    ("in_review", "pending"): CLAIM_RELEASED,
    ("in_review", "approved"): CLAIM_APPROVED,
    ("in_review", "rejected"): CLAIM_REJECTED,
    ("rejected", "draft"): CLAIM_REOPENED,
}


def system_actor(name: str) -> dict:
    """Действие совершила система: OCR-воркер (name='ocr-worker'), подметальщик ('sweeper')."""
    return {"_id": name, "email": None, "role": "system"}


def build_event(actor: dict, event_type: str, resource_type: str, resource_id, details: dict | None = None) -> dict:
    return {
        "eventType": event_type,
        "actor": {"type": actor["role"], "id": str(actor["_id"]), "role": actor["role"], "email": actor.get("email")},
        "resource": {"type": resource_type, "id": str(resource_id)},
        "timestamp": db.utcnow(),
        "details": details or {},
        "request_id": request_id_var.get(),
    }


async def log(actor: dict, event_type: str, resource_type: str, resource_id, details: dict | None = None,
              *, session=None) -> None:
    """Записывает событие. С session — часть транзакции: ошибка откатывает и смену статуса.
    Без неё (одиночный MongoDB без replica set) сбой записи журнала только пишется в лог:
    уже выполненное действие не отменяется."""
    event = build_event(actor, event_type, resource_type, resource_id, details)
    try:
        await db.get_db()[db.AUDIT_LOGS].insert_one(event, session=session)
    except Exception:
        if session is not None:
            raise
        logger.exception("Audit event %s for %s %s was not saved", event_type, resource_type, resource_id)
