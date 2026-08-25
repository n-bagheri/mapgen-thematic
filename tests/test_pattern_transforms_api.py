import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import cv2

from mapgen.patterns import (PATTERNS, _pattern_svg, normalize_pattern_transform,
                             render_pattern, svg_pattern_library)
from webui import server


class PatternTransformTests(unittest.TestCase):
    def test_user_transform_is_layered_after_original_illustrator_transform(self):
        asset = svg_pattern_library().get("03_lines_vertical")
        svg = _pattern_svg(asset, (120, 160), 5.0, {
            "scale_x_percent": 75,
            "scale_y_percent": 50,
            "move_x_mm": 2,
            "move_y_mm": -3,
            "rotate_deg": 30,
        })
        self.assertIn(asset.pattern_transform, svg)
        self.assertIn("rotate(30)", svg)
        self.assertIn("scale(0.75 0.5)", svg)

    def test_transform_validation_rejects_unsafe_scale(self):
        with self.assertRaisesRegex(ValueError, "scale_x_percent"):
            normalize_pattern_transform({"scale_x_percent": 0})

    def test_transform_visibly_changes_the_rendered_svg_pattern(self):
        original = render_pattern("03_lines_vertical", (128, 128), 5.0)
        changed = render_pattern("03_lines_vertical", (128, 128), 5.0, {
            "scale_x_percent": 160,
            "scale_y_percent": 70,
            "move_x_mm": 3,
            "move_y_mm": -2,
            "rotate_deg": 25,
        })
        self.assertGreater(np.count_nonzero(original != changed), 1000)


class PatternTransformApiTests(unittest.TestCase):
    def tearDown(self):
        server._pattern_preview_png.cache_clear()

    def test_pattern_previews_reuse_the_svg_raster_for_an_identical_transform(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            symbols_path = run_dir / "symbols.json"
            symbols_path.write_text(json.dumps({
                "area_assignments": [{
                    "label": "Grasslands",
                    "pattern": "03_lines_vertical",
                    "members": [0],
                    "transform": {"rotate_deg": 10},
                }],
            }), encoding="utf-8")

            server._pattern_preview_png.cache_clear()
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch("mapgen.patterns.render_pattern",
                          return_value=np.full((128, 128), 255, np.uint8)) as render:
                client = server.app.test_client()
                first = client.get("/api/pattern-preview/sample/0?t=first")
                second = client.get("/api/pattern-preview/sample/0?t=second")
                symbols = json.loads(symbols_path.read_text(encoding="utf-8"))
                symbols["area_assignments"][0]["transform"]["rotate_deg"] = 20
                symbols_path.write_text(json.dumps(symbols), encoding="utf-8")
                changed = client.get("/api/pattern-preview/sample/0?t=changed")

            self.assertEqual((first.status_code, second.status_code, changed.status_code),
                             (200, 200, 200))
            self.assertEqual(render.call_count, 2)

    def test_get_and_post_transform_without_rerunning_semantic_assignment(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [{
                    "label": "Grasslands",
                    "pattern": "03_lines_vertical",
                    "pattern_desc": "vertical parallel lines",
                    "rationale": "semantic rationale",
                    "members": [0],
                    "source_members": [2, 3],
                }],
                "line_styles": {},
                "notes": [],
            }), encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                client = server.app.test_client()
                current = client.get("/api/pattern-transforms/sample")
                self.assertEqual(current.status_code, 200)
                self.assertEqual(
                    current.get_json()["groups"][0]["transform"]["scale_x_percent"],
                    100.0,
                )
                self.assertEqual(len(current.get_json()["library"]), 10)
                sine = next(item for item in current.get_json()["library"]
                            if item["pattern"] == "04_waves_sine")
                self.assertTrue(sine["water_only"])
                self.assertFalse(current.get_json()["groups"][0]["is_water"])

                with patch("mapgen.symbols.rerender_step7_artifacts") as rerender, \
                        patch("mapgen.boundaries.run_step8"), \
                        patch("mapgen.cleanup.run_step8a"):
                    changed = client.post("/api/pattern-transforms/sample/0", json={
                        "scale_x_percent": 75,
                        "scale_y_percent": 50,
                        "move_x_mm": 2,
                        "move_y_mm": -3,
                        "rotate_deg": 30,
                    })
                self.assertEqual(changed.status_code, 200)
                passed_symbols = rerender.call_args.args[1]
                self.assertEqual(
                    passed_symbols["area_assignments"][0]["transform"]["rotate_deg"],
                    30.0,
                )

    def test_change_pattern_locks_choice_and_globally_maximizes_the_other_area(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            cv2.imwrite(
                str(run_dir / "label_map_gen.png"),
                np.array([[1, 1, 2, 2]], dtype=np.uint8),
            )
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [
                    {"label": "A", "pattern": "03_lines_vertical",
                     "pattern_desc": "vertical parallel lines", "members": [0]},
                    {"label": "B", "pattern": "01_noise_splash",
                     "pattern_desc": "dense splash noise", "members": [1]},
                ],
                "line_styles": {},
                "notes": [],
            }), encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch("mapgen.symbols.rerender_step7_artifacts") as rerender, \
                    patch("mapgen.boundaries.run_step8"), \
                    patch("mapgen.cleanup.run_step8a"):
                response = server.app.test_client().post(
                    "/api/pattern-assignments/sample/0",
                    json={"pattern": "01_noise_dots"},
                )

            self.assertEqual(response.status_code, 200)
            changed = rerender.call_args.args[1]["area_assignments"]
            self.assertEqual(changed[0]["pattern"], "01_noise_dots")
            self.assertEqual(changed[0]["user_locked"], True)
            self.assertEqual(changed[1]["pattern"], "04_waves_triangle")
            self.assertNotEqual(changed[1]["pattern"], "04_waves_sine")

    def test_change_pattern_can_leave_every_other_assignment_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            cv2.imwrite(str(run_dir / "label_map_gen.png"),
                        np.array([[1, 1, 2, 2]], dtype=np.uint8))
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [
                    {"label": "A", "pattern": "03_lines_vertical",
                     "pattern_desc": "vertical parallel lines", "members": [0]},
                    {"label": "B", "pattern": "01_noise_splash",
                     "pattern_desc": "dense splash noise", "members": [1]},
                ],
                "line_styles": {}, "notes": [],
            }), encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch("mapgen.symbols.rerender_step7_artifacts") as rerender, \
                    patch("mapgen.boundaries.run_step8"), \
                    patch("mapgen.cleanup.run_step8a"):
                response = server.app.test_client().post(
                    "/api/pattern-assignments/sample/0",
                    json={"pattern": "01_noise_dots",
                          "preserve_haptic_distances": False},
                )

            self.assertEqual(response.status_code, 200)
            changed = rerender.call_args.args[1]["area_assignments"]
            self.assertEqual(changed[0]["pattern"], "01_noise_dots")
            self.assertEqual(changed[1]["pattern"], "01_noise_splash")
            self.assertEqual(
                response.get_json()["pattern_optimization"]["method"],
                "independent_user_pattern_assignment",
            )

    def test_sine_wave_is_water_only_while_preservation_is_enabled(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            cv2.imwrite(str(run_dir / "label_map_gen.png"), np.ones((2, 2), np.uint8))
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [{
                    "label": "Forest", "pattern": "03_lines_vertical",
                    "pattern_desc": "vertical parallel lines", "members": [0],
                    "is_thematic": True, "is_water": False,
                }],
                "line_styles": {}, "notes": [],
            }), encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch("mapgen.symbols.rerender_step7_artifacts") as rerender, \
                    patch("mapgen.boundaries.run_step8"), \
                    patch("mapgen.cleanup.run_step8a"):
                response = server.app.test_client().post(
                    "/api/pattern-assignments/sample/0",
                    json={"pattern": "04_waves_sine",
                          "preserve_haptic_distances": True},
                )

            self.assertEqual(response.status_code, 400)
            self.assertIn("only be assigned to water", response.get_json()["error"])
            rerender.assert_not_called()

    def test_sine_wave_can_be_manually_assigned_when_preservation_is_off(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            cv2.imwrite(str(run_dir / "label_map_gen.png"), np.ones((2, 2), np.uint8))
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [{
                    "label": "Forest", "pattern": "03_lines_vertical",
                    "pattern_desc": "vertical parallel lines", "members": [0],
                    "is_thematic": True, "is_water": False,
                }],
                "line_styles": {}, "notes": [],
            }), encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch("mapgen.symbols.rerender_step7_artifacts") as rerender, \
                    patch("mapgen.boundaries.run_step8"), \
                    patch("mapgen.cleanup.run_step8a"):
                response = server.app.test_client().post(
                    "/api/pattern-assignments/sample/0",
                    json={"pattern": "04_waves_sine",
                          "preserve_haptic_distances": False},
                )

            self.assertEqual(response.status_code, 200)
            changed = rerender.call_args.args[1]["area_assignments"]
            self.assertEqual(changed[0]["pattern"], "04_waves_sine")


if __name__ == "__main__":
    unittest.main()
