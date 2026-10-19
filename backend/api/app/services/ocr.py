"""Распознавание сканов: Tesseract (русский + английский) и поиск печати и подписи.

Принимаются только PDF, JPEG и PNG (тип проверяется по содержимому ещё при загрузке).
Распознаётся целиком лицевой разворот паспорта, разворот с пропиской и скриншот с реквизитами;
на остальных документах (справки, профсоюзный билет) ищутся печать и подпись.

Результат для каждого файла — словарь, который воркер кладёт в attachments[].ocr:
    status, engine, confidence (средняя уверенность Tesseract по словам, 0..100),
    pages, words, extracted, text_preview, seal, signature, notes.
Полный текст в документе не хранится (лимит документа MongoDB и персональные данные);
он возвращается отдельно, только чтобы воркер сверил его с формой.
"""

import io
import logging
from dataclasses import dataclass
from statistics import mean

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytesseract
from PIL import Image, ImageOps, ImageStat

from app.services import marks, ocr_fields

log = logging.getLogger(__name__)

ENGINE = "tesseract"

# Типы документов, у которых распознаётся весь текст.
TEXT_TYPES = {"passport_registration", "bank_requisites_screenshot"}
# Паспорт и скриншот банка цветные, печатей и подписей на них нет — их не ищем.
MARKS_SKIP_TYPES = TEXT_TYPES

TEXT_PREVIEW_CHARS = 2000

# Защита от «бомб декомпрессии»: огромные картинки малого размера в байтах.
Image.MAX_IMAGE_PIXELS = 80_000_000


class OcrError(Exception):
    """Файл не удалось прочитать или распознать."""


@dataclass
class OcrSettings:
    lang: str = "rus+eng"
    max_side_px: int = 2000
    max_pdf_pages: int = 3
    timeout_sec: int = 180


def _downscale(img: Image.Image, max_side: int) -> Image.Image:
    scale = max_side / max(img.size)
    if scale >= 1:
        return img
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)


def load_pages(data: bytes, content_type: str, cfg: OcrSettings) -> list[Image.Image]:
    """Страницы документа как RGB-картинки, уменьшенные до cfg.max_side_px по длинной стороне."""
    try:
        if content_type == "application/pdf":
            pdf = pdfium.PdfDocument(data)
            try:
                pages = []
                for index in range(min(len(pdf), cfg.max_pdf_pages)):
                    page = pdf[index]
                    width, height = page.get_size()
                    image = page.render(scale=cfg.max_side_px / max(width, height)).to_pil()
                    pages.append(image.convert("RGB"))
                    page.close()
            finally:
                pdf.close()
            if not pages:
                raise OcrError("В PDF нет страниц")
            return pages
        if content_type in ("image/jpeg", "image/png"):
            image = Image.open(io.BytesIO(data))
            image.load()
            image = ImageOps.exif_transpose(image)  # фото с телефона часто повёрнуты через EXIF
            return [_downscale(image.convert("RGB"), cfg.max_side_px)]
    except OcrError:
        raise
    except Exception as exc:  # битый файл, неподдерживаемый вариант формата, bomb
        raise OcrError(f"Файл не читается: {type(exc).__name__}") from exc
    raise OcrError(f"Неподдерживаемый тип файла: {content_type}")


def estimate_skew(gray: np.ndarray) -> float:
    """Угол наклона страницы в градусах (−10..10): при каком повороте строки текста
    выстраиваются в чёткие горизонтальные полосы (максимум дисперсии построчной суммы)."""
    scale = 800 / max(gray.shape)
    small = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else gray
    _, binary = cv2.threshold(small, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    height, width = binary.shape

    def score(angle: float) -> float:
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        rotated = cv2.warpAffine(binary, matrix, (width, height), flags=cv2.INTER_NEAREST)
        return float(np.var(rotated.sum(axis=1).astype(np.float64)))

    coarse = max(np.arange(-10, 10.01, 1.0), key=score)
    return float(max(np.arange(coarse - 1, coarse + 1.01, 0.2), key=score))


def prepare_for_ocr(img: Image.Image, *, clean: bool = True) -> Image.Image:
    """Серое изображение, пригодное для Tesseract.

    clean=True — выравнивание наклона и подавление шума (фото с телефона: наклон в несколько градусов
    и зернистость ломают распознавание); clean=False — только базовая подготовка.
    """
    gray = ImageOps.grayscale(img)
    if ImageStat.Stat(gray).mean[0] < 110:
        gray = ImageOps.invert(gray)  # тёмная тема приложения банка: светлый текст на тёмном
    if clean:
        # Выравнивание и шумоподавление — в исходном масштабе: после увеличения точки шума
        # становятся крупнее ядра медианного фильтра, и он их уже не убирает.
        array = np.asarray(gray)
        angle = estimate_skew(array)
        if abs(angle) >= 0.3:
            height, width = array.shape
            matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
            array = cv2.warpAffine(array, matrix, (width, height), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        gray = Image.fromarray(cv2.medianBlur(array, 3))
    if max(gray.size) < 1000:
        gray = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)  # мелкий скриншот
    return ImageOps.autocontrast(gray, cutoff=1)


def recognize(img: Image.Image, cfg: OcrSettings, *, lang: str | None = None, psm: int = 3,
              digits_only: bool = False) -> tuple[str, float | None, int]:
    """Возвращает (текст, средняя уверенность 0..100 или None, число слов)."""
    config = f"--oem 1 --psm {psm}"
    if digits_only:
        config += " -c tessedit_char_whitelist=0123456789"
    try:
        data = pytesseract.image_to_data(
            img, lang=lang or cfg.lang, config=config,
            output_type=pytesseract.Output.DICT, timeout=cfg.timeout_sec,
        )
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrError("Tesseract не установлен") from exc
    except (pytesseract.TesseractError, RuntimeError) as exc:  # RuntimeError — истёк таймаут
        raise OcrError(f"Tesseract не справился: {exc}"[:200]) from exc

    lines: dict[tuple, list[str]] = {}
    confidences: list[float] = []
    for i, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(word)
        confidence = float(data["conf"][i])
        if confidence >= 0:
            confidences.append(confidence)
    text = "\n".join(" ".join(words) for words in lines.values())
    return text, (mean(confidences) if confidences else None), len(confidences)


GOOD_CONFIDENCE = 70


def recognize_best(img: Image.Image, cfg: OcrSettings) -> tuple[str, float | None, int, Image.Image]:
    """Распознаёт страницу с очисткой; если результат слабый — пробует без очистки и берёт лучший.
    Возвращает (текст, уверенность, число слов, использованное изображение)."""
    cleaned = prepare_for_ocr(img)
    text, confidence, words = recognize(cleaned, cfg)
    if confidence is not None and confidence >= GOOD_CONFIDENCE:
        return text, confidence, words, cleaned
    plain = prepare_for_ocr(img, clean=False)
    alt_text, alt_confidence, alt_words = recognize(plain, cfg)
    if alt_confidence is not None and (confidence is None or alt_confidence > confidence):
        return alt_text, alt_confidence, alt_words, plain
    return text, confidence, words, cleaned


def _find_rotated_passport_number(img: Image.Image, cfg: OcrSettings) -> str:
    """Серия и номер на развороте паспорта напечатаны вертикально вдоль края: читаем повёрнутую картинку."""
    for angle in (90, 270):
        text, _, _ = recognize(img.rotate(angle, expand=True), cfg, lang="eng", psm=6, digits_only=True)
        if ocr_fields.find_passport_numbers(text):
            return text
    return ""


def analyze(data: bytes, content_type: str, doc_type: str, cfg: OcrSettings) -> tuple[dict, str]:
    """Анализ одного вложения. Возвращает (результат для MongoDB, полный текст).

    Бросает OcrError, если файл не удалось прочитать или распознать.
    """
    pages = load_pages(data, content_type, cfg)
    result: dict = {
        "status": "done", "engine": ENGINE, "pages": len(pages), "confidence": None, "words": 0,
        "extracted": {}, "text_preview": None, "seal": None, "signature": None, "notes": [],
    }
    full_text = ""

    if doc_type in TEXT_TYPES:
        texts, weighted, total_words = [], 0.0, 0
        for page in pages:
            text, confidence, words, prepared = recognize_best(page, cfg)
            texts.append(text)
            if confidence is not None:
                weighted += confidence * words
                total_words += words
            if doc_type == "passport_registration" and not ocr_fields.find_passport_numbers(text):
                texts.append(_find_rotated_passport_number(prepared, cfg))
        full_text = "\n".join(t for t in texts if t)
        result["words"] = total_words
        result["confidence"] = round(weighted / total_words) if total_words else None
        result["text_preview"] = full_text[:TEXT_PREVIEW_CHARS] or None
        if doc_type == "passport_registration":
            numbers = ocr_fields.find_passport_numbers(full_text)
            result["extracted"] = {"passport_numbers": [f"{s} {n}" for s, n in numbers]}
        else:
            result["extracted"] = {
                "bik": ocr_fields.find_biks(full_text),
                "accounts": ocr_fields.find_accounts(full_text),
            }
        if not total_words:
            result["notes"].append("текст не распознан: плохое качество скана")

    if doc_type not in MARKS_SKIP_TYPES:
        found = [marks.detect_marks(page) for page in pages]
        result["seal"] = any(m.seal for m in found)
        result["signature"] = any(m.signature for m in found)
        result["notes"] += [
            "есть печать" if result["seal"] else "печать не обнаружена",
            "есть подпись" if result["signature"] else "подпись не обнаружена",
        ]
    return result, full_text
