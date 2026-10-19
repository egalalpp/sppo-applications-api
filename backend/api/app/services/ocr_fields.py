"""Поиск реквизитов в распознанном тексте: паспорт, БИК, номер счёта.

Чистые функции без Tesseract: на вход текст, на выход найденные значения. Сравнение с тем,
что студент ввёл в форме, — точное совпадение цифр: цифры OCR читает надёжнее слов.
"""

import re

# «45 10 123456», «4510 123456», «4510123456», «45 10 № 123456»
_PASSPORT = re.compile(r"(?<!\d)(\d{2})[ \t]?(\d{2})[ \t]*(?:№|N|No\.?|#)?[ \t]*(\d{6})(?!\d)")
# Любая последовательность цифр, возможно с пробелами и дефисами внутри («40817 810 0 0000 0000001»)
_DIGIT_RUN = re.compile(r"\d(?:[ \-]?\d){7,}")
_BIK = re.compile(r"(?<!\d)(04\d{7})(?!\d)")


def find_passport_numbers(text: str) -> list[tuple[str, str]]:
    """Пары (серия, номер), найденные в тексте."""
    return [(m.group(1) + m.group(2), m.group(3)) for m in _PASSPORT.finditer(text)]


def digit_runs(text: str) -> list[str]:
    """Длинные последовательности цифр без пробелов и дефисов."""
    return [re.sub(r"[ \-]", "", m.group(0)) for m in _DIGIT_RUN.finditer(text)]


def find_accounts(text: str) -> list[str]:
    """20-значные номера счетов (могут быть записаны с пробелами)."""
    found = []
    for run in digit_runs(text):
        if len(run) == 20:
            found.append(run)
        elif len(run) > 20:  # счёт слитно с соседними цифрами: ищем окно, начинающееся с типичных 4/3
            found += re.findall(r"(?=((?:40|30|31|32|33|34|35|36|37|38)\d{18}))", run)
    return list(dict.fromkeys(found))


def find_biks(text: str) -> list[str]:
    return list(dict.fromkeys(_BIK.findall(text)))


def contains_digits(text: str, expected: str) -> bool:
    """Есть ли expected среди найденных чисел (устойчиво к пробелам и дефисам внутри числа)."""
    return any(expected in run for run in digit_runs(text))


def compare_passport(text: str, series: str, number: str) -> tuple[str, list[str]]:
    """Возвращает (статус, найденные номера): ok | mismatch | not_found."""
    candidates = find_passport_numbers(text)
    shown = [f"{s} {n}" for s, n in candidates]
    if (series, number) in candidates:
        return "ok", shown
    return ("mismatch", shown) if candidates else ("not_found", shown)


def compare_bank(text: str, bik: str, account: str) -> tuple[str, dict]:
    """Сверка БИК и счёта из формы со скриншотом: ok | mismatch | not_found.

    mismatch — на скриншоте нашлись БИК или счёт, но не те, что в форме; not_found —
    ничего не прочиталось (плохое качество), проверить автоматически нельзя.
    """
    accounts, biks = find_accounts(text), find_biks(text)
    bik_ok, account_ok = contains_digits(text, bik), contains_digits(text, account)
    info = {"bik_found": biks, "accounts_found": accounts}
    if bik_ok and account_ok:
        return "ok", info
    if (biks and not bik_ok) or (accounts and not account_ok):
        return "mismatch", info
    return "not_found", info
