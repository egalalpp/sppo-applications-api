import pytest

from app.services.notifications import application_number, render

OCR_WORDS = ("ocr", "распознав", "tesseract", "точность", "accuracy")


@pytest.mark.parametrize("status", ["pending", "approved", "rejected"])
def test_student_emails_never_mention_ocr(status):
    subject, text, html = render(status, number="A1B2C3", basis_title="Студент-сирота",
                                 rejection_reason="Не хватает справки", cabinet_url="https://x.github.io/cabinet.html")
    for part in (subject, text, html):
        assert not any(word in part.lower() for word in OCR_WORDS)
    assert "A1B2C3" in text


def test_pending_email():
    subject, text, _ = render("pending", number="A1B2C3", basis_title="Студент-сирота")
    assert "принята" in subject
    assert "Студент-сирота" in text and "О решении мы сообщим" in text


def test_approved_email():
    subject, text, _ = render("approved", number="A1B2C3", basis_title="Студент-сирота")
    assert "одобрена" in subject and "одобрена" in text


def test_rejected_email_contains_reason():
    subject, text, html = render("rejected", number="A1B2C3", basis_title="Студент-сирота",
                                 rejection_reason="Справка просрочена")
    assert "отклонена" in subject
    assert "Причина: Справка просрочена" in text and "Справка просрочена" in html


def test_reason_is_html_escaped():
    _, _, html = render("rejected", number="A1B2C3", basis_title="Тест", rejection_reason="<script>alert(1)</script>")
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_cabinet_link():
    _, text, html = render("approved", number="A1B2C3", basis_title="Тест", cabinet_url="https://x.github.io/cabinet.html")
    assert "https://x.github.io/cabinet.html" in text and "https://x.github.io/cabinet.html" in html


@pytest.mark.parametrize("status", ["draft", "in_review"])
def test_no_email_for_other_statuses(status):
    with pytest.raises(ValueError):
        render(status, number="A", basis_title="T")


def test_application_number():
    assert application_number("6acd52dda70b810daca703cf") == "A703CF"
