"""Verify that /test-only rendering reuse preserves the original renderers."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from mapgen.interactive_step7 import apply_edit, render_edit
from mapgen.patterns import render_pattern
from mapgen.render_cache import PatternRasterCache, active_render_reuse, reuse_step7_rasters
from webui import server


class RasterReuseTests(unittest.TestCase):
    def test_cache_is_bounded_lru_and_returns_independent_arrays(self):
        cache = PatternRasterCache(max_bytes=8)
        image = np.zeros((2, 2), np.uint8)
        cache.put("a", image)
        image[:] = 20
        self.assertTrue(np.all(cache.get("a") == 0))
        cache.put("b", image)
        cache.get("a")[:] = 40
        cache.put("c", image)
        self.assertIsNone(cache.get("b"))
        self.assertTrue(np.all(cache.get("a") == 0))
        cache.put("oversized", np.zeros((3, 3), np.uint8))
        self.assertIsNone(cache.get("oversized"))
        self.assertLessEqual(cache._bytes, 8)

    def test_texture_cache_is_exact_and_invalidates_changed_inputs(self):
        import resvg_py

        cache = PatternRasterCache()
        original = render_pattern("03_lines_vertical", (40, 60), 5.0)
        with patch.object(resvg_py, "svg_to_bytes", wraps=resvg_py.svg_to_bytes) as raster:
            with reuse_step7_rasters(cache) as reuse:
                first = render_pattern("03_lines_vertical", (40, 60), 5.0)
                first[:] = 30  # Must not alter the cached value.
                again = render_pattern("03_lines_vertical", (40, 60), 5.0)
                np.testing.assert_array_equal(again, original)
                render_pattern("03_lines_vertical", (40, 60), 5.0, {"rotate_deg": 12})
                render_pattern("03_lines_vertical", (41, 60), 5.0)
                render_pattern("03_lines_vertical", (40, 60), 6.0)
                render_pattern("02_grid_dots", (40, 60), 5.0)
                self.assertEqual((reuse.pattern_hits, reuse.pattern_misses), (1, 5))
            self.assertIsNone(active_render_reuse.get())
            # Normal pipeline calls do not activate or consume this cache.
            render_pattern("03_lines_vertical", (40, 60), 5.0)
            render_pattern("03_lines_vertical", (40, 60), 5.0)
            self.assertEqual(raster.call_count, 7)

    def test_render_context_is_reset_after_exception(self):
        with self.assertRaises(RuntimeError):
            with reuse_step7_rasters(PatternRasterCache()):
                raise RuntimeError("render failed")
        self.assertIsNone(active_render_reuse.get())

    def test_boundary_free_base_reuse_is_per_edit_and_invalidates_inputs(self):
        from mapgen.boundaries import _render_step8_base
        from mapgen.isolate import imwrite

        with TemporaryDirectory() as directory:
            root = Path(directory)
            imwrite(root / "label_map_gen.png", np.ones((3, 4), np.uint8))
            (root / "step6_summary.json").write_text(json.dumps({"scale_mm_per_px": 1.0}))
            (root / "lines_gen.geojson").write_text(json.dumps({"features": []}))
            symbols = {"area_assignments": [{"pattern": "03_lines_vertical", "members": [0]}]}
            expected = np.full((6, 8), 255, np.uint8)
            with patch("mapgen.symbols.render_tactile", return_value=expected.copy()) as render:
                with reuse_step7_rasters(PatternRasterCache()) as reuse:
                    _render_step8_base(root, symbols)[:] = 0
                    np.testing.assert_array_equal(_render_step8_base(root, symbols), expected)
                    self.assertEqual((render.call_count, reuse.base_hits), (1, 1))
                    symbols["area_assignments"][0]["transform"] = {"rotate_deg": 10}
                    _render_step8_base(root, symbols)
                    (root / "lines_gen.geojson").write_text(json.dumps({"features": [], "changed": True}))
                    _render_step8_base(root, symbols)
                    self.assertEqual(render.call_count, 3)
                with reuse_step7_rasters(PatternRasterCache()):
                    _render_step8_base(root, symbols)
                _render_step8_base(root, symbols)
                _render_step8_base(root, symbols)
                self.assertEqual(render.call_count, 6)


class InteractiveEditTests(unittest.TestCase):
    def setUp(self):
        self.symbols = {"area_assignments": [
            {"pattern": "03_lines_vertical", "members": [0], "transform": {}},
            {"pattern": "02_grid_dots", "members": [1], "transform": {}},
        ]}

    def test_optimized_assignment_calls_original_optimizer(self):
        def assign(root, symbols, index, pattern, preserve):
            symbols["area_assignments"][index]["pattern"] = pattern
        with patch("mapgen.symbols.reassign_step7_pattern", side_effect=assign) as optimizer:
            changed = apply_edit(Path("unused"), self.symbols, {
                "action": "assign", "group_id": 0, "pattern": "01_noise_dots",
                "preserve_haptic_distances": True})
        self.assertTrue(changed)
        optimizer.assert_called_once_with(Path("unused"), self.symbols, 0, "01_noise_dots", True)

    def test_reset_validates_every_group_before_mutating(self):
        before = copy.deepcopy(self.symbols)
        with patch("mapgen.symbols.reassign_step7_pattern") as assign:
            with self.assertRaises(ValueError):
                apply_edit(Path("unused"), self.symbols, {"action": "reset", "groups": [
                    {"group_id": 0, "pattern": "01_noise_dots", "transform": {}},
                    {"group_id": 0, "pattern": "plain", "transform": {}},
                ]})
            assign.assert_not_called()
        self.assertEqual(self.symbols, before)

    def test_transform_does_not_call_assignment_algorithm(self):
        with patch("mapgen.symbols.reassign_step7_pattern") as assign:
            self.assertTrue(apply_edit(Path("unused"), self.symbols, {
                "action": "transform", "group_id": 0, "transform": {"rotate_deg": 23}}))
            assign.assert_not_called()
        self.assertEqual(self.symbols["area_assignments"][0]["transform"]["rotate_deg"], 23)

    def test_renderer_keeps_original_stage_order_and_resets_context(self):
        calls = []
        def record(name):
            def run(*args, **kwargs):
                self.assertIsNotNone(active_render_reuse.get())
                calls.append(name)
            return run
        with TemporaryDirectory() as directory, \
                patch("mapgen.symbols.rerender_step7_artifacts", side_effect=record("patterns")), \
                patch("mapgen.boundaries.run_step8", side_effect=record("boundaries")), \
                patch("mapgen.cleanup.run_step8a", side_effect=record("cleanup")):
            render_edit(Path("sample.png"), Path(directory), self.symbols)
        self.assertEqual(calls, ["patterns", "boundaries", "cleanup"])
        self.assertIsNone(active_render_reuse.get())


class InteractiveRouteTests(unittest.TestCase):
    def test_reset_renders_once_and_releases_project_lock(self):
        symbols = {"area_assignments": [
            {"label": "One", "pattern": "03_lines_vertical", "members": [0]},
            {"label": "Two", "pattern": "02_grid_dots", "members": [1]},
        ]}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "find_map", return_value=root / "sample.png"), \
                    patch.object(server, "_step7_symbols", return_value=(root, symbols)), \
                    patch.object(server, "_step7_review_payload", return_value={"preserve_haptic_distances": False}), \
                    patch.object(server, "_save_step7_review", side_effect=lambda stem, review: review), \
                    patch.object(server, "step_done", return_value=True), \
                    patch("mapgen.symbols.reassign_step7_pattern") as assign, \
                    patch("mapgen.interactive_step7.render_edit", return_value={}) as render:
                def update(out_dir, data, index, pattern, preserve):
                    self.assertIn("sample", server._test_step7_editing)
                    data["area_assignments"][index]["pattern"] = pattern
                assign.side_effect = update
                response = server.app.test_client().post("/api/test/step7-edit/sample", json={
                    "action": "reset", "groups": [
                        {"group_id": 0, "pattern": "01_noise_dots", "transform": {}},
                        {"group_id": 1, "pattern": "solid_black", "transform": {}},
                    ]})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertEqual(assign.call_count, 2)
                render.assert_called_once()
                self.assertTrue(response.get_json()["review"]["preserve_haptic_distances"])
                self.assertNotIn("sample", server._test_step7_editing)

    def test_failed_render_releases_lock_and_same_edit_can_retry(self):
        symbols = {"area_assignments": [{"label": "One", "pattern": "03_lines_vertical", "members": [0]}]}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, "find_map", return_value=root / "sample.png"), \
                    patch.object(server, "_step7_symbols", return_value=(root, symbols)), \
                    patch.object(server, "_step7_review_payload", return_value={"preserve_haptic_distances": False}), \
                    patch.object(server, "_save_step7_review", side_effect=lambda stem, review: review), \
                    patch.object(server, "step_done", return_value=False), \
                    patch("mapgen.interactive_step7.render_edit", side_effect=RuntimeError("failed")) as render:
                with patch.dict(server.app.config, TESTING=True):
                    with self.assertRaises(RuntimeError):
                        server.app.test_client().post("/api/test/step7-edit/sample", json={
                            "action": "assign", "group_id": 0, "pattern": "03_lines_vertical",
                            "preserve_haptic_distances": False})
                self.assertNotIn("sample", server._test_step7_editing)
                render.side_effect = None
                render.return_value = {}
                response = server.app.test_client().post("/api/test/step7-edit/sample", json={
                    "action": "assign", "group_id": 0, "pattern": "03_lines_vertical",
                    "preserve_haptic_distances": False})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["rendered"])


if __name__ == "__main__":
    unittest.main()
