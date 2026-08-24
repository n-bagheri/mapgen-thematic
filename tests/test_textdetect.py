import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from mapgen.textdetect import (
    _cache_matches,
    _text_input_signature,
    TextItem,
    TextKind,
    TextDetections,
    detect_text,
    extract_strokes,
    fuse_detections,
)


class WedgedTileSalvageTests(unittest.TestCase):
    """A tile whose decode wedges (EmptyModelResponse) is quartered once; a
    quadrant that still wedges is surrendered to the EasyOCR fusion instead of
    failing the whole step."""

    @staticmethod
    def _semantics():
        from types import SimpleNamespace
        return SimpleNamespace(
            subject="test map",
            overlay_text=SimpleNamespace(
                has_city_labels=True, has_region_labels=False,
                has_line_labels=False, notes="", capital_city=None),
            lines=[])

    def test_wedged_tile_is_quartered_and_offsets_are_kept(self):
        from mapgen.semantics import EmptyModelResponse
        quadrant_answers = [TextDetections(items=[TextItem(
            text=f"Q{index}", kind=TextKind.city, box_2d=[0, 0, 100, 100],
        )]) for index in range(4)]
        with patch("mapgen.textdetect._gemini_text",
                   side_effect=[EmptyModelResponse("wedged")] + quadrant_answers):
            items = detect_text(np.zeros((1000, 1000, 3), np.uint8),
                                self._semantics(), "test")
        self.assertEqual({it["text"] for it in items}, {"Q0", "Q1", "Q2", "Q3"})
        by_text = {it["text"]: it["box"] for it in items}
        self.assertEqual(by_text["Q0"][:2], [0, 0])
        self.assertEqual(by_text["Q1"][:2], [375, 0])     # right half offset
        self.assertEqual(by_text["Q2"][:2], [0, 375])     # lower half offset
        self.assertEqual(by_text["Q3"][:2], [375, 375])

    def test_persistently_wedged_region_yields_no_items_but_no_failure(self):
        from mapgen.semantics import EmptyModelResponse
        with patch("mapgen.textdetect._gemini_text",
                   side_effect=EmptyModelResponse("wedged")):
            items = detect_text(np.zeros((1000, 1000, 3), np.uint8),
                                self._semantics(), "test")
        self.assertEqual(items, [])


class TextDetectionTests(unittest.TestCase):
    @staticmethod
    def _semantics():
        return SimpleNamespace(
            subject="test map",
            overlay_text=SimpleNamespace(
                has_city_labels=False, has_region_labels=False,
                has_line_labels=True, notes="", capital_city=None,
            ),
            lines=[SimpleNamespace(
                kind=SimpleNamespace(value="river"), description="visible river")],
        )

    def test_text_pass_has_no_line_output(self):
        response = TextDetections(items=[TextItem(
            text="Seine", kind=TextKind.river_label,
            box_2d=[250, 100, 350, 300],
        )])

        with patch("mapgen.textdetect._gemini_text", return_value=response) as text_call:
            items = detect_text(np.zeros((100, 200, 3), np.uint8), self._semantics(), "test")

        self.assertNotIn("lines", TextDetections.model_fields)
        self.assertEqual(text_call.call_count, 1)
        self.assertEqual(items, [{"text": "Seine", "kind": "river_label",
                                  "box": [20, 25, 60, 35]}])

    def test_text_pass_skips_detection_when_step1_found_no_overlay_text(self):
        sem = self._semantics()
        sem.overlay_text.has_line_labels = False

        with patch("mapgen.textdetect._gemini_text") as text_call:
            items = detect_text(np.zeros((2000, 2000, 3), np.uint8), sem, "test")

        self.assertEqual(items, [])
        text_call.assert_not_called()

    def test_extract_strokes_can_convert_input_to_lab(self):
        image = np.zeros((20, 20, 3), dtype=np.uint8)

        result = extract_strokes(image, [2, 2, 18, 18])

        self.assertEqual(result, {"found": False})

    def test_detector_cache_signature_changes_with_input_pixels(self):
        first = np.zeros((20, 20, 3), dtype=np.uint8)
        second = first.copy()
        second[10, 10] = 255

        self.assertNotEqual(_text_input_signature(first), _text_input_signature(second))

    def test_missing_signature_never_matches_cache(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            self.assertFalse(_cache_matches(Path(directory) / "missing.sha256", "abc"))

    def test_confident_craft_only_island_label_reaches_output(self):
        craft = [{"text": "Ajaccio", "conf": 0.52, "box": [596, 687, 642, 701]}]

        fused = fuse_detections([], craft, diag=1000.0, vocab=set())

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["text"], "Ajaccio")
        self.assertEqual(fused[0]["localization"], "craft-only")
        self.assertEqual(fused[0]["recognition_status"], "easyocr-only")
        self.assertIsNone(fused[0]["gemini_text"])
        self.assertEqual(fused[0]["easyocr_text"], "Ajaccio")
        self.assertEqual(fused[0]["easyocr_conf"], 0.52)

    def test_matched_detection_keeps_both_readings_and_agreement(self):
        gemini = [{"text": "Paris", "kind": "capital", "box": [10, 10, 50, 25]}]
        easyocr = [{"text": "PARIS", "conf": 0.95, "box": [11, 9, 51, 24]}]

        fused = fuse_detections(gemini, easyocr, diag=1000.0, vocab={"Paris"})

        self.assertEqual(len(fused), 1)
        self.assertEqual(fused[0]["gemini_text"], "Paris")
        self.assertEqual(fused[0]["easyocr_text"], "PARIS")
        self.assertEqual(fused[0]["easyocr_conf"], 0.95)
        self.assertEqual(fused[0]["text_similarity"], 1.0)
        self.assertEqual(fused[0]["recognition_status"], "text-confirmed")

    def test_nearby_disagreeing_readings_are_geometry_only(self):
        gemini = [{"text": "Loire", "kind": "river_label", "box": [10, 10, 50, 25]}]
        easyocr = [{"text": "XYZ", "conf": 0.8, "box": [11, 9, 51, 24]}]

        fused = fuse_detections(gemini, easyocr, diag=1000.0, vocab=set())

        self.assertEqual(fused[0]["recognition_status"], "geometry-only")
        self.assertEqual(fused[0]["gemini_text"], "Loire")
        self.assertEqual(fused[0]["easyocr_text"], "XYZ")


if __name__ == "__main__":
    unittest.main()
