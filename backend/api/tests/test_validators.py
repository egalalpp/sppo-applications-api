import pytest

from app.services.validators import account_ok, inn12_ok, passport_format_ok


def make_inn(prefix10: str) -> str:
    d = list(map(int, prefix10))
    c1 = sum(a * b for a, b in zip([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10
    d.append(c1)
    c2 = sum(a * b for a, b in zip([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d)) % 11 % 10
    return prefix10 + str(c1) + str(c2)


def make_account(bik: str, account19: str) -> str:
    """Подбирает контрольную цифру (9-я позиция счёта) так, чтобы ключ сошёлся."""
    for key in "0123456789":
        account = account19[:8] + key + account19[8:]
        if account_ok(bik, account):
            return account
    raise AssertionError("no key digit found")


def test_inn_known_valid():
    assert inn12_ok("500100732259")


def test_inn_generated_and_corrupted():
    inn = make_inn("7707083893")
    assert inn12_ok(inn)
    corrupted = inn[:-1] + str((int(inn[-1]) + 1) % 10)
    assert not inn12_ok(corrupted)


@pytest.mark.parametrize("inn", ["", "12345", "50010073225a", "5001007322591"])
def test_inn_bad_format(inn):
    assert not inn12_ok(inn)


def test_account_generated_and_corrupted():
    bik = "044525225"
    account = make_account(bik, "4081781000000000001")
    assert len(account) == 20
    assert account_ok(bik, account)
    corrupted = account[:-1] + str((int(account[-1]) + 1) % 10)
    assert not account_ok(bik, corrupted)


@pytest.mark.parametrize("bik,account", [("04452522", "4" * 20), ("044525225", "123"), ("04452522x", "4" * 20)])
def test_account_bad_format(bik, account):
    assert not account_ok(bik, account)


def test_passport_format():
    assert passport_format_ok("4510", "123456")
    assert not passport_format_ok("451", "123456")
    assert not passport_format_ok("4510", "12345a")
