import pytest

from app.services.fio import classify, fio_similarity


@pytest.mark.parametrize("nominative,genitive", [
    ("Иванов Иван Иванович", "Иванова Ивана Ивановича"),
    ("Петрова Мария Сергеевна", "Петровой Марии Сергеевны"),
    ("Смирнов Алексей Петрович", "Смирнова Алексея Петровича"),
    ("Кузнецова Анна Олеговна", "Кузнецовой Анны Олеговны"),
])
def test_genitive_matches_nominative(nominative, genitive):
    assert classify(fio_similarity(nominative, genitive)) == "auto_ok"


def test_word_order_and_yo():
    assert fio_similarity("Ёлкин Пётр Семёнович", "Пётра Ёлкина Семеновича") >= 90


def test_different_people():
    assert classify(fio_similarity("Иванов Иван Иванович", "Сидоровой Екатерины Павловны")) == "mismatch"


def test_typo_needs_attention_but_not_mismatch():
    score = fio_similarity("Иванов Иван Иванович", "Иваова Ивана Ивановича")
    assert score >= 70


def test_empty():
    assert fio_similarity("", "Иванов Иван") == 0
