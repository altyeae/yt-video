import unittest
from pathlib import Path

from yt_video.errors import PipelineError
from yt_video.resolve_edit import (
    build_xmeml,
    connect_resolve,
    resolve_lib_path,
    source_window,
    timecode_frames,
)


class TimelineTests(unittest.TestCase):
    def test_xml_places_still_and_trims_video(self) -> None:
        segments = [
            {
                "id": "s01",
                "slot_start": 0,
                "slot_end": 4,
                "asset_path": "C:/tmp/s01.jpg",
                "asset_kind": "image",
            },
            {
                "id": "s02",
                "slot_start": 4,
                "slot_end": 9,
                "asset_path": "C:/tmp/s02.mp4",
                "asset_kind": "video",
                "asset_duration": 20,
            },
            {"id": "s03", "slot_start": 9, "slot_end": 10},
        ]
        notes: list[str] = []
        xml = build_xmeml(segments, Path("C:/tmp/audio/vo.mp3"), "16:9", 10, notes.append)
        self.assertIn("<start>0</start>", xml)
        self.assertIn("<end>120</end>", xml)
        self.assertIn("<in>225</in>", xml)
        self.assertIn("<out>375</out>", xml)
        self.assertIn("file://localhost/C:/tmp/s01.jpg", xml)
        self.assertIn("clip-voice", xml)
        self.assertNotIn("clip-s03", xml)
        self.assertTrue(any("пропуск" in note for note in notes))

    def test_record_frame_uses_timeline_start_and_source_rate(self) -> None:
        self.assertEqual(timecode_frames("01:00:00:00"), 108000)
        self.assertEqual(timecode_frames("01:00:00:10"), 108010)
        source_in, source_out = source_window(933, 50, 6.397)
        self.assertEqual((source_in, source_out), (306, 625))

    def test_connect_reports_missing_install(self) -> None:
        if resolve_lib_path().exists():
            self.skipTest("Resolve установлен")
        with self.assertRaises(PipelineError) as caught:
            connect_resolve()
        self.assertIn("Resolve", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
