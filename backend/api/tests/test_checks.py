from datetime import UTC, datetime

from app.seed import BENEFIT_TYPES
from app.services.checks import build_auto_checks, build_manual_checks, merge_checks, missing_slots
from tests.test_validators import make_account, make_inn

NOW = datetime(2026, 10, 11, tzinfo=UTC)
ORPHAN = next(t for t in BENEFIT_TYPES if t["_id"] == "orphan")
DORMITORY = next(t for t in BENEFIT_TYPES if t["_id"] == "dormitory")
ACCOUNT = make_account("044525225", "4081781000000000001")


def att(type_: str, slot: str, sha: str = "x") -> dict:
    return {"attachment_id": type_, "type": type_, "slot": slot, "storage": {"sha256": sha}}


FULL_ORPHAN_SET = [
    att("passport_registration", "passport"),
    att("union_card", "union_card"),
    att("bank_requisites_screenshot", "bank"),
    att("birth_certificate_student", "birth"),
    att("parental_rights_deprivation", "basis_doc"),  # вторая альтернатива из «ИЛИ»
]

GOOD_OCR = {
    "passport_registration": "ИВАНОВ ИВАН ИВАНОВИЧ\n45 10 123456",
    "bank_requisites_screenshot": f"БИК 044525225\nСчёт {ACCOUNT}\nИван Иванович И.",
}


def make_app(attachments) -> dict:
    return {
        "student": {
            "full_name": "Иванов Иван Иванович",
            "inn": make_inn("7707083893"),
            "passport": {"series": "4510", "number": "123456"},
        },
        "payout": {"bik": "044525225", "account_number": ACCOUNT},
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


def test_all_good_with_ocr():
    checks = by_code(build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW, ocr_texts=GOOD_OCR))
    for code in ("required_docs_complete", "inn_checksum", "passport_format", "bank_account_checksum",
                 "fio_form_vs_passport", "passport_number_vs_scan", "bank_requisites_vs_scan",
                 "bank_owner_confirmed", "duplicate_files", "duplicate_payout"):
        assert checks[code]["status"] == "auto_ok", code
    assert "fio_nominative_vs_written" not in checks  # родительный падеж больше не нужен


def test_ocr_failure_does_not_break_other_checks():
    """Распознавание упало: проверки без OCR посчитаны, остальные ждут оператора."""
    checks = by_code(build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW, ocr_texts={}))
    assert checks["inn_checksum"]["status"] == "auto_ok"
    assert checks["bank_account_checksum"]["status"] == "auto_ok"
    for code in ("fio_form_vs_passport", "passport_number_vs_scan", "bank_requisites_vs_scan",
                 "bank_owner_confirmed"):
        assert checks[code]["status"] == "needs_review", code


def test_ocr_mismatches_are_detected():
    ocr = {
        "passport_registration": "СИДОРОВА ЕКАТЕРИНА ПАВЛОВНА\n45 11 765432",
        "bank_requisites_screenshot": "БИК 044525225\nСчёт 40817810000000000002\nМария Сергеевна П.",
    }
    checks = by_code(build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW, ocr_texts=ocr))
    assert checks["fio_form_vs_passport"]["status"] == "mismatch"
    assert checks["passport_number_vs_scan"]["status"] == "mismatch"
    assert checks["passport_number_vs_scan"]["details"] == {"found_in_scan": ["4511 765432"]}
    assert checks["bank_requisites_vs_scan"]["status"] == "mismatch"
    assert checks["bank_owner_confirmed"]["status"] == "needs_review"  # не mismatch: только ручная проверка


def test_unreadable_scan_goes_to_operator():
    ocr = {"passport_registration": "", "bank_requisites_screenshot": ""}
    checks = by_code(build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW, ocr_texts=ocr))
    assert checks["passport_number_vs_scan"]["status"] == "needs_review"
    assert checks["bank_requisites_vs_scan"]["status"] == "needs_review"


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


def test_merge_keeps_operator_decisions_on_any_check():
    auto = build_auto_checks(make_app(FULL_ORPHAN_SET), ORPHAN, {}, NOW, ocr_texts={})
    manual = build_manual_checks(ORPHAN, NOW)
    existing = [
        {"code": "union_card_extended", "kind": "manual", "status": "manual_ok", "by": "op"},
        {"code": "fio_form_vs_passport", "kind": "auto", "status": "manual_ok", "by": "op"},
        {"code": "inn_checksum", "kind": "auto", "status": "auto_ok", "by": "system"},
    ]
    merged = by_code(merge_checks(existing, auto, manual))
    assert merged["union_card_extended"]["status"] == "manual_ok"
    # решение оператора по автоматической проверке не затирается повторным распознаванием
    assert merged["fio_form_vs_passport"]["status"] == "manual_ok"
    assert merged["inn_checksum"]["by"] == "system"
