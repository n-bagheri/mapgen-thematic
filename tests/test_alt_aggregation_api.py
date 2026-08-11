import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.alt_mapgen import _source_digest
from webui import server


class AltAggregationReviewApiTests(unittest.TestCase):
    def test_review_endpoint_persists_approval_and_invalidates_alt_steps6_and7(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps_dir / "sample.png"), np.full((8, 8, 3), 255, np.uint8))
            source = np.zeros((8, 8), np.int16)
            source[:, 3:6] = 1
            source[:, 6:] = 2
            classes = [
                {"index": 0, "label": "Forest", "rgb": [20, 130, 60],
                 "is_thematic": True, "area_px": 24},
                {"index": 1, "label": "Cropland", "rgb": [240, 155, 20],
                 "is_thematic": True, "area_px": 24},
                {"index": 2, "label": "Other crops", "rgb": [240, 235, 160],
                 "is_thematic": True, "area_px": 16},
            ]
            cv2.imwrite(str(run_dir / "label_map.png"), (source + 1).astype(np.uint8))
            (run_dir / "classes_final.json").write_text(
                json.dumps({"classes": classes}), encoding="utf-8")
            aggregation = {
                "branch": "alternate",
                "mode": "semantic_merge_proposal",
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
                "plain_thematic": [],
                "source_digest": _source_digest(source, classes),
            }
            (run_dir / "alt_aggregation.json").write_text(
                json.dumps(aggregation), encoding="utf-8")
            for step in (6, 7):
                for name in server.ALT_STEP_ARTIFACTS[step]:
                    (run_dir / name).write_bytes(b"stale")

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                client = server.app.test_client()
                pending = client.get("/api/alt-aggregation-review/sample")
                self.assertEqual(pending.status_code, 200)
                self.assertIsNone(pending.get_json()["review"])

                saved = client.post("/api/alt-aggregation-review/sample", json={"groups": [
                    {"label": "Forest", "members": [0], "approved": True},
                    {"label": "Field Crops", "members": [1, 2], "approved": True,
                     "rationale": "approved field-crop category"},
                ]})
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.get_json()["review"]["status"], "approved")

                current = client.get("/api/alt-aggregation-review/sample").get_json()
                self.assertTrue(current["review"]["approved"])

            self.assertTrue((run_dir / "alt_aggregation_review.json").exists())
            self.assertTrue((run_dir / "alt_group_map_source.png").exists())
            self.assertTrue((run_dir / "alt_groups.json").exists())
            for step in (6, 7):
                for name in server.ALT_STEP_ARTIFACTS[step]:
                    self.assertFalse((run_dir / name).exists())


if __name__ == "__main__":
    unittest.main()
