import hashlib

from app.services import hashing

PEPPER = "pepper-pepper-pepper-1"


def test_hash_is_deterministic_and_normalized():
    expected = hashing.passport_hash("4510", "123456", PEPPER)
    assert hashing.passport_hash("4510", "123456", PEPPER) == expected
    assert hashing.passport_hash("45 10", " 123 456", PEPPER) == expected  # пробелы и разделители не важны
    assert hashing.account_hash("40817 810 7 0000 0000001", PEPPER) == hashing.account_hash("40817810700000000001", PEPPER)
    assert hashing.norm_digits("№ 45-10 123456") == "4510123456"


def test_hash_differs_for_different_values_and_peppers():
    assert hashing.passport_hash("4510", "123456", PEPPER) != hashing.passport_hash("4510", "123457", PEPPER)
    assert hashing.passport_hash("4510", "123456", PEPPER) != hashing.passport_hash("4510", "123456", PEPPER + "x")


def test_hash_is_keyed_not_plain_sha256():
    """Простой SHA-256 паспорта перебирается за минуты; HMAC с секретом сервера — нет."""
    plain = hashlib.sha256(b"4510123456").hexdigest()
    keyed = hashing.passport_hash("4510", "123456", PEPPER)
    assert keyed != plain and len(keyed) == 64


def test_default_pepper_comes_from_settings():
    assert hashing.keyed_hash("1") == hashing.keyed_hash("1", "test-pepper-test-pepper-1234")


def test_apply_hashes_sets_and_clears():
    student = {"passport": {"series": "4510", "number": "123456"}}
    payout = {"account_number": "40817810700000000001"}
    hashing.apply_hashes(student, payout, PEPPER)
    assert student["passport_hash"] == hashing.passport_hash("4510", "123456", PEPPER)
    assert payout["account_number_hash"] == hashing.account_hash("40817810700000000001", PEPPER)

    del student["passport"]
    hashing.apply_hashes(student, payout, PEPPER)
    assert "passport_hash" not in student  # паспорта нет — устаревшего хэша тоже нет
    hashing.apply_hashes(None, None)  # не падает
