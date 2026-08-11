import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.labelreview import occurrence_id
from webui import server


class LabelReviewApiTests(unittest.TestCase):
    def test_review_and_crop_endpoints_preserve_raw_labels(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            runs_dir = root / "runs"
            run_dir = runs_dir / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps_dir / "sample.png"), np.full((80, 120, 3), 255, np.uint8))
            image = np.full((80, 120, 3), 255, np.uint8)
            cv2.putText(image, "Rhone", (18, 42), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (0, 0, 0), 1, cv2.LINE_AA)
            cv2.imwrite(str(run_dir / "map_text_input.png"), image)
            cv2.imwrite(str(run_dir / "text_mask.png"), np.zeros((80, 120), np.uint8))
            source = {"labels": [{
                "text": "Rhône", "kind": "river_label", "box": [15, 25, 72, 48],
                "recognition_status": "gemini-only", "localization": "gemini-unverified",
                "text_position": [43.5, 36.5], "text_position_source": "box_center",
                "feature_position": None, "feature_position_source": None, "quad": None,
            }], "warnings": []}
            raw_text = json.dumps(source, ensure_ascii=False)
            (run_dir / "labels.json").write_text(raw_text, encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), patch.object(server, "RUNS_DIR", runs_dir):
                client = server.app.test_client()
                review = client.get("/api/labelreview/sample")
                self.assertEqual(review.status_code, 200)
                occurrence = review.get_json()["occurrences"][0]
                self.assertTrue(occurrence["needs_review"])

                crop = client.get("/api/labelcrop/sample/0")
                self.assertEqual(crop.status_code, 200)
                self.assertEqual(crop.mimetype, "image/png")

                saved = client.post("/api/labelreview/sample", json={"decisions": [{
                    "id": occurrence_id(source["labels"][0], 0),
                    "text": "Rhin", "include": True,
                }]})
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.get_json()["approved"], 1)

            self.assertEqual((run_dir / "labels.json").read_text(encoding="utf-8"), raw_text)
            approved = json.loads((run_dir / "approved_labels.json").read_text(encoding="utf-8"))
            self.assertEqual(approved["labels"][0]["text"], "Rhin")


if __name__ == "__main__":
    unittest.main()
