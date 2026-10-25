"""Студент ничего не знает про распознавание: в его ответах нет ни OCR, ни проверок."""

import json
from datetime import UTC, datetime

from bson import ObjectId

from app.schemas import ApplicationOut, ApplicationSummary, AttachmentOut, StatusOut
from app.serializers import application_out, application_summary, attachment_out, status_out

NOW = datetime(2026, 10, 11, tzinfo=UTC)
FORBIDDEN = ("ocr", "checks", "accuracy", "confidence", "tesseract", "ready_for_review", "ocr_failed",
             "processing", "text_preview", "seal", "signature", "words")


def doc(status="pending"):
    return {
        "_id": ObjectId(), "applicant_user_id": ObjectId(), "schema_version": 4, "period": "2026-autumn",
        "basis": "orphan",
        "student": {"full_name": "Иванов Иван Иванович", "birth_date": "2005-03-14", "institute": "ИУЦТ",
                    "group": "ТКИ-211", "course": 3, "email": "student@rut.ru"},
        "payout": {"bik": "044525225", "account_number": "40817810000000000001"},
        "form": {"applicant_signed": True, "consent_pd_signed": True},
        "attachments": [{
            "attachment_id": "a1", "type": "passport_registration", "slot": "passport",
            "original_filename": "passport.pdf", "uploaded_at": NOW,
            "storage": {"bucket": "b", "key": "k", "content_type": "application/pdf", "size_bytes": 10, "sha256": "x"},
            "ocr": {"status": "done", "engine": "tesseract", "confidence": 76, "text_preview": "ИВАНОВ",
                    "seal": True, "signature": False, "notes": ["есть печать"]},
        }],
        "checks": [{"code": "inn_checksum", "kind": "auto", "status": "auto_ok", "by": "system", "at": NOW}],
        "status": status, "rejection_reason": "Причина" if status == "rejected" else None,
        "ocr": {"state": "ready_for_review", "accuracy": 76, "engine": "tesseract", "task_id": "t",
                "updated_at": NOW, "error": None},
        "created_at": NOW, "updated_at": NOW,
    }


def assert_no_ocr(payload):
    text = json.dumps(payload, default=str, ensure_ascii=False).lower()
    for word in FORBIDDEN:
        assert word not in text, word


def test_student_application_has_no_ocr_data():
    out = application_out(doc())
    assert_no_ocr(out)
    model = ApplicationOut.model_validate(out).model_dump(mode="json", exclude_unset=True)
    assert_no_ocr(model)
    assert "ocr" not in model and "checks" not in model


def test_student_summary_and_status_have_no_ocr_data():
    d = doc()
    assert_no_ocr(application_summary(d))
    assert_no_ocr(status_out(d))
    assert_no_ocr(ApplicationSummary.model_validate(application_summary(d)).model_dump(mode="json", exclude_unset=True))
    assert_no_ocr(StatusOut.model_validate(status_out(d)).model_dump(mode="json", exclude_unset=True))
    assert_no_ocr(AttachmentOut.model_validate(attachment_out(d["attachments"][0])).model_dump(
        mode="json", exclude_unset=True))


def test_operator_sees_ocr_and_checks():
    d = doc()
    out = ApplicationOut.model_validate(application_out(d, staff=True)).model_dump(mode="json", exclude_unset=True)
    assert out["ocr"]["state"] == "ready_for_review" and out["ocr"]["accuracy"] == 76
    assert out["checks"][0]["code"] == "inn_checksum"
    att = out["attachments"][0]["ocr"]
    assert att["confidence"] == 76 and att["notes"] == ["есть печать"] and att["seal"] is True

    summary = application_summary(d, staff=True)
    assert (summary["ocr_state"], summary["ocr_accuracy"]) == ("ready_for_review", 76)
    assert status_out(d, staff=True)["ocr"]["state"] == "ready_for_review"


def test_operator_sees_failed_ocr():
    d = doc()
    d["ocr"] = {"state": "ocr_failed", "accuracy": None, "error": "Не удалось обработать вложений: 1"}
    assert application_summary(d, staff=True)["ocr_state"] == "ocr_failed"


def test_rejection_reason_is_shown_to_student_only_when_rejected():
    assert "rejection_reason" not in application_out(doc("pending"))
    assert application_out(doc("rejected"))["rejection_reason"] == "Причина"
    assert status_out(doc("rejected"))["rejection_reason"] == "Причина"


def test_hashes_and_priority_are_never_shown_to_the_student():
    d = doc()
    d["priority"] = 10
    d["student"]["passport_hash"] = "a" * 64
    d["payout"]["account_number_hash"] = "b" * 64
    for out in (application_out(d), application_summary(d)):
        text = json.dumps(out, default=str)
        assert "passport_hash" not in text and "account_number_hash" not in text and "priority" not in text

    staff = application_out(d, staff=True)
    assert staff["priority"] == 10
    assert "passport_hash" not in staff["student"] and "account_number_hash" not in staff["payout"]
    assert application_summary(d, staff=True)["priority"] == 10


def test_submitted_at_is_visible_to_everyone():
    d = doc()
    d["submitted_at"] = NOW
    assert application_out(d)["submitted_at"] == NOW and application_summary(d)["submitted_at"] == NOW
