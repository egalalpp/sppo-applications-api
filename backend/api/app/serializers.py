"""Преобразование документов MongoDB в словари под схемы ответов."""

from app.domain import AppStatus, OcrState


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
        "priority": doc.get("priority", doc.get("form_no") or 100),
        "title": doc["title"],
        "requires": doc.get("requires", []),
        "manual_flags": doc.get("manual_flags", []),
        "is_active": doc.get("is_active", True),
    }


def attachment_out(att: dict, staff: bool = False) -> dict:
    storage = att["storage"]
    out = {
        "attachment_id": att["attachment_id"],
        "type": att["type"],
        "slot": att["slot"],
        "content_type": storage["content_type"],
        "size_bytes": storage["size_bytes"],
        "sha256": storage["sha256"],
        "original_filename": att.get("original_filename"),
        "uploaded_at": att["uploaded_at"],
    }
    if staff:
        # Распознавание видит только оператор: студент о нём ничего не знает.
        out["ocr"] = att.get("ocr") or {"status": "not_started"}
    return out


def ocr_out(doc: dict) -> dict:
    return doc.get("ocr") or {"state": OcrState.not_started}


def application_out(doc: dict, staff: bool = False) -> dict:
    """Студенту отдаются только данные заявки и статус. Поля checks и ocr — только staff=True.

    В ответ для студента ключи ocr и checks не попадают вовсе (роуты используют
    response_model_exclude_unset), а не приходят пустыми.
    """
    out = {
        "id": str(doc["_id"]),
        "applicant_user_id": str(doc["applicant_user_id"]),
        "schema_version": doc["schema_version"],
        "period": doc["period"],
        "basis": doc["basis"],
        # Хэши паспорта и счёта нужны только для поиска дублей, наружу не отдаются
        "student": {k: v for k, v in doc["student"].items() if k != "passport_hash"},
        "payout": {k: v for k, v in doc["payout"].items() if k != "account_number_hash"},
        "form": doc["form"],
        "attachments": [attachment_out(a, staff) for a in doc.get("attachments", [])],
        "status": doc["status"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }
    if doc.get("rejection_reason") and doc["status"] == AppStatus.rejected:
        out["rejection_reason"] = doc["rejection_reason"]
    if doc.get("submitted_at"):
        out["submitted_at"] = doc["submitted_at"]
    if staff:
        out["priority"] = doc.get("priority")
        out["checks"] = doc.get("checks", [])
        out["ocr"] = ocr_out(doc)
    return out


def application_summary(doc: dict, staff: bool = False) -> dict:
    student = doc["student"]
    out = {
        "id": str(doc["_id"]),
        "basis": doc["basis"],
        "period": doc["period"],
        "status": doc["status"],
        "student_full_name": student["full_name"],
        "institute": student["institute"],
        "group": student["group"],
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }
    if doc.get("rejection_reason") and doc["status"] == AppStatus.rejected:
        out["rejection_reason"] = doc["rejection_reason"]
    if doc.get("submitted_at"):
        out["submitted_at"] = doc["submitted_at"]
    if staff:
        ocr = ocr_out(doc)
        out["priority"] = doc.get("priority")
        out["ocr_state"] = ocr.get("state", OcrState.not_started)
        out["ocr_accuracy"] = ocr.get("accuracy")
    return out


# Поля, нужные для списков: не тащим из БД вложения, проверки и историю.
SUMMARY_PROJECTION = {
    "basis": 1, "period": 1, "status": 1, "rejection_reason": 1, "ocr.state": 1, "ocr.accuracy": 1,
    "priority": 1, "submitted_at": 1, "created_at": 1, "updated_at": 1,
    "student.full_name": 1, "student.institute": 1, "student.group": 1,
}

STATUS_PROJECTION = {"status": 1, "rejection_reason": 1, "ocr": 1, "updated_at": 1, "applicant_user_id": 1}


def status_out(doc: dict, staff: bool = False) -> dict:
    out = {"id": str(doc["_id"]), "status": doc["status"], "updated_at": doc["updated_at"]}
    if doc.get("rejection_reason") and doc["status"] == AppStatus.rejected:
        out["rejection_reason"] = doc["rejection_reason"]
    if staff:
        out["ocr"] = ocr_out(doc)
    return out


def audit_out(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "eventType": doc["eventType"],
        "actor": doc["actor"],
        "resource": doc["resource"],
        "timestamp": doc["timestamp"],
        "details": doc.get("details", {}),
        "request_id": doc.get("request_id"),
    }
