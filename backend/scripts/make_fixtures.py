"""Генерирует учебные «сканы» для сквозного теста (scripts/fixtures/*.png).

Данные вымышленные: паспорт 45 10 123456, БИК 044525225 и счёт, подобранный так, чтобы сходился
контрольный ключ. Запуск — в тестовом образе, где есть шрифт с кириллицей и пакет tests:

    docker build --target test -t sppo-api-test ./api
    docker run --rm -v "<путь к backend/scripts>:/out" sppo-api-test python /out/make_fixtures.py
"""

import sys
from pathlib import Path

sys.path.insert(0, "/srv")

from tests.imaging import add_seal, add_signature, page  # noqa: E402
from tests.test_validators import make_account  # noqa: E402

OUT = Path(__file__).resolve().parent / "fixtures"
OUT.mkdir(exist_ok=True)

BIK = "044525225"
ACCOUNT = make_account(BIK, "4081781000000000001")

passport, _ = page(width=1000, height=1000, size=40, lines=[
    "РОССИЙСКАЯ ФЕДЕРАЦИЯ", "ПАСПОРТ (учебный образец)", "Фамилия ИВАНОВ", "Имя ИВАН", "Отчество ИВАНОВИЧ",
    "Дата рождения 14.03.2005", "Серия и номер 45 10 123456"])
passport.save(OUT / "passport.png", optimize=True)

bank, _ = page(width=900, height=700, size=34, lines=[
    "Реквизиты счёта (учебный образец)", "Получатель: Иван Иванович И.", f"БИК: {BIK}", f"Номер счёта: {ACCOUNT}",
    "Банк: ПАО Сбербанк"])
bank.save(OUT / "bank.png", optimize=True)

certificate, draw = page(width=1000, height=1000, size=36, lines=[
    "СПРАВКА (учебный образец)", "Выдана Иванову Ивану Ивановичу", "в том, что он является студентом"])
add_seal(draw, 330, 760)
add_signature(draw, 600, 760)
certificate.save(OUT / "certificate.png", optimize=True)

print("account:", ACCOUNT)
for f in sorted(OUT.glob("*.png")):
    print(f.name, f.stat().st_size, "bytes")
