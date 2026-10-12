"""Pydantic-модели запросов и ответов API (из них же строится OpenAPI-схема)."""

from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.domain import ATTACHMENT_TYPES, AppStatus, CheckStatus, ProcessingState, Role

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
                           description="ФИО в именительном падеже")
    full_name_as_written: str = Field(min_length=5, max_length=200, examples=["Иванова Ивана Ивановича"],
                                      description="ФИО как в заявлении (родительный падеж)")
    birth_date: date
    institute: str = Field(min_length=2, max_length=100, examples=["ИУЦТ"])
    group: str = Field(min_length=2, max_length=50, examples=["ТКИ-211"])
    course: int = Field(ge=1, le=6)
    phone: str | None = Field(default=None, pattern=r"^\+?[\d\s\-()]{7,20}$")
    email: EmailStr | None = None
    inn: str | None = Field(default=None, pattern=r"^\d{12}$")
    registration_address: str | None = Field(default=None, max_length=500)
    passport: PassportIn | None = None


class PayoutIn(Strict):
    bank: str | None = Field(default=None, max_length=200, examples=["ПАО Сбербанк"])
    bik: str = Field(pattern=r"^\d{9}$", examples=["044525225"])
    account_number: str = Field(pattern=r"^\d{20}$")


class FormIn(Strict):
    applicant_signed: bool = False
    signed_date: date | None = None
    consent_pd_signed: bool = Field(default=False, description="Согласие на обработку ПДн (152-ФЗ)")
    dorm_head_signed: bool | None = None


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


class AttachmentOut(BaseModel):
    attachment_id: str
    type: str
    slot: str
    content_type: str
    size_bytes: int
    sha256: str
    original_filename: str | None = None
    uploaded_at: datetime
    ocr_status: str


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
    by: str
    by_role: str | None = None
    comment: str | None = None


class ProcessingOut(BaseModel):
    state: ProcessingState
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
    form: FormIn
    attachments: list[AttachmentOut]
    checks: list[CheckOut]
    status: AppStatus
    processing: ProcessingOut
    created_at: datetime
    updated_at: datetime


class ApplicationSummary(BaseModel):
    id: str
    basis: str
    period: str
    status: AppStatus
    student_full_name: str
    institute: str
    group: str
    processing_state: ProcessingState
    created_at: datetime
    updated_at: datetime


class StatusOut(BaseModel):
    id: str
    status: AppStatus
    processing: ProcessingOut
    updated_at: datetime


class SubmitOut(BaseModel):
    id: str
    status: AppStatus
    task_id: str
    status_url: str


class TransitionIn(Strict):
    to: AppStatus
    comment: str | None = Field(default=None, max_length=2000)


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

class AuditEventOut(BaseModel):
    id: str
    at: datetime
    actor_id: str
    actor_email: str
    actor_role: str
    action: str
    target_type: str
    target_id: str
    details: dict


class StatsOut(BaseModel):
    period: str
    applications_by_status: dict[str, int]
    applications_by_basis: dict[str, int]
    users_by_role: dict[str, int]
    queue_length: int
    max_queue_length: int
