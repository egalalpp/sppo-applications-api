"""Распознавание настоящим Tesseract на синтетических сканах.

Выполняются там, где установлен Tesseract с русским языком: в Docker-образе тестов
(docker build --target test). Локально без Tesseract тесты пропускаются.
"""

import io
import shutil
import subprocess

import pytest
from PIL import ImageOps

from app.services import ocr, ocr_fields
from tests.imaging import add_seal, add_signature, font, has_cyrillic_font, page


def _has_rus() -> bool:
    if not shutil.which("tesseract"):
        return False
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True).stdout
    return "rus" in out.split()


pytestmark = pytest.mark.skipif(not (_has_rus() and has_cyrillic_font()),
                                reason="нужны Tesseract с языком rus и шрифт с кириллицей")

CFG = ocr.OcrSettings(timeout_sec=120)


def png(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


PASSPORT_LINES = ["РОССИЙСКАЯ ФЕДЕРАЦИЯ", "Фамилия ИВАНОВ", "Имя ИВАН", "Отчество ИВАНОВИЧ",
                  "Дата рождения 14.03.2005", "Серия и номер 45 10 123456"]
BANK_LINES = ["Реквизиты счёта", "Получатель: Иван Иванович И.", "БИК: 044525225",
              "Номер счёта: 40817810000000000001", "Банк: ПАО Сбербанк"]


def test_passport_text_and_number_are_recognized():
    img, _ = page(lines=PASSPORT_LINES, size=42)
    result, text = ocr.analyze(png(img), "image/png", "passport_registration", CFG)
    assert result["status"] == "done" and result["engine"] == "tesseract"
    assert result["confidence"] is not None and result["confidence"] >= 60
    assert "ИВАНОВ" in text.upper()
    assert ("4510", "123456") in ocr_fields.find_passport_numbers(text)
    assert result["extracted"]["passport_numbers"] == ["4510 123456"]
    assert result["seal"] is None and result["signature"] is None  # на паспорте печать не ищем


def test_bank_screenshot_requisites():
    img, _ = page(width=900, height=700, lines=BANK_LINES, size=34)
    result, text = ocr.analyze(png(img), "image/png", "bank_requisites_screenshot", CFG)
    assert ocr_fields.compare_bank(text, "044525225", "40817810000000000001")[0] == "ok"
    assert "044525225" in result["extracted"]["bik"]
    assert "40817810000000000001" in result["extracted"]["accounts"]


def test_dark_theme_screenshot_is_inverted_before_recognition():
    img, _ = page(width=900, height=700, lines=BANK_LINES, size=34)
    dark = ImageOps.invert(img.convert("RGB"))
    _, text = ocr.analyze(png(dark), "image/png", "bank_requisites_screenshot", CFG)
    assert ocr_fields.compare_bank(text, "044525225", "40817810000000000001")[0] == "ok"


def test_pdf_is_supported():
    img, _ = page(lines=PASSPORT_LINES, size=42)
    buf = io.BytesIO()
    img.save(buf, "PDF", resolution=150)
    result, text = ocr.analyze(buf.getvalue(), "application/pdf", "passport_registration", CFG)
    assert result["pages"] == 1 and "ИВАНОВ" in text.upper()


def test_rotated_passport_number_is_found():
    """Серия и номер на развороте напечатаны вертикально вдоль края."""
    img, _ = page(lines=["РОССИЙСКАЯ ФЕДЕРАЦИЯ", "Фамилия ИВАНОВ"], size=42)
    strip, draw = page(width=420, height=90, size=40)
    draw.text((10, 20), "45 10 123456", fill=(0, 0, 0), font=font(46))
    img.paste(strip.rotate(90, expand=True), (1100, 300))
    result, _ = ocr.analyze(png(img), "image/png", "passport_registration", CFG)
    assert "4510 123456" in result["extracted"]["passport_numbers"]


def test_certificate_seal_and_signature_notes():
    img, draw = page(lines=["СПРАВКА", "Выдана Иванову Ивану Ивановичу", "в том, что он проживает в общежитии"], size=38)
    add_seal(draw, 420, 1300)
    add_signature(draw, 760, 1300)
    result, _ = ocr.analyze(png(img), "image/png", "death_certificate", CFG)
    assert result["seal"] is True and result["signature"] is True
    assert "есть печать" in result["notes"] and "есть подпись" in result["notes"]


def test_certificate_without_marks():
    img, _ = page(lines=["СПРАВКА", "Выдана Иванову Ивану Ивановичу"], size=38)
    result, _ = ocr.analyze(png(img), "image/png", "death_certificate", CFG)
    assert result["notes"] == ["печать не обнаружена", "подпись не обнаружена"]


def test_blank_scan_is_reported_not_crashing():
    img, _ = page(width=600, height=300)
    result, text = ocr.analyze(png(img), "image/png", "passport_registration", CFG)
    assert result["status"] == "done" and result["confidence"] is None and text == ""
    assert "текст не распознан" in result["notes"][0]


@pytest.mark.parametrize("data,content_type", [
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png"),  # битый файл с верной сигнатурой
    (b"%PDF-1.4\nnot really a pdf", "application/pdf"),
    (b"MZ\x90\x00", "image/jpeg"),
    (b"GIF89a", "image/gif"),  # формат, который мы не принимаем
])
def test_broken_or_unsupported_files_raise_ocr_error(data, content_type):
    with pytest.raises(ocr.OcrError):
        ocr.analyze(data, content_type, "passport_registration", CFG)


def degraded(img, blur=0.0, noise=0, quality=95, angle=0.0) -> bytes:
    """Имитация фото с телефона: наклон, размытие, зернистость, сжатие JPEG."""
    import random

    from PIL import ImageFilter
    img = img.convert("RGB").rotate(angle, expand=True, fillcolor=(255, 255, 255))
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    random.seed(3)
    pixels = img.load()
    for _ in range(noise):
        v = random.randrange(90, 200)
        pixels[random.randrange(img.width), random.randrange(img.height)] = (v, v, v)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


@pytest.mark.parametrize("params", [
    dict(blur=1.0, quality=60),
    dict(blur=1.5, noise=60000, quality=40),
    dict(angle=3, blur=1.0, noise=20000, quality=60),
    dict(angle=-4, blur=0.5, quality=70),
    dict(angle=6, blur=1.5, noise=60000, quality=35),
])
def test_phone_photo_quality_is_tolerated(params):
    """Наклон, шум и сжатие, типичные для фото с телефона, не должны ломать чтение номеров."""
    passport, _ = page(lines=PASSPORT_LINES, size=42)
    bank, _ = page(width=900, height=700, lines=BANK_LINES, size=34)
    _, passport_text = ocr.analyze(degraded(passport, **params), "image/jpeg", "passport_registration", CFG)
    _, bank_text = ocr.analyze(degraded(bank, **params), "image/jpeg", "bank_requisites_screenshot", CFG)
    assert ocr_fields.compare_passport(passport_text, "4510", "123456")[0] == "ok", params
    assert ocr_fields.compare_bank(bank_text, "044525225", "40817810000000000001")[0] == "ok", params


def _pdf(pages: int = 1) -> bytes:
    images = [page(lines=PASSPORT_LINES, size=42)[0] for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", resolution=150, save_all=True, append_images=images[1:])
    return buf.getvalue()


@pytest.mark.skipif(not shutil.which("pdftoppm"), reason="нужен poppler-utils")
def test_pdf_is_rendered_by_poppler_via_pdf2image(monkeypatch):
    """Основной рендер PDF — poppler (pdf2image); запасной pdfium при этом не вызывается."""
    def boom(*args, **kwargs):
        raise AssertionError("pdfium не должен вызываться, пока работает poppler")

    monkeypatch.setattr(ocr, "_render_pdf_pdfium", boom)
    pages = ocr.load_pages(_pdf(), "application/pdf", ocr.OcrSettings(max_side_px=1600))
    assert len(pages) == 1 and max(pages[0].size) == 1600  # size=int -> длинная сторона в пикселях
    result, text = ocr.analyze(_pdf(), "application/pdf", "passport_registration", CFG)
    assert "ИВАНОВ" in text.upper() and result["pages"] == 1


def test_pdf_falls_back_to_pdfium_when_poppler_fails(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("pdftoppm is missing")

    monkeypatch.setattr(ocr, "_render_pdf_poppler", broken)
    result, text = ocr.analyze(_pdf(), "application/pdf", "passport_registration", CFG)
    assert "ИВАНОВ" in text.upper() and result["pages"] == 1


def test_pdf_unreadable_by_both_renderers_is_an_ocr_error():
    with pytest.raises(ocr.OcrError):
        ocr.load_pages(b"%PDF-1.4\n garbage without structure", "application/pdf", CFG)


def test_pdf_page_limit():
    pages = ocr.load_pages(_pdf(pages=5), "application/pdf", ocr.OcrSettings(max_pdf_pages=3))
    assert len(pages) == 3
