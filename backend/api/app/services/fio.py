"""Сверка ФИО с распознанным текстом скана.

Форма заявления электронная, ФИО вводится в именительном падеже, как в паспорте, поэтому
склонять слова не нужно. Распознанный текст шумный (OCR путает буквы), поэтому каждое слово
ФИО сравнивается с ближайшим словом из текста по нечёткому совпадению (rapidfuzz).
Результат — подсказка оператору, а не автоматическое решение.
"""

import re
from statistics import mean

from rapidfuzz import fuzz

AUTO_OK_THRESHOLD = 80
NEEDS_REVIEW_THRESHOLD = 50

_NON_LETTERS = re.compile(r"[^a-zа-я\s-]")


_ZERO_INSIDE_WORD = re.compile(r"(?<=[a-zа-я])0|0(?=[a-zа-я])")


def normalize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    text = _ZERO_INSIDE_WORD.sub("о", text)  # частая ошибка OCR: нуль вместо буквы «о»
    text = _NON_LETTERS.sub(" ", text)
    return [word for word in text.split() if word.strip("-")]


def tokens_score(text: str, name_tokens: list[str]) -> int:
    """Насколько слова из name_tokens нашлись в тексте: 0..100 (среднее по словам)."""
    words = normalize(text)
    tokens = [t for t in name_tokens if len(t) >= 2]
    if not words or not tokens:
        return 0
    return round(mean(max(fuzz.ratio(token, word) for word in words) for token in tokens))


def name_in_text(text: str, full_name: str) -> int:
    """Все слова ФИО (фамилия, имя, отчество) — для паспорта."""
    return tokens_score(text, normalize(full_name))


def given_names_in_text(text: str, full_name: str) -> int:
    """Только имя и отчество — для скриншота банка, где фамилия обычно скрыта инициалом."""
    tokens = normalize(full_name)
    return tokens_score(text, tokens[1:3] if len(tokens) >= 3 else tokens[1:])


def classify(score: int) -> str:
    if score >= AUTO_OK_THRESHOLD:
        return "auto_ok"
    if score >= NEEDS_REVIEW_THRESHOLD:
        return "needs_review"
    return "mismatch"
