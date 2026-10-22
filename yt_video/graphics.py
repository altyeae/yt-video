"""Animated infographic templates: numbers, comparisons, timelines and callouts with arrows."""
from __future__ import annotations

from typing import Any, Callable

from yt_video.boards import (
    ACCENT,
    MUTED,
    WHITE,
    _board,
    _curve,
    _draw_curve,
    _ease_in_out,
    _ease_out_back,
    _ease_out_cubic,
    _fit,
    _font,
    _rise_text,
)

Painter = Callable[[int, int], Any]
RED = (255, 72, 56)
GREEN = (80, 210, 120)
CARD = (26, 28, 33, 235)


def _step(index: int, start: int, length: int) -> float:
    return max(0.0, min(1.0, (index - start) / max(1, length)))


def _title(draw, text: str, width: int, height: int, index: int) -> None:
    if text:
        font = _fit(draw, text, int(width * 0.8), int(height * 0.08), int(height * 0.06), 700)
        x = (width - draw.textlength(text, font=font)) / 2
        _rise_text(draw, text, x, int(height * 0.09), font, WHITE, index, 0, int(height * 0.03))


def _short(value: float) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.0f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M".replace(".0M", "M")
    if value >= 1000:
        return f"{value:,.0f}"
    return f"{value:.0f}"


def timeline(title: str, points: list[tuple[str, str]], width: int, height: int) -> Painter:
    """A line draws itself across the years; each date pops in with its event."""
    from PIL import ImageDraw

    base = _board(width, height)
    y = int(height * 0.55)
    left, right = int(width * 0.14), int(width * 0.86)
    xs = [left + (right - left) * i / max(1, len(points) - 1) for i in range(len(points))]
    edge = int(width * 0.03)
    year_font = _font(int(height * 0.075), 850)
    label_font = _font(int(height * 0.034), 550)

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        _title(draw, title, width, height, index)
        reach = _ease_in_out(_step(index, 6, int(frames * 0.6)))
        draw.line((left, y, left + (right - left) * reach, y), fill=MUTED + (200,), width=max(3, height // 270))
        for number, (year, label) in enumerate(points):
            at = (xs[number] - left) / max(1, right - left)
            if reach < at - 0.001:
                continue
            start = 6 + int(at * frames * 0.6)
            pop = _ease_out_back(_step(index, start, 9))
            r = int(height * 0.016 * max(0.01, pop))
            colour = RED if number == len(points) - 1 else ACCENT
            draw.ellipse((xs[number] - r, y - r, xs[number] + r, y + r), fill=colour)
            above = number % 2 == 0
            yy = y - int(height * 0.17) if above else y + int(height * 0.045)
            tx = xs[number] - draw.textlength(year, font=year_font) / 2
            _rise_text(draw, year, tx, yy, year_font, colour, index, start, int(height * 0.02))
            label_w = draw.textlength(label, font=label_font)
            lx = min(max(edge, xs[number] - label_w / 2), width - edge - label_w)
            ly = yy + int(height * 0.09)
            _rise_text(draw, label, lx, ly, label_font, MUTED, index, start + 4, int(height * 0.015))
        return frame

    return paint


def bars(title: str, items: list[tuple[str, float, str]], width: int, height: int) -> Painter:
    """Bars grow to their values with counters; a tiny bar next to a huge one tells the story."""
    from PIL import ImageDraw

    base = _board(width, height)
    rows = max(1, len(items))
    bar_h = int(height * (0.12 if rows <= 2 else 0.085))
    label_font = _font(int(height * 0.042), 600)
    value_font = _font(int(height * 0.07), 850)
    row_h = label_font.size + int(height * 0.025) + bar_h + int(height * 0.07)
    top = int(height * 0.22) + max(0, (int(height * 0.72) - rows * row_h) // 2)
    left = int(width * 0.1)
    span = int(width * 0.62)
    peak = max(value for _label, value, _suffix in items) or 1

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        _title(draw, title, width, height, index)
        for number, (label, value, suffix) in enumerate(items):
            y = top + number * row_h
            start = 8 + number * 12
            grow = _ease_out_cubic(_step(index, start, 26))
            _rise_text(draw, label, left, y, label_font, MUTED, index, start - 4, 12)
            bar_y = y + label_font.size + int(height * 0.025)
            draw.rounded_rectangle((left, bar_y, left + span, bar_y + bar_h), radius=10, fill=(40, 43, 50))
            length = max(8, int(span * value / peak * grow))
            colour = ACCENT if number == 0 else RED
            draw.rounded_rectangle((left, bar_y, left + length, bar_y + bar_h), radius=10, fill=colour)
            if grow > 0:
                text = _short(value * grow) + suffix
                draw.text((left + span + int(width * 0.02), bar_y + (bar_h - value_font.size) // 2 - 6), text, font=value_font, fill=WHITE)
        return frame

    return paint


def before_after(before: str, after: str, caption: str, width: int, height: int) -> Painter:
    """Old value, a curved arrow arcs over from it to the new value, which drops in red."""
    from PIL import ImageDraw

    base = _board(width, height)
    probe = ImageDraw.Draw(base)
    font = _fit(probe, max(before, after, key=len), int(width * 0.36), int(height * 0.2), int(height * 0.15), 850)
    caption_font = _font(int(height * 0.04), 500)
    lx, rx = int(width * 0.27), int(width * 0.73)
    y = int(height * 0.56)
    lift = int(font.size * 0.85)
    arrow = _curve((lx + int(width * 0.04), y - lift), (rx - int(width * 0.04), y - lift), int(height * 0.025), -1, True)
    arrow["width"] = max(6, height // 110)

    def paint(index: int, frames: int):
        frame = base.copy().convert("RGBA")
        draw = ImageDraw.Draw(frame)
        _rise_text(draw, before, lx - draw.textlength(before, font=font) / 2, y - font.size // 2, font, WHITE, index, 2, 30)
        progress = _step(index, 14, 16)
        if progress > 0:
            _draw_curve(frame, arrow, progress)
        drop = _ease_out_back(_step(index, 28, 12))
        if drop > 0:
            yy = y - font.size // 2 - int((1 - drop) * height * 0.12)
            draw.text((rx - draw.textlength(after, font=font) / 2, yy), after, font=font, fill=RED + (int(255 * min(1, drop)),))
        cx = (width - draw.textlength(caption, font=caption_font)) / 2
        _rise_text(draw, caption, cx, y + font.size + int(height * 0.06), caption_font, MUTED, index, 36, 16)
        return frame.convert("RGB")

    return paint


def ring(percent: float, caption: str, width: int, height: int) -> Painter:
    """A circle fills to the percentage while the number counts up."""
    from PIL import ImageDraw

    base = _board(width, height)
    radius = int(height * 0.24)
    cx, cy = width // 2, int(height * 0.46)
    number_font = _font(int(height * 0.13), 850)
    caption_font = _font(int(height * 0.036), 500)
    thick = int(radius * 0.13)

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        box = (cx - radius, cy - radius, cx + radius, cy + radius)
        draw.arc(box, 0, 360, fill=(48, 51, 58), width=thick)
        fill = _ease_out_cubic(_step(index, 4, 34)) * percent / 100
        if fill > 0:
            draw.arc(box, -90, -90 + 360 * fill, fill=RED if percent >= 50 else ACCENT, width=thick)
        text = f"{fill * 100:.0f}%"
        draw.text((cx - draw.textlength(text, font=number_font) / 2, cy - number_font.size * 0.62), text, font=number_font, fill=WHITE)
        _rise_text(draw, caption, cx - draw.textlength(caption, font=caption_font) / 2, cy + radius + int(height * 0.06),
                   caption_font, MUTED, index, 20, 16)
        return frame

    return paint


def line_chart(title: str, points: list[tuple[str, float]], width: int, height: int) -> Painter:
    """A line draws itself over the years: the rise, the peak, the crash."""
    from PIL import ImageDraw

    base = _board(width, height)
    left, right = int(width * 0.12), int(width * 0.88)
    top, bottom = int(height * 0.25), int(height * 0.8)
    peak = max(value for _label, value in points) or 1
    xy = [
        (left + (right - left) * i / max(1, len(points) - 1), bottom - (bottom - top) * value / peak)
        for i, (_label, value) in enumerate(points)
    ]
    label_font = _font(int(height * 0.028), 600)
    value_font = _font(int(height * 0.032), 800)

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        _title(draw, title, width, height, index)
        draw.line((left, bottom, right, bottom), fill=(70, 74, 82), width=2)
        reach = _ease_in_out(_step(index, 6, int(frames * 0.6))) * (len(xy) - 1)
        whole = int(reach)
        path = xy[: whole + 1]
        if whole < len(xy) - 1:
            part = reach - whole
            (x1, y1), (x2, y2) = xy[whole], xy[whole + 1]
            path = path + [(x1 + (x2 - x1) * part, y1 + (y2 - y1) * part)]
        if len(path) > 1:
            shade = path + [(path[-1][0], bottom), (path[0][0], bottom)]
            draw.polygon(shade, fill=ACCENT + (40,))
            draw.line(path, fill=ACCENT, width=max(4, height // 180), joint="curve")
        for number, ((x, y), (label, value)) in enumerate(zip(xy, points)):
            if number > reach + 0.001:
                continue
            colour = RED if value == points[-1][1] and number == len(points) - 1 else ACCENT
            draw.ellipse((x - 9, y - 9, x + 9, y + 9), fill=colour)
            draw.text((x - draw.textlength(label, font=label_font) / 2, bottom + 14), label, font=label_font, fill=MUTED)
            shown = _short(value)
            draw.text((x - draw.textlength(shown, font=value_font) / 2, y - value_font.size - 16), shown, font=value_font, fill=WHITE)
        return frame

    return paint


def versus(left: tuple[str, list[str]], right: tuple[str, list[str]], width: int, height: int) -> Painter:
    """Two columns, THEN against NOW, rows sliding in alternately."""
    from PIL import ImageDraw

    base = _board(width, height)
    head = _font(int(height * 0.06), 850)
    row = _font(int(height * 0.036), 550)
    columns = [(int(width * 0.27), left, ACCENT), (int(width * 0.73), right, RED)]

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        split = _ease_out_cubic(_step(index, 2, 12))
        draw.line((width // 2, int(height * 0.18), width // 2, int(height * 0.18 + height * 0.7 * split)), fill=(70, 74, 82), width=3)
        for side, (cx, (name, rows), colour) in enumerate(columns):
            _rise_text(draw, name, cx - draw.textlength(name, font=head) / 2, int(height * 0.18), head, colour, index, 4 + side * 4, 20)
            for number, text in enumerate(rows):
                start = 14 + number * 10 + side * 5
                y = int(height * 0.38 + number * height * 0.13)
                slide = _ease_out_cubic(_step(index, start, 9))
                if slide <= 0:
                    continue
                offset = int((1 - slide) * width * 0.05) * (-1 if side == 0 else 1)
                x = cx - draw.textlength(text, font=row) / 2 + offset
                draw.text((x, y), text, font=row, fill=WHITE)
        return frame

    return paint


def stat_grid(title: str, stats: list[tuple[str, str]], width: int, height: int) -> Painter:
    """Four cards pop in one by one, each with a big number and what it means."""
    from PIL import Image, ImageDraw

    base = _board(width, height).convert("RGBA")
    card_w, card_h = int(width * 0.34), int(height * 0.26)
    cells = [(int(width * 0.15), int(height * 0.25)), (int(width * 0.51), int(height * 0.25)),
             (int(width * 0.15), int(height * 0.57)), (int(width * 0.51), int(height * 0.57))]
    number_font = _font(int(height * 0.085), 850)
    note_font = _font(int(height * 0.03), 500)
    cards = []
    for number, (value, note) in enumerate(stats[:4]):
        card = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(card)
        draw.rounded_rectangle((0, 0, card_w - 1, card_h - 1), radius=18, fill=CARD)
        draw.rectangle((0, 18, 7, card_h - 18), fill=RED if number == len(stats) - 1 else ACCENT)
        draw.text((36, int(card_h * 0.14)), value, font=number_font, fill=WHITE)
        draw.text((36, int(card_h * 0.66)), note, font=note_font, fill=MUTED)
        cards.append(card)

    def paint(index: int, frames: int):
        frame = base.copy()
        _title(ImageDraw.Draw(frame), title, width, height, index)
        for number, (card, (x, y)) in enumerate(zip(cards, cells)):
            pop = _ease_out_back(_step(index, 6 + number * 8, 10))
            if pop <= 0:
                continue
            piece = card.resize((max(1, int(card_w * pop)), max(1, int(card_h * pop))))
            frame.alpha_composite(piece, (x + (card_w - piece.width) // 2, y + (card_h - piece.height) // 2))
        return frame.convert("RGB")

    return paint


def checklist(title: str, items: list[tuple[str, bool]], width: int, height: int) -> Painter:
    """Promises against reality: each line gets a green tick or a red cross as it appears."""
    from PIL import ImageDraw

    base = _board(width, height)
    font = _font(int(height * 0.042), 600)
    left = int(width * 0.26)

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        _title(draw, title, width, height, index)
        for number, (text, ok) in enumerate(items):
            start = 8 + number * 14
            y = int(height * 0.3 + number * height * 0.14)
            _rise_text(draw, text, left + int(height * 0.09), y, font, WHITE, index, start, 18)
            mark = _ease_out_back(_step(index, start + 8, 8))
            if mark <= 0:
                continue
            s = int(height * 0.028 * mark)
            cx, cy = left + int(height * 0.03), y + font.size // 2 + 4
            if ok:
                draw.line((cx - s, cy, cx - s // 3, cy + s * 0.8, cx + s * 1.1, cy - s), fill=GREEN, width=max(5, height // 150), joint="curve")
            else:
                draw.line((cx - s, cy - s, cx + s, cy + s), fill=RED, width=max(5, height // 150))
                draw.line((cx - s, cy + s, cx + s, cy - s), fill=RED, width=max(5, height // 150))
        return frame

    return paint


def subscribe(name: str, width: int, height: int) -> Painter:
    """Channel name, a red SUBSCRIBE button a cursor clicks, then the bell lights up."""
    from PIL import ImageDraw

    base = _board(width, height)
    name_font = _font(int(height * 0.075), 800)
    button_font = _font(int(height * 0.05), 800)
    button_w, button_h = int(width * 0.26), int(height * 0.11)
    bx, by = (width - button_w) // 2, int(height * 0.5)
    bell_x, bell_y = bx + button_w + int(width * 0.05), by + button_h // 2

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame, "RGBA")
        _rise_text(draw, name, (width - draw.textlength(name, font=name_font)) / 2, int(height * 0.3), name_font, WHITE, index, 0, 24)
        pop = _ease_out_back(_step(index, 6, 10))
        if pop <= 0:
            return frame
        click = int(frames * 0.35)
        done = index >= click
        w, h = int(button_w * pop), int(button_h * pop)
        x, y = bx + (button_w - w) // 2, by + (button_h - h) // 2
        draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=(70, 72, 78) if done else (230, 33, 23))
        label = "SUBSCRIBED" if done else "SUBSCRIBE"
        if pop > 0.9:
            draw.text((bx + (button_w - draw.textlength(label, font=button_font)) / 2, by + (button_h - button_font.size) / 2 - 6),
                      label, font=button_font, fill=WHITE)
        travel = _ease_in_out(_step(index, 12, max(1, click - 12)))
        cx = int(width * 0.85 + (bx + button_w * 0.7 - width * 0.85) * travel)
        cy = int(height * 0.9 + (by + button_h * 0.6 - height * 0.9) * travel)
        if index < click + 20:
            s = int(height * 0.045)
            draw.polygon([(cx, cy), (cx, cy + s), (cx + s * 0.28, cy + s * 0.75), (cx + s * 0.45, cy + s * 1.1),
                          (cx + s * 0.6, cy + s * 1.03), (cx + s * 0.43, cy + s * 0.7), (cx + s * 0.75, cy + s * 0.7)],
                         fill=WHITE, outline=(10, 10, 12))
        ring = _ease_out_back(_step(index, click + 4, 10))
        if ring > 0:
            r = int(height * 0.035 * ring)
            glow = ACCENT if (index // 6) % 2 == 0 or index > click + 40 else WHITE
            draw.pieslice((bell_x - r, bell_y - r * 1.3, bell_x + r, bell_y + r * 0.7), 180, 360, fill=glow)
            draw.rectangle((bell_x - r, bell_y - r * 0.3, bell_x + r, bell_y + r * 0.45), fill=glow)
            draw.rectangle((bell_x - r * 1.25, bell_y + r * 0.45, bell_x + r * 1.25, bell_y + r * 0.62), fill=glow)
            draw.ellipse((bell_x - r * 0.22, bell_y + r * 0.62, bell_x + r * 0.22, bell_y + r * 1.0), fill=glow)
        return frame

    return paint

