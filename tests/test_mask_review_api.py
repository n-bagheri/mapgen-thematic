import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from webui import server


class MaskReviewApiTests(unittest.TestCase):
    def test_erasing_mask_pixels_rebuilds_text_input_and_invalidates_step3(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps = root / "maps"
            runs = root / "runs"
            maps.mkdir()
            run_dir = runs / "sample"
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps / "sample.png"), np.full((40, 40, 3), 255, np.uint8))
            image = np.full((40, 40, 3), 255, np.uint8)
            image[10:30, 10:30] = (30, 140, 60)
            cv2.imwrite(str(run_dir / "map_area.png"), image)
            mask = np.zeros((40, 40), np.uint8)
            mask[10:30, 10:30] = 255
            cv2.imwrite(str(run_dir / "map_mask.png"), mask)
            (run_dir / "geometry.json").write_text(json.dumps({
                "map_crop": [0, 0, 40, 40], "furniture": [],
            }), encoding="utf-8")
            # A downstream file proves that a review cannot leave stale output.
            (run_dir / "labels.json").write_text("{}", encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps), patch.object(server, "RUNS_DIR", runs):
                client = server.app.test_client()
                response = client.post("/api/maskreview/sample", json={
                    "strokes": [{"mode": "erase", "radius": 3,
                                 "points": [[20, 20]]}],
                })
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                self.assertFalse((run_dir / "labels.json").exists())
                revised = cv2.imread(str(run_dir / "map_mask.png"), cv2.IMREAD_GRAYSCALE)
                self.assertEqual(revised[20, 20], 0)
                self.assertTrue((run_dir / "map_mask_auto.png").exists())
                self.assertTrue((run_dir / "map_mask_review.json").exists())

                recovered = client.post("/api/maskreview/sample", json={
                    "strokes": [{"mode": "restore", "radius": 2,
                                 "points": [[4, 4]]}],
                })
                self.assertEqual(recovered.status_code, 200, recovered.get_data(as_text=True))
                revised = cv2.imread(str(run_dir / "map_mask.png"), cv2.IMREAD_GRAYSCALE)
                self.assertEqual(revised[4, 4], 255)

                restored = client.post("/api/maskreview/sample", json={"reset": True})
                self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
                reset_mask = cv2.imread(str(run_dir / "map_mask.png"), cv2.IMREAD_GRAYSCALE)
                self.assertEqual(reset_mask[20, 20], 255)

                approved = client.post("/api/maskreview/sample", json={"approve": True})
                self.assertEqual(approved.status_code, 200, approved.get_data(as_text=True))
                self.assertTrue(approved.get_json()["approved"])
                status = client.get("/api/maskreview/sample").get_json()
                self.assertTrue(status["approved"])
                self.assertFalse(status["reviewed"])


if __name__ == "__main__":
    unittest.main()
