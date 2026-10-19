from app.services.ocr_fields import (
    compare_bank,
    compare_passport,
    contains_digits,
    find_accounts,
    find_biks,
    find_passport_numbers,
)

ACCOUNT = "40817810000000000001"


def test_passport_number_formats():
    for text in ("45 10 123456", "4510 123456", "4510123456", "Серия 45 10 № 123456"):
        assert ("4510", "123456") in find_passport_numbers(text), text
    assert find_passport_numbers("123 4567 89") == []


def test_compare_passport():
    assert compare_passport("паспорт 45 10 123456", "4510", "123456")[0] == "ok"
    outcome, found = compare_passport("паспорт 45 11 765432", "4510", "123456")
    assert outcome == "mismatch" and found == ["4511 765432"]
    assert compare_passport("ничего не видно", "4510", "123456")[0] == "not_found"


def test_account_with_spaces_and_dashes():
    assert find_accounts("Счёт: 40817 810 0 0000 0000001") == [ACCOUNT]
    assert find_accounts(f"Счёт {ACCOUNT[:5]}-{ACCOUNT[5:]}") == [ACCOUNT]


def test_account_glued_to_bik():
    assert ACCOUNT in find_accounts(f"044525225 {ACCOUNT}")


def test_bik():
    assert find_biks("БИК 044525225, корр. 30101810400000000225") == ["044525225"]


def test_contains_digits_ignores_separators():
    assert contains_digits("р/с 40817 810 0 0000 0000001", ACCOUNT)
    assert not contains_digits("р/с 40817 810 0 0000 0000002", ACCOUNT)


def test_compare_bank():
    text = f"БИК 044525225\nСчёт {ACCOUNT}\nПолучатель Иван Иванович И."
    assert compare_bank(text, "044525225", ACCOUNT)[0] == "ok"
    assert compare_bank(text, "044525226", ACCOUNT)[0] == "mismatch"  # другой БИК
    assert compare_bank(text, "044525225", "40817810000000000002")[0] == "mismatch"
    assert compare_bank("размытый скриншот", "044525225", ACCOUNT)[0] == "not_found"
    # БИК прочитан, счёт — нет: проверить автоматически нельзя, это не повод для mismatch
    assert compare_bank("БИК 044525225", "044525225", ACCOUNT)[0] == "not_found"
