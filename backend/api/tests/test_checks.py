from datetime import UTC, datetime

from app.seed import BENEFIT_TYPES
from app.services.checks import build_auto_checks, build_manual_checks, merge_checks, missing_slots
from tests.test_validators import make_account, make_inn

NOW = datetime(2026, 10, 11, tzinfo=UTC)
ORPHAN = next(t for t in BENEFIT_TYPES if t["_id"] == "orphan")
DORMITORY = next(t for t in BENEFIT_TYPES if t["_id"] == "dormitory")


def att(type_: str, slot: str, sha: str = "x") -> dict:
    return {"attachment_id": type_, "type": type_, "slot": slot, "storage": {"sha256": sha}}


FULL_ORPHAN_SET = [
    att("passport_registration", "passport"),
    att("union_card", "union_card"),
    att("bank_requisites_screenshot", "bank"),
    att("birth_certificate_student", "birth"),
    att("parental_rights_deprivation", "basis_doc"),  # вторая альтернатива из «ИЛИ»
]


def make_app(attachments) -> dict:
    return {
        "student": {
            "full_name": "Иванов Иван Иванович",
            "full_name_as_written": "Иванова Ивана Ивановича",
            "inn": make_inn("7707083893"),
            "passport": {"series": "4510", "number": "123456"},
        },
        "payout": {"bik": "044525225", "account_number": make_account("044525225", "4081781000000000001")},
        "attachments": attachments,
        "checks": [],
    }


def by_code(checks):
    return {c["code"]: c for c in checks}


def test_seed_has_13_types():
    assert len(BENEFIT_TYPES) == 13
    assert {t["form_no"] for t in BENEFIT_TYPES} == set(range(1, 14))


def test_missing_slots_any_of():
    assert missing_slots(FULL_ORPHAN_SET, ORPHAN) == []
    assert missing_slots(FULL_ORPHAN_SET[:3], ORPHAN) == ["birth", "basis_doc"]


def test_auto_checks_all_good():
    checks = by_code(build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW))
    for code in ("required_docs_complete", "inn_checksum", "passport_format",
                 "bank_account_checksum", "fio_nominative_vs_written", "duplicate_files", "duplicate_payout"):
        assert checks[code]["status"] == "auto_ok", code
    # Без настоящего OCR сверка со сканом паспорта всегда уходит человеку.
    assert checks["fio_form_vs_passport"]["status"] == "needs_review"


def test_auto_checks_detect_problems():
    app = make_app(FULL_ORPHAN_SET[:2])
    app["student"]["inn"] = "500100732250"
    app["payout"]["account_number"] = "40817810000000000000"
    checks = by_code(build_auto_checks(app, ORPHAN, {"files": ["abc"], "payout": []}, NOW))
    assert checks["required_docs_complete"]["status"] == "mismatch"
    assert set(checks["required_docs_complete"]["details"]["missing_slots"]) == {"bank", "birth", "basis_doc"}
    assert checks["inn_checksum"]["status"] == "mismatch"
    assert checks["duplicate_files"]["status"] == "needs_review"


def test_manual_checks_include_type_flags():
    codes = {c["code"] for c in build_manual_checks(DORMITORY, NOW)}
    assert codes == {"union_card_extended", "applicant_signature", "dorm_head_signed"}


def test_merge_keeps_manual_decisions_only():
    auto = build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW)
    manual = build_manual_checks(ORPHAN, NOW)
    existing = [
        {"code": "union_card_extended", "kind": "manual", "status": "manual_ok", "by": "mod"},
        {"code": "fio_form_vs_passport", "kind": "auto", "status": "manual_ok", "by": "mod"},
    ]
    merged = by_code(merge_checks(existing, auto, manual))
    assert merged["union_card_extended"]["status"] == "manual_ok"
    assert merged["fio_form_vs_passport"]["status"] == "needs_review"  # автопроверка пересчитана
