"""Pydantic-модели запросов и ответов API (из них же строится OpenAPI-схема)."""

from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.domain import ATTACHMENT_TYPES, AppStatus, CheckStatus, OcrState, Role

T = TypeVar("T")

AttachmentType = Literal[tuple(ATTACHMENT_TYPES)]  # type: ignore[valid-type]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Problem(BaseModel):
    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


# ---------- Авторизация и пользователи ----------

class RegisterIn(Strict):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=3, max_length=200)


class RefreshIn(Strict):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: datetime


class UserCreate(RegisterIn):
    role: Role


class UserUpdate(Strict):
    full_name: str | None = Field(default=None, min_length=3, max_length=200)
    role: Role | None = None
    is_active: bool | None = None


# ---------- Справочник оснований ----------

class RequiredSlot(Strict):
    slot: str = Field(pattern=r"^[a-z_]{2,40}$")
    any_of: list[AttachmentType] = Field(min_length=1)
    note: str | None = None


class BenefitTypeBase(Strict):
    title: str = Field(min_length=3, max_length=200)
    form_no: int | None = Field(default=None, ge=1)
    priority: int = Field(default=100, ge=1, description="Меньше число — выше приоритет: очередь оператора "
                                                         "сортируется по нему. По умолчанию как номер бланка")
    requires: list[RequiredSlot] = []
    manual_flags: list[str] = []
    is_active: bool = True


class BenefitTypeIn(BenefitTypeBase):
    code: str = Field(pattern=r"^[a-z_]{3,64}$", examples=["orphan"])


class BenefitTypeOut(BenefitTypeIn):
    pass


# ---------- Заявки ----------

class PassportIn(Strict):
    series: str = Field(pattern=r"^\d{4}$")
    number: str = Field(pattern=r"^\d{6}$")
    issued_by: str | None = Field(default=None, max_length=300)
    issued_date: date | None = None


class StudentIn(Strict):
    student_id: str | None = Field(default=None, max_length=50)
    full_name: str = Field(min_length=5, max_length=200, examples=["Иванов Иван Иванович"],
                           description="ФИО в именительном падеже, как в паспорте")
    full_name_as_written: str | None = Field(
        default=None, max_length=200,
        description="Устарело: родительный падеж больше не нужен, поле игнорируется и оставлено для совместимости")
    birth_date: date
    institute: str = Field(min_length=2, max_length=100, examples=["ИУЦТ"])
    group: str = Field(min_length=2, max_length=50, examples=["ТКИ-211"])
    course: int = Field(ge=1, le=6)
    phone: str | None = Field(default=None, pattern=r"^\+?[\d\s\-()]{7,20}$")
    email: EmailStr = Field(description="Адрес для уведомлений о статусе заявки (принята, одобрена, отклонена)")
    inn: str | None = Field(default=None, pattern=r"^\d{12}$")
    registration_address: str | None = Field(default=None, max_length=500)
    passport: PassportIn | None = None


class PayoutIn(Strict):
    bank: str | None = Field(default=None, max_length=200, examples=["ПАО Сбербанк"])
    bik: str = Field(pattern=r"^\d{9}$", examples=["044525225"])
    account_number: str = Field(pattern=r"^\d{20}$")


class FormIn(BaseModel):
    """Электронная форма заявления: подписи студента нет, есть согласие на обработку ПДн.

    Поля прежних версий (applicant_signed, signed_date, dorm_head_signed) принимаются и игнорируются:
    так клиент, который ещё их присылает, не получит 422. Подпись заведующего общежитием ставит оператор.
    """
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    consent_pd_signed: bool = Field(default=False, description="Согласие на обработку ПДн (152-ФЗ)")
    consent_text_version: str | None = Field(default=None, max_length=50, examples=["2026-09"],
                                             description="Версия текста согласия, который видел студент")


class FormOut(FormIn):
    consent_pd_at: datetime | None = Field(default=None, description="Когда дано согласие (ставит сервер)")
    dorm_head_signed: bool | None = Field(default=None, description="Подпись заведующего общежитием (ставит оператор)")


class ApplicationCreate(Strict):
    basis: str = Field(examples=["orphan"])
    period: str | None = Field(default=None, pattern=r"^\d{4}-(spring|autumn)$", examples=["2026-autumn"],
                               description="По умолчанию — текущий период из настроек")
    student: StudentIn
    payout: PayoutIn
    form: FormIn = FormIn()


class ApplicationUpdate(Strict):
    student: StudentIn | None = None
    payout: PayoutIn | None = None
    form: FormIn | None = None


class AttachmentOcrOut(BaseModel):
    """Результат распознавания вложения. Только для оператора и администратора."""
    status: str
    engine: str | None = None
    confidence: int | None = Field(default=None, description="Средняя уверенность Tesseract по словам, %")
    pages: int | None = None
    words: int | None = None
    extracted: dict | None = None
    text_preview: str | None = None
    seal: bool | None = Field(default=None, description="Найдена печать")
    signature: bool | None = Field(default=None, description="Найдена подпись")
    notes: list[str] = []
    error: str | None = None
    finished_at: datetime | None = None


class AttachmentOut(BaseModel):
    attachment_id: str
    type: str
    slot: str
    content_type: str
    size_bytes: int
    sha256: str
    original_filename: str | None = None
    uploaded_at: datetime
    ocr: AttachmentOcrOut | None = Field(default=None, description="Только для оператора и администратора")


class CheckOut(BaseModel):
    code: str
    kind: Literal["auto", "manual"]
    status: CheckStatus
    score: int | None = None
    details: Any = None
    by: str
    at: datetime
    comment: str | None = None


class HistoryItem(BaseModel):
    status: AppStatus
    at: datetime
    by: str | None = Field(default=None, description="Только для оператора и администратора")
    by_role: str | None = None
    comment: str | None = Field(
        default=None, description="Студент видит только причину отказа; оператор — все заметки")


class OcrOut(BaseModel):
    """Состояние распознавания заявки. Студенту не показывается."""
    state: OcrState
    accuracy: int | None = Field(default=None, description="Точность распознавания, %")
    engine: str | None = None
    task_id: str | None = None
    updated_at: datetime | None = None
    error: str | None = None


class ApplicationOut(BaseModel):
    id: str
    applicant_user_id: str
    schema_version: int
    period: str
    basis: str
    student: StudentIn
    payout: PayoutIn
    form: FormOut
    attachments: list[AttachmentOut]
    status: AppStatus
    rejection_reason: str | None = Field(default=None, description="Причина отказа (при статусе rejected)")
    submitted_at: datetime | None = Field(default=None, description="Когда заявка отправлена на рассмотрение")
    created_at: datetime
    updated_at: datetime
    # Только для оператора и администратора; студент этих полей не получает
    priority: int | None = Field(default=None, description="Приоритет основания на момент создания заявки")
    checks: list[CheckOut] | None = None
    ocr: OcrOut | None = None


class ApplicationSummary(BaseModel):
    id: str
    basis: str
    period: str
    status: AppStatus
    rejection_reason: str | None = None
    student_full_name: str
    institute: str
    group: str
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    # Только для оператора и администратора
    priority: int | None = Field(default=None, description="Меньше число — выше приоритет")
    ocr_state: OcrState | None = None
    ocr_accuracy: int | None = None


class StatusOut(BaseModel):
    id: str
    status: AppStatus
    rejection_reason: str | None = None
    updated_at: datetime
    ocr: OcrOut | None = Field(default=None, description="Только для оператора и администратора")


class SubmitOut(BaseModel):
    id: str
    status: AppStatus


class OcrRestartOut(BaseModel):
    id: str
    ocr: OcrOut


class TransitionIn(Strict):
    to: AppStatus
    rejection_reason: str | None = Field(
        default=None, min_length=3, max_length=2000,
        description="Причина отказа: обязательна при to=rejected, студент увидит её в кабинете и в письме")
    comment: str | None = Field(default=None, max_length=2000, description="Внутренняя заметка оператора")

    @model_validator(mode="after")
    def _reason_only_for_rejection(self) -> "TransitionIn":
        if self.to == AppStatus.rejected and not self.rejection_reason:
            raise ValueError("rejection_reason обязателен при отклонении заявки")
        if self.to != AppStatus.rejected and self.rejection_reason:
            raise ValueError("rejection_reason допустим только при to=rejected")
        return self


class CheckDecisionIn(Strict):
    status: Literal[CheckStatus.manual_ok, CheckStatus.manual_rejected]
    comment: str | None = Field(default=None, max_length=2000)


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int


class TaskOut(BaseModel):
    task_id: str
    state: str


# ---------- Админка ----------

class AuditActor(BaseModel):
    type: str = Field(description="student | operator | admin | system")
    id: str
    role: str
    email: str | None = None


class AuditResource(BaseModel):
    type: str
    id: str


class AuditLogOut(BaseModel):
    id: str
    eventType: str
    actor: AuditActor
    resource: AuditResource
    timestamp: datetime
    details: dict
    request_id: str | None = None


class StatsOut(BaseModel):
    period: str
    applications_by_status: dict[str, int]
    applications_by_basis: dict[str, int]
    users_by_role: dict[str, int]
    queue_length: int
    max_queue_length: int
