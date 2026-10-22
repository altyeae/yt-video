from __future__ import annotations

import json
import re
import threading
import zlib
from html import unescape
from typing import TYPE_CHECKING, Any

import httpx

from yt_video.errors import PipelineError

if TYPE_CHECKING:
    from yt_video.stock import Candidate

AGENT = "AfterTheCrowdsVideoMaker/1.0 (https://www.youtube.com/@afterthecrowds) python-httpx"
BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
COMMONS = "https://commons.wikimedia.org/w/api.php"
FREE_LICENSES = ("cc0", "public domain", "pd", "cc by", "cc-by", "attribution")
MIN_WIDTH = 1000
PER_SOURCE = 6


def _plain(html: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html or "")).split())[:80]


def wikimedia_search(query: str, client: httpx.Client) -> list[Candidate]:
    """Freely licensed photos of the real place from Wikimedia Commons, credits included. Commons matches file
    titles, so a long query that finds nothing is retried with just its first words (usually the place name)."""
    found = _wikimedia(query, client)
    words = query.split()
    return found or (_wikimedia(" ".join(words[:2]), client) if len(words) > 2 else [])


def _wikimedia(query: str, client: httpx.Client) -> list[Candidate]:
    from yt_video.stock import Candidate

    params = {
        "action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": 12, "prop": "imageinfo", "iiprop": "url|size|extmetadata", "iiurlwidth": 1920,
    }
    response = client.get(COMMONS, params=params, headers={"User-Agent": AGENT}, timeout=30)
    if response.status_code != 200:
        raise PipelineError(f"Wikimedia ответил {response.status_code}")
    pages = (response.json().get("query") or {}).get("pages") or {}
    found: list[Candidate] = []
    for page in sorted(pages.values(), key=lambda item: item.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        license_name = str((meta.get("LicenseShortName") or {}).get("value") or "")
        if not any(word in license_name.casefold() for word in FREE_LICENSES) or (info.get("width") or 0) < MIN_WIDTH:
            continue
        thumb = str(info.get("thumburl") or info.get("url") or "")
        preview = re.sub(r"/\d+px-", "/500px-", thumb)  # Wikimedia serves only standard widths if "px-" in thumb else thumb
        found.append(Candidate(
            "photo", int(page["pageid"]), preview, thumb, 0.0, str(info.get("descriptionurl") or ""),
            _plain(str((meta.get("Artist") or {}).get("value") or "")), str(page.get("title") or "")[5:80],
            source="wikimedia", license=license_name,
        ))
        if len(found) >= PER_SOURCE:
            break
    return found


GOOGLE = "https://www.google.com/search"
_google_lock = threading.Lock()
_google_blocked = threading.Event()
PAIR = re.compile(r'\["(https://encrypted-tbn0\.gstatic\.com/images\?[^"]+)",\d+,\d+\],\["(https?://[^"]+)",(\d+),(\d+)\]')
# social networks block downloads; photo banks put watermarks on every picture
SKIP_HOSTS = (
    "lookaside.", "instagram.", "facebook.", "fbsbx.", "tiktok.", "pinimg.", "alamy.", "shutterstock.", "gettyimages",
    "istockphoto.", "dreamstime.", "depositphotos.", "123rf.", "stock.adobe.", "agefotostock.",
)


def google_search(query: str) -> list[Candidate]:
    """Google Images through a headless browser, one search at a time; the last resort before a generated picture.
    If Google shows its 'unusual traffic' page the rest of the run skips Google instead of hammering it."""
    if _google_blocked.is_set():
        return []
    with _google_lock:
        html = _google_page(query)
    if html is None:
        _google_blocked.set()
        raise PipelineError("Google показал капчу: до конца сборки картинки из Google не ищутся")
    return google_candidates(html)


def _google_page(query: str) -> str | None:
    from urllib.parse import urlencode

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(locale="en-US", user_agent=BROWSER, viewport={"width": 1366, "height": 900})
            context.add_cookies([{"name": "SOCS", "value": "CAI", "domain": ".google.com", "path": "/"}])
            page = context.new_page()
            page.goto(f"{GOOGLE}?{urlencode({'q': query, 'udm': 2, 'hl': 'en'})}", wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(1800)
            return None if "/sorry/" in page.url else page.content()
        finally:
            browser.close()


def google_candidates(html: str) -> list[Candidate]:
    from yt_video.stock import Candidate

    found: list[Candidate] = []
    seen: set[str] = set()
    for thumb, raw, height, width in PAIR.findall(html):
        url = json.loads(f'"{raw}"')
        if url in seen or int(width) < MIN_WIDTH or any(host in url.casefold() for host in SKIP_HOSTS):
            continue
        seen.add(url)
        found.append(Candidate(
            "photo", zlib.crc32(url.encode()), json.loads(f'"{thumb}"'), url, 0.0, url, url.split("/")[2], "",
            source="google",
        ))
        if len(found) >= PER_SOURCE:
            break
    return found


def headers_for(candidate: Any) -> dict[str, str]:
    """Wikimedia refuses browser-like agents without contact details, other sites refuse bots."""
    wiki = candidate.source == "wikimedia" or "wikimedia.org" in candidate.file_url
    return {"User-Agent": AGENT if wiki else BROWSER}
