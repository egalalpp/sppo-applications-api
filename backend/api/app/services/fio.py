"""Сверка ФИО (schema_zayavok_sppo.md, раздел 6.2).

В заявлении ФИО записано в родительном падеже («Иванова Ивана Ивановича»),
поэтому каждое слово приводится к набору возможных лемм через pymorphy3,
а затем слова сравниваются через rapidfuzz. Результат — подсказка модератору.
"""

import re
from functools import lru_cache
from statistics import mean

from rapidfuzz import fuzz

AUTO_OK_THRESHOLD = 90
NEEDS_REVIEW_THRESHOLD = 70

_NON_LETTERS = re.compile(r"[^a-zа-я\s-]")
_morph = None


def _analyzer():
    # Словари pymorphy3 грузятся ~1 с, поэтому создаём анализатор лениво.
    global _morph
    if _morph is None:
        import pymorphy3

        _morph = pymorphy3.MorphAnalyzer(lang="ru")
    return _morph


def normalize(text: str) -> list[str]:
    text = text.lower().replace("ё", "е")
    text = _NON_LETTERS.sub(" ", text)
    return text.split()


@lru_cache(maxsize=4096)
def lemma_candidates(word: str) -> frozenset[str]:
    candidates = {word}
    for parse in _analyzer().parse(word):
        candidates.add(parse.normal_form.replace("ё", "е"))
    return frozenset(candidates)


def _word_score(a: str, b: str) -> float:
    return max(fuzz.ratio(x, y) for x in lemma_candidates(a) for y in lemma_candidates(b))


def fio_similarity(first: str, second: str) -> int:
    """Похожесть двух ФИО от 0 до 100, устойчивая к падежу, порядку слов и опечаткам."""
    a, b = normalize(first), normalize(second)
    if not a or not b:
        return 0
    forward = mean(max(_word_score(x, y) for y in b) for x in a)
    backward = mean(max(_word_score(y, x) for x in a) for y in b)
    return round(min(forward, backward))


def classify(score: int) -> str:
    if score >= AUTO_OK_THRESHOLD:
        return "auto_ok"
    if score >= NEEDS_REVIEW_THRESHOLD:
        return "needs_review"
    return "mismatch"
