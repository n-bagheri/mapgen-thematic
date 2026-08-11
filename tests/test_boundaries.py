import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mapgen.boundaries import (apply_boundary_strokes, boundary_centerline,
                               closed_pattern_centerline, run_step8,
                               open_endpoint_count, select_boundary_pairs)
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

        centerline, contour_count, closure_pairs, black_components = closed_pattern_centerline(
            group_map, {0: "wave_sine"}, {"wave_sine"},
        )

        self.assertGreaterEqual(contour_count, 2)
        self.assertEqual(closure_pairs, set())
        self.assertEqual(black_components, 0)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_shared_active_pattern_junctions_stay_closed_after_thinning(self):
        group_map = np.full((36, 48), -1, dtype=np.int16)
        group_map[2:32, 2:24] = 0
        group_map[5:20, 24:45] = 1
        group_map[20:34, 18:40] = 2
        group_map[11:16, 8:13] = -1

        centerline, contour_count, _, _ = closed_pattern_centerline(
            group_map,
            {0: "wave_sine", 1: "dots_sparse", 2: "lines_horizontal"},
            {"wave_sine", "dots_sparse", "lines_horizontal"},
        )

        self.assertGreaterEqual(contour_count, 4)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_black_component_carries_selected_boundary_around_outside(self):
        group_map = np.full((32, 42), -1, dtype=np.int16)
        group_map[8:28, 4:22] = 0
        group_map[0:21, 22:38] = 1

        centerline, contour_count, closure_pairs, black_components = closed_pattern_centerline(
            group_map,
            {0: "dots_sparse", 1: "solid_black"},
            {"dots_sparse"},
        )

        self.assertEqual(black_components, 1)
        self.assertGreaterEqual(contour_count, 2)
        self.assertIn((-1, 1), closure_pairs)
        self.assertEqual(open_endpoint_count(centerline), 0)

    def test_step8_runner_writes_final_raster_and_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run_dir = root / "runs" / "sample"
            run_dir.mkdir(parents=True)
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
