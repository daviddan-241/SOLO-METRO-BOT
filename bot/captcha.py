import io
import random
import string
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = Path(__file__).with_name("captcha_font.ttf")
ALPHABET = "".join(c for c in (string.ascii_letters + string.digits) if c not in "0OolI1")


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
    return "".join(random.choice(ALPHABET) for _ in range(length))


def render_image(text: str) -> bytes:
    width, height = 720, 220
    img = Image.new("RGB", (width, height), (252, 252, 252))
    draw = ImageDraw.Draw(img)
    font = _font(118)

    # scribbled interference lines, matching the Maestro-style captcha
    for _ in range(70):
        x1, y1 = random.randint(-40, width + 40), random.randint(-20, height + 20)
        x2, y2 = random.randint(-40, width + 40), random.randint(-20, height + 20)
        shade = random.randint(90, 170)
        draw.line((x1, y1, x2, y2), fill=(shade, shade, shade), width=random.randint(1, 2))

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width - tw) // 2 - bbox[0]
    y = (height - th) // 2 - bbox[1] - 8

    # slight per-letter jitter with white fill + black outline
    cursor = x
    for ch in text:
        cb = draw.textbbox((0, 0), ch, font=font)
        cw = cb[2] - cb[0]
        jx = cursor + random.randint(-2, 2)
        jy = y + random.randint(-10, 10)
        draw.text(
            (jx, jy),
            ch,
            font=font,
            fill=(255, 255, 255),
            stroke_width=3,
            stroke_fill=(10, 10, 10),
        )
        cursor += cw + random.randint(-6, 4)

    for _ in range(35):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        shade = random.randint(110, 180)
        draw.line((x1, y1, x2, y2), fill=(shade, shade, shade), width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
