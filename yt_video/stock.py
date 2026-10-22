from __future__ import annotations

import base64
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote

import httpx

from yt_video.config import Settings
from yt_video import usage
from yt_video.errors import PipelineError
from yt_video.websources import google_search, headers_for, wikimedia_search

Log = Callable[[str], None]
PEXELS = "https://api.pexels.com"
MIN_SCORE = 7
# the hook and the key moment get only the strongest real footage of the place itself
STRONG_SCORE = 8.5
STRONG_MARKS = {"HOOK", "KEY_MOMENT"}
VIDEO_BONUS = 0.5
# a photo of the real place from Wikimedia beats generic stock of the same score
PLACE_BONUS = 0.5
PER_KIND = 5
WORKERS = 4


@dataclass
class Candidate:
    kind: str  # "video" or "photo"
    id: int
    preview: str
    file_url: str
    duration: float
    page_url: str
    author: str
    note: str
    source: str = "pexels"
    license: str = ""


def prepare_stock(
    settings: Settings,
    project_dir: Path,
    segments: list[dict[str, Any]],
    aspect: str,
    client: httpx.Client,
    log: Log,
) -> None:
    """Real Pexels footage for the scenes it truly fits; Gemini looks at every candidate before it is used."""
    scenes = [segment for segment in segments if segment.get("kind") == "scene" and _needs_stock(segment)]
    if not scenes:
        return
    if not settings.pexels_api_key:
        log("сток  нет PEXELS_API_KEY, все сцены рисует нейросеть")
        return
    plans = plan_queries(settings, [segment for segment in scenes if "place_query" not in segment], client)
    for segment in scenes:
        if "place_query" in segment:
            continue
        plan = plans.get(segment["id"]) or {}
        segment["stock_query"] = str(plan.get("query") or "").strip()
        segment["place_query"] = str(plan.get("place_query") or "").strip()
        if plan.get("stamp"):
            segment["stamp"] = str(plan["stamp"]).strip()[:40]
    log(f"сток  ищу в Pexels для {len(scenes)} сцен")
    folder = settings.cache_dir / "stock"
    folder.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    lock = threading.Lock()
    found = 0
    done = 0

    limited = threading.Event()
    broke = threading.Event()

    def pick(segment: dict[str, Any], candidates: list[Candidate], need: float) -> tuple[Candidate | None, float, str]:
        """The best unused candidate; a good enough one is reserved so no other scene repeats it."""
        with lock:
            fresh = [item for item in candidates if same_picture(item) not in used]
        choice, score, why = _net(judge, settings, segment, fresh, client)
        if choice is not None and score >= need:
            with lock:
                if same_picture(choice) in used:
                    return None, 0, "уже стоит в другой сцене"
                used.add(same_picture(choice))
        return choice, score, why

    def source(segment: dict[str, Any]) -> None:
        nonlocal found, done
        query = segment.get("stock_query") or ""
        if limited.is_set() or broke.is_set():
            why = "лимит Pexels на этот час исчерпан" if limited.is_set() else "ошибка: баланс Gemini исчерпан"
            segment["stock"] = {"kind": "", "score": 0, "why": why, "query": query}
            return
        try:
            place = segment.get("place_query") or ""
            need = STRONG_SCORE if STRONG_MARKS & set(segment.get("marks") or []) else MIN_SCORE
            # real photos of the place (Wikimedia) and Pexels stock are judged together; Google only if both miss
            candidates = shortlist(
                _net(wikimedia_search, place, client) if place else [],
                _net(search, settings, query, aspect, client) if query else [],
            )
            choice, score, why = pick(segment, candidates, need)
            if (choice is None or score < need) and (place or query):
                try:
                    choice, score, why = pick(segment, _net(google_search, place or query), need)
                except PipelineError as exc:  # a captcha only ends Google for this run, not the scene
                    if "капч" in str(exc):
                        log(f"сток  {exc}")
                    else:
                        raise
            if choice is not None and score >= need:
                path = _net(fetch, choice, folder, client)
                segment["stock"] = {
                    "kind": choice.kind, "id": choice.id, "score": score, "why": why, "query": place or query,
                    "page_url": choice.page_url, "author": choice.author, "path": str(path),
                    "source": choice.source, "license": choice.license,
                }
                if choice.source == "pexels":
                    segment.pop("stamp", None)  # generic stock is not that place: no place-and-year label on it
                if choice.kind == "video":
                    segment["video"] = str(path)
                    segment["video_duration"] = choice.duration
                else:
                    segment["art"] = str(path)
                with lock:
                    found += 1
            else:
                segment["stock"] = {"kind": "", "score": score, "why": why, "query": query}
        except PexelsLimit as exc:
            if not limited.is_set():
                limited.set()
                log(f"сток  {exc}: остальные сцены нарисует нейросеть. Лимит можно бесплатно поднять, написав в Pexels")
            segment["stock"] = {"kind": "", "score": 0, "why": str(exc), "query": query}
        except (PipelineError, httpx.HTTPError, OSError, ValueError) as exc:
            if "402" in str(exc):
                broke.set()
            segment["stock"] = {"kind": "", "score": 0, "why": f"ошибка: {exc}", "query": query}
        with lock:
            done += 1
            if done % 10 == 0 or done == len(scenes):
                log(f"сток  {done}/{len(scenes)}, подошло {found}")

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        list(pool.map(source, scenes))
    if broke.is_set():
        raise PipelineError("закончился баланс Gemini (402): пополните AI Studio и продолжите сборку, готовое не потеряется")
    log(f"сток  реальные кадры для {found} из {len(scenes)} сцен")


MAX_PREVIEWS = 8
PLACE_PREVIEWS = 3


def shortlist(place: list[Candidate], stock: list[Candidate]) -> list[Candidate]:
    """At most MAX_PREVIEWS pictures go to Gemini per scene: up to 3 real photos of the place, then Pexels videos
    and photos taken in turn, since every preview is paid for."""
    videos = [item for item in stock if item.kind == "video"]
    photos = [item for item in stock if item.kind != "video"]
    mixed = [item for pair in zip(videos, photos) for item in pair]
    mixed += videos[len(photos):] + photos[len(videos):]
    chosen = place[:PLACE_PREVIEWS]
    return chosen + mixed[:MAX_PREVIEWS - len(chosen)]


def same_picture(candidate: Candidate) -> str:
    """One key for one picture whatever source found it: Google often returns the very Wikimedia file
    (or its thumbnail) that Wikimedia itself offered."""
    if candidate.kind == "video":
        return f"{candidate.source}:video:{candidate.id}"
    address = candidate.file_url.split("?")[0].rstrip("/")
    if "wikimedia.org" not in address:
        return "photo:" + address.casefold()
    return "photo:wikimedia:" + re.sub(r"^\d+px-", "", unquote(address.rsplit("/", 1)[-1])).casefold()


def _needs_stock(segment: dict[str, Any]) -> bool:
    """Not searched yet, or the search broke (network, Gemini balance, Pexels hourly limit) and is worth a retry."""
    stock = segment.get("stock")
    if not stock:
        return True
    why = str(stock.get("why") or "")
    return not stock.get("kind") and (why.startswith("ошибка") or "лимит" in why.casefold() or "limit" in why.casefold())


def plan_queries(settings: Settings, scenes: list[dict[str, Any]], client: httpx.Client) -> dict[str, dict[str, Any]]:
    from yt_video.script import ask_json

    plans: dict[str, dict[str, Any]] = {}
    for start in range(0, len(scenes), 40):
        batch = scenes[start:start + 40]
        listing = "\n".join(
            json.dumps({"id": segment["id"], "narration": segment["narration"], "picture": segment.get("image_prompt", "")}, ensure_ascii=False)
            for segment in batch
        )
        prompt = (
            "For each documentary shot below, write a search query for free stock footage on Pexels.\n"
            "query: 2-5 plain English words naming what is visibly on screen (objects, place type, weather, action), "
            "the way stock clips are tagged. No proper names, brands, dates or abstract ideas, because stock libraries "
            "do not have them: 'abandoned shopping mall interior', not 'Forest City mall 2019'.\n"
            "place_query: 2-6 words with the proper name of the place and what is shown, the way you would search an "
            "encyclopedia or Google Images: 'Tianducheng Eiffel Tower replica', 'Varosha beach hotels'. Empty string "
            "when the line is not about a specific real place.\n"
            "stamp: a short on-screen place and year label like 'JOHOR, MALAYSIA · 2016' only when the narration "
            "clearly names the place or the year of what is shown, otherwise an empty string.\n"
            'Return only JSON: {"shots": [{"id": "...", "query": "...", "place_query": "", "stamp": ""}]}\n'
            + listing
        )
        data = ask_json(settings, prompt, client, "shots")
        for shot in data.get("shots") or []:
            if isinstance(shot, dict) and shot.get("id"):
                plans[str(shot["id"])] = shot
    return plans


def search(settings: Settings, query: str, aspect: str, client: httpx.Client) -> list[Candidate]:
    orientation = "portrait" if aspect == "9:16" else "landscape"
    headers = {"Authorization": settings.pexels_api_key}
    params = {"query": query, "per_page": PER_KIND, "orientation": orientation}
    candidates: list[Candidate] = []
    videos = _get(client, f"{PEXELS}/videos/search", params, headers).get("videos") or []
    for video in videos:
        files = [item for item in video.get("video_files") or [] if item.get("link") and (item.get("width") or 0) >= 1280]
        if not files or float(video.get("duration") or 0) < 3:
            continue
        exact = [item for item in files if item.get("width") == (1080 if orientation == "portrait" else 1920)]
        best = (exact or sorted(files, key=lambda item: item.get("width") or 0))[0]
        candidates.append(Candidate(
            "video", int(video["id"]), str(video.get("image") or ""), str(best["link"]), float(video.get("duration") or 0),
            str(video.get("url") or ""), str((video.get("user") or {}).get("name") or ""), "",
        ))
    photos = _get(client, f"{PEXELS}/v1/search", params, headers).get("photos") or []
    for photo in photos:
        src = photo.get("src") or {}
        if not src.get("original"):
            continue
        candidates.append(Candidate(
            "photo", int(photo["id"]), str(src.get("medium") or src.get("small") or ""),
            f"{src['original']}?auto=compress&cs=tinysrgb&w=2600", 0.0, str(photo.get("url") or ""),
            str(photo.get("photographer") or ""), str(photo.get("alt") or ""),
        ))
    return [candidate for candidate in candidates if candidate.preview]


def judge(
    settings: Settings,
    segment: dict[str, Any],
    candidates: list[Candidate],
    client: httpx.Client,
) -> tuple[Candidate | None, float, str]:
    """Gemini sees every candidate next to the narration and scores how well it shows that exact line."""
    if not candidates:
        return None, 0, "в Pexels ничего не нашлось"
    parts: list[dict[str, Any]] = [{"text": (
        "You pick footage for one shot of a documentary. The viewer must understand the line from the picture alone.\n"
        f"Narration: {segment['narration']}\n"
        f"Intended picture: {segment.get('image_prompt', '')}\n"
        "Score every candidate 0-10 for how literally and believably it shows this line: 9-10 shows exactly it, "
        "7-8 clearly the same subject and mood, 4-6 related but generic, 0-3 wrong subject, wrong era or region, "
        "visible modern logos or text, staged stock look, or anything that would confuse the viewer.\n"
        "Generic stock that is not this specific place (people walking in a city, money, hands, laptops, handshakes, "
        "traffic) scores at most 5, unless the line itself is about exactly that generic thing.\n"
        "Collages, side-by-side comparisons, screenshots, watermarks or captions burned into the picture score at most 4.\n"
        "Anything that contradicts the line caps the score at 3: people or traffic where it says empty or abandoned, "
        "daylight where it says night, a city where it says desert, new and clean where it says ruined.\n"
        'Return only JSON: {"scores": [{"i": 0, "score": 0, "why": "few words"}]}'
    )}]
    shown: list[Candidate] = []
    for candidate in candidates:
        try:
            response = client.get(candidate.preview, headers=headers_for(candidate), timeout=30)
        except httpx.HTTPError:
            continue
        if response.status_code != 200 or len(response.content) < 1000:
            continue
        parts.append({"text": f"Candidate {len(shown)} ({candidate.kind}{', ' + candidate.note if candidate.note else ''}):"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(response.content).decode("ascii")}})
        shown.append(candidate)
    if not shown:
        return None, 0, "превью не загрузились"
    data = _vision_json(settings, parts, client)
    best: tuple[float, Candidate | None, float, str] = (-1, None, 0, "")
    for item in data.get("scores") or []:
        try:
            index = int(item.get("i"))
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(shown):
            continue
        rank = score + (VIDEO_BONUS if shown[index].kind == "video" else 0) + (PLACE_BONUS if shown[index].source == "wikimedia" else 0)
        if rank > best[0]:
            best = (rank, shown[index], score, str(item.get("why") or ""))
    return best[1], best[2], best[3] or "нет оценки"


def fetch(candidate: Candidate, folder: Path, client: httpx.Client) -> Path:
    suffix = ".mp4" if candidate.kind == "video" else ".jpg"
    prefix = "" if candidate.source == "pexels" else f"{candidate.source}_"
    path = folder / f"{prefix}{candidate.kind}_{candidate.id}{suffix}"
    if path.exists() and path.stat().st_size > 10_000:
        return path
    response = client.get(candidate.file_url, headers=headers_for(candidate), timeout=300)
    if response.status_code != 200 or len(response.content) < 10_000:
        raise PipelineError(f"{candidate.source} не отдал файл {candidate.id}: {response.status_code}")
    if candidate.kind == "photo":
        from io import BytesIO

        from PIL import Image

        try:
            Image.open(BytesIO(response.content)).convert("RGB").save(path, quality=93)
        except OSError as exc:
            raise PipelineError(f"{candidate.source}: файл {candidate.id} не картинка") from exc
        return path
    path.write_bytes(response.content)
    return path


def write_report(project_dir: Path, segments: list[dict[str, Any]]) -> Path:
    """visuals.txt: what is on screen for every line and why, to check the picture against the text."""
    lines = []
    for segment in segments:
        start = float(segment.get("slot_start") or 0)
        stamp = f"{int(start) // 60}:{int(start) % 60:02d}"
        kind = segment.get("kind")
        if kind == "scene":
            stock = segment.get("stock") or {}
            if stock.get("kind"):
                source = f"{str(stock.get('source') or 'pexels').upper()} {stock['kind']} {stock.get('score')}/10 «{stock.get('query', '')}» — {stock.get('why', '')}  {stock.get('page_url', '')}"
            elif segment.get("art"):
                reason = f" (сток {stock.get('score', 0)}/10 «{stock.get('query', '')}»: {stock.get('why', '')})" if stock else ""
                source = f"нейросеть{reason}"
            else:
                source = "текстовая доска: картинки нет"
        else:
            source = f"{kind}: {segment.get('screen', '')}"
        lines.append(f"{stamp}  {segment['id']}  {source}\n        {segment['narration']}")
    path = project_dir / "visuals.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


NETWORK_PAUSES = (15, 30, 45)


def _net(call, *args):
    """Run a network step, riding out a short outage (VPN reconnect, DNS hiccup) instead of giving up."""
    for pause in (*NETWORK_PAUSES, None):
        try:
            return call(*args)
        except (httpx.TransportError, OSError):
            if pause is None:
                raise
            time.sleep(pause)


class PexelsLimit(PipelineError):
    """The hourly Pexels quota is spent; asking again only wastes time."""


def _get(client: httpx.Client, url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = client.get(url, params=params, headers=headers, timeout=30)
    if response.status_code == 429 or response.headers.get("X-Ratelimit-Remaining") == "0":
        raise PexelsLimit("Pexels: исчерпан лимит запросов в час")
    if response.status_code != 200:
        raise PipelineError(f"Pexels ответил {response.status_code}")
    return response.json()


def _vision_json(settings: Settings, parts: list[dict[str, Any]], client: httpx.Client) -> dict[str, Any]:
    from yt_video.script import extract_json

    # scoring pictures needs no reasoning: thinking tokens are billed as output and were the main cost here
    config: dict[str, Any] = {"responseMimeType": "application/json", "temperature": 0.2, "thinkingConfig": {"thinkingBudget": 0}}
    for attempt in range(3):
        if attempt:
            time.sleep(3 * attempt)
        response = client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_text_model}:generateContent",
            headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"},
            json={"contents": [{"parts": parts}], "generationConfig": config},
        )
        if response.status_code == 400 and "thinking" in response.text.casefold() and "thinkingConfig" in config:
            config = {key: value for key, value in config.items() if key != "thinkingConfig"}
            continue
        if response.status_code != 200:
            if response.status_code in {429, 500, 502, 503, 504}:
                continue
            raise PipelineError(f"Gemini ответил {response.status_code}")
        usage.record("text", response.json())
        text = "".join(
            str(part.get("text") or "")
            for candidate in response.json().get("candidates") or []
            for part in (candidate.get("content") or {}).get("parts") or []
            if not part.get("thought")
        )
        try:
            return extract_json(text)
        except PipelineError:
            continue
    return {}
