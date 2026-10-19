"""Поиск печати и подписи на скане (эвристика, без машинного обучения).

Tesseract печати и подписи не распознаёт, поэтому они ищутся отдельно, по форме и цвету:

- Печать: замкнутый круглый контур заметного размера. Сначала ищем круг из «цветных» чернил
  (синие, фиолетовые, красные печати), затем — по краям на серой картинке (чёрно-белая копия).
- Подпись: тонкий непрерывный росчерк цветными чернилами (обычно шариковая ручка).

Ограничения, о которых должен знать оператор: подпись чёрной ручкой на чёрно-белой копии
неотличима от печатного текста, цветной печатный текст и логотипы дают ложные срабатывания.
Результат — подсказка для оператора («есть печать», «есть подпись»), решение принимает человек.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

WORK_SIDE = 1200  # размер картинки для анализа: хватает для крупных объектов, считается быстро


@dataclass
class Marks:
    seal: bool
    signature: bool


def _to_rgb_array(img: Image.Image) -> np.ndarray:
    img = img.convert("RGB")
    scale = WORK_SIDE / max(img.size)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    return np.asarray(img)


def _ink_mask(rgb: np.ndarray) -> np.ndarray:
    """Маска цветных чернил: синие/фиолетовые и красные оттенки с заметной насыщенностью."""
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    h, s, v = cv2.split(hsv)
    blue = (h >= 95) & (h <= 150) & (s >= 70) & (v >= 40)
    red = ((h <= 8) | (h >= 170)) & (s >= 110) & (v >= 70)
    return ((blue | red).astype(np.uint8)) * 255


def _seal_by_color(ink: np.ndarray, min_dim: int) -> tuple[float, float, float] | None:
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    merged = cv2.dilate(ink, kernel, iterations=2)  # кольцо и текст внутри печати сливаются в диск
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        if not (0.04 * min_dim <= radius <= 0.30 * min_dim):
            continue
        if cv2.contourArea(contour) / (math.pi * radius * radius) < 0.75:
            continue  # не круг
        x, y, w, h = cv2.boundingRect(contour)
        if not 0.75 <= w / h <= 1.33:
            continue
        coverage = ink[y:y + h, x:x + w].mean() / 255
        if not 0.03 <= coverage <= 0.60:
            continue  # пятно или почти пустая рамка
        return cx, cy, radius
    return None


def _seal_by_edges(rgb: np.ndarray, min_dim: int) -> tuple[float, float, float] | None:
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    blurred = cv2.medianBlur(gray, 5)
    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT, dp=1.2, minDist=min_dim * 0.1,
        param1=120, param2=55, minRadius=int(0.05 * min_dim), maxRadius=int(0.25 * min_dim),
    )
    if circles is None:
        return None
    dark_limit = min(150, float(gray.mean()) - 40)
    dark = gray < dark_limit
    height, width = gray.shape
    for cx, cy, radius in circles[0][:10]:
        hits = 0
        angles = np.linspace(0, 2 * math.pi, 120, endpoint=False)
        for angle in angles:
            px = int(round(cx + radius * math.cos(angle)))
            py = int(round(cy + radius * math.sin(angle)))
            window = dark[max(py - 3, 0):min(py + 4, height), max(px - 3, 0):min(px + 4, width)]
            hits += bool(window.size and window.any())
        if hits / len(angles) >= 0.75:  # окружность сплошная, а не случайная дуга из букв
            return float(cx), float(cy), float(radius)
    return None


def _has_signature(ink: np.ndarray, seal: tuple[float, float, float] | None) -> bool:
    height, width = ink.shape
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 9))
    merged = cv2.morphologyEx(ink, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (0.05 * width <= w <= 0.5 * width and 0.015 * height <= h <= 0.25 * height):
            continue
        if not 1.3 <= w / h <= 9:
            continue
        if seal is not None:
            cx, cy, radius = seal
            if math.hypot(x + w / 2 - cx, y + h / 2 - cy) < radius * 1.1:
                continue  # это содержимое печати, а не подпись
        region = ink[y:y + h, x:x + w]
        if not 0.02 <= region.mean() / 255 <= 0.35:
            continue  # сплошное пятно или почти пусто
        # Росчерк ручкой — одна связная линия через всю подпись; печатные буквы по отдельности короткие.
        count, _, stats, _ = cv2.connectedComponentsWithStats(region, connectivity=8)
        if count < 2:
            continue
        longest = max(stats[1:, cv2.CC_STAT_WIDTH])
        if longest >= 0.5 * w and longest >= 0.04 * width:
            return True
    return False


def detect_marks(img: Image.Image) -> Marks:
    rgb = _to_rgb_array(img)
    height, width = rgb.shape[:2]
    min_dim = min(height, width)
    ink = _ink_mask(rgb)
    seal = _seal_by_color(ink, min_dim) or _seal_by_edges(rgb, min_dim)
    return Marks(seal=seal is not None, signature=_has_signature(ink, seal))
