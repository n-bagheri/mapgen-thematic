import json
import os
import unittest
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from webui import server


class Step7ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.maps_dir = self.root / "maps"
        self.run_dir = self.root / "runs" / "sample"
        self.maps_dir.mkdir()
        self.run_dir.mkdir(parents=True)
        (self.maps_dir / "sample.png").write_bytes(b"map")
        (self.run_dir / "symbols.json").write_text(json.dumps({
            "area_assignments": [], "line_styles": {}, "notes": [],
        }), encoding="utf-8")
        self.maps_patch = patch.object(server, "MAPS_DIR", self.maps_dir)
        self.runs_patch = patch.object(server, "RUNS_DIR", self.root / "runs")
        self.maps_patch.start()
        self.runs_patch.start()
        self.client = server.app.test_client()

    def tearDown(self):
        self.runs_patch.stop()
        self.maps_patch.stop()
        self.temporary.cleanup()

    def test_modes_persist_and_approval_requires_a_finished_step(self):
        default = self.client.get("/api/step7-review/sample")
        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.get_json(), {
            "version": 1,
            "approved": False,
            "preserve_haptic_distances": True,
            "create_hybrid_map": False,
        })

        changed = self.client.post("/api/step7-review/sample", json={
            "preserve_haptic_distances": False,
            "create_hybrid_map": True,
        })
        self.assertEqual(changed.status_code, 200)
        self.assertFalse(changed.get_json()["preserve_haptic_distances"])
        self.assertTrue(changed.get_json()["create_hybrid_map"])

        unfinished = self.client.post("/api/step7-review/sample", json={"approve": True})
        self.assertEqual(unfinished.status_code, 409)
        with patch.object(server, "step_done", return_value=True):
            approved = self.client.post("/api/step7-review/sample", json={"approve": True})
            self.assertEqual(approved.status_code, 200)
            self.assertTrue(approved.get_json()["approved"])
            self.assertTrue(server.step7_review_ready("sample"))

    def test_review_rejects_non_boolean_modes(self):
        response = self.client.post("/api/step7-review/sample", json={
            "preserve_haptic_distances": "yes",
        })
        self.assertEqual(response.status_code, 400)

    def test_hybrid_mode_change_refreshes_existing_braille_and_legend_outputs(self):
        (self.run_dir / "braille_labels.json").write_text("{}", encoding="utf-8")
        (self.run_dir / "legend_labels.json").write_text("{}", encoding="utf-8")
        with patch("mapgen.braille.run_step8") as rerender_braille, \
                patch("mapgen.legend.run_step9") as rerender_legend:
            response = self.client.post("/api/step7-review/sample", json={
                "create_hybrid_map": True,
            })

        self.assertEqual(response.status_code, 200)
        rerender_braille.assert_called_once_with(
            self.maps_dir / "sample.png", runs_dir=self.root / "runs")
        rerender_legend.assert_called_once_with(
            self.maps_dir / "sample.png", runs_dir=self.root / "runs")

    def test_optimized_mode_blocks_pattern_changes_and_edits(self):
        changed = self.client.post("/api/pattern-assignments/sample/0", json={
            "pattern": "plain", "preserve_haptic_distances": True,
        })
        edited = self.client.post("/api/pattern-transforms/sample/0", json={})

        self.assertEqual(changed.status_code, 409)
        self.assertIn("turn off Use optimized patterns", changed.get_data(as_text=True))
        self.assertEqual(edited.status_code, 409)
        self.assertIn("turn off Use optimized patterns", edited.get_data(as_text=True))

    def test_hybrid_color_save_keeps_completed_step7_approvable(self):
        (self.run_dir / "symbols.json").write_text(json.dumps({
            "area_assignments": [{"label": "Forest", "pattern": "plain"}],
        }), encoding="utf-8")
        (self.run_dir / "label_map_gen.png").write_bytes(b"source")
        (self.run_dir / "step8_boundaries.json").write_text("{}", encoding="utf-8")
        for name in server.STEP_ARTIFACTS[7]:
            path = self.run_dir / name
            if not path.exists():
                path.write_bytes(b"finished")
        old = 1_700_000_000
        for name in ("step8a_cleanup.json", "step8a_cleanup.png", "step8a_debug.png"):
            os.utime(self.run_dir / name, (old, old))

        with patch.object(server, "_current_semantics",
                          return_value=SimpleNamespace(in_scope=True)), \
                patch("mapgen.symbols.rerender_hybrid_artifacts"):
            saved = self.client.post("/api/category-colors/sample", json={
                "colors": {"Forest": "#336699"},
            })
            approved = self.client.post("/api/step7-review/sample", json={"approve": True})

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(approved.status_code, 200)
        self.assertTrue(approved.get_json()["approved"])


if __name__ == "__main__":
    unittest.main()
