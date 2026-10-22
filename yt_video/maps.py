from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

import httpx

Log = Callable[[str], None]
ZOOM = 5
TILE = 256
GRID = (8, 5)
TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
AGENT = "yt-video documentary maker (one map per video)"


def tile_xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    """Fractional OpenStreetMap tile coordinates of a point."""
    n = 2 ** zoom
    lat = max(-85.0, min(85.0, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def prepare_maps(cache: Path, project_dir: Path, segments: list[dict[str, Any]], client: httpx.Client, log: Log) -> None:
    for segment in segments:
        if segment.get("kind") != "map" or segment.get("map_art"):
            continue
        data = segment.get("data") or {}
        path = project_dir / "art" / f"{segment['id']}_map.png"
        try:
            pin = build_map(cache, float(data["lat"]), float(data["lon"]), path, client)
        except (httpx.HTTPError, OSError, KeyError, ValueError) as exc:
            log(f"карта  не скачалась ({exc}), кадр станет плашкой с названием места")
            segment.update(kind="term", screen=str(data.get("label") or ""), note="")
            continue
        segment["map_art"] = str(path)
        segment["data"] = {**data, "pin": pin}
        log(f"карта  {data.get('label')}")


def build_map(cache: Path, lat: float, lon: float, destination: Path, client: httpx.Client) -> tuple[float, float]:
    """A GRID of tiles around the point; returns the pin position as shares of the picture."""
    from io import BytesIO

    from PIL import Image

    x, y = tile_xy(lat, lon, ZOOM)
    left = int(x) - GRID[0] // 2
    top = int(y) - GRID[1] // 2
    n = 2 ** ZOOM
    image = Image.new("RGB", (GRID[0] * TILE, GRID[1] * TILE), (170, 211, 223))
    folder = cache / "tiles"
    folder.mkdir(parents=True, exist_ok=True)
    for dx in range(GRID[0]):
        for dy in range(GRID[1]):
            tx, ty = (left + dx) % n, top + dy
            if not 0 <= ty < n:
                continue
            path = folder / f"{ZOOM}_{tx}_{ty}.png"
            if not path.exists():
                response = client.get(TILES.format(z=ZOOM, x=tx, y=ty), headers={"User-Agent": AGENT}, timeout=30)
                response.raise_for_status()
                path.write_bytes(response.content)
            image.paste(Image.open(BytesIO(path.read_bytes())).convert("RGB"), (dx * TILE, dy * TILE))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination)
    return (x - left) / GRID[0], (y - top) / GRID[1]


def map_painter(path: Path, label: str, pin: tuple[float, float], width: int, height: int):
    """The camera flies in on the place, the pin drops, the place name types in on a yellow box."""
    from PIL import Image, ImageDraw, ImageEnhance

    from yt_video.boards import ACCENT, INK, _ease_in_out, _ease_out_back, _font

    art = ImageEnhance.Color(Image.open(path).convert("RGB")).enhance(0.75)
    scale = max(width / art.width, height / art.height)
    art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    px, py = pin[0] * art.width, pin[1] * art.height
    size = max(22, int(height * 0.055))
    font = _font(size, 900)
    small = _font(max(12, int(height * 0.016)), 500)

    def paint(index: int, frames: int):
        t = (index - 1) / max(1, frames - 1)
        zoom = 1.0 + 1.0 * _ease_in_out(min(1.0, t / 0.55))
        box_w, box_h = width / zoom, height / zoom
        cx = art.width / 2 + (px - art.width / 2) * _ease_in_out(min(1.0, t / 0.55))
        cy = art.height / 2 + (py - art.height / 2) * _ease_in_out(min(1.0, t / 0.55))
        bl = min(max(0.0, cx - box_w / 2), art.width - box_w)
        bt = min(max(0.0, cy - box_h / 2), art.height - box_h)
        frame = art.resize((width, height), Image.BICUBIC, box=(bl, bt, bl + box_w, bt + box_h))
        draw = ImageDraw.Draw(frame)
        x = (px - bl) * zoom
        y = (py - bt) * zoom
        drop = min(1.0, max(0.0, (t - 0.35) / 0.12))
        if drop > 0:
            r = height * 0.022 * _ease_out_back(drop)
            ring = height * 0.05 * min(1.0, max(0.0, (t - 0.4) / 0.2))
            if ring:
                draw.ellipse((x - ring, y - ring, x + ring, y + ring), outline=(226, 30, 30), width=max(2, height // 220))
            draw.ellipse((x - r * 1.35, y - r * 1.35, x + r * 1.35, y + r * 1.35), fill=(255, 255, 255))
            draw.ellipse((x - r, y - r, x + r, y + r), fill=(226, 30, 30))
        shown = int(len(label) * min(1.0, max(0.0, t - 0.5) / 0.25))  # typed in fully by 75% of the shot
        if shown > 0:
            text = label[:shown]
            tw = draw.textlength(text, font=font)
            lx = min(max(int(width * 0.04), x + height * 0.05), width - tw - height * 0.1)
            ly = min(max(int(height * 0.06), y - size * 2.4), height - size * 2.2)
            draw.rectangle((lx, ly, lx + tw + size, ly + size * 1.5), fill=ACCENT)
            draw.text((lx + size / 2, ly + size * 0.2), text, font=font, fill=INK)
        draw.text((width - int(height * 0.2), height - int(height * 0.035)), "© OpenStreetMap", font=small, fill=(60, 60, 60))
        return frame

    return paint
