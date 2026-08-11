import unittest

from mapgen.symbols import build_overlay_labels


class OverlayLabelTests(unittest.TestCase):
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
