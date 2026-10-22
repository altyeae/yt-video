from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import httpx

from yt_video.boards import ACCENT, _font
from yt_video.config import Settings
from yt_video.illustrate import generate_image

SIZE = (1280, 720)


BRIGHT_LOOK = (
    "Crisp realistic drone or wide-angle photograph taken at bright midday: clear saturated blue sky, strong sunlight, "
    "vivid true colours, very sharp, high clarity, no fog, no gloom, no dark grading. One huge readable subject whose "
    "absurdity is obvious in a split second (enormous yet empty, brand new yet abandoned), placed slightly right of centre. "
    "Absolutely no text, letters, numbers, logos, arrows, circles, frames or any graphic overlays in the image."
)
RED_BOX = (226, 30, 30)


def make_bright_thumbnail(
    settings: Settings,
    prompt: str,
    text: str,
    destination: Path,
    client: httpx.Client,
) -> Path:
    """The style of the most watched channels in the niche: a bright daylight photo of an absurd sight,
    optionally one or two words on a red box with a red arrow pointing at the subject."""
    from io import BytesIO

    from PIL import Image

    data = generate_image(settings, f"{prompt}\n\n{BRIGHT_LOOK}", "16:9", client)
    art = Image.open(BytesIO(data)).convert("RGB")
    scale = max(SIZE[0] / art.width, SIZE[1] / art.height)
    art = art.resize((int(art.width * scale) + 1, int(art.height * scale) + 1), Image.LANCZOS)
    left, top = (art.width - SIZE[0]) // 2, (art.height - SIZE[1]) // 2
    image = art.crop((left, top, left + SIZE[0], top + SIZE[1]))
    image = _natural(image)
    if text:
        _label(image, text, right=True, target=None)
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, quality=92)
    return destination


def _arrow(draw, start: tuple[float, float], end: tuple[float, float]) -> None:
    """A thick red arrow with a white outline, the classic pointer of high-CTR thumbnails."""
    import math

    angle = math.atan2(end[1] - start[1], end[0] - start[0])
    head = 70
    base = (end[0] - math.cos(angle) * head * 0.8, end[1] - math.sin(angle) * head * 0.8)
    wing_a = (end[0] - head * math.cos(angle - 0.55), end[1] - head * math.sin(angle - 0.55))
    wing_b = (end[0] - head * math.cos(angle + 0.55), end[1] - head * math.sin(angle + 0.55))
    draw.line((start, base), fill=(255, 255, 255), width=34)
    draw.polygon([end, wing_a, wing_b], fill=(255, 255, 255))
    draw.line((start, base), fill=RED_BOX, width=22)
    inner = 0.82
    draw.polygon(
        [end, (end[0] + (wing_a[0] - end[0]) * inner, end[1] + (wing_a[1] - end[1]) * inner),
         (end[0] + (wing_b[0] - end[0]) * inner, end[1] + (wing_b[1] - end[1]) * inner)],
        fill=RED_BOX,
    )


def face_brief(
    settings: Settings,
    record: dict[str, Any],
    client: httpx.Client,
    avoid: list[str] | None = None,
    recent: list[str] | None = None,
) -> dict[str, str]:
    """A short honest question for the 'shocked face' thumbnail, the scene behind the host and the host's emotion,
    never an emotion of the last two videos so the channel page does not look repetitive."""
    from yt_video.script import ask_json

    story = " ".join(str(segment.get("narration") or "") for segment in record.get("segments") or [])[:4000]
    allowed = [name for name in EMOTIONS if name not in (avoid or [])]
    brief = ask_json(settings, (
        f"Documentary: {record.get('title', '')}\n"
        f"Story: {story}\n"
        "Write a high-CTR YouTube thumbnail of the 'shocked person' type, honest to the story.\n"
        "top: 1-3 words, the start of a question; bottom: 1-2 words that finish it with '?' "
        "(like 'WHO PAID' / '$2.4B?', 'WHO BOUGHT IT' / 'FOR €10,000?', 'HOW DID IT' / 'BURN?'). Capitals. The question "
        "must be answered by the video and must not claim anything the video does not show. It must be specific to "
        "this story: a number, a name or an object from it. A question that fits any abandoned place, such as "
        "'WHY IS IT EMPTY?', is not allowed.\n"
        + (f"The channel's recent thumbnails asked: {' | '.join(recent)}. Ask something different.\n" if recent else "")
        + "scene: English image description of what is behind the person: the place itself at its most striking, "
        "in bright daylight, recognisable, with no people near it.\n"
        "Never use brand names, trademarks, company or character names in top, bottom or scene.\n"
        f"emotion: the one that fits the story best, one of: {', '.join(allowed)}.\n"
        'Return only JSON: {"top": "...", "bottom": "...", "scene": "...", "emotion": "shock"}'
    ), client, "scene")
    if brief.get("emotion") not in allowed:
        brief["emotion"] = next(name for name in PREFERRED if name in allowed)
    return brief


def recent_questions(settings: Settings, project_dir: Path, count: int = 5) -> list[str]:
    """The questions on the latest thumbnails, so the channel page does not repeat itself."""
    import json

    found: list[str] = []
    for folder in sorted(settings.projects_dir.glob("*"), reverse=True):
        note = folder / "thumbnails" / "face.json"
        if folder != project_dir and note.exists() and len(found) < count:
            data = json.loads(note.read_text(encoding="utf-8"))
            found.append(f"{data.get('top', '')} {data.get('bottom', '')}".strip())
    return found


def recent_emotions(settings: Settings, project_dir: Path, count: int = 2) -> list[str]:
    """The host's emotions on the thumbnails of the videos made just before this one."""
    import json

    found: list[str] = []
    for folder in sorted(settings.projects_dir.glob("*"), reverse=True):
        note = folder / "thumbnails" / "face.json"
        if folder.name < project_dir.name and note.exists() and len(found) < count:
            found.append(str(json.loads(note.read_text(encoding="utf-8")).get("emotion") or ""))
    return found


def make_host_thumbnail(
    settings: Settings,
    project_dir: Path,
    record: dict[str, Any],
    client: httpx.Client,
    log: Callable[[str], None],
) -> Path | None:
    """The channel's main thumbnail: its host reacting to the place, with a question. Skipped for channels without a host."""
    import json

    from yt_video.config import CHANNELS

    host = CHANNELS / f"{record.get('channel') or settings.channel}_host.png"
    if not host.exists():
        return None
    brief = face_brief(settings, record, client, recent_emotions(settings, project_dir), recent_questions(settings, project_dir))
    folder = project_dir / "thumbnails"
    path = make_face_thumbnail(
        settings, brief["scene"], brief["top"], brief["bottom"], folder / "face.jpg", client, brief["emotion"], host
    )
    (folder / "face.json").write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"превью  главное: {brief['top']} / {brief['bottom']} ({brief['emotion']})")
    return path


EMOTIONS = {
    "shock": "a shocked open-mouthed expression, one hand on the head",
    "disbelief": "a frowning sceptical expression with one eyebrow raised, arms crossed",
    "pointing": "a worried, wide-eyed expression, pointing back over the shoulder at the place behind",
    "fear": "a frightened expression, both palms pressed to the cheeks",
    "confusion": "a baffled expression, shrugging with both palms up",
}
PREFERRED = ["shock", "pointing", "fear", "confusion", "disbelief"]
HOST_NOTE = (
    "The attached image shows the channel's host. Draw exactly this same person: same face, age, hair, build and "
    "clothes. Only the expression, pose and the background change. Do not copy anything else from the attached image."
)


def make_face_thumbnail(
    settings: Settings,
    scene: str,
    top: str,
    bottom: str,
    destination: Path,
    client: httpx.Client,
    emotion: str = "shock",
    host: Path | None = None,
) -> Path:
    """The style that tested best: the channel's host in the right foreground reacting to the place behind,
    a two-line question on red and yellow boxes. The same person every time, only the emotion changes."""
    prompt = (
        f"The host in the right third of the frame, face turned to the camera with {EMOTIONS.get(emotion, EMOTIONS['shock'])}, "
        f"sharp and well lit. Behind, filling the rest of the frame: {scene}. Bright daylight, photorealistic, "
        "vivid but natural colours, sharp detail. The host is a fictional character, not a real or famous individual. "
        "Absolutely no text, letters, logos, arrows, circles, frames or graphic overlays in the image."
    )
    reference = host.read_bytes() if host and host.exists() else None
    data = generate_image(settings, prompt, "16:9", client, reference, HOST_NOTE)
    image = _natural(_cover_image_bytes(data, SIZE))
    _boxed(image, (40, 40), top.upper(), 92, RED_BOX, (255, 255, 255))
    _boxed(image, (40, 190), bottom.upper(), 118, ACCENT, (10, 10, 12))
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, quality=92)
    return destination


def _boxed(image, xy: tuple[int, int], text: str, size: int, colour, ink) -> None:
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    font = _font(size, 900)
    while draw.textlength(text, font=font) > SIZE[0] * 0.55 and size > 50:
        size -= 6
        font = _font(size, 900)
    width = draw.textlength(text, font=font)
    x, y = xy
    draw.rectangle((x, y, x + width + 40, y + size + 36), fill=colour)
    draw.text((x + 20, y + 18 - size * 0.1), text, font=font, fill=ink)


def footage_thumbnails(
    settings: Settings,
    project_dir: Path,
    record: dict[str, Any],
    hero_prompt: str,
    words: list[str],
    client: httpx.Client,
    log: Callable[[str], None],
) -> bool:
    """1: the real place itself, drawn clean (no overlays), natural colour. 2: the same with words and an arrow
    that Gemini aims at the subject. 3: then/now split from the video's own real shots."""
    import base64
    from io import BytesIO

    from PIL import Image

    from yt_video.stock import _vision_json

    candidates = _thumbnail_candidates(project_dir, record.get("segments") or [])
    if len(candidates) < 2:
        return False
    hero = _cover_image_bytes(generate_image(settings, f"{hero_prompt}\n\n{BRIGHT_LOOK}", "16:9", client), SIZE)
    hero = _natural(hero)

    def encoded(image) -> dict[str, Any]:
        copy = image.copy()
        copy.thumbnail((384, 384))
        buffer = BytesIO()
        copy.save(buffer, format="JPEG", quality=80)
        return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode("ascii")}}

    parts: list[dict[str, Any]] = [{"text": (
        f"Documentary: {record.get('title', '')}.\n"
        "First image H is the thumbnail picture. Give hero_point: x and y (0-1) of the centre of its main subject, and "
        "hero_point_sure: true only if there is one clear main object.\n"
        "Then from the numbered images pick then: this very place in its busy prime (crowds, new, open) and now: this "
        "very place empty or abandoned, so the pair reads as before/after at tiny size. Both must plausibly show THIS "
        "place: same country, era, architecture and setting as the documentary. A generic photo of somewhere else "
        "(another city, country or culture) is never allowed, even if it looks busy; illustrations made for this video "
        "usually show the place itself, so prefer them unless a real photo clearly shows the same place. "
        "Bright and clear, no visible text or logos, no close-up faces.\n"
        'Return only JSON: {"hero_point": [0.5, 0.5], "hero_point_sure": true, "then": 0, "now": 1}'
    )}, {"text": "Image H:"}, encoded(hero)]
    for index, (kind, path, narration) in enumerate(candidates):
        parts.append({"text": f"Image {index} ({kind}): {narration[:120]}"})
        parts.append(encoded(Image.open(path).convert("RGB")))
    choice = _vision_json(settings, parts, client)
    folder = project_dir / "thumbnails"
    folder.mkdir(parents=True, exist_ok=True)
    hero.save(folder / "thumbnail_1.jpg", quality=92)
    second = hero.copy()
    if words and words[0]:
        try:
            x, y = float(choice["hero_point"][0]) * SIZE[0], float(choice["hero_point"][1]) * SIZE[1]
            sure = bool(choice.get("hero_point_sure"))
        except (KeyError, TypeError, ValueError, IndexError):
            x, y, sure = SIZE[0] / 2, SIZE[1] / 2, False
        _label(second, words[0], right=x < SIZE[0] / 2, target=(x, y) if sure else None)
    second.save(folder / "thumbnail_2.jpg", quality=92)
    try:
        then = candidates[int(choice["then"])][1]
        now = candidates[int(choice["now"])][1]
    except (KeyError, ValueError, TypeError, IndexError):
        log("превью 3  не нашлось пары было/стало")
        return True
    split = Image.new("RGB", SIZE, (255, 255, 255))
    half = (SIZE[0] // 2 - 4, SIZE[1])
    split.paste(_natural(_cover_image(then, half)), (0, 0))
    split.paste(_natural(_cover_image(now, half)), (SIZE[0] // 2 + 4, 0))
    if len(words) > 1 and words[1]:
        _label(split, words[1], right=False, target=None)
    split.save(folder / "thumbnail_3.jpg", quality=92)
    log(f"превью  место нарисовано, было/стало из кадров ролика: {then.name} | {now.name}")
    return True


def _cover_image_bytes(data: bytes, size: tuple[int, int]):
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(data)).convert("RGB")
    scale = max(size[0] / image.width, size[1] / image.height)
    image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
    left, top = (image.width - size[0]) // 2, (image.height - size[1]) // 2
    return image.crop((left, top, left + size[0], top + size[1]))


def _thumbnail_candidates(project_dir: Path, segments: list[dict[str, Any]]) -> list[tuple[str, Path, str]]:
    from yt_video.ff import run_ffmpeg

    real: list[tuple[str, Path, str]] = []
    drawn: list[tuple[str, Path, str]] = []
    frames = project_dir / "thumbnails" / "frames"
    for segment in segments:
        if segment.get("kind") != "scene":
            continue
        stock = segment.get("stock") or {}
        if segment.get("video"):
            frame = frames / f"{segment['id']}.jpg"
            if not frame.exists():
                frames.mkdir(parents=True, exist_ok=True)
                middle = max(0.0, float(segment.get("video_duration") or 2) / 2)
                try:
                    run_ffmpeg(["-ss", f"{middle:.2f}", "-i", str(segment["video"]), "-frames:v", "1", "-q:v", "2", str(frame)])
                except Exception:
                    continue
            real.append(("video frame", frame, segment["narration"]))
        elif stock.get("kind") == "photo" and segment.get("art"):
            real.append(("real photo", Path(segment["art"]), segment["narration"]))
        elif segment.get("art") and Path(segment["art"]).exists():
            drawn.append(("illustration", Path(segment["art"]), segment["narration"]))
    room = max(0, 36 - len(real[:24]))
    step = max(1, len(drawn) // max(1, room)) if drawn else 1
    return [item for item in real[:24] if item[1].exists()] + drawn[::step][:room]


def _cover_image(path: Path, size: tuple[int, int]):
    from PIL import Image

    image = Image.open(path).convert("RGB")
    scale = max(size[0] / image.width, size[1] / image.height)
    image = image.resize((int(image.width * scale) + 1, int(image.height * scale) + 1), Image.LANCZOS)
    left, top = (image.width - size[0]) // 2, (image.height - size[1]) // 2
    return image.crop((left, top, left + size[0], top + size[1]))


def _natural(image):
    """Natural colour, and dark or dusky shots lifted to daylight brightness: thumbnails are never gloomy."""
    from PIL import ImageEnhance, ImageOps, ImageStat

    image = ImageEnhance.Contrast(ImageEnhance.Color(image).enhance(1.08)).enhance(1.06)
    mean = sum(ImageStat.Stat(image.convert("L")).mean) or 128
    if mean < 115:
        image = ImageOps.autocontrast(image, cutoff=1)
        image = ImageEnhance.Brightness(image).enhance(min(1.6, 125 / max(40.0, mean)))
    return image


def _label(image, text: str, right: bool, target: tuple[float, float] | None) -> None:
    """One or two words on a red box at the top, away from the subject; an arrow only when the subject is known."""
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    size = 112
    font = _font(size, 900)
    while draw.textlength(text, font=font) > SIZE[0] * 0.46 and size > 56:
        size -= 6
        font = _font(size, 900)
    pad = 22
    width = draw.textlength(text, font=font)
    x = SIZE[0] - width - pad * 2 - 44 if right else 44
    y = 44
    draw.rectangle((x, y, x + width + pad * 2, y + size + pad * 2), fill=RED_BOX)
    draw.text((x + pad, y + pad - size * 0.1), text, font=font, fill=(255, 255, 255))
    if target is not None:
        start = (x + (width + pad * 2) / 2, y + size + pad * 2 + 12)
        if abs(target[1] - start[1]) > 90 or abs(target[0] - start[0]) > 90:
            _arrow(draw, start, target)


def chapters_block(segments: list[dict[str, Any]], intro: str = "Intro") -> str:
    lines = [f"0:00 {intro}"]
    for segment in segments:
        if segment.get("kind") != "chapter":
            continue
        seconds = int(float(segment.get("slot_start") or 0))
        title = str(segment.get("screen") or "").split(". ", 1)[-1].title()
        lines.append(f"{seconds // 60}:{seconds % 60:02d} {title}")
    return "\n".join(lines)


WHITE = (255, 255, 255)
RED = (255, 64, 48)
YELLOW = ACCENT


COLOURS = {"white": WHITE, "yellow": YELLOW, "red": RED}
SIZES = {"big": 0.2, "small": 0.11}


TITLE_FORMULAS = (
    "numbers contrast: '500,000 Tourists a Year. Now 230 People Live Here.'",
    "money + time or emptiness: 'The $2.4 Billion Casino That Closed in 28 Months'",
    "famous place + twist: 'Japan Built a Fake Disneyland. Then the Real One Arrived.'",
)


def build_publish_prompt(record: dict[str, Any], brief: str, recent: list[str] | None = None) -> str:
    segments = record.get("segments") or []
    chapters = [str(segment.get("screen") or "") for segment in segments if segment.get("kind") == "chapter"]
    numbers = [f"{segment.get('screen')} — {segment.get('note', '')}" for segment in segments if segment.get("kind") == "number"]
    story = " ".join(str(segment.get("narration") or "") for segment in segments)[:6000]
    language = "English" if record.get("language") == "en" else "Russian"
    related = (record.get("related") or {}).get("title", "")
    return (
        "Title rules: 45-65 characters, title case. Name the recognisable place or event (Varosha, Centralia, Dubai), "
        "never 'This Town' or 'This Place'. Prefer present tense and what is at stake today ('Is Sinking', "
        "'Almost Nobody Lives There', 'Still Burning'). Use these formulas:\n- "
        + "\n- ".join(TITLE_FORMULAS)
        + "\nNever use 'Why Did ... So Fast?'. "
        + (f"The channel's last titles were: {' | '.join(recent)}. Do not repeat the formula any of them used. " if recent else "")
        + "Every title stays factually true: never claim something is closed, dead or forever if the video says it was "
        "reused or reopened.\n"
        "You are a YouTube packaging expert. Package this finished video for maximum click-through "
        "without lying about its content.\n"
        + (f"Channel concept:\n{brief.strip()}\n" if brief.strip() else "")
        + f"Working title: {record.get('title', '')}\n"
        f"Chapters: {'; '.join(chapters)}\n"
        f"Key numbers: {'; '.join(numbers[:20])}\n"
        f"Narration excerpt: {story}\n"
        f"Write all text in {language}. Return only JSON:\n"
        '{"title": "the strongest title", '
        '"alt_titles": ["two more titles, each with a different formula"], '
        '"description": "3 short paragraphs: hook, what happened, the open question. No timestamps, no hashtags, no links", '
        + (f'"related_line": "one sentence inviting viewers to watch our related video \\"{related}\\" next", ' if related else "")
        +
        '"hashtags": ["3 hashtags without #"], '
        '"tags": ["15 search tags"], '
        '"pinned_comment": "a short comment from the channel to pin: one striking fact from the video, then a question '
        'viewers will want to answer (would you have stayed, which place next), 2-4 short lines, 1-2 emoji, no links", '
        '"thumbnails": [{"image_prompt": "English, what the picture shows, one bold subject, strong contrast, room for text, no text in the image", '
        '"lines": [{"text": "1-2 WORDS IN CAPS", "colour": "white|yellow|red", "size": "big|small"}]}]}\n'
        "Make exactly 3 thumbnails. The most watched channels in this niche use bright daylight photos where the "
        "absurdity is obvious at a glance (a 20-lane highway with one car, a giant resort with nobody in it), "
        "very few words, sometimes none. So: thumbnail 1 is that bright absurd sight with no text at all (lines: []); "
        "thumbnail 2 is a bright sight with 1-2 words that point at the paradox (shown on a red box with a red arrow); "
        "thumbnail 3 is a then-vs-now contrast with 1-3 words, shown as a split image. Every image_prompt describes a sunny "
        "clear-sky daylight view: bright, saturated, never dark, foggy or gloomy, even for the abandoned 'now' half. "
        "Every thumbnail must promise exactly what the video shows.\n"
        "Never put brand names, trademarks, company or character names in thumbnail text or image_prompt. "
        "In titles a brand may appear only as a plain fact. Never accuse anyone of a crime or fraud "
        "(no 'stolen', 'scam', 'fraud', 'illegal') unless a court established it."
    )


def media_credits(segments: list[dict[str, Any]]) -> tuple[str, str]:
    """Credits for Wikimedia photos (their licences require them, they go into the description) and the list of
    Google Images pictures, which carry no licence and must be replaced before the channel is monetised."""
    credits, google = [], []
    for segment in segments:
        stock = segment.get("stock") or {}
        if stock.get("source") == "wikimedia":
            line = f"{stock.get('author') or 'Wikimedia Commons'}, {stock.get('license', '')}, {stock.get('page_url', '')}"
            if line not in credits:
                credits.append(line)
        elif stock.get("source") == "google":
            google.append(f"{segment['id']}  {stock.get('page_url', '')}")
    block = ("\n\nPhotos: Wikimedia Commons\n" + "\n".join(credits)) if credits else ""
    note = ("GOOGLE IMAGES (no licence: replace before monetisation)\n" + "\n".join(google) + "\n\n") if google else ""
    return block, note


def synthetic_note(project_dir: Path, segments: list[dict[str, Any]]) -> str:
    """Generated realistic pictures of real places must be disclosed when uploading."""
    art = (project_dir / "art").resolve()
    drawn = [
        segment["id"] for segment in segments
        if segment.get("kind") == "scene" and not segment.get("video")
        and segment.get("art") and Path(segment["art"]).resolve().parent == art
    ]
    if not drawn:
        return "ALTERED OR SYNTHETIC CONTENT\nNo: every scene is real footage or photos.\n"
    return (
        "ALTERED OR SYNTHETIC CONTENT\nYes — tick it when uploading. "
        f"{len(drawn)} scenes are generated realistic pictures of real places: {', '.join(drawn[:40])}\n"
    )


def prepare_publish(
    settings: Settings,
    project_dir: Path,
    record: dict[str, Any],
    brief: str,
    client: httpx.Client,
    log: Callable[[str], None],
) -> None:
    """Three titles, description with chapter timestamps and a link to the related video, tags, the end screen pick,
    the synthetic-content flag and the thumbnails."""
    from yt_video.channel import published_videos, recent_titles
    from yt_video.script import ask_json

    recent = recent_titles(settings, project_dir, published_videos(brief, client))
    data = ask_json(settings, build_publish_prompt(record, brief, recent), client, "thumbnails")
    segments = record.get("segments") or []
    intro = "Intro" if record.get("language") == "en" else "Начало"
    hashtags = " ".join(f"#{tag.lstrip('#')}" for tag in data.get("hashtags") or [])
    related = record.get("related") or {}
    watch_next = f"\n\n{str(data.get('related_line') or 'Watch next:').strip()}\n{related['url']}" if related.get("url") else ""
    credits, google = media_credits(segments)
    text = (
        f"TITLE\n{data.get('title', record.get('title', ''))}\n\n"
        "ALTERNATIVE TITLES\n" + "\n".join(str(title) for title in (data.get("alt_titles") or [])[:2]) + "\n\n"
        f"DESCRIPTION\n{str(data.get('description', '')).strip()}{watch_next}\n\n{chapters_block(segments, intro)}{credits}\n\n{hashtags}\n\n"
        "TAGS\n" + ", ".join(str(tag) for tag in data.get("tags") or []) + "\n\n"
        f"PINNED COMMENT\n{str(data.get('pinned_comment', '')).strip()}\n\n"
        + (f"END SCREEN (last 20 seconds)\nVideo: {related.get('title', '')}\n{related.get('url', '')}\nPlus: Subscribe button\n\n" if related else "")
        + google
        + synthetic_note(project_dir, segments)
    )
    (project_dir / "youtube.txt").write_text(text, encoding="utf-8")
    log("публикация  youtube.txt: название, описание, главы, теги")
    try:
        make_host_thumbnail(settings, project_dir, record, client, log)
    except Exception as exc:  # the footage thumbnails below still give the video a cover
        log(f"превью  главное не получилось: {exc}")
    thumbs = (data.get("thumbnails") or [])[:3]
    words = [
        " ".join(str(line.get("text") or "").upper() for line in thumb.get("lines") or [] if isinstance(line, dict)).strip()[:24]
        for thumb in thumbs[1:]
    ]
    try:
        hero_prompt = str((thumbs[0] if thumbs else {}).get("image_prompt") or record.get("title"))
        if footage_thumbnails(settings, project_dir, record, hero_prompt, words, client, log):
            return
    except Exception as exc:  # fall back to generated pictures
        log(f"превью  из кадров ролика не вышло: {exc}")
    for number, thumb in enumerate(thumbs, start=1):
        lines = [
            (str(line.get("text") or "").upper(), COLOURS.get(str(line.get("colour")), WHITE), SIZES.get(str(line.get("size")), 0.2))
            for line in thumb.get("lines") or []
            if isinstance(line, dict) and line.get("text")
        ][:2]
        path = project_dir / "thumbnails" / f"thumbnail_{number}.jpg"
        prompt = str(thumb.get("image_prompt") or record.get("title"))
        try:
            make_bright_thumbnail(settings, prompt, " ".join(line[0] for line in lines), path, client)
        except Exception as exc:  # a thumbnail must never cost the finished video
            log(f"превью {number}  не получилось: {exc}")
            continue
        log(f"превью {number}  {' / '.join(line[0] for line in lines)}")
