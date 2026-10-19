"""Доменные константы: роли, статусы, переходы, типы документов.

Статусная модель (обновлённое ТЗ): draft -> pending -> in_review -> approved | rejected.
Промежуточного статуса «нужны документы» нет: оператор либо одобряет заявку, либо отклоняет
с обязательной причиной (rejection_reason).
"""

from enum import StrEnum


class Role(StrEnum):
    student = "student"
    operator = "operator"  # член комиссии профкома (раньше — «модератор»)
    admin = "admin"


STAFF_ROLES = (Role.operator, Role.admin)


class AppStatus(StrEnum):
    draft = "draft"
    pending = "pending"
    in_review = "in_review"
    approved = "approved"
    rejected = "rejected"


class CheckStatus(StrEnum):
    auto_ok = "auto_ok"
    needs_review = "needs_review"
    mismatch = "mismatch"
    manual_ok = "manual_ok"
    manual_rejected = "manual_rejected"


class OcrState(StrEnum):
    """Состояние распознавания заявки. Видно только оператору и администратору:
    студент о распознавании ничего не знает."""

    not_started = "not_started"
    queued = "queued"
    running = "running"
    ready_for_review = "ready_for_review"
    ocr_failed = "ocr_failed"


OCR_IN_FLIGHT = {OcrState.queued, OcrState.running}

# Студент может редактировать заявку и менять вложения только в черновике.
EDITABLE_STATUSES = {AppStatus.draft}

# Переходы, которые студент делает сам (через POST /submit).
STUDENT_TRANSITIONS = {
    (AppStatus.draft, AppStatus.pending),
}

# Переходы, которые делает оператор или админ (через POST /transitions).
STAFF_TRANSITIONS = {
    (AppStatus.pending, AppStatus.in_review),
    (AppStatus.in_review, AppStatus.pending),  # вернуть в общую очередь
    (AppStatus.in_review, AppStatus.approved),
    (AppStatus.in_review, AppStatus.rejected),
}

# Статусы, о переходе в которые студент получает письмо.
NOTIFY_STATUSES = {AppStatus.pending, AppStatus.approved, AppStatus.rejected}

# Проверки в этих статусах блокируют одобрение заявки: оператор должен разобрать их вручную.
BLOCKING_CHECK_STATUSES = {
    CheckStatus.needs_review,
    CheckStatus.mismatch,
    CheckStatus.manual_rejected,
}


def can_transition(current: str, target: str, role: str) -> bool:
    pair = (AppStatus(current), AppStatus(target))
    if role == Role.student:
        return pair in STUDENT_TRANSITIONS
    if role in STAFF_ROLES:
        return pair in STAFF_TRANSITIONS
    return False


ATTACHMENT_TYPES = [
    "passport_registration",
    "union_card",
    "bank_requisites_screenshot",
    "birth_certificate_student",
    "birth_certificate_child",
    "death_certificate",
    "parental_rights_deprivation",
    "paternity_basis_certificate",
    "missing_person_court_decision",
    "pension_certificate_parents",
    "large_family_certificate",
    "family_composition_certificate",
    "disability_certificate",
    "military_service_certificate",
    "military_id",
    "medical_certificate",
    "chernobyl_certificate",
    "divorce_certificate",
    "grades_extract",
    "other",
]

MAX_ATTACHMENTS = 12

# Разрешённые форматы файлов: сигнатура (magic bytes) -> (content-type, расширение).
# Тип определяется по содержимому, а не по заголовку от клиента.
FILE_SIGNATURES = {
    b"%PDF-": ("application/pdf", "pdf"),
    b"\xff\xd8\xff": ("image/jpeg", "jpg"),
    b"\x89PNG\r\n\x1a\n": ("image/png", "png"),
}


def detect_file_type(head: bytes) -> tuple[str, str] | None:
    for signature, result in FILE_SIGNATURES.items():
        if head.startswith(signature):
            return result
    return None
