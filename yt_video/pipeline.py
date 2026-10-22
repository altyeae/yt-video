from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import httpx

from yt_video import usage
from yt_video.config import Settings, channel_brief
from yt_video.errors import PipelineError
from yt_video.boards import prepare_boards
from yt_video.illustrate import prepare_illustrations
from yt_video.resolve_edit import open_in_resolve, write_timeline
from yt_video.channel import next_angle, pick_related, published_videos
from yt_video.script import ANGLES, generate_script
from yt_video.sound import prepare_sound, render_video
from yt_video.stock import write_report
from yt_video.thumbnail import prepare_publish
from yt_video.voice import synthesize

Log = Callable[[str], None]
Stage = Callable[[str, str], None]


def run_pipeline(
    settings: Settings,
    topic: str,
    language: str,
    aspect: str,
    log: Log,
    set_stage: Stage,
    set_script: Callable[[str], None],
    length: str = "normal",
    channel: str = "",
) -> str:
    topic = topic.strip()
    if not topic:
        raise PipelineError("тема пустая")
    if language not in {"ru", "en"}:
        raise PipelineError("язык должен быть ru или en")
    if aspect not in {"16:9", "9:16"}:
        raise PipelineError("кадр должен быть 16:9 или 9:16")
    if length not in {"short", "normal", "long"}:
        raise PipelineError("длина должна быть short, normal или long")
    project_dir = settings.projects_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    project_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, Any] = {
        "topic": topic,
        "language": language,
        "aspect": aspect,
        "channel": channel or settings.channel,
        "length": length,
        "title": "",
        "music": "",
        "segments": [],
        "audio": "audio/vo.mp3",
        "timeline": "timeline.xml",
        "video": "video.mp4",
    }
    return _build(settings, project_dir, record, log, set_stage, set_script)


def resume_pipeline(
    settings: Settings,
    project_id: str,
    log: Log,
    set_stage: Stage,
    set_script: Callable[[str], None],
) -> str:
    """Continue a build that stopped: finished stages (script, voice, drawn pictures) are reused, not paid for again."""
    name = project_id or last_unfinished(settings)
    project_dir = settings.projects_dir / name
    path = project_dir / "project.json"
    if not name or not path.exists():
        raise PipelineError("нет незаконченного проекта")
    record = json.loads(path.read_text(encoding="utf-8"))
    log(f"продолжение  {project_dir.name}  «{record.get('topic', '')}»")
    return _build(settings, project_dir, record, log, set_stage, set_script)


TRIAL_DROP = (
    "art", "video", "video_duration", "stock", "stock_query", "stamp", "icon_art", "backdrop",
    "frames", "asset_path", "asset_kind", "asset", "motion", "transition",
)


def trial_cut(
    settings: Settings,
    project_id: str,
    minutes: float,
    log: Log,
    set_stage: Stage,
    set_script: Callable[[str], None],
) -> str:
    """A free look at new visuals: the first minutes of a finished video, its paid script, voice and pictures reused.
    Pexels is searched again; where it has no good match the old generated picture stays. Nothing new is generated."""
    import copy
    import dataclasses

    from yt_video.ff import run_ffmpeg

    source = settings.projects_dir / project_id
    original = json.loads((source / "project.json").read_text(encoding="utf-8"))
    kept = [segment for segment in original["segments"] if float(segment["slot_end"]) <= minutes * 60] or original["segments"][:1]
    segments = copy.deepcopy(kept)
    for segment in segments:
        if segment.get("art"):
            segment["fallback_art"] = segment["art"]
        for key in TRIAL_DROP:
            segment.pop(key, None)
    duration = float(segments[-1]["slot_end"])
    project_dir = settings.projects_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-trial"
    (project_dir / "audio").mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(source / "audio" / "vo.mp3"), "-t", f"{duration:.3f}", "-c", "copy", str(project_dir / "audio" / "vo.mp3")])
    record = {**{key: value for key, value in original.items() if key not in {"segments", "resolve_project"}},
              "segments": segments, "duration": duration, "publish": False, "trial_of": project_id}
    log(f"проба  {project_id}: первые {duration / 60:.1f} мин, {len(segments)} кадров, картинки только реальные: Wikimedia, Pexels, Google")
    return _build(dataclasses.replace(settings, art_source="stock"), project_dir, record, log, set_stage, set_script)


def last_unfinished(settings: Settings) -> str:
    for folder in sorted(settings.projects_dir.glob("2*"), reverse=True):
        if (folder / "project.json").exists() and not (folder / "video.mp4").exists():
            return folder.name
    return ""


def _build(
    settings: Settings,
    project_dir: Path,
    record: dict[str, Any],
    log: Log,
    set_stage: Stage,
    set_script: Callable[[str], None],
) -> str:
    """Runs the stages and, finished or not, writes cost.txt: what this run spent on Gemini and ElevenLabs."""
    usage.reset()
    voiced = bool(record.get("duration"))
    try:
        return _stages(settings, project_dir, record, log, set_stage, set_script)
    finally:
        chars = 0 if voiced or not record.get("duration") else sum(len(s["narration"]) for s in record.get("segments") or [])
        try:
            spent = usage.report(project_dir / "cost.txt", chars)
            log(f"расходы  Gemini ~{spent:.0f} ₽ за этот запуск, подробно в cost.txt")
        except OSError:
            pass


def _stages(
    settings: Settings,
    project_dir: Path,
    record: dict[str, Any],
    log: Log,
    set_stage: Stage,
    set_script: Callable[[str], None],
) -> str:
    language = record["language"]
    aspect = record["aspect"]
    channel = record.get("channel") or settings.channel
    project_path = project_dir / "project.json"
    voice_path = project_dir / "audio" / "vo.mp3"

    def save() -> None:
        project_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    with httpx.Client(timeout=180, follow_redirects=True) as client:
        set_stage("script", "running")
        if record.get("segments"):
            log(f"сценарий  уже готов, {len(record['segments'])} сегментов")
        else:
            writers = {"gemini": "Gemini", "claude": "Claude"}
            log(f"сценарий  канал {channel}, запрос к {writers.get(settings.script_provider, 'Cursor')}")
            if record.get("length") == "long":
                _plan_series(settings, record, channel, client, log)
            title, segments, music = generate_script(
                settings, record["topic"], language, client, record.get("length", "normal"), channel_brief(channel), log,
                related=(record.get("related") or {}).get("title", ""), angle=record.get("angle", ""),
            )
            record.update(title=title, music=music, segments=segments)
            save()
            log(f"сценарий  {len(segments)} сегментов, язык {language}, музыка «{music}»")
        segments = record["segments"]
        set_script(_script_view(record))
        set_stage("script", "done")

        set_stage("voice", "running")
        if record.get("duration") and voice_path.exists():
            duration = float(record["duration"])
            log(f"озвучка  уже готова, {duration:.1f} с")
        else:
            log("озвучка  ElevenLabs")
            duration = synthesize(settings, segments, language, voice_path, client, log)
            if segments and segments[-1]["kind"] in {"subscribe", "endscreen"}:
                duration = _add_outro(voice_path, segments, duration)
            record["duration"] = duration
            save()
            log(f"озвучка  vo.mp3, {duration:.1f} с")
        set_script(_script_view(record))
        set_stage("voice", "done")

        set_stage("art", "running")
        log(f"иллюстрации  Gemini {settings.gemini_image_model}")
        prepare_illustrations(settings, project_dir, segments, aspect, client, log)
        log(f"кадры  отчёт {write_report(project_dir, segments)}")
        save()
        set_script(_script_view(record))
        set_stage("art", "done")

        set_stage("boards", "running")
        log("кадры  сцены, главы, цифры, схемы")
        prepare_boards(project_dir, segments, aspect, log)
        save()
        set_stage("boards", "done")

        set_stage("sound", "running")
        beds = prepare_sound(settings, project_dir, segments, record.get("music") or "", duration, client, log)
        set_stage("sound", "done")

    set_stage("assemble", "running")
    video = render_video(segments, [voice_path, *beds], duration, project_dir / "video.mp4")
    log(f"ролик  {video}")
    if record.get("publish", True):
        try:
            with httpx.Client(timeout=180, follow_redirects=True) as client:
                prepare_publish(settings, project_dir, record, channel_brief(channel), client, log)
        except Exception as exc:  # packaging must never cost the finished video
            log(f"публикация  превью и описание не получились: {exc}")
    write_timeline(segments, voice_path, aspect, duration, project_dir / "timeline.xml", lambda _message: None, beds)
    project_name = f"yt-{project_dir.name}"
    record["resolve_project"] = project_name
    save()
    log("таймлайн  DaVinci")
    try:
        message = open_in_resolve(project_name, aspect, segments, voice_path, duration, log, beds)
    except PipelineError as exc:
        message = f"DaVinci не открылся: {exc} Готов {video.name} и timeline.xml"
    log(message)
    set_stage("assemble", "done")
    set_stage("done", "done")
    return message


def _plan_series(settings: Settings, record: dict[str, Any], channel: str, client: httpx.Client, log: Log) -> None:
    """Ties the video to the channel: the closest published video for the end screen and a storytelling angle
    different from the last video's."""
    record["angle"] = next_angle(settings, ANGLES)
    try:
        videos = published_videos(channel_brief(channel), client)
        record["related"] = pick_related(settings, record["topic"], videos, client) if videos else {}
    except PipelineError as exc:
        log(f"канал  связанный ролик не выбран: {exc}")
        record["related"] = {}
    if record["related"]:
        log(f"канал  конечная заставка и подводка: «{record['related']['title']}»")


END_SCREEN_SECONDS = 20


def _add_outro(voice_path: Path, segments: list[dict[str, Any]], duration: float) -> float:
    """Silence after the closing line: the last shot, a calm background with no text, lasts END_SCREEN_SECONDS
    so YouTube end screen elements have their zone."""
    from yt_video.ff import run_ffmpeg

    last = segments[-1]
    pad = max(1.0, END_SCREEN_SECONDS - (float(last["slot_end"]) - float(last["slot_start"])))
    padded = voice_path.with_name("vo_outro.mp3")
    run_ffmpeg(["-i", str(voice_path), "-af", f"apad=pad_dur={pad:.3f}", "-c:a", "libmp3lame", "-q:a", "2", str(padded)])
    padded.replace(voice_path)
    last["slot_end"] = round(float(last["slot_end"]) + pad, 3)
    return duration + pad


def _script_view(record: dict[str, Any]) -> str:
    lines = []
    if record.get("title"):
        lines.append(str(record["title"]))
        lines.append("")
    for segment in record.get("segments") or []:
        start = segment.get("slot_start")
        end = segment.get("slot_end")
        timing = f"  {start:.1f}–{end:.1f} с" if isinstance(start, (int, float)) and isinstance(end, (int, float)) else ""
        lines.append(f"{segment['id']}{timing}")
        kind = segment.get("kind") or ""
        screen = segment.get("screen") or ""
        if kind or screen:
            lines.append(f"{kind}  {screen}".strip())
        lines.append(segment["narration"])
        if segment.get("image_prompt"):
            lines.append(segment["image_prompt"])
        lines.append("")
    return "\n".join(lines).strip()
