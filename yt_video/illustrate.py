from __future__ import annotations

import base64
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

import httpx

from yt_video.config import Settings
from yt_video import usage
from yt_video.errors import PipelineError
from yt_video.stock import prepare_stock

Log = Callable[[str], None]

STYLE = (
    "Cinematic hyper-detailed realistic digital art for a documentary film. "
    "Photorealistic materials and textures, physically accurate volumetric lighting, "
    "shallow depth of field, rich contrast, slightly desaturated filmic colour grade with warm highlights, "
    "dramatic composition with one clear subject filling the frame, a detailed believable environment. "
    "Absolutely no text, letters, numbers, labels, logos or watermarks anywhere in the image."
)
ICON_STYLE = (
    "A single glowing golden line-art icon, thick neon-gold strokes with soft glow, "
    "centered, on a pure black background, flat, minimal detail, no text, no letters."
)
RETRIES = 4
WORKERS = 4
STYLE_REFERENCE = "Match the rendering style, lighting and colour grade of the attached image. Do not copy its content."


def prepare_illustrations(
    settings: Settings,
    project_dir: Path,
    segments: list[dict[str, Any]],
    aspect: str,
    client: httpx.Client,
    log: Log,
) -> None:
    from yt_video.maps import prepare_maps

    prepare_maps(settings.cache_dir, project_dir, segments, client, log)
    wanted = [segment for segment in segments if segment.get("kind") == "scene" or segment.get("icons")]
    if not wanted:
        log("иллюстрации  в сценарии нет сцен")
        return
    if not settings.gemini_api_key:
        log("иллюстрации  нет GEMINI_API_KEY, сцены будут текстовыми досками")
        return
    if settings.art_source in {"mixed", "stock"}:
        prepare_stock(settings, project_dir, segments, aspect, client, log)
    previous_art = ""
    for segment in segments:
        fallback = segment.get("fallback_art")
        if (
            segment.get("kind") == "scene" and not segment.get("art") and not segment.get("video")
            and fallback and Path(fallback).exists() and fallback != previous_art
        ):
            segment["art"] = fallback
        previous_art = str(segment.get("art") or segment.get("video") or "")
    folder = project_dir / "art"
    folder.mkdir(parents=True, exist_ok=True)
    icon_cache = settings.cache_dir / "icons"
    icon_cache.mkdir(parents=True, exist_ok=True)

    scenes: list[tuple[str, str]] = []
    owner: dict[str, str] = {}
    last_prompt = ""
    for segment in segments:
        prompt = str(segment.get("image_prompt") or "")
        if prompt.strip().casefold() == "same":
            prompt = f"A different shot of the same place as: {last_prompt}. Now show: {segment['narration']}"
        if segment.get("kind") == "scene":
            last_prompt = prompt
        reused = bool(segment.get("fallback_art")) and segment.get("art") == segment.get("fallback_art")
        if segment.get("kind") != "scene" or (segment.get("stock") or {}).get("kind") or reused:
            continue
        scenes.append((segment["id"], prompt))
        owner[segment["id"]] = segment["id"]
        if segment.get("compare_prompt"):
            scenes.append((f"{segment['id']}_b", str(segment["compare_prompt"])))
            owner[f"{segment['id']}_b"] = f"{segment['id']}_b"
    subjects = sorted({str(subject) for segment in segments for subject in segment.get("icons") or []})
    icons = {subject: icon_cache / f"{_slug(subject)}.png" for subject in subjects}
    missing = [subject for subject, path in icons.items() if not path.exists()]
    if settings.art_source == "stock":
        if scenes or missing:
            log(f"иллюстрации  режим stock: {len(scenes)} сцен без реального кадра станут досками, нейросеть не рисует")
        scenes, missing = [], []
    log(f"иллюстрации  {len(scenes)} сцен, {len(missing)} новых иконок, {len(subjects) - len(missing)} из кэша")

    drawn: dict[str, Path] = {}
    style_ref: bytes | None = None
    done = 0
    for shot, _prompt in scenes:
        path = folder / f"{shot}.png"
        if path.exists():
            drawn[shot] = path
            style_ref = style_ref or path.read_bytes()
    if drawn:
        log(f"иллюстрации  {len(drawn)} сцен уже нарисованы, дорисовываю остальные")
        scenes = [job for job in scenes if job[0] not in drawn]

    broke = threading.Event()

    def draw_scene(job: tuple[str, str]) -> tuple[str, Path | None, str]:
        shot, prompt = job
        if broke.is_set():
            return shot, None, "баланс Gemini исчерпан"
        try:
            data = generate_image(settings, f"{prompt}\n\n{STYLE}", aspect, client, style_ref)
        except PipelineError as exc:
            if _out_of_credit(exc):
                broke.set()
            if not _refused(exc):
                return shot, None, str(exc)
            prompt = neutral_prompt(settings, prompt, client)
            try:
                data = generate_image(settings, f"{prompt}\n\n{STYLE}", aspect, client, style_ref)
            except PipelineError as again:
                return shot, None, f"{exc}; без брендов тоже отказ: {again}"
            log(f"{shot}  перерисовано без брендов: {prompt[:70]}")
        path = folder / f"{shot}.png"
        path.write_bytes(data)
        return shot, path, prompt

    def draw_icon(subject: str) -> tuple[str, Path | None, str]:
        if broke.is_set():
            return subject, None, "баланс Gemini исчерпан"
        try:
            data = generate_image(settings, f"Icon of: {subject}.\n\n{ICON_STYLE}", "1:1", client, None)
        except PipelineError as exc:
            return subject, None, str(exc)
        icons[subject].write_bytes(data)
        return subject, icons[subject], subject

    if scenes:
        shot, path, note = draw_scene(scenes[0])
        if path is not None:
            drawn[shot] = path
            style_ref = path.read_bytes()
        else:
            log(f"{shot}  сцена не нарисовалась: {note}")
        done = 1
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(draw_scene, job) for job in scenes[1:]]
        futures += [pool.submit(draw_icon, subject) for subject in missing]
        total = len(futures) + done
        for future in as_completed(futures):
            key, path, note = future.result()
            done += 1
            if path is None:
                log(f"{key}  не нарисовалось: {note}")
            elif key in owner:
                drawn[key] = path
            if done % 10 == 0 or done == total:
                log(f"иллюстрации  {done}/{total}")
    if broke.is_set():
        raise PipelineError(
            "закончился баланс Gemini (402). Пополните AI Studio и нажмите «Продолжить»: сценарий, озвучка, "
            f"сток и уже нарисованные {len(drawn)} сцен сохранены и не оплачиваются заново"
        )

    for segment in segments:
        source = owner.get(segment["id"])
        if source and source in drawn and not segment.get("art"):
            segment["art"] = str(drawn[source])
        if f"{segment['id']}_b" in drawn and segment.get("art"):
            segment["art_b"] = str(drawn[f"{segment['id']}_b"])
        if segment.get("icons"):
            segment["icon_art"] = [
                str(icons[str(subject)]) if icons[str(subject)].exists() else None for subject in segment["icons"]
            ]
    log(f"иллюстрации  нарисовано {len(drawn)} сцен")


def _out_of_credit(exc: Exception) -> bool:
    return "402" in str(exc) or "credits are depleted" in str(exc)


def _refused(exc: Exception) -> bool:
    text = str(exc)
    return any(word in text for word in ("PROHIBITED_CONTENT", "SAFETY", "IMAGE_SAFETY", "BLOCKLIST", "OTHER"))


def neutral_prompt(settings: Settings, prompt: str, client: httpx.Client) -> str:
    """Gemini refuses brands, trademarks and famous characters; describe the same scene without them."""
    from yt_video.script import extract_json

    request = (
        "This image description was refused by an image model, most likely because it names a brand, trademark, "
        "company, famous character or real person. Rewrite it so it shows the same scene, era, mood and composition "
        "with only generic descriptions: 'a large theme park with a fairy-tale castle at the entrance', not the brand.\n"
        f"Description: {prompt}\n"
        'Return only JSON: {"prompt": "..."}'
    )
    response = client.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_text_model}:generateContent",
        headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"},
        json={"contents": [{"parts": [{"text": request}]}], "generationConfig": {"responseMimeType": "application/json"}},
    )
    if response.status_code != 200:
        raise PipelineError(f"Gemini ответил {response.status_code}")
    usage.record("text", response.json())
    text = "".join(
        str(part.get("text") or "")
        for candidate in response.json().get("candidates") or []
        for part in (candidate.get("content") or {}).get("parts") or []
        if not part.get("thought")
    )
    rewritten = str(extract_json(text).get("prompt") or "").strip()
    if not rewritten:
        raise PipelineError("Gemini не переписал описание")
    return rewritten


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")[:60] or "icon"


def generate_image(
    settings: Settings,
    prompt: str,
    aspect: str,
    client: httpx.Client,
    style_ref: bytes | None = None,
    reference_note: str = STYLE_REFERENCE,
) -> bytes:
    """style_ref is an attached image; reference_note says how to use it (style only, or the same person)."""
    parts: list[dict[str, Any]] = [{"text": prompt}]
    if style_ref:
        parts[0]["text"] = f"{prompt}\n{reference_note}"
        parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(style_ref).decode("ascii")}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": aspect},
        },
    }
    error = "нет ответа"
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(4 * attempt)
        try:
            response = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_image_model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"},
                json=body,
            )
        except httpx.HTTPError as exc:
            error = str(exc)
            continue
        if response.status_code == 200:
            try:
                usage.record("image", response.json())
                return image_bytes(response.json())
            except PipelineError as exc:
                error = str(exc)
                continue
        error = f"Gemini ответил {response.status_code}: {response.text[:300]}"
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
    raise PipelineError(error)


def image_bytes(payload: dict[str, Any]) -> bytes:
    for candidate in payload.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            if inline.get("data"):
                return base64.b64decode(inline["data"])
    reason = ""
    for candidate in payload.get("candidates") or []:
        reason = str(candidate.get("finishReason") or "")
    raise PipelineError(f"Gemini не вернул картинку {reason}".strip())
