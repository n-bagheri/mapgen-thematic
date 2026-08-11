import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from webui import server


class AggregationReviewApiTests(unittest.TestCase):
    def test_canonical_review_is_saved_and_invalidates_step7(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir, runs_dir = root / "maps", root / "runs"
            run_dir = runs_dir / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps_dir / "sample.png"),
                        np.full((8, 8, 3), 255, np.uint8))
            aggregation = {
                "mode": "semantic",
                "slots": 2,
                "review_required": True,
                "review_status": "needs_review",
                "source_classes": [
                    {"index": 0, "label": "Forest"},
                    {"index": 1, "label": "Cropland"},
                    {"index": 2, "label": "Other crops"},
                ],
                "groups": [
                    {"label": "Forest", "members": [0],
                     "member_labels": ["Forest"], "rationale": "identity"},
                    {"label": "Field Crops", "members": [1, 2],
                     "member_labels": ["Cropland", "Other crops"],
                     "rationale": "semantic proposal"},
                ],
            }
            (run_dir / "aggregation.json").write_text(
                json.dumps(aggregation), encoding="utf-8")
            for name in server.STEP_ARTIFACTS[7]:
                (run_dir / name).write_bytes(b"stale")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", runs_dir):
                client = server.app.test_client()
                pending = client.get("/api/aggregation-review/sample")
                self.assertEqual(pending.status_code, 200)
                self.assertIsNone(pending.get_json()["review"])

                saved = client.post("/api/aggregation-review/sample", json={"groups": [
                    {"label": "Forest", "members": [0], "approved": True},
                    {"label": "Field Crops", "members": [1, 2], "approved": True},
                ]})
                self.assertEqual(saved.status_code, 200)
                self.assertTrue(saved.get_json()["review"]["approved"])

            self.assertTrue((run_dir / "aggregation_review.json").exists())
            for name in server.STEP_ARTIFACTS[7]:
                self.assertFalse((run_dir / name).exists())


if __name__ == "__main__":
    unittest.main()
