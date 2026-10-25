from datetime import UTC, datetime

from bson import ObjectId

from app import audit
from app.bootstrap import legacy_event_to_log, legacy_event_type
from app.context import request_id_var

OPERATOR = {"_id": ObjectId(), "email": "operator@rut.ru", "role": "operator"}


def test_event_is_flat_and_complete():
    token = request_id_var.set("req-123")
    try:
        event = audit.build_event(OPERATOR, audit.CLAIM_REJECTED, "application", ObjectId("6acd52dda70b810daca703cf"),
                                  {"from": "in_review", "to": "rejected", "rejection_reason": "Нет справки"})
    finally:
        request_id_var.reset(token)
    assert event["eventType"] == "claim_rejected"
    assert event["actor"] == {"type": "operator", "id": str(OPERATOR["_id"]), "role": "operator",
                              "email": "operator@rut.ru"}
    assert event["resource"] == {"type": "application", "id": "6acd52dda70b810daca703cf"}
    assert event["details"]["rejection_reason"] == "Нет справки"
    assert event["request_id"] == "req-123"
    assert isinstance(event["timestamp"], datetime) and event["timestamp"].tzinfo == UTC
    assert set(event) == {"eventType", "actor", "resource", "timestamp", "details", "request_id"}


def test_system_actor():
    event = audit.build_event(audit.system_actor("ocr-worker"), audit.OCR_DONE, "application", "abc")
    assert event["actor"] == {"type": "system", "id": "ocr-worker", "role": "system", "email": None}
    assert event["request_id"] is None and event["details"] == {}


def test_status_transitions_map_to_event_types():
    assert audit.TRANSITION_EVENTS[("draft", "pending")] == "claim_submitted"
    assert audit.TRANSITION_EVENTS[("pending", "in_review")] == "claim_review_started"
    assert audit.TRANSITION_EVENTS[("in_review", "pending")] == "claim_released"
    assert audit.TRANSITION_EVENTS[("in_review", "approved")] == "claim_approved"
    assert audit.TRANSITION_EVENTS[("in_review", "rejected")] == "claim_rejected"


def test_legacy_audit_events_are_mapped():
    assert legacy_event_type("application.transition", {"from": "in_review", "to": "approved"}) == "claim_approved"
    assert legacy_event_type("application.transition", {"from": "pending", "to": "in_review"}) == "claim_review_started"
    assert legacy_event_type("application.check_decision", None) == "check_set"
    assert legacy_event_type("attachment.download_url", None) == "scan_viewed"
    assert legacy_event_type("application.ocr_restart", None) == "ocr_restarted"
    assert legacy_event_type("something.new", None) == "something.new"  # неизвестное название сохраняется как есть


def test_legacy_event_conversion():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    legacy = {"at": now, "actor_id": OPERATOR["_id"], "actor_email": "m@rut.ru", "actor_role": "moderator",
              "action": "application.transition", "target_type": "application", "target_id": "abc",
              "details": {"from": "in_review", "to": "rejected", "comment": "Нет справки"}}
    event = legacy_event_to_log(legacy)
    assert event["eventType"] == "claim_rejected" and event["timestamp"] == now
    assert event["actor"]["type"] == "operator"  # moderator -> operator
    assert event["details"]["rejection_reason"] == "Нет справки"  # причина лежала в comment
    assert event["resource"] == {"type": "application", "id": "abc"}
