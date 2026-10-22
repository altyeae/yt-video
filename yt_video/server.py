from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_video.config import Settings, list_channels, load_settings
from yt_video.errors import PipelineError
from yt_video.pipeline import resume_pipeline, run_pipeline

STAGES = [
    ("script", "Сценарий"),
    ("voice", "Озвучка"),
    ("art", "Иллюстрации"),
    ("boards", "Кадры"),
    ("sound", "Музыка и звуки"),
    ("assemble", "Таймлайн"),
    ("done", "Готово"),
]
PAGE = Path(__file__).resolve().parent / "web" / "index.html"
HOST = "127.0.0.1"
PORT = 8765


class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.status = "idle"
        self.stage = ""
        self.stage_status = {key: "wait" for key, _label in STAGES}
        self.log: list[str] = []
        self.script = ""
        self.error = ""
        self.result = ""
        self.output_url = ""

    def snapshot(self, settings: Settings) -> dict[str, Any]:
        with self._lock:
            return {
                "status": self.status,
                "stage": self.stage,
                "stages": [
                    {"id": key, "label": label, "status": self.stage_status[key]}
                    for key, label in STAGES
                ],
                "log": list(self.log),
                "script": self.script,
                "error": self.error,
                "result": self.result,
                "output_url": self.output_url,
                "keys": settings.key_flags(),
                "channels": list_channels(),
                "channel": settings.channel,
            }

    def reset(self) -> None:
        with self._lock:
            self.status = "running"
            self.stage = "script"
            self.stage_status = {key: "wait" for key, _label in STAGES}
            self.log = []
            self.script = ""
            self.error = ""
            self.result = ""
            self.output_url = ""

    def add_log(self, line: str) -> None:
        with self._lock:
            self.log.append(line)

    def mark(self, stage: str, status: str) -> None:
        with self._lock:
            self.stage = stage
            if stage in self.stage_status:
                self.stage_status[stage] = status

    def set_script(self, text: str) -> None:
        with self._lock:
            self.script = text

    def finish(self, message: str) -> None:
        with self._lock:
            self.status = "done"
            self.result = message
            self.stage = "done"

    def fail(self, message: str) -> None:
        with self._lock:
            self.status = "error"
            self.error = message
            if self.stage in self.stage_status and self.stage_status[self.stage] == "running":
                self.stage_status[self.stage] = "error"


def make_handler(settings: Settings, store: Store) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/":
                self._bytes(200, "text/html; charset=utf-8", PAGE.read_bytes())
                return
            if path == "/api/state":
                self._json(200, store.snapshot(settings))
                return
            self._json(404, {"error": "нет такого адреса"})

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/run":
                self._json(404, {"error": "нет такого адреса"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "не JSON"})
                return
            topic = str(payload.get("topic") or "").strip()
            language = str(payload.get("language") or "ru")
            aspect = str(payload.get("aspect") or "16:9")
            length = str(payload.get("length") or "normal")
            channel = str(payload.get("channel") or settings.channel)
            if channel not in list_channels():
                self._json(400, {"error": "нет такого канала"})
                return
            resume = bool(payload.get("resume"))
            if not topic and not resume:
                self._json(400, {"error": "нужна тема"})
                return
            with store._lock:
                busy = store.status == "running"
            if busy:
                self._json(409, {"error": "сборка уже идёт"})
                return
            store.reset()
            threading.Thread(
                target=_worker,
                args=(settings, store, topic, language, aspect, length, channel, resume),
                daemon=True,
            ).start()
            self._json(202, {"ok": True})

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._bytes(status, "application/json; charset=utf-8", body)

        def _bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def _worker(
    settings: Settings,
    store: Store,
    topic: str,
    language: str,
    aspect: str,
    length: str,
    channel: str,
    resume: bool = False,
) -> None:
    from datetime import datetime

    def log(message: str) -> None:
        store.add_log(f"{datetime.now().strftime('%H:%M:%S')}  {message}")

    try:
        if resume:
            message = resume_pipeline(settings, "", log, store.mark, store.set_script)
            store.finish(message)
            return
        message = run_pipeline(
            settings,
            topic,
            language,
            aspect,
            log,
            store.mark,
            store.set_script,
            length,
            channel,
        )
        store.finish(message)
    except PipelineError as exc:
        log(str(exc))
        store.fail(str(exc))
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        log(message)
        store.fail(message)


def main() -> None:
    settings = load_settings()
    settings.projects_dir.mkdir(parents=True, exist_ok=True)
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    store = Store()
    server = ThreadingHTTPServer((HOST, PORT), make_handler(settings, store))
    url = f"http://{HOST}:{PORT}"
    print(url, flush=True)
    webbrowser.open(url)
    server.serve_forever()
