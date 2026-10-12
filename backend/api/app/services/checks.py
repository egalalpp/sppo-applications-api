"""Формирование списка проверок заявки (checks[]).

Чистые функции без обращений к БД: на вход документ заявки, справочник основания
и найденные дубли, на выходе список проверок. Их вызывает Celery-воркер,
и их же покрывают юнит-тесты.
"""

from datetime import datetime

from app.domain import CheckStatus
from app.services import fio, validators

# Проверки, которые делает только человек (раздел 6.5 схемы).
BASE_MANUAL_CHECKS = {
    "union_card_extended": "Профсоюзный билет продлён на текущий год",
    "applicant_signature": "Подпись студента на заявлении",
}

MANUAL_FLAG_TITLES = {
    "dorm_head_signed": "Подпись заведующего общежитием",
    "doctor_certified": "Справка заверена врачом (подпись и печать)",
}


def _check(code: str, kind: str, status: str, now: datetime, details=None, score=None) -> dict:
    return {
        "code": code,
        "kind": kind,
        "status": status,
        "score": score,
        "details": details,
        "by": "system",
        "at": now,
    }


def missing_slots(attachments: list[dict], benefit_type: dict) -> list[str]:
    """Слоты справочника, для которых нет ни одного подходящего вложения."""
    present = {(a.get("slot"), a.get("type")) for a in attachments}
    missing = []
    for req in benefit_type.get("requires", []):
        if not any((req["slot"], t) in present for t in req["any_of"]):
            missing.append(req["slot"])
    return missing


def build_auto_checks(app: dict, benefit_type: dict, duplicates: dict, now: datetime) -> list[dict]:
    student = app.get("student", {})
    payout = app.get("payout", {})
    checks = []

    missing = missing_slots(app.get("attachments", []), benefit_type)
    checks.append(_check(
        "required_docs_complete", "auto",
        CheckStatus.mismatch if missing else CheckStatus.auto_ok, now,
        details={"missing_slots": missing} if missing else None,
    ))

    inn = student.get("inn")
    if inn:
        ok = validators.inn12_ok(inn)
        checks.append(_check("inn_checksum", "auto",
                             CheckStatus.auto_ok if ok else CheckStatus.mismatch, now,
                             details=None if ok else "Контрольные цифры ИНН не сходятся"))
    else:
        checks.append(_check("inn_checksum", "auto", CheckStatus.needs_review, now,
                             details="ИНН не указан"))

    passport = student.get("passport")
    if passport:
        ok = validators.passport_format_ok(passport.get("series", ""), passport.get("number", ""))
        checks.append(_check("passport_format", "auto",
                             CheckStatus.auto_ok if ok else CheckStatus.mismatch, now))
    else:
        checks.append(_check("passport_format", "auto", CheckStatus.needs_review, now,
                             details="Паспортные данные не указаны"))

    ok = validators.account_ok(payout.get("bik", ""), payout.get("account_number", ""))
    checks.append(_check("bank_account_checksum", "auto",
                         CheckStatus.auto_ok if ok else CheckStatus.mismatch, now,
                         details=None if ok else "Контрольный ключ счёта по БИК не сходится"))

    score = fio.fio_similarity(student.get("full_name", ""), student.get("full_name_as_written", ""))
    checks.append(_check("fio_nominative_vs_written", "auto", fio.classify(score), now, score=score))

    # OCR в прототипе — заглушка, поэтому сверка со сканом паспорта уходит человеку.
    checks.append(_check("fio_form_vs_passport", "auto", CheckStatus.needs_review, now,
                         details="OCR-заглушка: сверьте ФИО со сканом паспорта вручную"))

    dup_files = duplicates.get("files", [])
    checks.append(_check(
        "duplicate_files", "auto",
        CheckStatus.needs_review if dup_files else CheckStatus.auto_ok, now,
        details={"application_ids": dup_files} if dup_files else None,
    ))

    dup_payout = duplicates.get("payout", [])
    checks.append(_check(
        "duplicate_payout", "auto",
        CheckStatus.needs_review if dup_payout else CheckStatus.auto_ok, now,
        details={"application_ids": dup_payout} if dup_payout else None,
    ))
    return checks


def build_manual_checks(benefit_type: dict, now: datetime) -> list[dict]:
    titles = dict(BASE_MANUAL_CHECKS)
    for flag in benefit_type.get("manual_flags", []):
        titles[flag] = MANUAL_FLAG_TITLES.get(flag, flag)
    return [
        _check(code, "manual", CheckStatus.needs_review, now, details=title)
        for code, title in titles.items()
    ]


def merge_checks(existing: list[dict], auto: list[dict], manual: list[dict]) -> list[dict]:
    """Автопроверки пересчитываются заново (после досылки документов прежние решения
    по ним устарели); решения модератора по ручным проверкам сохраняются."""
    decided = {
        c["code"]: c
        for c in existing
        if c.get("kind") == "manual"
        and c.get("status") in (CheckStatus.manual_ok, CheckStatus.manual_rejected)
    }
    return auto + [decided.get(c["code"], c) for c in manual]
