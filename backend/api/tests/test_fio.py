from app.services.fio import classify, given_names_in_text, name_in_text


def test_full_name_found_in_passport_text():
    text = "РОССИЙСКАЯ ФЕДЕРАЦИЯ\nФамилия\nИВАНОВ\nИмя\nИВАН\nОтчество\nИВАНОВИЧ\nПол МУЖ."
    assert classify(name_in_text(text, "Иванов Иван Иванович")) == "auto_ok"


def test_ocr_noise_is_tolerated():
    text = "ИВАН0В ИВАН ИВАН0ВИЧ"  # нули вместо «О»
    assert name_in_text(text, "Иванов Иван Иванович") >= 80


def test_yo_and_case_do_not_matter():
    assert name_in_text("ЕЛКИН ПЕТР СЕМЕНОВИЧ", "Ёлкин Пётр Семёнович") == 100


def test_different_person():
    score = name_in_text("СИДОРОВА ЕКАТЕРИНА ПАВЛОВНА", "Иванов Иван Иванович")
    assert classify(score) == "mismatch"


def test_bank_screenshot_with_masked_surname():
    text = "Получатель\nИван Иванович И.\nСчёт 40817810000000000001"
    assert classify(given_names_in_text(text, "Иванов Иван Иванович")) == "auto_ok"


def test_bank_screenshot_of_other_person():
    text = "Получатель\nМария Сергеевна П."
    assert classify(given_names_in_text(text, "Иванов Иван Иванович")) != "auto_ok"


def test_empty_text():
    assert name_in_text("", "Иванов Иван") == 0
    assert name_in_text("Иванов", "") == 0
