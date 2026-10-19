"""Синтетические «сканы» для тестов: страница с текстом, печатью и подписью."""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BLUE = (25, 55, 190)
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # в Docker-образе тестов
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
]


def font(size: int) -> ImageFont.ImageFont:
    for path in FONT_PATHS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def has_cyrillic_font() -> bool:
    return any(Path(p).exists() for p in FONT_PATHS)


def page(width: int = 1240, height: int = 1754, lines: list[str] | None = None, size: int = 34,
         grayscale: bool = False) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("L" if grayscale else "RGB", (width, height), 255 if grayscale else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    f = font(size)
    for i, line in enumerate(lines or []):
        draw.text((90, 90 + i * (size + 22)), line, fill=0 if grayscale else (10, 10, 10), font=f)
    return img, draw


def add_seal(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int = 140, color=BLUE) -> None:
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=color, width=6)
    draw.ellipse((cx - r + 38, cy - r + 38, cx + r - 38, cy + r - 38), outline=color, width=3)
    draw.text((cx - 55, cy - 18), "PROFKOM", fill=color, font=font(30))


def add_signature(draw: ImageDraw.ImageDraw, x: int, y: int, length: int = 380, color=BLUE) -> None:
    points = [(x + i, y + 45 * math.sin(i / 22) + 18 * math.sin(i / 7)) for i in range(0, length, 3)]
    draw.line(points, fill=color, width=4, joint="curve")
