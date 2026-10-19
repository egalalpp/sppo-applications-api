import pytest

from app.domain import AppStatus, OcrState, Role, can_transition, detect_file_type


def test_final_status_list():
    assert [s.value for s in AppStatus] == ["draft", "pending", "in_review", "approved", "rejected"]


def test_roles_have_operator_instead_of_moderator():
    assert [r.value for r in Role] == ["student", "operator", "admin"]


def test_ocr_states_for_operator():
    assert {s.value for s in OcrState} >= {"queued", "running", "ready_for_review", "ocr_failed"}


@pytest.mark.parametrize("current,target,role,allowed", [
    ("draft", "pending", "student", True),
    ("pending", "approved", "student", False),
    ("rejected", "pending", "student", False),  # после отказа заявка не переоткрывается
    ("draft", "pending", "operator", False),
    ("pending", "in_review", "operator", True),
    ("in_review", "approved", "operator", True),
    ("in_review", "rejected", "operator", True),
    ("in_review", "approved", "admin", True),
    ("in_review", "pending", "operator", True),  # вернуть в общую очередь
    ("pending", "approved", "operator", False),  # без взятия в работу
    ("pending", "rejected", "operator", False),
    ("approved", "rejected", "admin", False),  # финальный статус
])
def test_transitions(current, target, role, allowed):
    assert can_transition(current, target, role) is allowed


def test_detect_file_type():
    assert detect_file_type(b"%PDF-1.7\n") == ("application/pdf", "pdf")
    assert detect_file_type(b"\xff\xd8\xff\xe0abc") == ("image/jpeg", "jpg")
    assert detect_file_type(b"\x89PNG\r\n\x1a\n...") == ("image/png", "png")
    assert detect_file_type(b"MZ\x90\x00") is None  # exe под видом картинки
