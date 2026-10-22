from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from yt_video.config import Settings
from yt_video.errors import PipelineError
from yt_video.script import narration_text


def assign_times(
    segments: list[dict[str, Any]],
    full_text: str,
    alignment: dict[str, Any] | None,
    audio_duration: float,
) -> None:
    chars = list((alignment or {}).get("characters") or [])
    starts = list((alignment or {}).get("character_start_times_seconds") or [])
    ends = list((alignment or {}).get("character_end_times_seconds") or [])
    aligned = "".join(chars) == full_text and len(chars) == len(starts) == len(ends) and bool(chars)
    cursor = 0
    if aligned:
        for segment in segments:
            narration = segment["narration"]
            index = full_text.find(narration, cursor)
            if index < 0:
                aligned = False
                break
            end_index = index + len(narration) - 1
            segment["start_sec"] = float(starts[index])
            segment["end_sec"] = float(ends[end_index])
            cursor = index + len(narration)
    if not aligned:
        total = max(len(full_text), 1)
        position = 0
        duration = max(audio_duration, 0.3 * len(segments))
        for segment in segments:
            start = position / total * duration
            position += len(segment["narration"])
            end = max(position / total * duration, start + 0.3)
            segment["start_sec"] = start
            segment["end_sec"] = end


def apply_slots(segments: list[dict[str, Any]], audio_duration: float) -> None:
    if not segments:
        return
    duration = max(audio_duration, 0.3)
    previous = 0.0
    for index, segment in enumerate(segments):
        start = 0.0 if index == 0 else float(segment.get("start_sec") or previous)
        if index + 1 < len(segments):
            end = float(segments[index + 1].get("start_sec") or start + 0.3)
        else:
            end = duration
        start = max(start, previous)
        end = max(end, start + 0.3)
        segment["slot_start"] = round(start, 3)
        segment["slot_end"] = round(end, 3)
        segment["start_sec"] = round(float(segment.get("start_sec") or start), 3)
        segment["end_sec"] = round(float(segment.get("end_sec") or end), 3)
        previous = end
    if duration > segments[-1]["slot_end"]:
        segments[-1]["slot_end"] = round(duration, 3)


CHUNK_CHARS = 2400


def chunk_segments(segments: list[dict[str, Any]], limit: int = CHUNK_CHARS) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    size = 0
    for segment in segments:
        length = len(segment["narration"]) + 1
        if chunks and size + length <= limit:
            chunks[-1].append(segment)
            size += length
        else:
            chunks.append([segment])
            size = length
    return chunks


def synthesize(
    settings: Settings,
    segments: list[dict[str, Any]],
    language: str,
    audio_path: Path,
    client: httpx.Client,
    log: Callable[[str], None] = lambda _message: None,
) -> float:
    if not settings.elevenlabs_api_key:
        raise PipelineError("не задан ELEVENLABS_API_KEY")
    voice_id = settings.voice_id(language)
    if language == "ru" and voice_id in {"", "21m00Tcm4TlvDq8ikWAM"}:
        voice_id = "MMEB4YSe9SpycxYSvzhG"
    if not voice_id:
        raise PipelineError("не задан голос ElevenLabs")
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    chunks = chunk_segments(segments)
    parts: list[Path] = []
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    offset = 0.0
    for number, chunk in enumerate(chunks, start=1):
        text = narration_text(chunk)
        raw_path = audio_path.with_name(f"vo_raw_{number:02d}.mp3")
        part_path = audio_path.with_name(f"vo_part_{number:02d}.wav")
        alignment = _speak(settings, voice_id, language, text, raw_path, client)
        keep = speech_intervals(alignment)
        if not keep or "".join(alignment.get("characters") or []) != text:
            raise PipelineError(f"ElevenLabs не вернул тайминги для части {number}")
        cut_pauses(raw_path, keep, part_path)
        tight = remap_alignment(alignment, keep)
        if chars:
            chars.append(" ")
            starts.append(offset)
            ends.append(offset)
        chars.extend(tight["characters"])
        starts.extend(offset + t for t in tight["character_start_times_seconds"])
        ends.extend(offset + t for t in tight["character_end_times_seconds"])
        offset += map_time(keep[-1][1], keep)
        parts.append(part_path)
        if len(chunks) > 1:
            log(f"озвучка  часть {number}/{len(chunks)}, {offset:.0f} с")
    _join_parts(parts, audio_path)
    full_text = narration_text(segments)
    alignment = {
        "characters": chars,
        "character_start_times_seconds": starts,
        "character_end_times_seconds": ends,
    }
    assign_times(segments, full_text, alignment, offset)
    pieces = plan_pacing(segments, offset)
    if len(pieces) > 1 or (pieces and pieces[0][2] != 1.0):
        offset = apply_pacing(audio_path, pieces)
        for segment in segments:
            segment["start_sec"] = map_paced(float(segment["start_sec"]), pieces)
            segment["end_sec"] = map_paced(float(segment["end_sec"]), pieces)
        log(f"озвучка  хук быстрее, паузы перед {sum(1 for piece in pieces if piece[3])} поворотами и цифрами")
    apply_slots(segments, offset)
    return offset


HOOK_TEMPO = 1.05
KEY_PAUSE = 0.4
PAUSE_MARKS = {"TWIST", "KEY_MOMENT"}


def plan_pacing(segments: list[dict[str, Any]], duration: float) -> list[tuple[float, float, float, float]]:
    """Pieces of the voice track: (start, end, tempo, silence after). The hook runs a little faster; a twist,
    the key moment and every key number get a beat of silence before them."""
    hook = [segment for segment in segments if "HOOK" in (segment.get("marks") or [])]
    hook_end = float(hook[-1]["end_sec"]) + 0.05 if hook else 0.0
    cuts: dict[float, float] = {}
    for previous, segment in zip(segments, segments[1:]):
        if not (PAUSE_MARKS & set(segment.get("marks") or []) or segment.get("kind") == "number"):
            continue
        start = float(segment["start_sec"])
        gap = start - float(previous["end_sec"])
        if KEY_PAUSE - gap > 0.05 and start > hook_end:
            cuts[round(max(float(previous["end_sec"]), start - 0.03), 3)] = round(KEY_PAUSE - max(0.0, gap), 3)
    bounds = sorted({0.0, duration, *([hook_end] if 0 < hook_end < duration else []), *cuts})
    return [
        (a, b, HOOK_TEMPO if b <= hook_end else 1.0, cuts.get(b, 0.0))
        for a, b in zip(bounds, bounds[1:])
        if b > a
    ]


def map_paced(t: float, pieces: list[tuple[float, float, float, float]]) -> float:
    out = 0.0
    for a, b, tempo, silence in pieces:
        if t <= b:
            return out + max(0.0, t - a) / tempo
        out += (b - a) / tempo + silence
    return out


def apply_pacing(audio_path: Path, pieces: list[tuple[float, float, float, float]]) -> float:
    from yt_video.ff import run_ffmpeg

    lines = [f"[0:a]asplit={len(pieces)}" + "".join(f"[p{i}]" for i in range(len(pieces))) + ";"]
    labels = []
    for i, (a, b, tempo, silence) in enumerate(pieces):
        lines.append(
            f"[p{i}]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS,"
            + (f"atempo={tempo}," if tempo != 1.0 else "")
            + f"aformat=sample_rates=44100:channel_layouts=mono[q{i}];"
        )
        labels.append(f"[q{i}]")
        if silence:
            lines.append(f"aevalsrc=0:d={silence:.3f}:s=44100,aformat=sample_rates=44100:channel_layouts=mono[z{i}];")
            labels.append(f"[z{i}]")
    lines.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[out]")
    script = audio_path.with_name("pacing.graph.txt")
    script.write_text("\n".join(lines), encoding="utf-8")
    paced = audio_path.with_name("vo_paced.mp3")
    run_ffmpeg(["-i", str(audio_path), "-/filter_complex", str(script), "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", str(paced)])
    paced.replace(audio_path)
    return round(sum((b - a) / tempo + silence for a, b, tempo, silence in pieces), 3)


def _speak(
    settings: Settings,
    voice_id: str,
    language: str,
    text: str,
    raw_path: Path,
    client: httpx.Client,
) -> dict[str, Any]:
    response = None
    for attempt, pause in enumerate((0, 15, 30, 60, 90, 120)):
        if attempt:
            time.sleep(pause)
        try:
            response = client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps",
                headers={"xi-api-key": settings.elevenlabs_api_key, "content-type": "application/json"},
                json={
                    "text": text,
                    "model_id": "eleven_v3",
                    "language_code": language,
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.7},
                },
            )
        except httpx.HTTPError:
            continue
        blocked = response.status_code == 403 and "text/html" in response.headers.get("content-type", "")
        if response.status_code == 200 or not (blocked or response.status_code in {429, 500, 502, 503, 504}):
            break
    if response is None or response.status_code != 200:
        detail = f"{response.status_code}: {response.text[:300]}" if response is not None else "нет ответа"
        raise PipelineError(f"ElevenLabs ответил {detail}")
    payload = response.json()
    audio_b64 = payload.get("audio_base64")
    if not audio_b64:
        raise PipelineError("ElevenLabs не вернул аудио")
    raw_path.write_bytes(base64.b64decode(audio_b64))
    alignment = payload.get("alignment")
    return alignment if isinstance(alignment, dict) else {}


def _join_parts(parts: list[Path], destination: Path) -> None:
    from yt_video.ff import run_ffmpeg

    listing = destination.with_name("vo_parts.txt")
    listing.write_text("".join(f"file '{part.as_posix()}'\n" for part in parts), encoding="utf-8")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame", "-q:a", "2", str(destination)])


MAX_PAUSE = 0.2
LEAD_IN = 0.04
TAIL = 0.08
TEMPO = 1.06


def speech_intervals(alignment: dict[str, Any] | None) -> list[tuple[float, float]]:
    """Spans of the raw audio to keep: every pause longer than MAX_PAUSE shrinks to LEAD_IN + TAIL."""
    if not alignment:
        return []
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    if not chars or not (len(chars) == len(starts) == len(ends)):
        return []
    spoken = [
        (float(start), float(end))
        for char, start, end in zip(chars, starts, ends)
        if str(char).isalnum()
    ]
    if not spoken:
        return []
    merged: list[list[float]] = [list(spoken[0])]
    for start, end in spoken[1:]:
        if start - merged[-1][1] <= MAX_PAUSE:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    keep: list[tuple[float, float]] = []
    for start, end in merged:
        start = max(0.0, start - LEAD_IN)
        end = end + TAIL
        if keep and start <= keep[-1][1]:
            keep[-1] = (keep[-1][0], end)
        else:
            keep.append((start, end))
    return keep


def map_time(t: float, keep: list[tuple[float, float]]) -> float:
    offset = 0.0
    for start, end in keep:
        if t < start:
            return offset / TEMPO
        if t <= end:
            return (offset + t - start) / TEMPO
        offset += end - start
    return offset / TEMPO


def remap_alignment(alignment: dict[str, Any], keep: list[tuple[float, float]]) -> dict[str, Any]:
    return {
        "characters": list(alignment.get("characters") or []),
        "character_start_times_seconds": [map_time(float(t), keep) for t in alignment["character_start_times_seconds"]],
        "character_end_times_seconds": [map_time(float(t), keep) for t in alignment["character_end_times_seconds"]],
    }


def cut_pauses(source: Path, keep: list[tuple[float, float]], destination: Path) -> None:
    from yt_video.ff import run_ffmpeg

    spans = "+".join(f"between(t,{start:.3f},{end:.3f})" for start, end in keep)
    run_ffmpeg([
        "-i", str(source),
        "-af", f"aselect='{spans}',asetpts=N/SR/TB,atempo={TEMPO}",
        *(["-c:a", "pcm_s16le", "-ar", "44100", "-ac", "1"] if destination.suffix == ".wav" else ["-c:a", "libmp3lame", "-q:a", "2"]),
        str(destination),
    ])
