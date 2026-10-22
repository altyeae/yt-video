from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Callable

import httpx

from yt_video.boards import cue_points
from yt_video.config import Settings
from yt_video.ff import run_ffmpeg as _run

Log = Callable[[str], None]
EPIDEMIC = "https://api.epidemicsound.com/v0"
# category: (gain dB, longest it may ring in seconds, preferred words in the file name)
SFX = {
    "transition": (-10, 1.4, ("swoosh",)),
    "zoom": (-10, 1.4, ("short", "medium")),
    "appear": (-7, 1.0, ("pop",)),
    "highlight": (-9, 1.4, ("short", "default", "ping")),
    "line_draw": (-11, 0.9, ("short", "default")),
    "counter": (-12, 0.7, ()),
    "ui_click": (-8, 0.8, ("click",)),
    "text_type": (-15, 1.2, ("short",)),
}
TRANSITION_SFX: dict[str, str] = {}
MUSIC_LUFS = -24


def sfx_root(root: Path) -> Path | None:
    for candidate in (root / "sfx_library" / "sfx_library", root / "sfx_library"):
        if (candidate / "appear").is_dir():
            return candidate
    return None


def plan_sfx(segments: list[dict[str, Any]], library: Path) -> list[tuple[float, Path, int, float]]:
    files: dict[str, list[Path]] = {}
    for category, (_gain, _longest, words) in SFX.items():
        folder = library / category
        if folder.is_dir():
            found = sorted(path for path in folder.iterdir() if path.suffix.lower() in {".wav", ".mp3"})
            preferred = [path for path in found if any(word in path.stem.casefold() for word in words)]
            files[category] = preferred or found
    used: dict[str, int] = {}
    planned: list[tuple[float, Path, int, float]] = []
    for segment in segments:
        start = float(segment.get("slot_start") or 0)
        span = max(0.1, float(segment.get("slot_end") or start + 1) - start)
        cues = list(cue_points(segment))
        entry = TRANSITION_SFX.get(str(segment.get("transition") or ""))
        if entry:
            cues.append((0.0, entry))
        for share, category in cues:
            choices = files.get(category) or []
            if not choices:
                continue
            index = used.get(category, 0)
            used[category] = index + 1
            gain, longest, _words = SFX[category]
            at = max(0.0, start + share * span - (0.12 if share == 0 and category in {"transition", "zoom"} else 0))
            planned.append((round(at, 3), choices[index % len(choices)], gain, longest))
    return sorted(planned, key=lambda item: item[0])


def find_music(settings: Settings, query: str, client: httpx.Client, log: Log, count: int = 1) -> list[Path]:
    if not settings.epidemic_api_key:
        log("музыка  нет EPIDEMIC_API_KEY, ролик без музыки")
        return []
    headers = {"Authorization": f"Bearer {settings.epidemic_api_key}", "x-partner-user-id": "yt-video"}
    response = client.get(f"{EPIDEMIC}/tracks/search", params={"term": query, "limit": 20}, headers=headers)
    if response.status_code != 200:
        log(f"музыка  Epidemic ответил {response.status_code}: {response.text[:200]}")
        return []
    tracks = [track for track in response.json().get("tracks") or [] if isinstance(track, dict) and track.get("id")]
    if not tracks:
        log(f"музыка  по запросу «{query}» ничего не нашлось")
        return []
    instrumental = [track for track in tracks if not track.get("hasVocals") and not track.get("vocals")]
    long_enough = [track for track in instrumental or tracks if float(track.get("length") or 0) >= 90]
    paths: list[Path] = []
    for track in (long_enough or instrumental or tracks)[:count]:
        path = _download_track(settings, track, headers, client, log)
        if path is not None:
            paths.append(path)
    return paths


def _download_track(settings: Settings, track: dict[str, Any], headers: dict[str, str], client: httpx.Client, log: Log) -> Path | None:
    folder = settings.cache_dir / "music"
    path = folder / f"{track['id']}.mp3"
    title = str(track.get("title") or track["id"])
    if path.exists():
        log(f"музыка  {title}  из кэша")
        return path
    link = client.get(
        f"{EPIDEMIC}/tracks/{track['id']}/download",
        params={"format": "mp3", "quality": "normal"},
        headers=headers,
    )
    if link.status_code != 200 or not link.json().get("url"):
        log(f"музыка  скачивание не выдано {link.status_code}: {link.text[:200]}")
        return None
    audio = client.get(link.json()["url"])
    if audio.status_code != 200 or len(audio.content) < 10_000:
        log(f"музыка  файл не скачался {audio.status_code}")
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path.write_bytes(audio.content)
    log(f"музыка  {title}  скачан")
    return path


def mix_music(tracks: list[Path], voice: Path, duration: float, destination: Path) -> Path:
    fade = min(2.5, duration / 4)
    inputs: list[str] = []
    for index, track in enumerate(tracks):
        inputs += (["-stream_loop", "-1"] if len(tracks) == 1 else []) + ["-i", str(track)]
    chain = "".join(f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo[t{index}];" for index in range(len(tracks)))
    label = "t0"
    for index in range(1, len(tracks)):
        chain += f"[{label}][t{index}]acrossfade=d=5:c1=tri:c2=tri[x{index}];"
        label = f"x{index}"
    voice_index = len(tracks)
    graph = (
        chain
        + f"[{label}]apad,atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
        f"loudnorm=I={MUSIC_LUFS}:TP=-3:LRA=11,aresample=48000,afade=t=in:d=1.2,afade=t=out:st={duration - fade:.3f}:d={fade:.3f}[m];"
        f"[{voice_index}:a]aformat=sample_rates=48000:channel_layouts=stereo[v];"
        "[m][v]sidechaincompress=threshold=0.15:ratio=2.5:attack=40:release=600[out]"
    )
    _run([*inputs, "-i", str(voice), "-filter_complex", graph, "-map", "[out]", "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(destination)])
    return destination


HOOK_MUSIC = "tense suspense build up cinematic documentary"
SECTION_LONGEST = 150
CROSSFADE = 1.5
HUSH = 1.2


def music_sections(segments: list[dict[str, Any]], duration: float) -> list[tuple[float, float]]:
    """Where the music changes: after the hook, on every twist, and at a chapter start whenever one track
    would otherwise run longer than SECTION_LONGEST."""
    def marked(mark: str) -> list[dict[str, Any]]:
        return [segment for segment in segments if mark in (segment.get("marks") or [])]

    bounds = {0.0, round(duration, 3)}
    hook = marked("HOOK")
    if hook:
        bounds.add(round(float(hook[-1]["slot_end"]), 3))
    bounds |= {round(float(segment["slot_start"]), 3) for segment in marked("TWIST")}
    chapters = [float(segment["slot_start"]) for segment in segments if segment.get("kind") == "chapter"]
    points = sorted(bound for bound in bounds if 0 <= bound <= duration)
    sections: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        while b - a > SECTION_LONGEST:
            inside = [c for c in chapters if a + 60 < c < b - 30]
            if not inside:
                break
            cut = min(inside, key=lambda c: abs(c - a - SECTION_LONGEST * 0.8))
            sections.append((a, cut))
            a = cut
        if b - a > 0.5:
            sections.append((a, b))
    return sections


def hush_windows(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """A short drop-out of the music right before the key moment, so the reveal lands in silence."""
    return [
        (max(0.0, float(segment["slot_start"]) - HUSH), float(segment["slot_start"]) + 0.2)
        for segment in segments
        if "KEY_MOMENT" in (segment.get("marks") or [])
    ]


def mix_sections(
    tracks: list[Path],
    sections: list[tuple[float, float]],
    hush: list[tuple[float, float]],
    voice: Path,
    duration: float,
    destination: Path,
) -> Path:
    inputs: list[str] = []
    lines: list[str] = []
    for index, (start, end) in enumerate(sections):
        inputs += ["-stream_loop", "-1", "-i", str(tracks[index])]
        length = end - start + (CROSSFADE if index + 1 < len(sections) else 0)
        fade_in = 1.2 if index == 0 else CROSSFADE
        lines.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,atrim=0:{length:.3f},asetpts=PTS-STARTPTS,"
            f"afade=t=in:d={fade_in},afade=t=out:st={max(0.0, length - CROSSFADE):.3f}:d={CROSSFADE},"
            f"adelay={int(start * 1000)}:all=1[s{index}];"
        )
    voice_index = len(sections)
    inputs += ["-i", str(voice)]
    mutes = "".join(f",volume=enable='between(t,{a:.3f},{b:.3f})':volume=0" for a, b in hush)
    fade = min(2.5, duration / 4)
    lines.append(
        "".join(f"[s{index}]" for index in range(len(sections)))
        + f"amix=inputs={len(sections)}:normalize=0:duration=longest,apad,atrim=0:{duration:.3f},"
        f"loudnorm=I={MUSIC_LUFS}:TP=-3:LRA=11,aresample=48000{mutes},"
        f"afade=t=out:st={duration - fade:.3f}:d={fade:.3f}[m];"
    )
    lines.append(f"[{voice_index}:a]aformat=sample_rates=48000:channel_layouts=stereo[v];")
    lines.append("[m][v]sidechaincompress=threshold=0.15:ratio=2.5:attack=40:release=600[out]")
    script = destination.with_suffix(".graph.txt")
    script.write_text("\n".join(lines), encoding="utf-8")
    _run([*inputs, "-/filter_complex", str(script), "-map", "[out]", "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(destination)])
    return destination


def mix_sfx(planned: list[tuple[float, Path, int, float]], duration: float, destination: Path) -> Path:
    """Each sound file is read once and split for every use; the graph goes to a script file,
    so hundreds of cues on a long video stay under the Windows command-line limit."""
    files = sorted({path for _at, path, _gain, _longest in planned})
    uses = {path: [index for index, item in enumerate(planned) if item[1] == path] for path in files}
    inputs: list[str] = []
    lines: list[str] = []
    for number, path in enumerate(files):
        inputs += ["-i", str(path)]
        outs = "".join(f"[u{index}]" for index in uses[path])
        lines.append(f"[{number}:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit={len(uses[path])}{outs};")
    for index, (at, _path, gain, longest) in enumerate(planned):
        fade = min(0.3, longest / 3)
        lines.append(
            f"[u{index}]atrim=0:{longest},asetpts=PTS-STARTPTS,volume={gain}dB,"
            f"afade=t=out:st={longest - fade:.3f}:d={fade:.3f},adelay={int(at * 1000)}:all=1[s{index}];"
        )
    labels = "".join(f"[s{index}]" for index in range(len(planned)))
    lines.append(f"{labels}amix=inputs={len(planned)}:normalize=0:duration=longest,apad[out]")
    script = destination.with_suffix(".graph.txt")
    script.write_text("\n".join(lines), encoding="utf-8")
    _run([*inputs, "-/filter_complex", str(script), "-map", "[out]", "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(destination)])
    return destination


def render_video(
    segments: list[dict[str, Any]],
    audio: list[Path],
    duration: float,
    destination: Path,
) -> Path:
    clips = [Path(segment["asset_path"]) for segment in segments if segment.get("asset_kind") == "clip"]
    listing = destination.with_name("clips.txt")
    listing.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
    inputs = ["-f", "concat", "-safe", "0", "-i", str(listing)]
    for path in audio:
        inputs += ["-i", str(path)]
    sound = "".join(f"[{index + 1}:a]" for index in range(len(audio)))
    sound += f"amix=inputs={len(audio)}:normalize=0:duration=first[a]"
    _run([
        *inputs, "-filter_complex", sound, "-map", "0:v", "-map", "[a]",
        "-t", f"{duration:.3f}", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(destination),
    ])
    return destination


def prepare_sound(
    settings: Settings,
    project_dir: Path,
    segments: list[dict[str, Any]],
    music_query: str,
    duration: float,
    client: httpx.Client,
    log: Log,
) -> list[Path]:
    voice = project_dir / "audio" / "vo.mp3"
    beds: list[Path] = []
    sections = music_sections(segments, duration)
    if len(sections) > 1:
        hook = find_music(settings, HOOK_MUSIC, client, log, count=1) if any(
            "HOOK" in (segment.get("marks") or []) for segment in segments) else []
        rest = [track for track in find_music(settings, music_query, client, log, count=len(sections)) if track not in hook]
        pool = rest or hook
        if pool:
            first = hook[:1] if hook else []
            tracks = first + [pool[i % len(pool)] for i in range(len(sections) - len(first))]
            hush = hush_windows(segments)
            beds.append(mix_sections(tracks, sections, hush, voice, duration, project_dir / "audio" / "music.wav"))
            log(f"музыка  {len(sections)} частей, {len(set(tracks))} трек(ов), тишина перед {len(hush)} главными моментами")
    else:
        tracks = find_music(settings, music_query, client, log, count=max(1, math.ceil(duration / 170)))
        if tracks:
            beds.append(mix_music(tracks, voice, duration, project_dir / "audio" / "music.wav"))
            log(f"музыка  {len(tracks)} трек(а) сведены под голос")
    library = sfx_root(settings.root)
    if library is None:
        log("звуки  нет папки sfx_library")
        return beds
    planned = plan_sfx(segments, library)
    if planned:
        beds.append(mix_sfx(planned, duration, project_dir / "audio" / "sfx.wav"))
        log(f"звуки  {len(planned)} эффектов на появления")
    return beds
