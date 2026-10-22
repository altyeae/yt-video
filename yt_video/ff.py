from __future__ import annotations

import shutil
import subprocess

from yt_video.errors import PipelineError


def ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        found = shutil.which("ffmpeg")
        if found:
            return found
    raise PipelineError("нет ffmpeg: выполните pip install imageio-ffmpeg")


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        [ffmpeg_exe(), "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise PipelineError(f"ffmpeg: {result.stderr.strip()[-400:]}")
