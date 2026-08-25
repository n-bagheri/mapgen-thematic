import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from mapgen.isolate import LegendSwatchDetectionError
from mapgen.segment import run_step4

SEMANTICS = {
    "map_type": "area_class_chorochromatic",
    "in_scope": True,
    "data_ordering": "qualitative",
    "map_language": "English",
    "subject": "Synthetic test map",
    "description": "Synthetic test map",
    "title": None,
    "legend_present": True,
    "legend_title": None,
    "legend_entries": [],
    "water_present": False,
    "thematic_classes": [],
    "non_thematic": [],
    "lines": [],
    "overlay_text": {
        "has_city_labels": False,
        "capital_city": None,
        "has_region_labels": False,
        "has_line_labels": False,
        "notes": "",
    },
}


class EmptyPaletteGuardTests(unittest.TestCase):
    """Step 2 writes its palette last, so a Step 2 that fails part-way used to
    leave the previous run's classes.json behind.  Step 4 read it without a
    freshness check, and an empty palette rendered as one flat blob instead of
    stopping the pipeline (the Africa ethnolinguistic sheet)."""

    def _run_dir(self, tmp: str, classes: list[dict]) -> Path:
        run_dir = Path(tmp) / "runs" / "synthetic"
        run_dir.mkdir(parents=True)
        (run_dir / "step1_semantics.json").write_text(
            json.dumps(SEMANTICS), encoding="utf-8")
        (run_dir / "geometry.json").write_text(json.dumps({
            "image_size": [40, 40], "map_boxes_vlm": [[0, 0, 40, 40]],
            "map_crop": [0, 0, 40, 40], "legend_crop": None, "furniture": [],
        }), encoding="utf-8")
        (run_dir / "classes.json").write_text(
            json.dumps({"classes": classes, "warnings": []}), encoding="utf-8")
        # Present so Step 4 does not try to run Step 3 and call a model.
        cv2.imwrite(str(run_dir / "text_mask.png"), np.zeros((40, 40), np.uint8))
        return run_dir

    def test_a_palette_with_no_sampled_colour_stops_step_4(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, [])
            with self.assertRaises(LegendSwatchDetectionError) as caught:
                run_step4(Path("synthetic.png"), runs_dir=run_dir.parent)
            self.assertIn("no legend colour", str(caught.exception))

    def test_uncoloured_rows_alone_are_not_a_palette(self):
        """A legend can hold symbol or line rows that carry no area fill; on
        their own they still leave every pixel unassigned."""
        rows = [{"label": "river", "is_thematic": False, "priority": None,
                 "rgb": None, "lab": None, "hex": None, "swatch_bbox_orig": None}]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, rows)
            with self.assertRaises(LegendSwatchDetectionError):
                run_step4(Path("synthetic.png"), runs_dir=run_dir.parent)

    def test_a_sampled_colour_clears_the_guard(self):
        rows = [{"label": "forest", "is_thematic": True, "priority": 1,
                 "rgb": [40, 180, 70], "lab": [65.0, -50.0, 45.0],
                 "hex": "#28b446", "swatch_bbox_orig": [0, 0, 4, 4]}]
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = self._run_dir(tmp, rows)
            # Fails later, on the artifacts this fixture does not build, but it
            # must get past the palette guard.
            with self.assertRaises(Exception) as caught:
                run_step4(Path("synthetic.png"), runs_dir=run_dir.parent)
            self.assertNotIsInstance(caught.exception, LegendSwatchDetectionError)


if __name__ == "__main__":
    unittest.main()
