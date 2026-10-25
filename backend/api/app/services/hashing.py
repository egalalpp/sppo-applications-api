"""Детерминированные хэши паспорта и номера счёта для поиска дублей.

Решение команды: не шифровать поля на уровне БД, а хранить рядом хэши, по которым индекс находит
повторы за миллисекунды. Простой SHA-256 здесь не годится: паспорт — около 10^10 вариантов, счёт физлица —
примерно столько же, это перебирается на одной видеокарте за минуты, и при утечке базы хэши раскрылись бы.
Поэтому HMAC-SHA-256 с секретом HASH_PEPPER, который лежит в окружении сервера, а не в базе.

Сырые значения в документах остаются (нужны следующему процессу вне проекта). Хэши — для поиска, а не вместо.
Хэш не уникален: один счёт законно может встретиться дважды (опечатка в первой заявке, карта родителя),
решает оператор. Смена HASH_PEPPER требует пересчёта: `python -m app.bootstrap --rehash`.
"""

import hashlib
import hmac
import re

from app.config import get_settings

_NON_DIGITS = re.compile(r"\D")


def norm_digits(value: str) -> str:
    """Только цифры, без пробелов и разделителей: «45 10 123456» и «4510123456» хэшируются одинаково."""
    return _NON_DIGITS.sub("", value or "")


def keyed_hash(value: str, pepper: str | None = None) -> str:
    key = (pepper or get_settings().hash_pepper).encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def passport_hash(series: str, number: str, pepper: str | None = None) -> str:
    return keyed_hash(norm_digits(series) + norm_digits(number), pepper)


def account_hash(account_number: str, pepper: str | None = None) -> str:
    return keyed_hash(norm_digits(account_number), pepper)


def apply_hashes(student: dict | None, payout: dict | None, pepper: str | None = None) -> None:
    """Дописывает хэши в словари student и payout (на месте). Вызывать после любого изменения паспорта и счёта."""
    if student is not None:
        passport = student.get("passport")
        if passport and passport.get("series") and passport.get("number"):
            student["passport_hash"] = passport_hash(passport["series"], passport["number"], pepper)
        else:
            student.pop("passport_hash", None)
    if payout is not None:
        if payout.get("account_number"):
            payout["account_number_hash"] = account_hash(payout["account_number"], pepper)
        else:
            payout.pop("account_number_hash", None)
