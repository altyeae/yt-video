from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANNELS = Path(__file__).resolve().parent / "channels"


def list_channels() -> list[str]:
    return sorted(path.stem for path in CHANNELS.glob("*.txt"))


def channel_brief(name: str) -> str:
    path = CHANNELS / f"{name}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    script_provider: str
    gemini_api_key: str
    gemini_text_model: str
    gemini_image_model: str
    anthropic_api_key: str
    anthropic_model: str
    elevenlabs_api_key: str
    elevenlabs_voice_ru: str
    elevenlabs_voice_en: str
    epidemic_api_key: str
    pexels_api_key: str
    art_source: str
    channel: str
    root: Path

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    def voice_id(self, language: str) -> str:
        if language == "en":
            return self.elevenlabs_voice_en
        return self.elevenlabs_voice_ru

    def key_flags(self) -> dict[str, bool]:
        return {
            "gemini": bool(self.gemini_api_key),
            "elevenlabs": bool(self.elevenlabs_api_key),
            "epidemic": bool(self.epidemic_api_key),
            "pexels": bool(self.pexels_api_key),
        }


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    return Settings(
        script_provider=os.environ.get("SCRIPT_PROVIDER", "gemini").strip().lower() or "gemini",
        gemini_api_key=os.environ.get("GEMINI_API_KEY", "").strip(),
        gemini_text_model=os.environ.get("GEMINI_TEXT_MODEL", "gemini-flash-latest").strip() or "gemini-flash-latest",
        gemini_image_model=os.environ.get("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-image").strip() or "gemini-3.1-flash-image",
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5").strip(),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        elevenlabs_voice_ru=os.environ.get("ELEVENLABS_VOICE_RU", "MMEB4YSe9SpycxYSvzhG").strip(),
        elevenlabs_voice_en=os.environ.get("ELEVENLABS_VOICE_EN", "21m00Tcm4TlvDq8ikWAM").strip(),
        epidemic_api_key=os.environ.get("EPIDEMIC_API_KEY", "").strip(),
        pexels_api_key=os.environ.get("PEXELS_API_KEY", "").strip(),
        art_source=os.environ.get("ART_SOURCE", "mixed").strip().lower() or "mixed",
        channel=os.environ.get("CHANNEL", "after_the_crowd").strip() or "after_the_crowd",
        root=ROOT,
    )
