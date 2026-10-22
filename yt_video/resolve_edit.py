from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from xml.sax.saxutils import escape

from yt_video.errors import PipelineError

Log = Callable[[str], None]
FPS = 30
DISSOLVE_FRAMES = 15
SCRIPT_API = r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
SCRIPT_LIB = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"


def canvas_size(aspect: str) -> tuple[int, int]:
    if aspect == "9:16":
        return 1080, 1920
    return 1920, 1080


def frames(seconds: float, fps: int = FPS) -> int:
    return max(0, int(round(float(seconds) * fps)))


def timecode_frames(timecode: str, fps: int = FPS) -> int:
    parts = str(timecode or "").split(":")
    if len(parts) != 4:
        return frames(3600, fps)
    hours, minutes, seconds, frame = (int(part) for part in parts)
    return ((hours * 3600 + minutes * 60 + seconds) * fps) + frame


def source_window(source_frames: int, source_fps: float, slot_seconds: float) -> tuple[int, int]:
    fps = float(source_fps) if source_fps and source_fps > 0 else float(FPS)
    count = int(source_frames) if source_frames and source_frames > 0 else max(1, int(round(slot_seconds * fps)))
    take = min(count, max(1, int(round(float(slot_seconds) * fps))))
    source_in = max(0, (count - take) // 2)
    source_out = min(count - 1, source_in + take - 1)
    return source_in, max(source_in, source_out)


def file_url(path: Path) -> str:
    return "file://localhost/" + quote(path.resolve().as_posix(), safe="/:")


def build_xmeml(
    segments: list[dict[str, Any]],
    audio_path: Path,
    aspect: str,
    audio_duration: float,
    log: Log | None = None,
    beds: list[Path] | tuple[Path, ...] = (),
) -> str:
    width, height = canvas_size(aspect)
    video_items: list[str] = []
    timeline_end = frames(audio_duration)
    for segment in segments:
        asset = segment.get("asset_path")
        if not asset:
            if log:
                log(f"{segment['id']}  нет файла, на таймлайне пропуск")
            continue
        start = frames(segment["slot_start"])
        slot = max(1, frames(segment["slot_end"]) - start)
        source_seconds = float(segment.get("asset_duration") or 0)
        if segment.get("asset_kind") == "video" and source_seconds > 0:
            available = max(1, frames(source_seconds))
            take = min(slot, available)
            source_in = (available - take) // 2
        else:
            take = slot
            source_in = 0
        end = start + take
        timeline_end = max(timeline_end, end)
        video_items.append(
            _clip_item(
                clip_id=f"clip-{segment['id']}",
                file_id=f"file-{segment['id']}",
                name=Path(asset).name,
                path=Path(asset),
                start=start,
                end=end,
                source_in=source_in,
                source_out=source_in + take,
                width=width,
                height=height,
                audio=False,
            )
        )
        if log:
            log(f"{segment['id']}  {start / FPS:.1f}–{end / FPS:.1f} с  {Path(asset).name}")

    audio_end = max(1, frames(audio_duration))
    timeline_end = max(timeline_end, audio_end)
    audio_item = _clip_item(
        clip_id="clip-voice",
        file_id="file-voice",
        name=audio_path.name,
        path=audio_path,
        start=0,
        end=audio_end,
        source_in=0,
        source_out=audio_end,
        width=width,
        height=height,
        audio=True,
    )
    bed_tracks = "".join(
        "        <track>\n"
        + _clip_item(
            clip_id=f"clip-bed-{index}",
            file_id=f"file-bed-{index}",
            name=bed.name,
            path=bed,
            start=0,
            end=audio_end,
            source_in=0,
            source_out=audio_end,
            width=width,
            height=height,
            audio=True,
        )
        + "        </track>\n"
        for index, bed in enumerate(beds)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE xmeml>\n"
        '<xmeml version="4">\n'
        "  <sequence>\n"
        f"    <name>{escape(audio_path.parent.parent.name)}</name>\n"
        f"    <duration>{timeline_end}</duration>\n"
        f"{_rate(2)}"
        "    <media>\n"
        "      <video>\n"
        "        <format><samplecharacteristics>\n"
        f"          <width>{width}</width><height>{height}</height>\n"
        "          <pixelaspectratio>square</pixelaspectratio>\n"
        f"{_rate(10)}"
        "        </samplecharacteristics></format>\n"
        "        <track>\n"
        f"{''.join(video_items)}"
        "        </track>\n"
        "      </video>\n"
        "      <audio>\n"
        "        <track>\n"
        f"{audio_item}"
        "        </track>\n"
        f"{bed_tracks}"
        "      </audio>\n"
        "    </media>\n"
        "  </sequence>\n"
        "</xmeml>\n"
    )


def write_timeline(
    segments: list[dict[str, Any]],
    audio_path: Path,
    aspect: str,
    audio_duration: float,
    xml_path: Path,
    log: Log,
    beds: list[Path] | tuple[Path, ...] = (),
) -> Path:
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(
        build_xmeml(segments, audio_path, aspect, audio_duration, log, beds),
        encoding="utf-8",
    )
    return xml_path


def open_in_resolve(
    project_name: str,
    aspect: str,
    segments: list[dict[str, Any]],
    audio_path: Path,
    audio_duration: float,
    log: Log,
    beds: list[Path] | tuple[Path, ...] = (),
) -> str:
    audio_path = audio_path.resolve()
    beds = [Path(bed).resolve() for bed in beds]
    resolve = connect_resolve()
    width, height = canvas_size(aspect)
    project_manager = resolve.GetProjectManager()
    project = project_manager.CreateProject(project_name)
    if not project:
        raise PipelineError(
            f"Resolve не создал проект {project_name}. Закройте окна в DaVinci и повторите."
        )
    project.SetSetting("timelineResolutionWidth", str(width))
    project.SetSetting("timelineResolutionHeight", str(height))
    project.SetSetting("timelineFrameRate", str(FPS))
    project.SetSetting("timelinePlaybackFrameRate", str(FPS))
    pool = project.GetMediaPool()
    paths = [
        segment["asset_path"]
        for segment in segments
        if segment.get("asset_path") and segment.get("asset_kind") != "sequence"
    ]
    paths.append(str(audio_path))
    paths.extend(str(bed) for bed in beds)
    imported = pool.ImportMedia(paths) or []
    by_name = {item.GetName(): item for item in imported}
    for item in pool.GetRootFolder().GetClipList() or []:
        by_name.setdefault(item.GetName(), item)
    timeline = pool.CreateEmptyTimeline(project_name)
    if not timeline:
        raise PipelineError("Resolve не создал таймлайн.")
    project.SetCurrentTimeline(timeline)
    resolve.OpenPage("edit")
    _import_sequences(pool, segments, by_name, log)
    for segment in segments:
        path = segment.get("asset_path")
        if not path or segment.get("asset_kind") == "sequence":
            continue
        clip = by_name.get(Path(path).name)
        if clip is not None:
            by_name[segment["id"]] = clip
    origin = timecode_frames(timeline.GetStartTimecode() or "01:00:00:00")

    audio_item = by_name.get(audio_path.name)
    if not audio_item:
        raise PipelineError("Resolve не принял звуковую дорожку.")
    audio_end = max(1, frames(audio_duration))  # endFrame is exclusive
    if not pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": audio_item,
                "startFrame": 0,
                "endFrame": audio_end,
                "trackIndex": 1,
                "recordFrame": origin,
                "mediaType": 2,
            }
        ]
    ):
        raise PipelineError("Resolve не положил звук на таймлайн.")

    place_beds(pool, timeline, beds, by_name, audio_end, origin, log)
    place_segments(pool, timeline, segments, by_name, origin, log)

    project_manager.SaveProject()
    return f"Таймлайн {project_name} открыт в DaVinci"


def resolve_lib_path() -> Path:
    configured = os.environ.get("RESOLVE_SCRIPT_LIB", "").strip()
    if configured and Path(configured).exists():
        return Path(configured)
    candidates = [Path(SCRIPT_LIB), Path(r"Z:\davinci\fusionscript.dll")]
    try:
        import subprocess

        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "(Get-Process -Name Resolve -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path)"],
            capture_output=True,
            text=True,
            check=False,
        )
        executable = result.stdout.strip().strip('"')
        if executable:
            candidates.insert(0, Path(executable).with_name("fusionscript.dll"))
    except OSError:
        pass
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(SCRIPT_LIB)


def connect_resolve() -> Any:
    api = os.environ.get("RESOLVE_SCRIPT_API", SCRIPT_API)
    lib = str(resolve_lib_path())
    modules = str(Path(api) / "Modules")
    if not Path(lib).exists() or not Path(modules).exists():
        raise PipelineError(
            "DaVinci Resolve не найден. Нужна установленная Resolve Studio."
        )
    os.environ["RESOLVE_SCRIPT_API"] = api
    os.environ["RESOLVE_SCRIPT_LIB"] = lib
    if modules not in sys.path:
        sys.path.append(modules)
    try:
        import DaVinciResolveScript as script
    except ImportError as exc:
        raise PipelineError("не удалось загрузить скриптинг DaVinci") from exc
    resolve = script.scriptapp("Resolve")
    if not resolve:
        raise PipelineError(
            "Resolve не отвечает. Откройте DaVinci Resolve Studio и включите "
            "Preferences, System, General, External scripting using: Local."
        )
    return resolve


def place_segments(
    pool: Any,
    timeline: Any,
    segments: list[dict[str, Any]],
    by_name: dict[str, Any],
    origin: int,
    log: Log,
) -> None:
    placed: list[tuple[Any, str]] = []
    record = origin
    for index, segment in enumerate(segments):
        asset = segment.get("asset_path")
        if not asset:
            log(f"{segment['id']}  нет файла, на таймлайне пропуск")
            record = origin + frames(segment["slot_end"])
            continue
        clip = by_name.get(segment["id"]) or by_name.get(Path(asset).name)
        if not clip:
            log(f"{segment['id']}  Resolve не принял файл")
            continue
        kind = str(segment.get("asset_kind") or "")
        if kind in {"sequence", "clip"}:
            record = origin + frames(segment["slot_start"])
            item = _append_sequence(pool, timeline, clip, record, int(segment.get("frames") or 1))
        elif kind == "video":
            following = _next_record(segments, index, origin)
            timeline_frames = max(1, (following if following is not None else origin + frames(segment["slot_end"])) - record)
            item = _append_span(pool, timeline, clip, record, timeline_frames)
        else:
            item = _append_still(pool, timeline, clip, record)
            if item:
                log(f"{segment['id']}  картинка встанет на стандартные 5 секунд Resolve")
        if not item:
            log(f"{segment['id']}  не встал на таймлайн")
            continue
        placed.append((item, kind))
        record = int(item.GetEnd())
        log(f"{segment['id']}  на таймлайне с {segment['slot_start']:.1f} с")
    for (left, left_kind), (right, right_kind) in zip(placed, placed[1:]):
        if {left_kind, right_kind} & {"sequence", "clip"}:
            continue
        if int(right.GetStart()) != int(left.GetEnd()):
            continue
        transition = left.AddTransition(
            {
                "type": "Cross Dissolve",
                "category": "simple",
                "position": "end",
                "alignment": "center",
                "duration": DISSOLVE_FRAMES,
            }
        )
        if not transition:
            log(f"{left.GetName()}  переход не встал")


def _import_sequences(pool: Any, segments: list[dict[str, Any]], by_name: dict[str, Any], log: Log) -> None:
    for segment in segments:
        if segment.get("asset_kind") != "sequence":
            continue
        pattern = str(segment.get("sequence") or segment.get("asset_path") or "")
        pattern = str(Path(pattern).resolve()) if pattern else ""
        count = int(segment.get("frames") or 0)
        if not pattern or count < 1:
            continue
        imported = pool.ImportMedia(
            [{"FilePath": pattern, "StartIndex": 1, "EndIndex": count}]
        ) or []
        clip = imported[0] if imported else None
        if clip is None:
            log(f"{segment['id']}  кадр не попал в корзину")
            continue
        _match_rate(clip, segment["id"], log)
        by_name[segment["id"]] = clip


def _match_rate(clip: Any, label: str, log: Log) -> None:
    """Resolve imports image sequences at 24 fps; on a 30 fps timeline they would play 25% longer."""
    if not clip.SetClipProperty("FPS", str(FPS)):
        log(f"{label}  Resolve не принял {FPS} fps для кадров")


def _append_sequence(pool: Any, timeline: Any, clip: Any, record: int, count: int) -> Any:
    end = max(1, int(count))  # endFrame is exclusive
    if not pool.AppendToTimeline(
        [
            {
                "mediaPoolItem": clip,
                "startFrame": 0,
                "endFrame": end,
                "trackIndex": 1,
                "recordFrame": record,
                "mediaType": 1,
            }
        ]
    ):
        return None
    return (timeline.GetItemListInTrack("video", 1) or [None])[-1]


def place_beds(
    pool: Any,
    timeline: Any,
    beds: list[Path] | tuple[Path, ...],
    by_name: dict[str, Any],
    end_frame: int,
    origin: int,
    log: Log,
) -> None:
    for index, bed in enumerate(beds):
        track = 2 + index
        while int(timeline.GetTrackCount("audio")) < track:
            timeline.AddTrack("audio")
        clip = by_name.get(bed.name)
        placed = clip is not None and pool.AppendToTimeline(
            [
                {
                    "mediaPoolItem": clip,
                    "startFrame": 0,
                    "endFrame": end_frame,
                    "trackIndex": track,
                    "recordFrame": origin,
                    "mediaType": 2,
                }
            ]
        )
        if not placed:
            log(f"{bed.name}  не встал на дорожку A{track}")


def _next_record(segments: list[dict[str, Any]], index: int, origin: int) -> int | None:
    for later in segments[index + 1 :]:
        if later.get("asset_path"):
            return origin + frames(later["slot_start"])
    return None


def _append_still(pool: Any, timeline: Any, clip: Any, record: int) -> Any:
    if not _append_clip(pool, clip, record, None, None):
        return None
    return (timeline.GetItemListInTrack("video", 1) or [None])[-1]


def _append_span(pool: Any, timeline: Any, clip: Any, record: int, timeline_frames: int) -> Any:
    fps, count = _clip_rate(clip)
    take = max(1, int(round(timeline_frames * fps / FPS)))
    best_take = take
    best_gap = 10**9
    for _ in range(12):
        if count > 0:
            take = min(count, max(1, take))
        item = _append_take(pool, timeline, clip, record, count, take)
        if item is None:
            break
        duration = int(item.GetDuration())
        gap = abs(duration - timeline_frames)
        if gap < best_gap:
            best_gap = gap
            best_take = take
        timeline.DeleteClips([item])
        if duration == timeline_frames:
            break
        take += 1 if duration < timeline_frames else -1
        if take < 1 or (count > 0 and take > count):
            break
    return _append_take(pool, timeline, clip, record, count, best_take)


def _append_take(
    pool: Any,
    timeline: Any,
    clip: Any,
    record: int,
    count: int,
    take: int,
) -> Any:
    source_in, source_out = _source_bounds(count, take)
    if not _append_clip(pool, clip, record, source_in, source_out):
        return None
    return (timeline.GetItemListInTrack("video", 1) or [None])[-1]


def _append_clip(pool: Any, clip: Any, record: int, source_in: int | None, source_out: int | None) -> bool:
    info: dict[str, Any] = {
        "mediaPoolItem": clip,
        "trackIndex": 1,
        "recordFrame": record,
        "mediaType": 1,
    }
    if source_in is not None and source_out is not None:
        info["startFrame"] = source_in
        info["endFrame"] = source_out
    return bool(pool.AppendToTimeline([info]))


def _source_bounds(count: int, take: int) -> tuple[int, int]:
    if count <= 0:
        return 0, max(0, take - 1)
    take = min(count, max(1, take))
    source_in = max(0, (count - take) // 2)
    return source_in, min(count - 1, source_in + take - 1)


def _clip_rate(clip: Any) -> tuple[float, int]:
    props = clip.GetClipProperty() or {}
    try:
        fps = float(props.get("FPS") or 0)
    except (TypeError, ValueError):
        fps = 0.0
    try:
        count = int(float(props.get("Frames") or 0))
    except (TypeError, ValueError):
        count = 0
    if fps <= 0:
        fps = float(FPS)
    return fps, count


def _clip_item(
    clip_id: str,
    file_id: str,
    name: str,
    path: Path,
    start: int,
    end: int,
    source_in: int,
    source_out: int,
    width: int,
    height: int,
    audio: bool,
) -> str:
    safe_name = escape(name)
    url = escape(file_url(path))
    duration = max(1, source_out)
    media = (
        "          <media><audio><samplecharacteristics>"
        "<depth>16</depth><samplerate>44100</samplerate>"
        "</samplecharacteristics></audio></media>\n"
        if audio
        else (
            "          <media><video><samplecharacteristics>"
            f"<width>{width}</width><height>{height}</height>"
            "</samplecharacteristics></video></media>\n"
        )
    )
    return (
        f'          <clipitem id="{clip_id}">\n'
        f"            <name>{safe_name}</name>\n"
        f"            <duration>{duration}</duration>\n"
        f"{_rate(12)}"
        f"            <start>{start}</start>\n"
        f"            <end>{end}</end>\n"
        f"            <in>{source_in}</in>\n"
        f"            <out>{source_out}</out>\n"
        f'            <file id="{file_id}">\n'
        f"              <name>{safe_name}</name>\n"
        f"              <pathurl>{url}</pathurl>\n"
        f"{_rate(14)}"
        f"              <duration>{duration}</duration>\n"
        f"{media}"
        "            </file>\n"
        "          </clipitem>\n"
    )


def _rate(indent: int) -> str:
    pad = " " * indent
    return f"{pad}<rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>\n"
