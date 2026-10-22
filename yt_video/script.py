from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from yt_video.config import Settings
from yt_video import usage
from yt_video.errors import PipelineError


def extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise PipelineError("сценарий пришёл не JSON")
    try:
        data = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PipelineError(f"сценарий не разобрался: {exc}") from exc
    if not isinstance(data, dict):
        raise PipelineError("сценарий пришёл не объектом")
    return data


GRAPHIC_KINDS = ("timeline", "bars", "before_after", "ring", "chart", "versus", "stats", "checklist")
SHOT_KINDS = ("chapter", "scene", "term", "number", "diagram", *GRAPHIC_KINDS)
SCENE_ALIASES = ("metaphor", "proof", "host")


def normalize_segments(data: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    raw_segments = data.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise PipelineError("в сценарии нет сегментов")
    title = str(data.get("title") or "").strip()
    segments: list[dict[str, Any]] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        narration = str(item.get("narration") or "").strip()
        if not narration:
            continue
        segments.append(_as_shot(item, narration))
    if not segments:
        raise PipelineError("в сценарии нет фраз для озвучки")
    width = 2 if len(segments) < 100 else 3
    for index, segment in enumerate(segments, start=1):
        segment["id"] = f"s{index:0{width}d}"
    return title, segments


def _as_shot(item: dict[str, Any], narration: str) -> dict[str, Any]:
    kind = str(item.get("kind") or "").strip()
    if kind in SCENE_ALIASES:
        kind = "scene"
    if kind not in SHOT_KINDS:
        kind = ""
    screen = " ".join(str(item.get("screen") or "").split())
    note = " ".join(str(item.get("note") or "").split())[:90]
    steps = _steps(item.get("steps"))
    icons = _icons(item.get("icons"), len(steps))
    image_prompt = " ".join(str(item.get("image_prompt") or item.get("visual_intent") or "").split())[:600]
    visual = str(item.get("visual_intent") or "").strip()
    data = graphic_data(kind, item) if kind in GRAPHIC_KINDS else None
    if kind in GRAPHIC_KINDS and data is None:
        kind = ""
    if not kind and image_prompt:
        kind = "scene"
    if kind == "scene" and not image_prompt:
        kind = ""
    if not screen and kind != "scene":
        screen = _best_screen(item, narration)
    if not screen:
        screen = _headline(narration)
    if not kind:
        kind = _kind_for(screen, steps)
    if kind == "chapter":
        screen = screen.upper()[:48]
    elif kind == "diagram" and not steps:
        kind = "term"
    shot = {
        "narration": narration,
        "kind": kind,
        "screen": screen[:48],
        "note": note,
        "steps": steps if kind == "diagram" else [],
        "icons": icons if kind == "diagram" else [],
        "image_prompt": image_prompt if kind == "scene" else "",
        "visual_intent": visual,
        "asset": None,
    }
    if data is not None:
        shot["data"] = data
    marks = item.get("marks") or item.get("mark") or []
    marks = [str(mark).strip().upper() for mark in ([marks] if isinstance(marks, str) else marks)]
    if [mark for mark in marks if mark in MARKS]:
        shot["marks"] = [mark for mark in MARKS if mark in marks]
    if kind == "scene":
        if item.get("archival") is True:
            shot["archival"] = True
        compare = " ".join(str(item.get("compare_prompt") or "").split())[:600]
        if compare:
            shot["compare_prompt"] = compare
    return shot


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _whole_words(value: Any, limit: int) -> str:
    """Never cut a word in half on screen: '10,000 hotel beds' becomes '10,000 hotel', not '10,000 hotel b'."""
    kept = ""
    for word in str(value or "").split():
        if len(f"{kept} {word}".strip()) > limit:
            break
        kept = f"{kept} {word}".strip()
    return kept


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").replace("$", "").replace("%", "").strip())
    except ValueError:
        return None
    return number if number >= 0 else None


def graphic_data(kind: str, item: dict[str, Any]) -> dict[str, Any] | None:
    """Clean data for an infographic shot, or None when the script did not give enough to draw it."""
    title = _text(item.get("screen"), 48)
    if kind == "timeline":
        points = [
            (_text(point.get("year"), 12), _text(point.get("label"), 32))
            for point in item.get("points") or [] if isinstance(point, dict) and point.get("year")
        ][:5]
        return {"title": title, "points": points} if len(points) >= 3 else None
    if kind == "bars":
        items = []
        for row in item.get("items") or []:
            value = _number(row.get("value")) if isinstance(row, dict) else None
            if value is not None and row.get("label"):
                items.append((_text(row["label"], 36), value, _text(row.get("suffix"), 6)))
        return {"title": title, "items": items[:3]} if len(items) >= 2 and max(value for _l, value, _s in items) > 0 else None
    if kind == "before_after":
        before, after = _whole_words(item.get("before"), 14), _whole_words(item.get("after"), 14)
        return {"before": before, "after": after, "note": _text(item.get("note"), 70)} if before and after else None
    if kind == "ring":
        percent = _number(item.get("percent"))
        return {"percent": percent, "note": _text(item.get("note"), 60)} if percent is not None and 0 < percent <= 100 else None
    if kind == "chart":
        points = []
        for point in item.get("points") or []:
            value = _number(point.get("value")) if isinstance(point, dict) else None
            if value is not None and point.get("label"):
                points.append((_text(point["label"], 10), value))
        return {"title": title, "points": points[:7]} if len(points) >= 4 else None
    if kind == "versus":
        left = [_text(row, 32) for row in item.get("left") or [] if _text(row, 32)][:3]
        right = [_text(row, 32) for row in item.get("right") or [] if _text(row, 32)][:3]
        if len(left) < 2 or len(right) < 2:
            return None
        return {"left": (_text(item.get("left_title"), 14) or "THEN", left), "right": (_text(item.get("right_title"), 14) or "NOW", right)}
    if kind == "stats":
        stats = [
            (_text(stat.get("value"), 12), _text(stat.get("note"), 30))
            for stat in item.get("stats") or [] if isinstance(stat, dict) and stat.get("value")
        ][:4]
        return {"title": title, "stats": stats} if len(stats) == 4 else None
    if kind == "checklist":
        rows = [
            (_text(row.get("text"), 40), bool(row.get("ok")))
            for row in item.get("items") or [] if isinstance(row, dict) and row.get("text")
        ][:4]
        return {"title": title, "items": rows} if len(rows) >= 3 else None
    return None


def _icons(raw: Any, count: int) -> list[str]:
    if not isinstance(raw, list) or count == 0:
        return []
    icons = [" ".join(str(item).split())[:80] for item in raw[:count]]
    if len(icons) != count or not all(icons):
        return []
    return icons


def _steps(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    steps: list[str] = []
    for item in raw:
        text = " ".join(str(item).split())
        if len(text) > 30:
            text = text[:30].rsplit(" ", 1)[0]
        if text:
            steps.append(text)
        if len(steps) == 4:
            break
    return steps


def _best_screen(item: dict[str, Any], narration: str) -> str:
    found: list[str] = []
    for insert in _clean_inserts(item.get("inserts"), narration):
        found.append(insert["text"])
    for insert in _derive_inserts(narration):
        found.append(insert["text"])
    best = ""
    best_score = 0
    for text in found:
        score = _insert_score(text, narration)
        if score > best_score:
            best = text
            best_score = score
    if best_score >= 80:
        return best
    return ""


def _kind_for(screen: str, steps: list[str]) -> str:
    if steps:
        return "diagram"
    if re.match(r"^\d+\.", screen):
        return "chapter"
    if re.fullmatch(r"20\d{2}", screen) or re.fullmatch(r"\d{1,2}:\d{2}", screen):
        return "number"
    if screen and re.search(r"\d", screen) and len(screen) <= 16:
        return "number"
    if screen and len(screen.split()) <= 4:
        return "term"
    return "term"


def _headline(narration: str) -> str:
    text = narration.split("—")[0].split(":")[0].strip(" .")
    words = text.split()
    return " ".join(words[:4])[:40]


PLACES = (
    "upper_left",
    "upper_right",
    "lower_left",
    "lower_right",
    "center",
    "left",
    "right",
    "upper_center",
)


def _clean_inserts(raw: Any, narration: str) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    inserts: list[dict[str, str]] = []
    used: set[str] = set()
    spoken = narration.casefold()
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text or len(text) > 28 or len(text.split()) > 4:
            continue
        if text.casefold() == spoken:
            continue
        place = str(item.get("place") or "").strip()
        if place not in PLACES or place in used:
            place = next((name for name in PLACES if name not in used), PLACES[len(inserts) % len(PLACES)])
        used.add(place)
        inserts.append({"text": text, "place": place})
        if len(inserts) == 3:
            break
    return inserts


_MONTHS = (
    ("январ", "январь"),
    ("феврал", "февраль"),
    ("март", "март"),
    ("апрел", "апрель"),
    ("июн", "июнь"),
    ("июл", "июль"),
    ("август", "август"),
    ("сентябр", "сентябрь"),
    ("октябр", "октябрь"),
    ("ноябр", "ноябрь"),
    ("декабр", "декабрь"),
)
_CLOCK = (
    ("восьми утра", "08:00"),
    ("девяти утра", "09:00"),
    ("десяти утра", "10:00"),
    ("одиннадцати утра", "11:00"),
    ("двенадцати", "12:00"),
    ("восемнадцати", "18:00"),
    ("девятнадцати", "19:00"),
    ("двадцати часов", "20:00"),
    ("двадцати одного", "21:00"),
    ("двадцати двух", "22:00"),
)
_SKIP = {
    "этот", "эта", "это", "эти", "всей", "всего", "всех", "своей", "своём", "своем",
    "после", "перед", "через", "можно", "будет", "будут", "пройдут", "начинает",
    "местного", "времени", "ряда", "регионов", "всей", "по всей",
}


def _derive_inserts(narration: str) -> list[dict[str, str]]:
    found: list[str] = []
    folded = narration.casefold()
    found.extend(re.findall(r"«([^»]{1,24})»", narration))
    found.extend(re.findall(r"\b20\d{2}\b", narration))
    found.extend(re.findall(r"\b\d+\s?%", narration))
    used_tokens: set[str] = set()
    for phrase, label in _CLOCK:
        if phrase in folded:
            found.append(label)
            used_tokens.update(phrase.split())
    for stem, label in _MONTHS:
        if stem in folded and label not in found:
            found.append(label)
            break
    if re.search(r"\bма[йяе]\b", folded) and "май" not in found and "март" not in found:
        found.append("май")
    nouns: list[str] = []
    for word in re.findall(r"[A-Za-zА-Яа-яЁё-]{5,}", narration):
        token = word.casefold()
        if token in _SKIP or token in used_tokens or token in {item.casefold() for item in found}:
            continue
        if token.endswith(("ые", "ие", "ых", "их", "ого", "ему", "ами", "ями", "ах", "ях", "ом", "ем", "ая", "яя", "ое", "ее", "ись", "ются", "атся", "ется", "ится", "ает", "яет", "ует", "аёт", "яёт", "уёт", "ят", "ат", "ать", "ять", "еть", "ешь")) or token.endswith("е"):
            continue
        nouns.append(word)
    if len(found) < 2 and nouns:
        found.append(nouns[-1])
    if len(found) < 2 and len(nouns) > 1:
        found.insert(len(found) - 1 if found else 0, nouns[-2])
    unique: list[str] = []
    for text in found:
        clean = " ".join(text.split())
        if clean and clean.casefold() not in {item.casefold() for item in unique}:
            unique.append(clean)
    return [{"text": text} for text in unique[:3]]


def _insert_score(text: str, narration: str) -> int:
    if re.fullmatch(r"20\d{2}", text):
        return 100
    if re.search(r"\d", text) and "%" in text:
        return 90
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        return 88
    if text in re.findall(r"«([^»]+)»", narration):
        return 85
    return 0


def narration_text(segments: list[dict[str, Any]]) -> str:
    return " ".join(segment["narration"] for segment in segments)


LENGTHS = {
    "short": {"segments": "9–11", "seconds": "25–30"},
    "normal": {"segments": "24–32", "seconds": "70–90"},
}
LONG_CHAPTERS = "7–8"
LONG_MINUTES = 9.5
MAX_MINUTES = 11
HOOK_WORDS = 110
CONTEXT_WORDS = 170
WORDS_PER_MINUTE = {"en": 150, "ru": 125}
MARKS = ("HOOK", "OPEN_LOOP", "TWIST", "KEY_MOMENT", "END_CTA")
# Ways to tell a story; the outline gets a different one each video so videos do not look mass-produced.
ANGLES = (
    "chronological: the dream, the boom, the turn, the fall, today",
    "investigation: start from the place today and work back through the evidence to who decided what",
    "countdown: the story told through the 5-6 decisions or numbers that doomed the place",
    "people first: follow 2-3 real people whose lives were tied to the place",
    "then and now: alternate the place in its prime with the same spots today, chapter by chapter",
)


def _language_name(language: str) -> str:
    return "английском" if language == "en" else "русском"


def _brief_block(brief: str) -> str:
    return f"Концепция канала, ей подчиняется всё:\n{brief.strip()}\n" if brief.strip() else ""


def _shot_rules(language: str, long: bool) -> str:
    pace = (
        "каждая фраза 8–16 слов и звучит 3–5 секунд, кадр меняется на каждой фразе. "
        "Повествование как в сильном документальном YouTube-ролике: интрига, конкретные факты, даты, суммы, "
        "открытые вопросы перед поворотами сюжета.\n"
        if long
        else "каждая фраза 5–11 слов и звучит 2–3 секунды, кадр меняется на каждой фразе. "
        "Темп быстрый: короткие рубленые фразы, вопрос, интрига, неожиданный факт.\n"
    )
    return (
        f"Язык диктора: {_language_name(language)}. Все надписи на экране (screen, note, steps) на языке диктора, "
        "image_prompt и icons всегда на английском.\n"
        "Каждый сегмент — объект:\n"
        '{"narration": "фраза", "kind": "chapter", "screen": "1. HOW IT STARTED"}\n'
        '{"narration": "фраза", "kind": "scene", "image_prompt": "Aerial view of a half-built resort town on a desert lake shore at golden hour, 1958"}\n'
        '{"narration": "фраза", "kind": "diagram", "screen": "Why it failed", "steps": ["salt", "fish die", "tourists leave"], "icons": ["salt crystals", "dead fish", "empty beach chair"]}\n'
        '{"narration": "фраза", "kind": "number", "screen": "$100 000 000", "note": "invested by 1960"}\n'
        "kind только из списка: chapter, scene, diagram, number, term, timeline, bars, before_after, ring, chart, versus, stats, checklist.\n"
        "scene — главный тип, 60–70% кадров. image_prompt 1–2 предложения: что в кадре, крупный объект, "
        "ракурс, обстановка, эпоха, свет. Реалистичные кинематографичные кадры, соседние сцены отличаются планом "
        "(общий, средний, деталь, вид сверху). Без текста и надписей на картинке. "
        "В image_prompt никаких брендов, торговых марок, названий компаний, известных персонажей и узнаваемых "
        "реальных людей — нейросеть их не рисует: пиши «большой парк развлечений с замком», а не название бренда. "
        "У каждой сцены своё описание: никогда не повторяй картинку прошлой сцены, даже если говорим о том же месте — "
        "тогда покажи его с другого ракурса или другую деталь.\n"
        "Главное правило сцены: зритель без звука понимает, о чём фраза. В кадре ровно то место, предмет, "
        "люди или событие, которые названы во фразе. Ничего, чего нет во фразе, никаких абстрактных метафор. "
        "Фраза под сцену конкретная: кто, что, где, когда.\n"
        + "chapter — жёлтая плашка, screen до 5 слов, с номером главы.\n"
        "diagram — причина и следствие или процесс из 2–4 шагов: steps — подписи по 1–3 слова, icons — столько же предметов для иконок.\n"
        "number — сумма, год, количество людей или процент крупно, note объясняет. Ставь после сцены, к которой цифра относится. "
        "Диктор называет число ровно так, как оно стоит на экране: «2.4 billion dollars» под «$2.4B», «five hundred "
        "thousand» под «500,000» — без округлений и пересчётов. "
        "Не чаще одной цифры на 5 кадров и никогда две цифры подряд: только самые сильные числа истории.\n"
        "term — одно слово или имя крупно, note — пояснение в одну строку.\n"
        "Анимированная инфографика: ставь её вместо number и diagram, когда она подходит по смыслу, примерно раз в 8–10 кадров, "
        f"разные типы, каждый тип не больше {GRAPHIC_LIMIT} раз за ролик, только реальные данные из истории. "
        f"Между любыми графическими кадрами (инфографика, number, term, diagram) минимум {GRAPHIC_GAP} сцены:\n"
        '- timeline, 3–5 дат: {"kind": "timeline", "screen": "заголовок", "points": [{"year": "1905", "label": "2–4 слова"}]}\n'
        '- bars, сравнение 2–3 величин: {"kind": "bars", "screen": "заголовок", "items": [{"label": "что", "value": 500000, "suffix": ""}]}\n'
        '- before_after, было и стало: {"kind": "before_after", "before": "$3,000", "after": "$100", "note": "что это"}; '
        "before и after — только число или значение до 10 символов, всё пояснение в note\n"
        '- ring, доля: {"kind": "ring", "percent": 98, "note": "чего"}\n'
        '- chart, динамика по годам, 4–7 точек: {"kind": "chart", "screen": "что", "points": [{"label": "1960", "value": 300000}]}\n'
        '- versus, тогда и сейчас по 3 пункта: {"kind": "versus", "left_title": "THEN", "left": ["..."], "right_title": "NOW", "right": ["..."]}\n'
        '- stats, ровно 4 главные цифры: {"kind": "stats", "screen": "заголовок", "stats": [{"value": "$100B", "note": "что это"}]}\n'
        '- checklist, обещания и реальность, 3–4 пункта: {"kind": "checklist", "screen": "заголовок", "items": [{"text": "...", "ok": false}]}\n'
        "У сцены можно добавить archival: true, если она показывает прошлое (до 1990-х), и compare_prompt — второе описание "
        "картинки, если фраза сравнивает два состояния (было и стало): тогда кадры встанут рядом.\n"
        "Без приветствия, без «в этом видео», без призывов подписаться. Никогда не пересказывай уже сказанное, "
        "никаких «as we mentioned», «как мы уже говорили», «remember that»; не больше двух дат подряд — даты "
        "превращай в события.\n"
        "Метки: если задание просит пометить сегмент, добавь ему поле marks, например "
        '{"narration": "...", "kind": "scene", "image_prompt": "...", "marks": ["TWIST"]}. '
        f"Допустимые метки: {', '.join(MARKS)}. Без просьбы метки не ставь.\n"
        + "Речь: " + pace
    )


def build_prompt(topic: str, language: str, length: str = "normal", brief: str = "") -> str:
    size = LENGTHS.get(length, LENGTHS["normal"])
    return (
        "Ты пишешь раскадровку YouTube-ролика. Почти каждый кадр — реалистичная иллюстрация, "
        "которая буквально показывает то, что говорит диктор. Стоковых видео нет.\n"
        + _brief_block(brief)
        + f"Тема: {topic}\n"
        "Верни только JSON без пояснений:\n"
        '{"title": "короткий заголовок", "music": "mood and genre of background music in English, instrumental", '
        '"segments": [ ... ]}\n'
        + _shot_rules(language, long=False)
        + f"Нужно {size['segments']} кадров, речь на {size['seconds']} секунд. "
        "Начни с цепляющей сцены, потом глава, глава раз в 4–6 кадров."
    )


def build_outline_prompt(topic: str, language: str, brief: str = "", angle: str = "") -> str:
    return (
        f"Ты продюсер документального YouTube-ролика длиной {LONG_MINUTES:g} минут (жёстко не больше {MAX_MINUTES}).\n"
        + _brief_block(brief)
        + f"Тема: {topic}\n"
        f"Язык ролика: {_language_name(language)}.\n"
        + (f"Способ рассказа в этот раз: {angle}.\n" if angle else "")
        + f"Составь план из {LONG_CHAPTERS} глав. Удержание решают первые 2 минуты, поэтому:\n"
        "- глава 0 — холодное начало на 45 секунд, без номера: 20 секунд хука (самый эффектный факт истории и "
        "подтверждение того, что обещает название, без предыстории) и 25 секунд открытой петли (что зритель увидит "
        "дальше и почему досмотреть: «by the end you'll know...»). Хук только дразнит главный момент, не раскрывает "
        "ни причину, ни развязку;\n"
        "- глава 1 — сжатый контекст, не больше 75 секунд;\n"
        "- дальше главы по 60–90 секунд, каждая начинается с нового поворота, цифры или вопроса;\n"
        "- главный момент истории (тот, что дразнит хук) раскрывается в первой трети ролика: key_moment — номер этой главы;\n"
        "- в середине ролика (40–60% длины) обязателен дополнительный поворот, о котором зритель не догадывался: "
        "twist — номер этой главы;\n"
        "- последняя глава — что место сейчас.\n"
        "Для каждой главы напиши summary: что происходит, конкретные факты с именами, годами, суммами и числами, "
        "и открытый вопрос или неожиданный поворот в конце, который закроется только в следующих главах.\n"
        "Через всю историю проведи 1–3 конкретных реальных человека с задокументированной судьбой: житель, рабочий, "
        "владелец, инвестор, чиновник, турист. Что они делали, что потеряли, что с ними стало. Зритель следит за "
        "людьми, а не за зданиями. Только реальные, проверяемые люди и эпизоды, никаких выдуманных героев; "
        "если имени нет в источниках, называй по роли («один из последних жителей»).\n"
        "place — где это: подпись для карты (место и страна заглавными на английском) и координаты.\n"
        "Название: узнаваемое место в названии (не «This Town»), настоящее время и текущие ставки "
        "(«Is Sinking», «Almost Nobody Lives There»), никогда «Why Did ... So Fast?».\n"
        "Верни только JSON:\n"
        '{"title": "название ролика на языке ролика, до 70 символов", '
        '"music": "mood and genre of background music in English, instrumental", '
        '"place": {"label": "TIANDUCHENG, CHINA", "lat": 30.37, "lon": 120.22}, '
        '"key_moment": 2, "twist": 4, '
        '"chapters": [{"title": "короткое название главы на языке ролика", "summary": "..."}]}'
    )


def build_chapter_prompt(
    topic: str,
    language: str,
    brief: str,
    outline: dict[str, Any],
    index: int,
    previous: list[str],
    words: int = 240,
    used: dict[str, int] | None = None,
    related: str = "",
) -> str:
    chapters = outline.get("chapters") or []
    plan = "\n".join(
        f"{number}. {chapter.get('title', '')}: {chapter.get('summary', '')}"
        for number, chapter in enumerate(chapters)
    )
    chapter = chapters[index]
    if index == 0:
        opening = (
            "Это холодное начало на 45 секунд, без плашки главы.\n"
            "Хук, первые 20 секунд (около 50 слов): короткие фразы по 4–7 слов, кадр меняется каждые 2 секунды. "
            "Первая фраза — самый эффектный конкретный факт истории (цифра, дата, контраст «было — стало»), без "
            "предыстории, пейзажа и атмосферы; дальше — подтверждение того, что обещает название, и намёк на главный "
            "момент без раскрытия. Пометь эти сегменты marks [\"HOOK\"].\n"
            "Открытая петля, следующие 25 секунд (около 60 слов): что зритель увидит дальше и почему досмотреть "
            "(«by the end of this video you'll know...»). Пометь их marks [\"OPEN_LOOP\"]. "
            "Не раскрывай причину и развязку."
        )
    else:
        opening = (
            f'Первый сегмент — kind chapter, screen "{index}. {str(chapter.get("title", "")).upper()}". '
            "Сразу после плашки — фраза с новым поворотом, цифрой или вопросом, никаких «чтобы понять, вернёмся назад» "
            "и сухой справки. Закончи главу открытым вопросом или намёком на поворот в следующей главе. "
            "В главе 1–2 графические вставки (number или инфографика): каждые 60–90 секунд зрителю нужна смена картинки."
        )
        if index == 1:
            opening += " Это сжатый контекст: только то, без чего не понять историю, не больше 75 секунд."
    if index == _chapter_number(outline.get("key_moment")):
        opening += (
            " В этой главе раскрывается главный момент истории, который дразнил хук: пометь фразу с раскрытием "
            "marks [\"KEY_MOMENT\"] и дай ей самую сильную картинку — реальное место, «было/стало»."
        )
    if index == _chapter_number(outline.get("twist")):
        opening += (
            " В этой главе дополнительный поворот, которого зритель не ждал: пометь первую фразу поворота marks [\"TWIST\"]."
        )
    if index == len(chapters) - 1:
        opening += (
            " Последние сегменты главы (около 20 секунд) пометь marks [\"END_CTA\"]: конкретный вопрос зрителю по теме "
            "(«would you have stayed?», «which place should we visit next?») "
            + (f"и подводка к ролику канала «{related}», который будет на экране. " if related else "")
            + "Без плотного текста на экране."
        )
    opening += (
        " Если в плане у этой главы есть конкретные люди, расскажи их историю: что они видели, решали и теряли — "
        "через них зритель переживает события. В кадре люди только со спины, силуэтом или издалека, без узнаваемых лиц."
    )
    tail = " ".join(previous[-3:])
    return (
        "Ты пишешь раскадровку одной главы документального YouTube-ролика. Почти каждый кадр — реалистичная "
        "иллюстрация, которая буквально показывает то, что говорит диктор.\n"
        + _brief_block(brief)
        + f"Тема ролика: {topic}\n"
        f"План всего ролика (глава 0 — холодное начало):\n{plan}\n"
        f"Сейчас пиши только главу {index}: {chapter.get('title', '')}. {chapter.get('summary', '')}\n"
        + (f"Конец предыдущей главы, продолжай с этого места: «{tail}»\n" if tail else "")
        + f"{opening}\n"
        "Верни только JSON: {\"segments\": [ ... ]}\n"
        + _shot_rules(language, long=True)
        + _usage_note(used or {})
        + f"В этой главе {max(8, words // 13)}–{max(10, words // 10)} сегментов и всего около {words} слов речи — "
        "это жёсткий лимит хронометража, не превышай его. Не повторяй факты из других глав."
    )


def _chapter_number(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


CTA = {
    "en": (
        "If stories like this are your kind of thing, subscribe to {name}. There are many more coming.",
        "The next story is on your screen right now. Subscribe to {name}, and I will see you there.",
    ),
    "ru": (
        "Если вам интересны такие истории, подпишитесь на {name}. Впереди ещё много.",
        "Следующая история уже на экране. Подписывайтесь на {name}, увидимся там.",
    ),
}


def channel_name(brief: str) -> str:
    first = brief.strip().splitlines()[0] if brief.strip() else ""
    return first.split(" — ")[0].strip() if " — " in first else "this channel"


def add_calls_to_action(segments: list[dict[str, Any]], language: str, brief: str) -> None:
    """A short subscribe line once viewers are hooked (after chapter 2) and a closing line over a calm background
    with no text: the last 20 seconds belong to the YouTube end screen."""
    name = channel_name(brief)
    middle, closing = (line.format(name=name) for line in CTA.get(language, CTA["en"]))

    def card(text: str, kind: str) -> dict[str, Any]:
        return {"narration": text, "kind": kind, "screen": name, "note": "", "steps": [], "icons": [],
                "image_prompt": "", "visual_intent": "", "asset": None}

    chapters = [index for index, segment in enumerate(segments) if segment["kind"] == "chapter"]
    if len(chapters) >= 3:
        segments.insert(chapters[2], card(middle, "subscribe"))
    segments.append(card(closing, "endscreen"))
    segments[-1]["marks"] = ["END_CTA"]
    width = 2 if len(segments) < 100 else 3
    for index, segment in enumerate(segments, start=1):
        segment["id"] = f"s{index:0{width}d}"


GRAPHIC_LIMIT = 2
GRAPHIC_GAP = 3
CARD_KINDS = {*GRAPHIC_KINDS, "number", "term", "diagram"}


def _graphic_usage(raw: list[dict[str, Any]]) -> dict[str, int]:
    used: dict[str, int] = {}
    for item in raw:
        kind = str(item.get("kind") or "")
        if kind in GRAPHIC_KINDS:
            used[kind] = used.get(kind, 0) + 1
    return used


def _usage_note(used: dict[str, int]) -> str:
    if not used:
        return ""
    counts = ", ".join(f"{kind} ×{count}" for kind, count in sorted(used.items()))
    spent = [kind for kind, count in used.items() if count >= GRAPHIC_LIMIT]
    fresh = [kind for kind in GRAPHIC_KINDS if kind not in used]
    return (
        f"Инфографика, уже использованная в прошлых главах: {counts}. "
        + (f"Больше не используй: {', '.join(spent)}. " if spent else "")
        + (f"Ещё не было, предпочитай их: {', '.join(fresh)}.\n" if fresh else "\n")
    )


def space_out_graphics(segments: list[dict[str, Any]]) -> int:
    """Hard rule after the script is written: every infographic type at most GRAPHIC_LIMIT times, and at least
    GRAPHIC_GAP scenes between any two cards. A card that breaks it becomes a scene for the same line."""
    used: dict[str, int] = {}
    since = GRAPHIC_GAP
    changed = 0
    for segment in segments:
        kind = segment["kind"]
        if kind not in CARD_KINDS:
            since = since + 1 if kind == "scene" else since
            continue
        over = kind in GRAPHIC_KINDS and used.get(kind, 0) >= GRAPHIC_LIMIT
        if over or since < GRAPHIC_GAP:
            segment.update(kind="scene", image_prompt=f"Documentary shot that shows: {segment['narration']}", steps=[], icons=[])
            segment.pop("data", None)
            changed += 1
            since += 1
            continue
        if kind in GRAPHIC_KINDS:
            used[kind] = used.get(kind, 0) + 1
        since = 0
    return changed


def music_query(data: dict[str, Any]) -> str:
    music = " ".join(str(data.get("music") or "").split())[:200]
    return music or "calm cinematic instrumental background"


def generate_script(
    settings: Settings,
    topic: str,
    language: str,
    client: httpx.Client,
    length: str = "normal",
    brief: str = "",
    log: Callable[[str], None] = lambda _message: None,
    related: str = "",
    angle: str = "",
) -> tuple[str, list[dict[str, Any]], str]:
    if length != "long":
        data = ask_json(settings, build_prompt(topic, language, length, brief), client, "segments")
        title, segments = normalize_segments(data)
        space_out_graphics(segments)
        return title, segments, music_query(data)
    outline = ask_json(settings, build_outline_prompt(topic, language, brief, angle), client, "chapters")
    chapters = [chapter for chapter in outline.get("chapters") or [] if isinstance(chapter, dict)]
    if not chapters:
        raise PipelineError("в плане ролика нет глав")
    outline["chapters"] = chapters
    log(f"сценарий  план: {len(chapters)} глав, «{outline.get('title', '')}», подача: {angle or 'свободная'}")
    rate = WORDS_PER_MINUTE.get(language, 150)
    total_words = int(LONG_MINUTES * rate)
    parts: list[list[dict[str, Any]]] = []
    for index, chapter in enumerate(chapters):
        words = chapter_words(index, len(chapters), total_words)
        raw = [item for part in parts for item in part]
        previous = [str(item.get("narration") or "") for item in raw]
        prompt = build_chapter_prompt(topic, language, brief, outline, index, previous, words, _graphic_usage(raw), related)
        part = _segments(ask_json(settings, prompt, client, "segments"))
        if _word_count(part) > words * 1.2:
            log(f"сценарий  глава {index} длинная ({_word_count(part)} слов из {words}), сокращаю")
            part = _segments(ask_json(settings, build_shorten_prompt(part, words), client, "segments")) or part
        parts.append(part)
        log(f"сценарий  глава {index}: {chapter.get('title', '')}, {len(part)} кадров, {_word_count(part)} слов")
    limit = int(MAX_MINUTES * rate)
    while sum(_word_count(part) for part in parts) > limit:
        longest = max(range(1, len(parts)), key=lambda number: _word_count(parts[number]))
        target = int(_word_count(parts[longest]) * 0.85)
        log(f"сценарий  ролик длиннее {MAX_MINUTES} мин, сокращаю главу {longest} до {target} слов")
        shorter = _segments(ask_json(settings, build_shorten_prompt(parts[longest], target), client, "segments"))
        if not shorter or _word_count(shorter) >= _word_count(parts[longest]):
            break
        parts[longest] = shorter
    title, segments = normalize_segments({"title": outline.get("title"), "segments": [item for part in parts for item in part]})
    changed = space_out_graphics(segments)
    if changed:
        log(f"сценарий  {changed} графических кадров стали сценами: повтор шаблона или слишком близко к другой вставке")
    ensure_marks(segments)
    if place_map(segments, outline.get("place")):
        log(f"сценарий  карта с локацией: {outline['place'].get('label')}")
    add_calls_to_action(segments, language, brief)
    return title, segments, music_query(outline)


def chapter_words(index: int, chapters: int, total: int) -> int:
    """Cold open 45 s, context at most 75 s, the rest split evenly into 60-90 s blocks."""
    if index == 0:
        return HOOK_WORDS
    if index == 1:
        return CONTEXT_WORDS
    return int((total - HOOK_WORDS - CONTEXT_WORDS) / max(1, chapters - 2))


HOOK_SPOKEN = 50


def ensure_marks(segments: list[dict[str, Any]]) -> None:
    """The cold open is always marked even if the model forgot: about 20 s of HOOK, the rest of it OPEN_LOOP."""
    if any("HOOK" in (segment.get("marks") or []) for segment in segments):
        return
    words = 0
    for segment in segments:
        if segment["kind"] == "chapter":
            break
        mark = "HOOK" if words < HOOK_SPOKEN else "OPEN_LOOP"
        segment["marks"] = [mark, *[m for m in segment.get("marks") or [] if m not in {"HOOK", "OPEN_LOOP"}]]
        words += len(segment["narration"].split())


MAP_BEFORE_WORDS = 70


def place_map(segments: list[dict[str, Any]], place: Any) -> bool:
    """Within the first 30 seconds the viewer must see where we are: the first open-loop line (or the first line
    that names the place) becomes an animated map. The hook keeps its strongest pictures."""
    if not isinstance(place, dict) or _number(place.get("lat")) is None or _number(place.get("lon")) is None:
        return False
    label = _text(place.get("label"), 40).upper()
    name = label.split(",")[0].strip().casefold()
    words = 0
    choice = None
    for segment in segments:
        if words > MAP_BEFORE_WORDS or segment["kind"] == "chapter":
            break
        marks = segment.get("marks") or []
        if segment["kind"] == "scene" and "KEY_MOMENT" not in marks:
            if "OPEN_LOOP" in marks or (name and name in segment["narration"].casefold() and "HOOK" not in marks):
                choice = segment
                break
        words += len(segment["narration"].split())
    if choice is None:
        return False
    choice.update(kind="map", image_prompt="", data={"label": label, "lat": _number(place["lat"]), "lon": _number(place["lon"])})
    return True


def build_shorten_prompt(part: list[dict[str, Any]], words: int) -> str:
    return (
        f"Вот сегменты главы документального ролика. Сократи суммарную речь (narration) до {words} слов: "
        "укороти фразы и убери наименее важные сегменты. Сохрани порядок, первый сегмент, все поля сегментов "
        "(kind, screen, note, steps, icons, image_prompt) и самые сильные факты.\n"
        "Верни только JSON: {\"segments\": [ ... ]}\n"
        + json.dumps({"segments": part}, ensure_ascii=False)
    )


def _segments(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in data.get("segments") or [] if isinstance(item, dict)]


def _word_count(part: list[dict[str, Any]]) -> int:
    return sum(len(str(item.get("narration") or "").split()) for item in part)


def ask_json(settings: Settings, prompt: str, client: httpx.Client, key: str) -> dict[str, Any]:
    prompt += "\nОтветь только JSON."
    failure: PipelineError | None = None
    for _attempt in range(3):
        if settings.script_provider == "claude":
            text = _generate_with_claude(settings, prompt, client)
        else:
            text = _generate_with_gemini(settings, prompt, client)
        try:
            data = extract_json(text)
        except PipelineError as exc:
            failure = exc
            continue
        if data.get(key):
            return data
        failure = PipelineError(f"в ответе нет {key}")
    raise failure or PipelineError("сценарий не получился")


def _generate_with_gemini(settings: Settings, prompt: str, client: httpx.Client) -> str:
    if not settings.gemini_api_key:
        raise PipelineError("не задан GEMINI_API_KEY")
    response = None
    for pause in (0, 10, 30, 60, 90):
        if pause:
            time.sleep(pause)
        try:
            response = client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{settings.gemini_text_model}:generateContent",
                headers={"x-goog-api-key": settings.gemini_api_key, "content-type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9, "maxOutputTokens": 32768},
                },
            )
        except httpx.TransportError:
            continue
        if response.status_code not in {429, 500, 502, 503, 504}:
            break
    if response is None:
        raise PipelineError("Gemini не отвечает: сеть оборвалась несколько раз подряд")
    if response.status_code != 200:
        raise PipelineError(f"Gemini ответил {response.status_code}: {_error_text(response)}")
    payload = response.json()
    usage.record("text", payload)
    parts = [
        part
        for candidate in payload.get("candidates") or []
        for part in (candidate.get("content") or {}).get("parts") or []
    ]
    text = "".join(str(part.get("text") or "") for part in parts if not part.get("thought"))
    if not text.strip():
        raise PipelineError("Gemini не вернул сценарий")
    return text


def _generate_with_claude(settings: Settings, prompt: str, client: httpx.Client) -> str:
    if not settings.anthropic_api_key:
        raise PipelineError("не задан ANTHROPIC_API_KEY")
    response = client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": settings.anthropic_model,
            "max_tokens": 8000,
            "messages": [{"role": "user", "content": prompt}],
        },
    )
    if response.status_code != 200:
        raise PipelineError(f"Claude ответил {response.status_code}: {_error_text(response)}")
    payload = response.json()
    parts = payload.get("content") or []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict))


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return response.text[:300]
