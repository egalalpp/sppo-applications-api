import pytest

from app.domain import can_transition, detect_file_type


@pytest.mark.parametrize("current,target,role,allowed", [
    ("draft", "pending", "student", True),
    ("need_more_docs", "pending", "student", True),
    ("pending", "approved", "student", False),
    ("draft", "pending", "moderator", False),
    ("pending", "in_review", "moderator", True),
    ("in_review", "approved", "moderator", True),
    ("in_review", "approved", "admin", True),
    ("pending", "approved", "moderator", False),  # без взятия в работу
    ("approved", "rejected", "admin", False),  # финальный статус
])
def test_transitions(current, target, role, allowed):
    assert can_transition(current, target, role) is allowed


def test_detect_file_type():
    assert detect_file_type(b"%PDF-1.7\n") == ("application/pdf", "pdf")
    assert detect_file_type(b"\xff\xd8\xff\xe0abc") == ("image/jpeg", "jpg")
    assert detect_file_type(b"\x89PNG\r\n\x1a\n...") == ("image/png", "png")
    assert detect_file_type(b"MZ\x90\x00") is None  # exe под видом картинки
