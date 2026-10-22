import tempfile
import unittest
from pathlib import Path

from yt_video import boards
from yt_video.boards import render_frame
from yt_video.errors import PipelineError
from yt_video.illustrate import image_bytes
from yt_video.resolve_edit import canvas_size
from yt_video.script import extract_json, narration_text, normalize_segments
from yt_video.voice import apply_slots, assign_times


def _frame_count(clip: Path) -> int:
    import re
    import subprocess

    from yt_video.ff import ffmpeg_exe

    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(clip), "-map", "0:v", "-c", "copy", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    return int(re.findall(r"frame=\s*(\d+)", result.stderr)[-1])


class CoreTests(unittest.TestCase):
    def test_extract_json_from_fence(self) -> None:
        text = 'пояснение\n```json\n{"title": "a", "segments": [{"narration": "Раз."}]}\n```'
        data = extract_json(text)
        self.assertEqual(data["title"], "a")

    def test_normalize_segments_assigns_ids(self) -> None:
        title, segments = normalize_segments(
            {
                "title": "Диск",
                "segments": [
                    {"narration": "Первая фраза.", "search_queries": ["ssd"]},
                    {"narration": "   "},
                    {"narration": "Вторая фраза.", "visual_intent": "circuit board"},
                ],
            }
        )
        self.assertEqual(title, "Диск")
        self.assertEqual([item["id"] for item in segments], ["s01", "s02"])
        self.assertEqual(segments[0]["kind"], "term")
        self.assertEqual(segments[0]["screen"], "Первая фраза")
        self.assertEqual(segments[1]["kind"], "scene")
        self.assertEqual(segments[1]["image_prompt"], "circuit board")

    def test_old_stock_kinds_become_scenes(self) -> None:
        _, segments = normalize_segments(
            {
                "title": "Док",
                "segments": [
                    {"narration": "Вот документ.", "kind": "proof", "visual_intent": "court document"},
                    {"narration": "Мышца как склад.", "kind": "metaphor", "image_prompt": "muscle as a warehouse"},
                    {"narration": "Без картинки.", "kind": "scene"},
                ],
            }
        )
        self.assertEqual([segment["kind"] for segment in segments], ["scene", "scene", "term"])
        self.assertEqual(segments[0]["image_prompt"], "court document")
        self.assertEqual(segments[2]["image_prompt"], "")

    def test_diagram_icons_match_steps(self) -> None:
        _, segments = normalize_segments(
            {
                "title": "Схема",
                "segments": [
                    {"narration": "Раз.", "kind": "diagram", "steps": ["мышца", "АТФ"], "icons": ["arm", "battery"]},
                    {"narration": "Два.", "kind": "diagram", "steps": ["мышца", "АТФ"], "icons": ["arm"]},
                ],
            }
        )
        self.assertEqual(segments[0]["icons"], ["arm", "battery"])
        self.assertEqual(segments[1]["icons"], [])

    def test_gemini_image_is_read_from_inline_data(self) -> None:
        payload = {"candidates": [{"content": {"parts": [{"text": "ok"}, {"inlineData": {"data": "aGk="}}]}}]}
        self.assertEqual(image_bytes(payload), b"hi")
        with self.assertRaises(PipelineError):
            image_bytes({"candidates": [{"finishReason": "SAFETY"}]})

    def test_scene_slowly_zooms(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            art = Path(folder) / "art.png"
            source = Image.new("RGB", (320, 180), (200, 40, 40))
            source.paste((40, 40, 200), (0, 0, 160, 180))
            source.paste((20, 200, 40), (200, 20, 260, 70))
            source.save(art)
            for motion in boards.SCENE_MOTIONS:
                segment = {"id": "s01", "kind": "scene", "art": str(art), "motion": motion}
                paint = boards._painter(segment, 320, 180, 30)
                first, last = paint(1, 30), paint(30, 30)
                self.assertEqual(first.size, (320, 180))
                self.assertNotEqual(first.tobytes(), last.tobytes(), motion)

    def test_neighbouring_shots_get_different_moves(self) -> None:
        segments = [{"kind": kind} for kind in ("scene", "chapter", "scene", "scene", "scene", "number", "diagram", "scene")]
        boards.plan_motion(segments)
        self.assertEqual(segments[0]["transition"], "cut")
        self.assertEqual(segments[0]["motion"], "push")
        self.assertTrue(all(segment["transition"] == "dissolve" for segment in segments[1:]))
        scenes = [segment["motion"] for segment in segments if segment["kind"] == "scene"]
        self.assertTrue(all(a != b for a, b in zip(scenes, scenes[1:])))
        self.assertTrue(all(segment["transition"] in boards.TRANSITION_FRAMES for segment in segments))

    def test_every_shot_kind_renders_through_its_motion(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            shots = [
                {"id": "s01", "kind": "chapter", "screen": "1. ГЛАВА"},
                {"id": "s02", "kind": "number", "screen": "в 16 раз", "note": "сильнее"},
                {"id": "s03", "kind": "term", "screen": "Рэлей", "note": "рассеяние"},
                {"id": "s04", "kind": "diagram", "screen": "Схема", "steps": ["a", "b", "c"]},
                {"id": "s05", "kind": "scene", "screen": "без картинки"},
            ]
            for index, shot in enumerate(shots):
                shot["slot_start"], shot["slot_end"] = index, index + 1.0
            boards.prepare_boards(Path(folder), shots, "16:9", lambda _message: None)
            for shot in shots:
                self.assertEqual(shot["asset_kind"], "clip")
                self.assertEqual(_frame_count(Path(shot["asset_path"])), shot["frames"])

    def test_infographics_keep_clean_data_or_fall_back(self) -> None:
        _, segments = normalize_segments(
            {
                "segments": [
                    {"narration": "a", "kind": "timeline", "screen": "T", "points": [{"year": "1905", "label": "x"}, {"year": "1958", "label": "y"}, {"year": "2024", "label": "z"}]},
                    {"narration": "b", "kind": "bars", "items": [{"label": "then", "value": "500,000"}, {"label": "now", "value": 230}]},
                    {"narration": "c", "kind": "ring", "percent": 140},
                    {"narration": "d", "kind": "before_after", "before": "$3,000", "after": "$100"},
                    {"narration": "e", "kind": "scene", "image_prompt": "beach", "archival": True, "compare_prompt": "dead beach"},
                ]
            }
        )
        self.assertEqual(segments[0]["data"]["points"][0], ("1905", "x"))
        self.assertEqual(segments[1]["data"]["items"][0][1], 500000)
        self.assertNotEqual(segments[2]["kind"], "ring")
        self.assertEqual(segments[3]["kind"], "before_after")
        self.assertTrue(segments[4]["archival"])
        self.assertEqual(segments[4]["compare_prompt"], "dead beach")

    def test_subscribe_calls_sit_after_chapter_two_and_at_the_end(self) -> None:
        from yt_video.script import add_calls_to_action

        kinds = ["scene", "chapter", "scene", "chapter", "scene", "chapter", "scene"]
        segments = [{"narration": f"line {i}", "kind": kind} for i, kind in enumerate(kinds)]
        add_calls_to_action(segments, "en", "After the Crowd — documentary channel")
        self.assertEqual([s["kind"] for s in segments][4:7], ["scene", "subscribe", "chapter"])
        self.assertEqual(segments[-1]["kind"], "endscreen")
        self.assertEqual(segments[-1]["marks"], ["END_CTA"])
        self.assertIn("After the Crowd", segments[-1]["narration"])
        self.assertEqual(segments[0]["id"], "s01")

    def test_cold_open_gets_hook_and_open_loop_marks(self) -> None:
        from yt_video.script import ensure_marks, place_map

        lines = ["word " * 25, "word " * 25, "By the end you will know who paid for Tianducheng"]
        segments = [{"narration": text.strip(), "kind": "scene", "image_prompt": "p"} for text in lines]
        segments.append({"narration": "x", "kind": "chapter"})
        ensure_marks(segments)
        self.assertEqual([s["marks"][0] for s in segments[:3]], ["HOOK", "HOOK", "OPEN_LOOP"])
        self.assertNotIn("marks", segments[3])
        self.assertTrue(place_map(segments, {"label": "Tianducheng, China", "lat": 30.37, "lon": 120.22}))
        self.assertEqual(segments[2]["kind"], "map")
        self.assertEqual(segments[2]["data"]["label"], "TIANDUCHENG, CHINA")
        self.assertFalse(place_map(segments, {"label": "x"}))

    def test_chapter_budget_fits_target_length(self) -> None:
        from yt_video.script import LONG_MINUTES, MAX_MINUTES, chapter_words

        total = int(LONG_MINUTES * 150)
        words = [chapter_words(index, 8, total) for index in range(8)]
        self.assertEqual(sum(words), total - (total - 110 - 170) % 6)
        self.assertLessEqual(words[1], 75 * 150 / 60)
        self.assertTrue(all(150 <= w <= 225 for w in words[2:]))
        self.assertLessEqual(total, MAX_MINUTES * 150)

    def test_pacing_speeds_hook_and_adds_pauses(self) -> None:
        from yt_video.voice import KEY_PAUSE, map_paced, plan_pacing

        segments = [
            {"start_sec": 0.0, "end_sec": 2.0, "marks": ["HOOK"]},
            {"start_sec": 2.1, "end_sec": 5.0},
            {"start_sec": 5.1, "end_sec": 8.0, "marks": ["TWIST"]},
            {"start_sec": 8.6, "end_sec": 10.0, "kind": "number"},
        ]
        pieces = plan_pacing(segments, 10.0)
        self.assertEqual(sum(1 for piece in pieces if piece[3]), 1)
        self.assertLess(map_paced(2.0, pieces), 2.0)
        before, after = map_paced(5.0, pieces), map_paced(5.1, pieces)
        self.assertAlmostEqual(after - before, KEY_PAUSE, places=2)

    def test_music_changes_after_hook_on_twists_and_hushes_key_moment(self) -> None:
        from yt_video.sound import SECTION_LONGEST, hush_windows, music_sections

        segments = [{"slot_start": 0.0, "slot_end": 20.0, "marks": ["HOOK"]}]
        segments += [{"slot_start": float(t), "slot_end": float(t + 60), "kind": "chapter"} for t in range(20, 560, 60)]
        segments[5]["marks"] = ["TWIST"]
        segments[3]["marks"] = ["KEY_MOMENT"]
        sections = music_sections(segments, 600.0)
        self.assertEqual(sections[0], (0.0, 20.0))
        self.assertIn(segments[5]["slot_start"], [start for start, _end in sections])
        self.assertTrue(all(end - start <= SECTION_LONGEST for start, end in sections))
        self.assertEqual(hush_windows(segments), [(140 - 1.2, 140.2)])

    def test_google_images_pairs_are_parsed_and_filtered(self) -> None:
        from yt_video.websources import google_candidates

        html = (
            '["https://encrypted-tbn0.gstatic.com/images?q\\u003dtbn:A",194,259],["https://a.com/x.jpg?w\\u003d2000",1500,2000]'
            '["https://encrypted-tbn0.gstatic.com/images?q\\u003dtbn:B",194,259],["https://a.com/small.jpg",300,400]'
            '["https://encrypted-tbn0.gstatic.com/images?q\\u003dtbn:C",194,259],["https://c8.alamy.com/y.jpg",1500,2000]'
        )
        found = google_candidates(html)
        self.assertEqual([c.file_url for c in found], ["https://a.com/x.jpg?w=2000"])
        self.assertEqual(found[0].preview, "https://encrypted-tbn0.gstatic.com/images?q=tbn:A")
        self.assertEqual(found[0].source, "google")

    def test_shortlist_caps_previews_and_keeps_place_photos_first(self) -> None:
        from yt_video.stock import MAX_PREVIEWS, Candidate, shortlist

        def make(kind: str, n: int, source: str = "pexels") -> list[Candidate]:
            return [Candidate(kind, i, "p", f"https://x/{source}{kind}{i}.jpg", 0, "", "", "", source=source) for i in range(n)]

        picked = shortlist(make("photo", 6, "wikimedia"), make("video", 5) + make("photo", 5))
        self.assertEqual(len(picked), MAX_PREVIEWS)
        self.assertEqual([c.source for c in picked[:3]], ["wikimedia"] * 3)
        self.assertEqual([c.kind for c in picked[3:]], ["video", "photo", "video", "photo", "video"])

    def test_usage_report_counts_tokens_and_cost(self) -> None:
        from yt_video import usage

        usage.reset()
        usage.record("text", {"usageMetadata": {"promptTokenCount": 1_000_000, "candidatesTokenCount": 0}})
        usage.record("image", {"usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 1_000_000}})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cost.txt"
            rub = usage.report(path, voice_chars=2000)
            text = path.read_text(encoding="utf-8")
        price_text, price_image = usage._prices("text")[0], usage._prices("image")[1]
        self.assertAlmostEqual(rub, (price_text + price_image) * float(__import__("os").environ.get("USD_RUB", "82")), places=3)
        self.assertIn("ElevenLabs: 2,000", text)
        usage.reset()

    def test_map_tile_math(self) -> None:
        from yt_video.maps import tile_xy

        self.assertEqual(tuple(round(v, 3) for v in tile_xy(0.0, 0.0, 1)), (1.0, 1.0))
        x, y = tile_xy(51.5, -0.12, 5)
        self.assertEqual((int(x), int(y)), (15, 10))

    def test_graphics_are_capped_and_spaced(self) -> None:
        from yt_video.script import space_out_graphics

        kinds = ["ring", "scene", "versus", "scene", "scene", "scene", "ring", "scene", "scene", "scene", "ring", "number"]
        segments = [{"narration": f"line {i}", "kind": kind, "data": {}} for i, kind in enumerate(kinds)]
        changed = space_out_graphics(segments)
        self.assertEqual(
            [segment["kind"] for segment in segments],
            ["ring", "scene", "scene", "scene", "scene", "scene", "ring", "scene", "scene", "scene", "scene", "number"],
        )
        self.assertEqual(changed, 2)
        self.assertNotIn("data", segments[2])
        self.assertIn("line 2", segments[2]["image_prompt"])

    def test_every_infographic_and_special_move_renders(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as folder:
            art = Path(folder) / "a.png"
            Image.new("RGB", (320, 180), (90, 120, 160)).save(art)
            shots = [
                {"id": "s01", "kind": "chapter", "screen": "1. ГЛАВА"},
                {"id": "s02", "kind": "scene", "art": str(art)},
                {"id": "s03", "kind": "scene", "art": str(art), "archival": True},
                {"id": "s04", "kind": "scene", "art": str(art), "art_b": str(art)},
                {"id": "s05", "kind": "timeline", "data": {"title": "T", "points": [("1905", "a"), ("1958", "b"), ("2024", "c")]}},
                {"id": "s06", "kind": "bars", "data": {"title": "B", "items": [("x", 500000, ""), ("y", 230, "")]}},
                {"id": "s07", "kind": "before_after", "data": {"before": "$3", "after": "$1", "note": "n"}},
                {"id": "s08", "kind": "ring", "data": {"percent": 98, "note": "n"}},
                {"id": "s09", "kind": "chart", "data": {"title": "C", "points": [("1", 1), ("2", 3), ("3", 2), ("4", 0)]}},
                {"id": "s10", "kind": "versus", "data": {"left": ("THEN", ["a", "b"]), "right": ("NOW", ["c", "d"])}},
                {"id": "s11", "kind": "stats", "data": {"title": "S", "stats": [("1", "a"), ("2", "b"), ("3", "c"), ("4", "d")]}},
                {"id": "s12", "kind": "checklist", "data": {"title": "C", "items": [("a", True), ("b", False), ("c", False)]}},
            ]
            for index, shot in enumerate(shots):
                shot["slot_start"], shot["slot_end"] = index * 0.5, index * 0.5 + 0.5
            boards.prepare_boards(Path(folder), shots, "16:9", lambda _message: None)
            self.assertEqual([shot["motion"] for shot in shots[1:4]], ["rack_focus", "photo_drop", "split"])
            for shot in shots:
                self.assertEqual(_frame_count(Path(shot["asset_path"])), shot["frames"], shot["id"])

    def test_long_narration_is_split_on_phrase_boundaries(self) -> None:
        from yt_video.voice import chunk_segments

        segments = [{"narration": "x" * 900} for _ in range(7)]
        chunks = chunk_segments(segments, limit=2000)
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 2, 1])
        self.assertEqual(sum(chunks, []), segments)

    def test_chapter_prompt_carries_the_plan_and_the_channel(self) -> None:
        from yt_video.script import build_chapter_prompt

        outline = {"chapters": [{"title": "Cold open", "summary": "a"}, {"title": "The Dream", "summary": "b"}]}
        prompt = build_chapter_prompt("Salton Sea", "en", "After the Crowd brief", outline, 1, ["last words."])
        self.assertIn('"1. THE DREAM"', prompt)
        self.assertIn("After the Crowd brief", prompt)
        self.assertIn("last words.", prompt)

    def test_number_counts_up(self) -> None:
        match = boards._count_target("в 16 раз")
        self.assertEqual(boards._counted("в 16 раз", match, 0.0), "в 0 раз")
        self.assertEqual(boards._counted("в 16 раз", match, 1.0), "в 16 раз")
        self.assertEqual(boards._counted("2,5 кг", boards._count_target("2,5 кг"), 1.0), "2,5 кг")
        self.assertIsNone(boards._count_target("5–6 часов"))

    def test_long_pauses_shrink(self) -> None:
        from yt_video.voice import TEMPO, map_time, speech_intervals

        text = "аб вг"
        starts = [0.0, 0.1, 0.2, 1.2, 1.3]
        alignment = {
            "characters": list(text),
            "character_start_times_seconds": starts,
            "character_end_times_seconds": [start + 0.1 for start in starts],
        }
        keep = speech_intervals(alignment)
        self.assertEqual(len(keep), 2)
        gap = map_time(1.2, keep) - map_time(0.2, keep)
        self.assertLess(gap, 0.3)
        self.assertAlmostEqual(map_time(0.1, keep), 0.1 / TEMPO, places=3)

    def test_sfx_follow_the_pops(self) -> None:
        from yt_video.sound import plan_sfx

        with tempfile.TemporaryDirectory() as folder:
            library = Path(folder)
            for category in ("appear", "highlight", "line_draw", "transition"):
                (library / category).mkdir()
                (library / category / "a.wav").write_bytes(b"x")
            segments = [
                {"kind": "chapter", "slot_start": 0, "slot_end": 2, "transition": "cut"},
                {"kind": "scene", "slot_start": 2, "slot_end": 5, "transition": "dissolve"},
                {"kind": "diagram", "steps": ["a", "b"], "slot_start": 5, "slot_end": 9, "transition": "cut"},
            ]
            planned = plan_sfx(segments, library)
        self.assertEqual(
            [(at, path.parent.name) for at, path, _gain, _longest in planned],
            [(0.0, "appear"), (5.16, "appear"), (6.08, "line_draw"), (6.56, "appear")],
        )

    def test_number_sits_on_the_previous_scene(self) -> None:
        segments = [
            {"kind": "number"},
            {"kind": "scene", "art": "a.png"},
            {"kind": "term"},
            {"kind": "scene", "art": "b.png"},
            {"kind": "number"},
        ]
        boards._assign_backdrops(segments)
        self.assertEqual([segment.get("backdrop") for segment in segments], ["a.png", None, "a.png", None, "b.png"])

    def test_assign_times_uses_alignment(self) -> None:
        segments = [{"narration": "Раз два."}, {"narration": "Три."}]
        full = narration_text(segments)
        alignment = {
            "characters": list(full),
            "character_start_times_seconds": [index * 0.1 for index in range(len(full))],
            "character_end_times_seconds": [(index + 1) * 0.1 for index in range(len(full))],
        }
        assign_times(segments, full, alignment, 10)
        apply_slots(segments, 10)
        self.assertEqual(segments[0]["slot_start"], 0)
        self.assertEqual(segments[1]["slot_start"], segments[1]["start_sec"])
        self.assertEqual(segments[-1]["slot_end"], 10)
        self.assertLess(segments[0]["slot_end"], segments[1]["slot_start"] + 0.001)

    def test_assign_times_falls_back_without_alignment(self) -> None:
        segments = [{"narration": "аааа"}, {"narration": "бб"}]
        assign_times(segments, narration_text(segments), None, 6)
        apply_slots(segments, 6)
        self.assertEqual(segments[0]["slot_start"], 0)
        self.assertEqual(segments[-1]["slot_end"], 6)

    def test_inserts_are_short_and_not_the_spoken_line(self) -> None:
        _, segments = normalize_segments(
            {
                "title": "Год",
                "segments": [
                    {
                        "narration": "Весной пройдут выборы.",
                        "inserts": [
                            {"text": "Весной пройдут выборы.", "place": "center"},
                            {"text": "2026", "place": "upper_right"},
                            {"text": "14%", "place": "upper_right"},
                            {"text": "это уже целая подпись на весь низ кадра и она слишком длинная", "place": "bottom"},
                            {"text": "март", "place": "left"},
                        ],
                    }
                ],
            }
        )
        self.assertEqual(segments[0]["kind"], "number")
        self.assertEqual(segments[0]["screen"], "2026")

    def test_missing_inserts_are_taken_from_the_phrase(self) -> None:
        _, segments = normalize_segments(
            {
                "title": "Год",
                "segments": [
                    {"narration": "В марте 2026 года по всей России пройдут президентские выборы."},
                    {"narration": "Участки откроются с восьми утра."},
                    {"narration": "Голосование идёт через портал «Госуслуги»."},
                ],
            }
        )
        self.assertEqual(segments[0]["screen"], "2026")
        self.assertEqual(segments[0]["kind"], "number")
        self.assertEqual(segments[1]["screen"], "08:00")
        self.assertEqual(segments[2]["screen"], "Госуслуги")
        self.assertEqual(segments[2]["kind"], "term")

    def test_ordinary_words_do_not_get_a_card(self) -> None:
        _, segments = normalize_segments(
            {
                "title": "Участок",
                "segments": [
                    {"narration": "Избиратель опускает бюллетень в урну."},
                    {"narration": "Комиссия следит за порядком на участке."},
                ],
            }
        )
        self.assertEqual(segments[0]["kind"], "term")
        self.assertEqual(segments[0]["screen"], "Избиратель опускает бюллетень в")

    def test_chapter_card_is_yellow(self) -> None:
        image = render_frame({"kind": "chapter", "screen": "1. КАК ЭТО РАБОТАЕТ"}, 640, 360)
        red, green, blue = image.getpixel((320, 180))
        self.assertGreater(red, 200)
        self.assertGreater(green, 160)
        self.assertLess(blue, 80)

    def test_number_fills_the_frame(self) -> None:
        image = render_frame({"kind": "number", "screen": "15%", "note": "ставка"}, 640, 360)
        seen = [
            image.getpixel((x, y))
            for x in range(180, 460)
            for y in range(100, 250)
        ]
        self.assertTrue(any(pixel[0] > 240 and pixel[1] > 240 and pixel[2] > 240 for pixel in seen))

    def test_canvas(self) -> None:
        self.assertEqual(canvas_size("16:9"), (1920, 1080))
        self.assertEqual(canvas_size("9:16"), (1080, 1920))


if __name__ == "__main__":
    unittest.main()
