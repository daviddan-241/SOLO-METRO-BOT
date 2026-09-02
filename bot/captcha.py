import io
import random
import string
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).with_name("captcha_font.ttf")
UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
LOWER = "abcdefghjkmnpqrstuvwxyz"
DIGIT = "23456789"


def _font(size: int) -> ImageFont.FreeTypeFont:
    candidates = [
        FONT_PATH,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for path in candidates:
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def generate_text(length: int = 5) -> str:
    """Maestro-style mixed case, e.g. Kuay5 / BhqSa."""
    chars = [random.choice(UPPER), random.choice(LOWER), random.choice(DIGIT)]
    pool = UPPER + LOWER + DIGIT
    while len(chars) < length:
        chars.append(random.choice(pool))
    random.shuffle(chars)
    if chars[0] in DIGIT:
        chars[0] = random.choice(UPPER)
    return "".join(chars[:length])


def render_image(text: str) -> bytes:
    width, height = 720, 220
    img = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _font(124)

    for _ in range(90):
        x1, y1 = random.randint(-50, width + 50), random.randint(-30, height + 30)
        x2, y2 = random.randint(-50, width + 50), random.randint(-30, height + 30)
        shade = random.randint(70, 160)
        draw.line((x1, y1, x2, y2), fill=(shade, shade, shade), width=random.randint(1, 2))

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2 - bbox[0]
    y = (height - th) // 2 - bbox[1] - 6

    cursor = x
    for ch in text:
        cb = draw.textbbox((0, 0), ch, font=font)
        cw = cb[2] - cb[0]
        jx = cursor + random.randint(-3, 3)
        jy = y + random.randint(-12, 12)
        draw.text(
            (jx, jy),
            ch,
            font=font,
            fill=(255, 255, 255),
            stroke_width=4,
            stroke_fill=(0, 0, 0),
        )
        cursor += cw + random.randint(-8, 2)

    for _ in range(40):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        shade = random.randint(90, 170)
        draw.line((x1, y1, x2, y2), fill=(shade, shade, shade), width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
