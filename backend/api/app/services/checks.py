"""Формирование списка проверок заявки (checks[]).

Чистые функции без обращений к БД и Tesseract: на вход документ заявки, справочник основания,
найденные дубли и распознанные тексты, на выходе список проверок. Их вызывает Celery-воркер,
и их же покрывают юнит-тесты.

Проверки, не зависящие от OCR (комплект, ИНН, счёт, паспорт, дубли), считаются всегда.
Проверки по распознанным сканам при сбое распознавания получают статус needs_review:
сбой OCR — внутреннее дело оператора, он проверяет такие пункты глазами.
"""

from datetime import datetime

from app.domain import CheckStatus
from app.services import fio, ocr_fields, validators

# Проверки, которые делает только человек (раздел 6.5 схемы).
BASE_MANUAL_CHECKS = {
    "union_card_extended": "Профсоюзный билет продлён на текущий год",
    "applicant_signature": "Подпись студента на заявлении",
}

MANUAL_FLAG_TITLES = {
    "dorm_head_signed": "Подпись заведующего общежитием",
    "doctor_certified": "Справка заверена врачом (подпись и печать)",
}

OCR_UNAVAILABLE = "Распознавание не выполнено: проверьте вручную по скану"


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


def _ocr_checks(student: dict, payout: dict, ocr_texts: dict[str, str], now: datetime) -> list[dict]:
    """Сверка формы со сканами. ocr_texts: тип документа -> распознанный текст;
    типа нет в словаре, если распознать его не удалось."""
    checks = []
    passport_text = ocr_texts.get("passport_registration")
    bank_text = ocr_texts.get("bank_requisites_screenshot")

    # ФИО из формы и в паспорте
    if passport_text is None:
        checks.append(_check("fio_form_vs_passport", "auto", CheckStatus.needs_review, now,
                             details=OCR_UNAVAILABLE))
    else:
        score = fio.name_in_text(passport_text, student.get("full_name", ""))
        checks.append(_check("fio_form_vs_passport", "auto", fio.classify(score), now, score=score))

    # Серия и номер паспорта: точное совпадение цифр
    passport = student.get("passport")
    if not passport:
        checks.append(_check("passport_number_vs_scan", "auto", CheckStatus.needs_review, now,
                             details="Паспортные данные не указаны"))
    elif passport_text is None:
        checks.append(_check("passport_number_vs_scan", "auto", CheckStatus.needs_review, now,
                             details=OCR_UNAVAILABLE))
    else:
        outcome, found = ocr_fields.compare_passport(passport_text, passport["series"], passport["number"])
        status = {"ok": CheckStatus.auto_ok, "mismatch": CheckStatus.mismatch}.get(outcome, CheckStatus.needs_review)
        details = None if outcome == "ok" else (
            {"found_in_scan": found} if found else "Серия и номер на скане не распознаны")
        checks.append(_check("passport_number_vs_scan", "auto", status, now, details=details))

    # Реквизиты: БИК и счёт из формы на скриншоте банка
    if bank_text is None:
        checks.append(_check("bank_requisites_vs_scan", "auto", CheckStatus.needs_review, now,
                             details=OCR_UNAVAILABLE))
        checks.append(_check("bank_owner_confirmed", "auto", CheckStatus.needs_review, now,
                             details=OCR_UNAVAILABLE))
    else:
        outcome, info = ocr_fields.compare_bank(bank_text, payout.get("bik", ""), payout.get("account_number", ""))
        status = {"ok": CheckStatus.auto_ok, "mismatch": CheckStatus.mismatch}.get(outcome, CheckStatus.needs_review)
        checks.append(_check("bank_requisites_vs_scan", "auto", status, now,
                             details=None if outcome == "ok" else info))
        # На скриншоте должно быть хоть какое-то подтверждение, что счёт принадлежит заявителю
        # (ответ оператора профкома на интервью). Без совпадения — только на ручную проверку.
        score = fio.given_names_in_text(bank_text, student.get("full_name", ""))
        confirmed = score >= fio.AUTO_OK_THRESHOLD
        checks.append(_check(
            "bank_owner_confirmed", "auto",
            CheckStatus.auto_ok if confirmed else CheckStatus.needs_review, now, score=score,
            details=None if confirmed else "На скриншоте не найдены имя и отчество владельца счёта"))
    return checks


def build_auto_checks(app: dict, benefit_type: dict, duplicates: dict, now: datetime,
                      ocr_texts: dict[str, str] | None = None) -> list[dict]:
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

    checks += _ocr_checks(student, payout, ocr_texts or {}, now)

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

    dup_passport = duplicates.get("passport", [])
    checks.append(_check(
        "duplicate_passport", "auto",
        CheckStatus.needs_review if dup_passport else CheckStatus.auto_ok, now,
        details={"application_ids": dup_passport} if dup_passport else None,
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
    """При повторном распознавании пересчитываются все проверки, кроме тех, по которым
    оператор уже принял решение: ручное решение (manual_ok / manual_rejected) сохраняется."""
    decided = {
        c["code"]: c
        for c in existing
        if c.get("status") in (CheckStatus.manual_ok, CheckStatus.manual_rejected)
    }
    return [decided.get(c["code"], c) for c in auto + manual]
