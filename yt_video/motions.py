"""Extra camera moves for still scenes. Each returns a painter (index, frames) -> RGB image."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from yt_video.boards import _ease_in_out, _ease_out_cubic, _zoomed

Painter = Callable[[int, int], Any]


def _cover(path: Path, width: int, height: int, extra: float = 1.0):
    from PIL import Image

    art = Image.open(path).convert("RGB")
    scale = max(width / art.width, height / art.height) * extra
    art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    left, top = (art.width - int(width * extra)) // 2, (art.height - int(height * extra)) // 2
    return art.crop((left, top, left + int(width * extra), top + int(height * extra)))


def _view(art, width: int, height: int, zoom: float, cx: float = 0.5, cy: float = 0.5):
    from PIL import Image

    box_w, box_h = art.width / zoom, art.height / zoom
    left = min(max(0.0, art.width * cx - box_w / 2), art.width - box_w)
    top = min(max(0.0, art.height * cy - box_h / 2), art.height - box_h)
    return art.resize((width, height), Image.BICUBIC, box=(left, top, left + box_w, top + box_h))


def rack_focus(path: Path, width: int, height: int) -> Painter:
    """Starts out of focus and sharpens while the camera pushes in."""
    from PIL import ImageFilter

    art = _cover(path, width, height, 1.2)

    def paint(index: int, frames: int):
        t = (index - 1) / max(1, frames - 1)
        frame = _view(art, width, height, 1.0 + 0.12 * _ease_in_out(t))
        blur = 14 * (1 - _ease_out_cubic(min(1.0, t / 0.35)))
        return frame.filter(ImageFilter.GaussianBlur(blur)) if blur > 0.3 else frame

    return paint


def photo_drop(path: Path, width: int, height: int) -> Painter:
    """An archive print lands on a dark table with a shadow, then the camera creeps in."""
    from PIL import Image, ImageDraw, ImageFilter

    from yt_video.boards import _board

    table = _board(width, height)
    card_w = int(width * 0.62)
    card_h = int(card_w * height / width)
    border = max(10, card_w // 40)
    photo = _cover(path, card_w, card_h)
    card = Image.new("RGBA", (card_w + border * 2, card_h + border * 2 + border * 3), (238, 233, 222, 255))
    card.paste(photo, (border, border))
    shadow = Image.new("RGBA", (card.width + 80, card.height + 80), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle((40, 40, 40 + card.width, 40 + card.height), fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(24))

    def paint(index: int, frames: int):
        t = (index - 1) / max(1, frames - 1)
        drop = _ease_out_cubic(min(1.0, t / 0.22))
        angle = -6 + 4.5 * drop
        scale = 1.25 - 0.25 * drop
        frame = table.convert("RGBA")
        rotated_shadow = shadow.rotate(angle, expand=True, resample=Image.BICUBIC)
        rotated = card.rotate(angle, expand=True, resample=Image.BICUBIC)
        rotated = rotated.resize((int(rotated.width * scale), int(rotated.height * scale)), Image.BICUBIC)
        rotated_shadow = rotated_shadow.resize((int(rotated_shadow.width * scale), int(rotated_shadow.height * scale)), Image.BICUBIC)
        cx, cy = width // 2, height // 2 + int((1 - drop) * height * 0.08)
        frame.alpha_composite(rotated_shadow, (cx - rotated_shadow.width // 2 + 18, cy - rotated_shadow.height // 2 + 24))
        frame.alpha_composite(rotated, (cx - rotated.width // 2, cy - rotated.height // 2))
        return _zoomed(frame.convert("RGB"), 1 + 0.08 * _ease_in_out(t))

    return paint


def framed(path: Path, width: int, height: int) -> Painter:
    """Frame in frame: the picture as a card over its own blurred copy, both drifting."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    art = _cover(path, width, height, 1.15)
    back = ImageEnhance.Brightness(art.filter(ImageFilter.GaussianBlur(width // 60))).enhance(0.5)
    card_w, card_h = int(width * 0.7), int(height * 0.7)
    radius = max(8, card_w // 60)
    mask = Image.new("L", (card_w, card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, card_w - 1, card_h - 1), radius=radius, fill=255)

    def paint(index: int, frames: int):
        e = _ease_in_out((index - 1) / max(1, frames - 1))
        frame = _view(back, width, height, 1.0 + 0.06 * e)
        card = _view(art, card_w, card_h, 1.0 + 0.1 * e, 0.45 + 0.1 * e)
        frame.paste(card, ((width - card_w) // 2, (height - card_h) // 2), mask)
        return frame

    return paint


def split_then_now(path: Path, other: Path, width: int, height: int) -> Painter:
    """Two pictures slide in side by side, for then-and-now or cause-and-effect lines."""
    from PIL import Image, ImageDraw

    left_art = _cover(path, width // 2, height, 1.15)
    right_art = _cover(other, width // 2, height, 1.15)

    def paint(index: int, frames: int):
        t = (index - 1) / max(1, frames - 1)
        e = _ease_out_cubic(min(1.0, t / 0.25))
        drift = _ease_in_out(t)
        frame = Image.new("RGB", (width, height), (10, 10, 12))
        left = _view(left_art, width // 2, height, 1.0 + 0.08 * drift)
        right = _view(right_art, width // 2, height, 1.08 - 0.08 * drift)
        frame.paste(left, (int(-width // 2 * (1 - e)), 0))
        frame.paste(right, (width // 2 + int(width // 2 * (1 - e)), 0))
        ImageDraw.Draw(frame).rectangle((width // 2 - 3, 0, width // 2 + 3, int(height * e)), fill=(255, 212, 0))
        return frame

    return paint
