from __future__ import annotations

import math
import os
import re
import subprocess
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from yt_video.ff import ffmpeg_exe
from yt_video.resolve_edit import canvas_size

Log = Callable[[str], None]
Painter = Callable[[int, int], Any]

FPS = 30
ACCENT = (255, 212, 0)
WHITE = (255, 255, 255)
MUTED = (196, 200, 208)
INK = (12, 12, 14)
FONT_FILE = Path(__file__).resolve().parent / "fonts" / "Inter.ttf"

SCENE_MOTIONS = ("push", "pan_right", "framed", "diagonal_up", "pull", "tilt", "pan_left", "rotate", "diagonal_down")
SPECIAL_MOTIONS = {"framed", "rack_focus", "photo_drop", "split"}
GRAPHIC_CUES = {
    "timeline": [(0.06, "line_draw"), (0.2, "appear"), (0.4, "appear"), (0.6, "appear")],
    "bars": [(0.06, "counter"), (0.15, "counter")],
    "before_after": [(0.02, "appear"), (0.1, "line_draw"), (0.2, "highlight")],
    "ring": [(0.03, "counter")],
    "chart": [(0.05, "line_draw")],
    "versus": [(0.02, "appear"), (0.12, "appear"), (0.3, "appear")],
    "stats": [(0.04, "appear"), (0.1, "appear"), (0.16, "appear"), (0.22, "appear")],
    "checklist": [(0.1, "appear"), (0.2, "appear"), (0.3, "appear"), (0.4, "appear")],
}
TRANSITION_FRAMES = {"dissolve": 10, "cut": 0}
POP_FRAMES = 9
COUNT_FRAMES = 16
ARTFUL_ZOOM = 1.4


def prepare_boards(project_dir: Path, segments: list[dict[str, Any]], aspect: str, log: Log) -> None:
    width, height = canvas_size(aspect)
    _assign_backdrops(segments)
    plan_motion(segments)
    folder = project_dir / "boards"
    folder.mkdir(parents=True, exist_ok=True)
    counts = []
    for segment in segments:
        start = float(segment.get("slot_start") or 0)
        end = float(segment.get("slot_end") or start + 3)
        counts.append(max(12, int(round(end * FPS)) - int(round(start * FPS))))
    jobs = []
    for index, segment in enumerate(segments):
        before = segments[index - 1] if index else None
        jobs.append((segment, before, counts[index - 1] if index else 0, width, height, counts[index], str(folder / f"{segment['id']}.mp4")))
    workers = max(1, (os.cpu_count() or 2) - 1)
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for segment, count, path in zip(segments, counts, pool.map(render_shot, jobs)):
            segment["frames"] = count
            segment["asset_path"] = path
            segment["asset_kind"] = "clip"
            segment["asset"] = f"boards/{segment['id']}.mp4"
            done += 1
            if done % 10 == 0 or done == len(segments):
                log(f"кадры  {done}/{len(segments)}")
    log(f"кадры  отрисовано {len(segments)} клипов на {workers} ядрах")


def render_shot(job: tuple) -> str:
    """Render one shot straight into an H.264 clip; runs in a worker process."""
    segment, before, before_count, width, height, count, out = job
    paint = _painter(segment, width, height, count)
    previous = _painter(before, width, height, before_count)(before_count, before_count) if before else None
    length = TRANSITION_FRAMES.get(str(segment.get("transition") or ""), 0) if previous is not None else 0
    encoder = subprocess.Popen(
        [
            ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", "-r", str(FPS), out,
        ],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for index in range(1, count + 1):
            frame = paint(index, count)
            if index <= length:
                frame = _transition(str(segment["transition"]), previous, frame, index / (length + 1))
            encoder.stdin.write(frame.tobytes())
    except (BrokenPipeError, OSError):
        pass  # ffmpeg quit early; its own message below says why
    finally:
        try:
            encoder.stdin.close()
        except OSError:
            pass
        error = encoder.stderr.read().decode("utf-8", "replace")
        encoder.wait()
    if encoder.returncode != 0:
        raise RuntimeError(f"ffmpeg {segment.get('id')}: {error[-300:]}")
    return out


def plan_motion(segments: list[dict[str, Any]]) -> None:
    """One slow camera move per scene, neighbours never repeat; every shot dissolves into the next."""
    scenes = 0
    for index, segment in enumerate(segments):
        previous = segments[index - 1] if index else {}
        if segment.get("kind") == "scene" and segment.get("video"):
            segment["motion"] = "video"
        elif segment.get("kind") == "scene" and segment.get("art_b"):
            segment["motion"] = "split"
        elif segment.get("kind") == "scene" and segment.get("archival"):
            segment["motion"] = "photo_drop"
        elif segment.get("kind") == "scene" and previous.get("kind") == "chapter":
            segment["motion"] = "rack_focus"
        elif segment.get("kind") == "scene":
            segment["motion"] = SCENE_MOTIONS[scenes % len(SCENE_MOTIONS)]
            scenes += 1
        segment["transition"] = "dissolve" if index else "cut"


def cue_points(segment: dict[str, Any]) -> list[tuple[float, str]]:
    """Moments, as a share of the shot, where something appears, with the sound category for it."""
    kind = str(segment.get("kind") or "")
    if kind == "subscribe":
        return [(0.04, "appear"), (0.35, "ui_click")]
    if kind == "map":
        return [(0.0, "zoom"), (0.37, "appear"), (0.5, "text_type")]
    if kind in GRAPHIC_CUES:
        return list(GRAPHIC_CUES[kind])
    if kind == "scene" and segment.get("stamp"):
        return [(0.08, "text_type")]
    if kind == "chapter":
        return [(0.0, "appear")]
    if kind == "number":
        return [(0.03, "counter" if _count_target(str(segment.get("screen") or "")) else "highlight")]
    if kind == "term":
        return [(0.0, "highlight")]
    if kind != "diagram":
        return []
    cues: list[tuple[float, str]] = []
    for index, (arrow_at, node_at) in enumerate(_diagram_times(len(segment.get("steps") or []) or 1)):
        if index:
            cues.append((arrow_at, "line_draw"))
        cues.append((node_at, "appear"))
    return cues


def _diagram_times(count: int) -> list[tuple[float, float]]:
    """(arrow starts, node pops) for each step, as shares of the shot."""
    times = []
    for index in range(count):
        node = 0.04 + index / max(1, count) * 0.7
        times.append((max(0.0, node - 0.12), node))
    return times


def _assign_backdrops(segments: list[dict[str, Any]]) -> None:
    arts = [(index, segment["art"]) for index, segment in enumerate(segments) if segment.get("art")]
    if not arts:
        return
    for index, segment in enumerate(segments):
        if segment.get("kind") not in {"number", "term", "endscreen"}:
            continue
        before = [art for position, art in arts if position < index]
        segment["backdrop"] = before[-1] if before else arts[0][1]


def render_frame(segment: dict[str, Any], width: int, height: int, reveal: float = 1.0):
    count = 60
    return _painter(segment, width, height, count)(max(1, int(round(reveal * count))), count)


def _painter(segment: dict[str, Any], width: int, height: int, count: int) -> Painter:
    kind = str(segment.get("kind") or "term")
    screen = str(segment.get("screen") or "").replace("\u2212", "-")
    note = str(segment.get("note") or "")
    if kind == "chapter":
        return _breathe(_chapter_painter(screen, width, height))
    if kind == "diagram":
        return _breathe(_diagram_painter(segment, width, height))
    if kind == "subscribe":
        from yt_video import graphics

        return _breathe(graphics.subscribe(screen, width, height), 0.02)
    if kind == "map" and segment.get("map_art"):
        from yt_video.maps import map_painter

        data = segment.get("data") or {}
        return map_painter(Path(segment["map_art"]), str(data.get("label") or ""), tuple(data.get("pin") or (0.5, 0.5)), width, height)
    if kind == "endscreen":
        return _breathe(_calm_backdrop(segment.get("backdrop"), width, height), 0.05)
    if segment.get("data") and kind in GRAPHIC_CUES:
        return _breathe(_graphic_painter(kind, segment["data"], width, height), 0.02)
    if kind == "scene" and (segment.get("video") or segment.get("art")):
        motion = str(segment.get("motion") or "push")
        if segment.get("video"):
            paint = _video_painter(Path(segment["video"]), width, height, float(segment.get("video_duration") or 0))
        elif motion in SPECIAL_MOTIONS:
            paint = _special_motion(motion, segment, width, height)
        else:
            paint = _scene_painter(Path(segment["art"]), width, height, motion)
        return _with_stamp(paint, str(segment["stamp"]), width, height) if segment.get("stamp") else paint
    if kind == "scene":
        return _breathe(_title_painter(_board(width, height), screen, "", width, height, count_up=False))
    backdrop = _backdrop(segment.get("backdrop"), width, height)
    return _breathe(_title_painter(backdrop, screen, note, width, height, count_up=kind == "number"))


def _graphic_painter(kind: str, data: dict[str, Any], width: int, height: int) -> Painter:
    from yt_video import graphics

    if kind == "timeline":
        return graphics.timeline(data["title"], [tuple(point) for point in data["points"]], width, height)
    if kind == "bars":
        return graphics.bars(data["title"], [tuple(item) for item in data["items"]], width, height)
    if kind == "before_after":
        return graphics.before_after(data["before"], data["after"], data.get("note", ""), width, height)
    if kind == "ring":
        return graphics.ring(float(data["percent"]), data.get("note", ""), width, height)
    if kind == "chart":
        return graphics.line_chart(data["title"], [tuple(point) for point in data["points"]], width, height)
    if kind == "versus":
        left, right = data["left"], data["right"]
        return graphics.versus((left[0], list(left[1])), (right[0], list(right[1])), width, height)
    if kind == "stats":
        return graphics.stat_grid(data["title"], [tuple(stat) for stat in data["stats"]], width, height)
    return graphics.checklist(data["title"], [tuple(row) for row in data["items"]], width, height)


def _special_motion(motion: str, segment: dict[str, Any], width: int, height: int) -> Painter:
    from yt_video import motions

    art = Path(segment["art"])
    if motion == "split" and segment.get("art_b"):
        return motions.split_then_now(art, Path(segment["art_b"]), width, height)
    if motion == "photo_drop":
        return motions.photo_drop(art, width, height)
    if motion == "rack_focus":
        return motions.rack_focus(art, width, height)
    return motions.framed(art, width, height)


# Scenes: one slow camera move over the whole illustration.

def _camera(motion: str, t: float) -> tuple[float, float, float]:
    """zoom, centre x, centre y (shares of the picture) at time t of the move."""
    e = _ease_in_out(t)
    if motion == "pull":
        return 1.16 - 0.14 * e, 0.5, 0.5
    if motion == "pan_right":
        return 1.18, 0.4 + 0.2 * e, 0.5
    if motion == "pan_left":
        return 1.18, 0.6 - 0.2 * e, 0.5
    if motion == "tilt":
        return 1.18, 0.5, 0.6 - 0.2 * e
    if motion == "diagonal_up":
        return 1.2, 0.42 + 0.16 * e, 0.58 - 0.16 * e
    if motion == "diagonal_down":
        return 1.2, 0.58 - 0.16 * e, 0.42 + 0.16 * e
    if motion == "rotate":
        return 1.1 + 0.06 * e, 0.5, 0.5
    return 1.02 + 0.12 * e, 0.5, 0.5


def _scene_painter(path: Path, width: int, height: int, motion: str) -> Painter:
    from PIL import Image

    art = Image.open(path).convert("RGB")
    scale = max(width / art.width, height / art.height) * ARTFUL_ZOOM
    art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    full_w = art.width / ARTFUL_ZOOM
    full_h = full_w * height / width
    if full_h > art.height / ARTFUL_ZOOM:
        full_h = art.height / ARTFUL_ZOOM
        full_w = full_h * width / height
    def paint(index: int, frames: int):
        zoom, cx, cy = _camera(motion, (index - 1) / max(1, frames - 1))
        box_w = full_w / zoom
        box_h = full_h / zoom
        left = min(max(0.0, art.width * cx - box_w / 2), art.width - box_w)
        top = min(max(0.0, art.height * cy - box_h / 2), art.height - box_h)
        frame = art.resize((width, height), Image.BICUBIC, box=(left, top, left + box_w, top + box_h))
        if motion == "rotate":
            angle = 2.2 * (1 - _ease_in_out((index - 1) / max(1, frames - 1)))
            frame = _zoomed(frame.rotate(angle, resample=Image.BICUBIC), 1.09)
        return frame

    return paint


def _video_painter(path: Path, width: int, height: int, duration: float) -> Painter:
    """Real footage, decoded frame by frame; a clip shorter than the shot is slowed down, then held."""
    from PIL import Image

    size = width * height * 3
    state: dict[str, Any] = {"proc": None, "next": 0, "last": None}

    def close() -> None:
        proc = state["proc"]
        if proc is not None:
            proc.stdout.close()
            proc.kill()
            proc.wait()
        state["proc"] = None

    def open_at(index: int, frames: int) -> None:
        close()
        span = frames / FPS
        usable = max(0.5, duration - 0.3) if duration else span
        slow = min(1.6, max(1.0, span / usable))
        start = max(0.0, min(0.4, usable - span / slow))
        offset = start + (index - 1) / FPS / slow
        state["proc"] = subprocess.Popen(
            [
                ffmpeg_exe(), "-v", "error", "-ss", f"{offset:.3f}", "-i", str(path), "-an",
                "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"setpts={slow:.4f}*(PTS-STARTPTS),fps={FPS}",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def paint(index: int, frames: int):
        if state["proc"] is None or state["next"] != index:
            open_at(index, frames)
        data = state["proc"].stdout.read(size)
        if len(data) == size:
            state["last"] = Image.frombytes("RGB", (width, height), data)
        state["next"] = index + 1
        if index >= frames:
            close()
        return state["last"] if state["last"] is not None else Image.new("RGB", (width, height))

    return paint


def _with_stamp(paint: Painter, text: str, width: int, height: int) -> Painter:
    """A documentary place-and-year label that types itself in the lower left and fades out with the shot."""
    from PIL import Image, ImageDraw

    size = max(18, int(height * 0.03))
    font = _font(size, 600)
    margin = int(height * 0.075)
    x = margin + int(size * 0.7)
    y = height - margin - size

    def stamped(index: int, frames: int):
        frame = paint(index, frames)
        shown = int(max(0, index - 8) / 1.4)
        fade = min(1.0, max(0.0, (frames - index) / 8))
        if shown <= 0 or fade <= 0:
            return frame
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        alpha = int(255 * fade)
        draw.rectangle((margin, y - 4, margin + max(3, size // 6), y + size + 6), fill=ACCENT + (alpha,))
        draw.text((x, y), text[:shown], font=font, fill=WHITE + (alpha,), stroke_width=2, stroke_fill=(0, 0, 0, int(170 * fade)))
        out = frame.convert("RGBA")
        out.alpha_composite(layer)
        return out.convert("RGB")

    return stamped


def _count_target(text: str) -> re.Match | None:
    found = list(re.finditer(r"\d+(?:[.,]\d+)?", text))
    return found[0] if len(found) == 1 else None


def _title_painter(background, text: str, note: str, width: int, height: int, count_up: bool) -> Painter:
    from PIL import Image, ImageDraw

    base = background.convert("RGBA")
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    label = text or " "
    short = len(label) <= 10
    font = _fit(probe, label, int(width * 0.84), int(height * (0.3 if short else 0.2)), int(height * (0.22 if short else 0.11)), 750)
    note_font = _fit(probe, note, int(width * 0.74), int(height * 0.08), max(22, int(height * 0.04)), 500) if note else None
    text_h = font.getbbox("0Ay")[3]
    gap = int(height * 0.045)
    block = text_h + (gap + note_font.getbbox("Ay")[3] if note_font else 0)
    top = (height - block) // 2
    target = _count_target(label) if count_up else None
    words = label.split(" ")
    space = probe.textlength(" ", font=font)
    total = probe.textlength(label, font=font)
    rise = int(height * 0.04)
    bar_w = int(width * 0.06)

    def paint(index: int, frames: int):
        frame = base.copy()
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        grow = _ease_out_cubic(min(1.0, index / 8))
        bar_x = (width - bar_w) // 2
        draw.rounded_rectangle((bar_x, top - gap, bar_x + max(2, int(bar_w * grow)), top - gap + max(4, height // 140)), radius=3, fill=ACCENT)
        if target is not None:
            shown = _counted(label, target, min(1.0, index / COUNT_FRAMES))
            _rise_text(draw, shown, (width - probe.textlength(shown, font=font)) / 2, top, font, WHITE, index, 0, rise)
        else:
            x = (width - total) / 2
            for order, word in enumerate(words):
                _rise_text(draw, word, x, top, font, WHITE, index, order * 3, rise)
                x += probe.textlength(word, font=font) + space
        if note_font is not None:
            note_x = (width - probe.textlength(note, font=note_font)) / 2
            _rise_text(draw, note, note_x, top + text_h + gap, note_font, MUTED, index, 8 + 3 * len(words), rise // 2)
        frame.alpha_composite(layer)
        return frame.convert("RGB")

    return paint


def _counted(label: str, match: re.Match, progress: float) -> str:
    raw = match.group(0)
    value = float(raw.replace(",", "."))
    current = value * _ease_out_cubic(progress)
    decimals = len(raw.split(",")[-1].split(".")[-1]) if ("," in raw or "." in raw) else 0
    shown = f"{current:.{decimals}f}"
    if "," in raw:
        shown = shown.replace(".", ",")
    return label[: match.start()] + shown + label[match.end():]


def _rise_text(draw, text: str, x: float, y: float, font, fill, index: int, delay: int, rise: int) -> None:
    t = min(1.0, max(0.0, (index - delay) / 7))
    if t <= 0:
        return
    offset = int(rise * (1 - _ease_out_cubic(t)))
    draw.text((x, y + offset), text, font=font, fill=fill + (int(255 * t),))


def _chapter_painter(text: str, width: int, height: int) -> Painter:
    from PIL import Image, ImageDraw

    base = _board(width, height).convert("RGBA")
    lines = _wrap(text.upper(), 22 if width >= height else 13)[:3] or [" "]
    size = max(28, int(min(width, height) * 0.075))
    font = _font(size, 800)
    probe = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    line_gap = int(size * 1.18)
    block_w = max(probe.textlength(line, font=font) for line in lines)
    pad_x = int(size * 0.8)
    pad_y = int(size * 0.55)
    card_w = int(min(width - 40, block_w + pad_x * 2))
    card_h = line_gap * len(lines) + pad_y * 2
    left = (width - card_w) // 2
    top = (height - card_h) // 2

    def paint(index: int, frames: int):
        frame = base.copy()
        draw = ImageDraw.Draw(frame)
        grow = _ease_out_cubic(min(1.0, index / 7))
        shown = max(card_h, int(card_w * grow))
        draw.rounded_rectangle(
            (width // 2 - shown // 2, top, width // 2 + shown // 2, top + card_h),
            radius=int(card_h * 0.14),
            fill=ACCENT,
        )
        layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
        text_draw = ImageDraw.Draw(layer)
        y = top + pad_y
        for order, line in enumerate(lines):
            x = (width - probe.textlength(line, font=font)) / 2
            _rise_text(text_draw, line, x, y, font, INK, index, 5 + order * 2, int(size * 0.4))
            y += line_gap
        mask = Image.new("L", frame.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((left, top, left + card_w, top + card_h), radius=int(card_h * 0.14), fill=255)
        frame.paste(layer, (0, 0), Image.composite(layer, Image.new("RGBA", frame.size, (0, 0, 0, 0)), mask))
        return frame.convert("RGB")

    return paint


# Diagrams: nodes pop in, curved arrows draw themselves between them.

def _diagram_painter(segment: dict[str, Any], width: int, height: int) -> Painter:
    from PIL import Image, ImageDraw

    base = _board(width, height).convert("RGBA")
    labels = [str(step) for step in segment.get("steps") or []][:4] or [str(segment.get("screen") or " ")]
    icon_paths = list(segment.get("icon_art") or [])
    title = str(segment.get("screen") or "")
    title_font = _fit(ImageDraw.Draw(base), title, int(width * 0.8), int(height * 0.1), int(min(width, height) * 0.068), 650) if title else None
    horizontal = width >= height
    count = len(labels)
    if horizontal:
        radius = int(min(width / (count + 1) * 0.25, height * 0.14))
    else:
        radius = int(min(height * 0.74 / (count + 1) * 0.28, width * 0.15))
    centers = []
    for index in range(count):
        share = (index + 1) / (count + 1)
        if horizontal:
            centers.append((int(width * share), int(height * 0.52)))
        else:
            centers.append((int(width * 0.5), int(height * (0.17 + share * 0.74))))
    label_font = _font(max(20, int(radius * 0.3)), 550)
    tiles = []
    for index, label in enumerate(labels):
        icon = icon_paths[index] if index < len(icon_paths) else None
        tiles.append(_node_tile(label, icon, radius, label_font))
    times = _diagram_times(count)
    arrows = []
    for index in range(1, count):
        bend = -1 if index % 2 else 1
        arrows.append(_curve(centers[index - 1], centers[index], radius, bend, horizontal))

    def paint(index: int, frames: int):
        share = index / max(1, frames)
        frame = base.copy()
        if title_font is not None:
            layer = Image.new("RGBA", frame.size, (0, 0, 0, 0))
            tx = (width - ImageDraw.Draw(layer).textlength(title, font=title_font)) / 2
            _rise_text(ImageDraw.Draw(layer), title, tx, int(height * 0.1), title_font, WHITE, index, 0, int(height * 0.03))
            frame.alpha_composite(layer)
        for order, (arrow_at, node_at) in enumerate(times):
            if order:
                progress = (share - arrow_at) / max(0.01, node_at - arrow_at)
                if progress > 0:
                    _draw_curve(frame, arrows[order - 1], min(1.0, progress))
            start = int(round(node_at * frames)) + 1
            if index < start:
                continue
            tile = tiles[order]
            step = index - start + 1
            if step < POP_FRAMES:
                scale = max(0.05, _ease_out_back(step / POP_FRAMES))
                tile = tile.resize((max(1, int(tile.width * scale)), max(1, int(tile.height * scale))), Image.BILINEAR)
            cx, cy = centers[order]
            cy += int(radius * 0.3)
            frame.alpha_composite(tile, (cx - tile.width // 2, cy - tile.height // 2))
        return frame.convert("RGB")

    return paint


def _node_tile(label: str, icon: str | None, radius: int, font):
    from PIL import Image, ImageDraw, ImageFilter

    pad = int(radius * 0.5)
    label_h = int(radius * 1.15)
    size = (int(radius * 3.6), radius * 2 + pad * 2 + label_h)
    cx, cy = size[0] // 2, pad + radius
    ring = max(4, radius // 14)
    tile = Image.new("RGBA", size, (0, 0, 0, 0))
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=ACCENT + (170,), width=ring * 3)
    tile.alpha_composite(glow.filter(ImageFilter.GaussianBlur(radius * 0.12)))
    draw = ImageDraw.Draw(tile)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=(18, 19, 22, 255))
    inner = radius - ring * 2
    if icon and Path(icon).exists():
        art = Image.open(icon).convert("RGB").resize((inner * 2, inner * 2), Image.LANCZOS)
        mask = Image.new("L", art.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, inner * 2 - 1, inner * 2 - 1), fill=255)
        tile.paste(art, (cx - inner, cy - inner), mask)
        _label(draw, label, cx, cy + radius + int(pad * 0.6), size[0] - 8, font.size)
    else:
        text_font = _fit(draw, label, int(inner * 1.6), int(inner * 0.8), int(radius * 0.36), 600)
        draw.text((cx, cy), label, font=text_font, anchor="mm", fill=WHITE)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=ACCENT, width=ring)
    return tile


def _label(draw, text: str, cx: int, top: int, width: int, size: int) -> None:
    """A node caption: one line if it fits at a readable size, otherwise two balanced lines."""
    lines = [text]
    font = _font(size, 550)
    if draw.textlength(text, font=font) > width and " " in text:
        words = text.split()
        best = min(range(1, len(words)), key=lambda cut: abs(len(" ".join(words[:cut])) - len(" ".join(words[cut:]))))
        lines = [" ".join(words[:best]), " ".join(words[best:])]
    while size > 16 and max(draw.textlength(line, font=font) for line in lines) > width:
        size -= 2
        font = _font(size, 550)
    for index, line in enumerate(lines):
        draw.text((cx, top + index * int(size * 1.2)), line, font=font, anchor="mt", fill=WHITE)


def _curve(start: tuple[int, int], end: tuple[int, int], radius: int, bend: int, horizontal: bool) -> dict[str, Any]:
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy) or 1
    ux, uy = dx / length, dy / length
    nx, ny = -uy * bend, ux * bend
    gap = radius * 1.1
    p0 = (start[0] + ux * gap, start[1] + uy * gap)
    p2 = (end[0] - ux * gap, end[1] - uy * gap)
    lift = length * 0.32
    p1 = ((p0[0] + p2[0]) / 2 + nx * lift, (p0[1] + p2[1]) / 2 + ny * lift)
    points = [_bezier(p0, p1, p2, step / 40) for step in range(41)]
    return {"points": points, "width": max(4, radius // 11)}


def _bezier(p0, p1, p2, t: float) -> tuple[float, float]:
    a = (1 - t) ** 2
    b = 2 * (1 - t) * t
    c = t * t
    return a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]


def _draw_curve(frame, curve: dict[str, Any], progress: float) -> None:
    from PIL import Image, ImageDraw

    points = curve["points"][: max(2, int(round(len(curve["points"]) * _ease_in_out(progress))))]
    thick = curve["width"]
    head = thick * 3.2
    xs = [x for x, _ in curve["points"]]
    ys = [y for _, y in curve["points"]]
    margin = int(head * 2)
    left, top = int(min(xs)) - margin, int(min(ys)) - margin
    box = (int(max(xs)) + margin - left, int(max(ys)) + margin - top)
    scale = 2
    layer = Image.new("RGBA", (box[0] * scale, box[1] * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    local = [((x - left) * scale, (y - top) * scale) for x, y in points]
    draw.line(local, fill=WHITE + (235,), width=thick * scale, joint="curve")
    r = thick * scale / 2
    draw.ellipse((local[0][0] - r, local[0][1] - r, local[0][0] + r, local[0][1] + r), fill=WHITE + (235,))
    if len(local) >= 2:
        (x1, y1), (x2, y2) = local[-2], local[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        size = head * scale
        tip = (x2 + math.cos(angle) * size * 0.35, y2 + math.sin(angle) * size * 0.35)
        wing_a = (tip[0] - size * math.cos(angle - 0.5), tip[1] - size * math.sin(angle - 0.5))
        wing_b = (tip[0] - size * math.cos(angle + 0.5), tip[1] - size * math.sin(angle + 0.5))
        draw.polygon([tip, wing_a, wing_b], fill=WHITE + (235,))
    layer = layer.resize(box, Image.LANCZOS)
    frame.alpha_composite(layer, (left, top))


# Transition into a shot, blended over its first frames.

def _transition(kind: str, previous, current, t: float):
    from PIL import Image

    if kind == "dissolve":
        return Image.blend(previous, current, _ease_in_out(t))
    return current


def _zoomed(image, scale: float):
    from PIL import Image

    width, height = image.size
    box_w, box_h = width / scale, height / scale
    left, top = (width - box_w) / 2, (height - box_h) / 2
    return image.resize((width, height), Image.BILINEAR, box=(left, top, left + box_w, top + box_h))


def _breathe(paint: Painter, amount: float = 0.035) -> Painter:
    """A slow push-in on top of any static layout, so no shot stands still."""

    def breathing(index: int, frames: int):
        return _zoomed(paint(index, frames), 1 + amount * _ease_in_out(index / max(1, frames)))

    return breathing


# Backgrounds.

def _backdrop(path: Any, width: int, height: int):
    from PIL import Image, ImageEnhance, ImageFilter

    if not path or not Path(str(path)).exists():
        return _board(width, height)
    art = Image.open(str(path)).convert("RGB")
    scale = max(width / art.width, height / art.height)
    art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    left = (art.width - width) // 2
    top = (art.height - height) // 2
    art = art.crop((left, top, left + width, top + height))
    art = art.filter(ImageFilter.GaussianBlur(max(10, width // 70)))
    return ImageEnhance.Brightness(art).enhance(0.38)


def _calm_backdrop(path: Any, width: int, height: int) -> Painter:
    """The end-screen zone: the last picture of the story, softly blurred and bright, no text on it,
    so YouTube's end screen elements sit on a clean background."""
    from PIL import Image, ImageEnhance, ImageFilter

    if not path or not Path(str(path)).exists():
        still = _board(width, height)
    else:
        art = Image.open(str(path)).convert("RGB")
        scale = max(width / art.width, height / art.height)
        art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
        left, top = (art.width - width) // 2, (art.height - height) // 2
        art = art.crop((left, top, left + width, top + height)).filter(ImageFilter.GaussianBlur(max(6, width // 160)))
        still = ImageEnhance.Brightness(art).enhance(0.8)
    return lambda _index, _frames: still


@lru_cache(maxsize=4)
def _board_cached(width: int, height: int):
    from PIL import Image, ImageDraw, ImageFilter

    top, bottom = (24, 26, 31), (11, 12, 14)
    gradient = Image.new("RGB", (1, height))
    for y in range(height):
        k = y / max(1, height - 1)
        gradient.putpixel((0, y), tuple(int(a + (b - a) * k) for a, b in zip(top, bottom)))
    board = gradient.resize((width, height))
    draw = ImageDraw.Draw(board)
    step = max(32, width // 30)
    dot = max(1, width // 900)
    for x in range(step // 2, width, step):
        for y in range(step // 2, height, step):
            draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=(40, 43, 50))
    shade = Image.new("L", (width, height), 0)
    ImageDraw.Draw(shade).ellipse((-width * 0.25, -height * 0.3, width * 1.25, height * 1.3), fill=255)
    shade = shade.filter(ImageFilter.GaussianBlur(min(width, height) // 5))
    return Image.composite(board, Image.new("RGB", (width, height), (6, 6, 8)), shade)


def _board(width: int, height: int):
    return _board_cached(width, height).copy()


# Easing, text helpers, fonts.

def _ease_out_back(t: float) -> float:
    c1 = 1.70158
    c3 = c1 + 1
    return 1 + c3 * (t - 1) ** 3 + c1 * (t - 1) ** 2


def _ease_out_cubic(t: float) -> float:
    return 1 - (1 - max(0.0, min(1.0, t))) ** 3


def _ease_in_out(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * max(0.0, min(1.0, t)))


def _wrap(text: str, limit: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if current and len(trial) > limit:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return lines


def _fit(draw, text: str, max_w: int, max_h: int, start: int, weight: int = 700):
    size = max(18, start)
    font = _font(size, weight)
    while size > 18:
        font = _font(size, weight)
        box = draw.textbbox((0, 0), text, font=font)
        if box[2] - box[0] <= max_w and box[3] - box[1] <= max_h:
            return font
        size -= 4
    return font


@lru_cache(maxsize=128)
def _font(size: int, weight: int = 700):
    from PIL import ImageFont

    if FONT_FILE.exists():
        font = ImageFont.truetype(str(FONT_FILE), size)
        try:
            font.set_variation_by_axes([min(32, max(14, size)), weight])
        except (OSError, ValueError):
            pass
        return font
    for path in (Path(r"C:\Windows\Fonts\segoeuib.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()
