"""Детерминированные проверки реквизитов (schema_zayavok_sppo.md, раздел 6.1)."""

import re

_PASSPORT_SERIES = re.compile(r"^\d{4}$")
_PASSPORT_NUMBER = re.compile(r"^\d{6}$")


def account_ok(bik: str, account: str) -> bool:
    """Контрольный ключ лицевого счёта по БИК (вариант для коммерческих банков)."""
    if len(bik) != 9 or len(account) != 20 or not (bik + account).isdigit():
        return False
    digits = bik[-3:] + account
    weights = [7, 1, 3] * 8
    return sum(int(d) * w % 10 for d, w in zip(digits, weights)) % 10 == 0


def inn12_ok(inn: str) -> bool:
    """Контрольные цифры ИНН физического лица (12 знаков)."""
    if len(inn) != 12 or not inn.isdigit():
        return False
    d = list(map(int, inn))
    c1 = sum(a * b for a, b in zip([7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d[:10])) % 11 % 10
    c2 = sum(a * b for a, b in zip([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8], d[:11])) % 11 % 10
    return c1 == d[10] and c2 == d[11]


def passport_format_ok(series: str, number: str) -> bool:
    return bool(_PASSPORT_SERIES.match(series) and _PASSPORT_NUMBER.match(number))
