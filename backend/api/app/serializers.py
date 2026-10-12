"""Преобразование документов MongoDB в словари под схемы ответов."""

from app.domain import ProcessingState


def user_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "email": doc["email"],
        "full_name": doc["full_name"],
        "role": doc["role"],
        "is_active": doc["is_active"],
        "created_at": doc["created_at"],
    }


def benefit_type_out(doc: dict) -> dict:
    return {
        "code": doc["_id"],
        "form_no": doc.get("form_no"),
        "title": doc["title"],
        "requires": doc.get("requires", []),
        "manual_flags": doc.get("manual_flags", []),
        "is_active": doc.get("is_active", True),
    }


def attachment_out(att: dict) -> dict:
    storage = att["storage"]
    return {
        "attachment_id": att["attachment_id"],
        "type": att["type"],
        "slot": att["slot"],
        "content_type": storage["content_type"],
        "size_bytes": storage["size_bytes"],
        "sha256": storage["sha256"],
        "original_filename": att.get("original_filename"),
        "uploaded_at": att["uploaded_at"],
        "ocr_status": att.get("ocr", {}).get("status", "not_started"),
    }


def processing_out(doc: dict) -> dict:
    return doc.get("processing") or {"state": ProcessingState.idle}


def application_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "applicant_user_id": str(doc["applicant_user_id"]),
        "schema_version": doc["schema_version"],
        "period": doc["period"],
        "basis": doc["basis"],
        "student": doc["student"],
        "payout": doc["payout"],
        "form": doc["form"],
        "attachments": [attachment_out(a) for a in doc.get("attachments", [])],
        "checks": doc.get("checks", []),
        "status": doc["status"],
        "processing": processing_out(doc),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


def application_summary(doc: dict) -> dict:
    student = doc["student"]
    return {
        "id": str(doc["_id"]),
        "basis": doc["basis"],
        "period": doc["period"],
        "status": doc["status"],
        "student_full_name": student["full_name"],
        "institute": student["institute"],
        "group": student["group"],
        "processing_state": processing_out(doc)["state"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


# Поля, нужные для списков: не тащим из БД вложения, проверки и историю.
SUMMARY_PROJECTION = {
    "basis": 1, "period": 1, "status": 1, "processing": 1, "created_at": 1, "updated_at": 1,
    "student.full_name": 1, "student.institute": 1, "student.group": 1,
}


def status_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "status": doc["status"],
        "processing": processing_out(doc),
        "updated_at": doc["updated_at"],
    }


def audit_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "at": doc["at"],
        "actor_id": str(doc["actor_id"]),
        "actor_email": doc["actor_email"],
        "actor_role": doc["actor_role"],
        "action": doc["action"],
        "target_type": doc["target_type"],
        "target_id": doc["target_id"],
        "details": doc.get("details", {}),
    }
