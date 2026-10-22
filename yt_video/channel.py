from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any

import httpx

from yt_video.config import Settings

BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def channel_handle(brief: str) -> str:
    found = re.search(r"YouTube:\s*(@[\w.-]+)", brief)
    return found.group(1) if found else ""


def published_videos(brief: str, client: httpx.Client) -> list[dict[str, str]]:
    """The channel's latest uploads (title and link) from its public RSS feed; empty if YouTube is unreachable."""
    handle = channel_handle(brief)
    if not handle:
        return []
    try:
        page = client.get(f"https://www.youtube.com/{handle}/videos", headers={"User-Agent": BROWSER, "Accept-Language": "en"},
                          cookies={"SOCS": "CAI"}, timeout=30)
        found = re.search(r'<meta itemprop="identifier" content="(UC[\w-]{22})"', page.text) or re.search(
            r'"browseId":"(UC[\w-]{22})"', page.text)
        if not found:
            return []
        feed = client.get(f"https://www.youtube.com/feeds/videos.xml?channel_id={found.group(1)}", timeout=30)
    except httpx.HTTPError:
        return []
    return [
        {"title": unescape(title), "url": f"https://www.youtube.com/watch?v={video}"}
        for video, title in re.findall(r"<yt:videoId>([\w-]{11})</yt:videoId>.*?<title>(.*?)</title>", feed.text, re.S)
    ]


def pick_related(settings: Settings, topic: str, videos: list[dict[str, str]], client: httpx.Client) -> dict[str, str]:
    """The already published video closest to this topic: it goes on the end screen and into the description."""
    from yt_video.script import ask_json

    if not videos:
        return {}
    listing = "\n".join(f"{index}. {video['title']}" for index, video in enumerate(videos))
    data = ask_json(settings, (
        f"New documentary: {topic}\n"
        f"Videos already on the channel:\n{listing}\n"
        "Which one would a viewer of the new documentary most want to watch next (same kind of place, same country, "
        'same kind of failure)? Return only JSON with its exact title: {"title": "..."}'
    ), client, "title")
    wanted = str(data.get("title") or "").casefold().strip(" .?!\"'")
    return next((video for video in videos if video["title"].casefold().strip(" .?!\"'") == wanted), videos[0])


def next_angle(settings: Settings, angles: tuple[str, ...]) -> str:
    """A different way of telling the story than the last video used."""
    for folder in sorted(settings.projects_dir.glob("2*"), reverse=True):
        path = folder / "project.json"
        if not path.exists():
            continue
        last = str(json.loads(path.read_text(encoding="utf-8")).get("angle") or "")
        if last in angles:
            return angles[(angles.index(last) + 1) % len(angles)]
    return angles[0]


def recent_titles(settings: Settings, project_dir: Path, videos: list[dict[str, str]], count: int = 3) -> list[str]:
    if videos:
        return [video["title"] for video in videos[:count]]
    titles: list[str] = []
    for folder in sorted(settings.projects_dir.glob("2*"), reverse=True):
        path = folder / "youtube.txt"
        if folder != project_dir and path.exists() and len(titles) < count:
            lines = path.read_text(encoding="utf-8").splitlines()
            titles += lines[1:2]
    return titles


def related_line(record: dict[str, Any]) -> str:
    related = record.get("related") or {}
    return f"{related.get('title', '')}\n{related.get('url', '')}".strip() if related else ""
