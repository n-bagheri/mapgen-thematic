import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mapgen.boundaries import (apply_boundary_strokes, boundary_centerline,
                               closed_pattern_centerline,
                               discard_open_centerline_branches, run_step8,
                               open_endpoint_count, repair_tiny_centerline_gaps,
                               select_boundary_pairs)
from mapgen.isolate import imwrite


class BoundarySelectionTests(unittest.TestCase):
    def test_pattern_next_to_plain_or_black_has_no_boundary_without_priority(self):
        group_map = np.array([[0, 0, 1, 1, 2, 2]], dtype=np.int16)
        patterns = {0: "dots_sparse", 1: "plain", 2: "solid_black"}

        selected, active, _ = select_boundary_pairs(group_map, patterns)

        self.assertEqual(active, set())
        self.assertEqual(selected, set())

    def test_pattern_pattern_case_activates_both_patterns_everywhere(self):
        # dots touches lines, and separate occurrences touch plain, black, and
        # outside.  Both regular patterns must receive all of those edges.
        group_map = np.array([
            [3, 0, 1, 2],
            [3, 0, 1, 2],
        ], dtype=np.int16)
        patterns = {
            0: "wave_sine",
            1: "lines_horizontal",
            2: "plain",
            3: "solid_black",
        }

        selected, active, _ = select_boundary_pairs(group_map, patterns)

        self.assertEqual(active, {"wave_sine", "lines_horizontal"})
        self.assertIn((0, 1), selected)       # base pattern-pattern case
        self.assertIn((-1, 0), selected)      # active wave against outside
        self.assertIn((0, 3), selected)       # active wave against pure black
        self.assertIn((1, 2), selected)       # active lines against plain
        self.assertNotIn((-1, 2), selected)   # plain alone never gains priority

    def test_compound_stroke_is_white_with_centered_black_line(self):
        group_map = np.zeros((41, 81), dtype=np.int16)
        group_map[:, 40:] = 1
        centerline = boundary_centerline(group_map, {(0, 1)})
        canvas = np.zeros_like(group_map, dtype=np.uint8)

        result, white_px, black_px = apply_boundary_strokes(
            canvas, centerline, px_per_mm=5.0,
        )

        self.assertEqual((white_px, black_px), (25, 5))
        row = result[20]
        self.assertTrue(np.all(row[37:42] == 0))
        self.assertTrue(np.all(row[27:37] == 255))
        self.assertTrue(np.all(row[42:52] == 255))
        self.assertEqual(row[20], 0)
        self.assertEqual(row[60], 0)

    def test_priority_contour_is_closed_when_region_touches_canvas_outside(self):
        group_map = np.full((24, 28), -1, dtype=np.int16)
        group_map[0:15, 5:19] = 0
        group_map[5:10, 19:27] = 0
        group_map[8:12, 9:13] = -1  # a hole must also be closed

        centerline, contour_count, closure_pairs, black_components, repaired = closed_pattern_centerline(
            group_map, {0: "wave_sine"}, {"wave_sine"},
        )

        self.assertGreaterEqual(contour_count, 2)
        self.assertEqual(closure_pairs, set())
        self.assertEqual(black_components, 0)
        self.assertEqual(repaired, 0)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_shared_active_pattern_junctions_stay_closed_after_thinning(self):
        group_map = np.full((36, 48), -1, dtype=np.int16)
        group_map[2:32, 2:24] = 0
        group_map[5:20, 24:45] = 1
        group_map[20:34, 18:40] = 2
        group_map[11:16, 8:13] = -1

        centerline, contour_count, _, _, _ = closed_pattern_centerline(
            group_map,
            {0: "wave_sine", 1: "dots_sparse", 2: "lines_horizontal"},
            {"wave_sine", "dots_sparse", "lines_horizontal"},
        )

        self.assertGreaterEqual(contour_count, 4)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_sub_resolution_pattern_sliver_does_not_create_open_contour(self):
        group_map = np.full((24, 36), -1, dtype=np.int16)
        # A three-pixel-tall rasterization remnant cannot carry an embossed
        # boundary at the render scale and previously yielded a dangling end.
        group_map[10:13, 8:20] = 0

        centerline, contour_count, _, _, _ = closed_pattern_centerline(
            group_map, {0: "wave_sine"}, {"wave_sine"},
        )

        self.assertEqual(contour_count, 0)
        self.assertFalse(centerline.any())
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_black_component_carries_selected_boundary_around_outside(self):
        group_map = np.full((32, 42), -1, dtype=np.int16)
        group_map[8:28, 4:22] = 0
        group_map[0:21, 22:38] = 1

        centerline, contour_count, closure_pairs, black_components, _ = closed_pattern_centerline(
            group_map,
            {0: "dots_sparse", 1: "solid_black"},
            {"dots_sparse"},
        )

        self.assertEqual(black_components, 1)
        self.assertGreaterEqual(contour_count, 2)
        self.assertIn((-1, 1), closure_pairs)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_tiny_same_contour_gap_is_repaired_without_joining_regions(self):
        centerline = np.zeros((20, 20), dtype=np.uint8)
        # A diagonal pinch leaves a short spur connected to an otherwise
        # closed loop. It is one connected network, but has one endpoint.
        centerline[5:14, 5] = 255
        centerline[5:14, 13] = 255
        centerline[5, 5:14] = 255
        centerline[13, 5:14] = 255
        centerline[2, 2] = 255
        centerline[3, 2] = 255
        centerline[4, 3] = 255
        centerline[5, 4] = 255

        repaired, bridges = repair_tiny_centerline_gaps(centerline)

        self.assertEqual(open_endpoint_count(centerline), 1)
        self.assertEqual(bridges, 1)
        self.assertEqual(open_endpoint_count(repaired), 0)

    def test_open_raster_spur_is_discarded_without_removing_closed_outline(self):
        centerline = np.zeros((24, 24), dtype=np.uint8)
        centerline[6, 6:18] = 255
        centerline[17, 6:18] = 255
        centerline[6:18, 6] = 255
        centerline[6:18, 17] = 255
        centerline[3:7, 11] = 255

        cleaned, removed = discard_open_centerline_branches(centerline)

        self.assertGreater(removed, 0)
        self.assertEqual(open_endpoint_count(cleaned), 0)
        self.assertEqual(cleaned[17, 17], 255)

    def test_step8_runner_writes_final_raster_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "runs" / "sample"
            run_dir.mkdir(parents=True)
            (run_dir / "step1_semantics.json").write_text(json.dumps({
                "map_type": "area_class_chorochromatic",
                "in_scope": True,
                "data_ordering": "qualitative",
                "map_language": "English",
                "subject": "Synthetic map",
                "description": "Synthetic map for boundary rendering.",
                "title": None,
                "legend_present": True,
                "legend_title": "Classes",
                "legend_entries": [{
                    "label": "Crops", "color_hint": "green", "is_thematic": True,
                    "kind": "area_fill",
                }],
                "water_present": True,
                "thematic_classes": [{
                    "label": "Crops", "priority": 1,
                    "approx_area_share_percent": 50.0,
                }],
                "non_thematic": [],
                "lines": [],
                "overlay_text": {
                    "has_city_labels": False, "capital_city": None,
                    "has_region_labels": False, "has_line_labels": False,
                    "notes": "",
                },
            }), encoding="utf-8")
            source_labels = np.array([
                [1, 1, 2, 2, 3, 3],
                [1, 1, 2, 2, 3, 3],
            ], dtype=np.uint8)
            imwrite(run_dir / "label_map_gen.png", source_labels)
            imwrite(run_dir / "step7_tactile.png", np.zeros((20, 60), np.uint8))
            (run_dir / "step5_summary.json").write_text(
                json.dumps({"scale_mm_per_px": 2.0}), encoding="utf-8",
            )
            (run_dir / "lines_gen.geojson").write_text(
                json.dumps({"type": "FeatureCollection", "features": []}),
                encoding="utf-8",
            )
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [
                    {"label": "Water", "members": [0], "pattern": "wave_sine"},
                    {"label": "Crops", "members": [1], "pattern": "lines_horizontal"},
                    {"label": "Empty", "members": [2], "pattern": "plain"},
                ],
                "render_px_per_mm": 5.0,
            }), encoding="utf-8")
            step7_before = (run_dir / "step7_tactile.png").read_bytes()
            symbols_before = (run_dir / "symbols.json").read_bytes()

            result = run_step8(
                root / "sample.png", runs_dir=root / "runs",
            )

            self.assertEqual((run_dir / "step7_tactile.png").read_bytes(), step7_before)
            self.assertEqual((run_dir / "symbols.json").read_bytes(), symbols_before)
            self.assertEqual(result["active_patterns"], ["lines_horizontal", "wave_sine"])
            self.assertTrue((run_dir / "step8_boundaries.png").exists())
            self.assertTrue((run_dir / "step8_debug.png").exists())
            audit = json.loads((run_dir / "step8_boundaries.json").read_text())
            self.assertEqual(audit["white_stroke_px"], 25)
            self.assertEqual(audit["black_stroke_px"], 5)
            crop_plain = next(edge for edge in audit["adjacencies"]
                              if {edge["side_a"]["label"], edge["side_b"]["label"]}
                              == {"Crops", "Empty"})
            self.assertTrue(crop_plain["boundary_drawn"])
            self.assertEqual(crop_plain["reason"], "global_pattern_priority")


if __name__ == "__main__":
    unittest.main()
