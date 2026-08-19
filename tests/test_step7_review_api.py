import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
