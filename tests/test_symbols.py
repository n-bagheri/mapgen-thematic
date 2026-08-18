import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mapgen.isolate import imread, imwrite
from mapgen.symbols import (build_overlay_labels, render_hybrid_from_tactile,
                            resolve_group_raster_indices)


class OverlayLabelTests(unittest.TestCase):
    def test_hybrid_render_uses_category_color_under_tactile_and_boundary_layers(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [{"members": [0], "color": "#59F7FF"}],
            }), encoding="utf-8")
            imwrite(out_dir / "label_map_gen.png", np.ones((3, 3), np.uint8))
            tactile = np.full((3, 3), 255, np.uint8)
            tactile[0, 0] = 0
            white_mask = np.zeros((3, 3), np.uint8)
            white_mask[1, :] = 255
            black_mask = np.zeros((3, 3), np.uint8)
            black_mask[1, 1] = 255
            imwrite(out_dir / "step8_white_stroke_mask.png", white_mask)
            imwrite(out_dir / "step8_black_stroke_mask.png", black_mask)

            self.assertTrue(render_hybrid_from_tactile(out_dir, tactile, "hybrid.png"))

            rendered = imread(out_dir / "hybrid.png")
            self.assertEqual(rendered[2, 2].tolist(), [255, 247, 89])
            self.assertEqual(rendered[0, 0].tolist(), [0, 0, 0])
            self.assertEqual(rendered[1, 0].tolist(), [255, 255, 255])
            self.assertEqual(rendered[1, 1].tolist(), [0, 0, 0])

    def test_step7_resolves_aggregated_source_members_to_group_raster_id(self):
        classes = [
            {"index": 0, "members": [4], "label": "Forest"},
            {"index": 1, "members": [7, 9], "label": "Field Crops"},
        ]

        self.assertEqual(resolve_group_raster_indices(classes, [9, 7]), [1])

    def test_transforms_explicit_text_and_feature_positions(self):
        source = {
            "labels": [{
                "text": "Ajaccio",
                "kind": "city",
                "priority": 5,
                "recognition_status": "easyocr-only",
                "text_position": [40, 30],
                "text_position_source": "stroke_centroid",
                "feature_position": [45, 35],
                "feature_position_source": "point_symbol",
                "box": [30, 20, 50, 40],
                "quad": [[30, 20], [50, 20], [50, 40], [30, 40]],
            }],
        }

        result = build_overlay_labels(
            source, source_shape=(100, 200), canvas_shape=(200, 500), mm_per_px=0.25,
        )

        label = result["labels"][0]
        self.assertEqual(label["text_position_source_px"], [40.0, 30.0])
        self.assertEqual(label["text_position_tactile_px"], [100.0, 60.0])
        self.assertEqual(label["text_position_mm"], [10.0, 7.5])
        self.assertEqual(label["feature_position_tactile_px"], [112.5, 70.0])
        self.assertEqual(label["box_tactile_px"], [75.0, 40.0, 125.0, 80.0])
        self.assertEqual(
            result["coordinate_contract"]["source_to_tactile_scale"], [2.5, 2.0],
        )

    def test_upgrades_legacy_city_anchor_without_using_it_as_text_position(self):
        source = {
            "labels": [{
                "text": "Paris",
                "kind": "capital",
                "box": [10, 20, 30, 40],
                "quad": None,
                "anchor": [8, 25],
                "anchor_source": "point_symbol",
            }],
        }

        result = build_overlay_labels(
            source, source_shape=(100, 100), canvas_shape=(200, 200), mm_per_px=0.5,
        )

        label = result["labels"][0]
        self.assertEqual(label["text_position_source_px"], [20.0, 30.0])
        self.assertEqual(label["text_position_source"], "box_center_legacy")
        self.assertEqual(label["feature_position_source_px"], [8.0, 25.0])
        self.assertEqual(label["feature_position_source"], "point_symbol_legacy")


if __name__ == "__main__":
    unittest.main()
