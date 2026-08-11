import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.linereview import automatic_feature_id
from webui import server


class LineReviewApiTests(unittest.TestCase):
    def test_review_writes_authoritative_lines_and_invalidates_later_steps(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir, runs_dir = root / "maps", root / "runs"
            run_dir = runs_dir / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            image = np.full((60, 100, 3), 245, np.uint8)
            mask = np.zeros((60, 100), np.uint8)
            mask[5:55, 5:95] = 255
            cv2.imwrite(str(maps_dir / "sample.png"), image)
            cv2.imwrite(str(run_dir / "map_area.png"), image)
            cv2.imwrite(str(run_dir / "map_mask.png"), mask)
            automatic = {
                "type": "FeatureCollection",
                "features": [{
                    "type": "Feature",
                    "properties": {"kind": "coastline", "source": "map_mask"},
                    "geometry": {"type": "LineString", "coordinates": [[5, 5], [95, 5]]},
                }, {
                    "type": "Feature",
                    "properties": {"kind": "river", "source": "image_processing"},
                    "geometry": {"type": "LineString", "coordinates": [[10, 30], [45, 30]]},
                }],
            }
            (run_dir / "lines_auto.geojson").write_text(
                json.dumps(automatic), encoding="utf-8")
            (run_dir / "lines_gen.geojson").write_text("{}", encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps_dir), patch.object(server, "RUNS_DIR", runs_dir):
                client = server.app.test_client()
                view = client.get("/api/linereview/sample")
                self.assertEqual(view.status_code, 200)
                river_id = automatic_feature_id(automatic["features"][1], 1)
                saved = client.post("/api/linereview/sample", json={
                    "include_auto_ids": [river_id],
                    "manual_rivers": [{
                        "id": "manual-test", "label": "Test River", "edit_kind": "drawn",
                        "points": [[45, 30], [70, 34], [90, 40]],
                    }],
                })
                self.assertEqual(saved.status_code, 200)
                self.assertTrue(saved.get_json()["downstream_invalidated"])

            approved = json.loads((run_dir / "lines.geojson").read_text(encoding="utf-8"))
            self.assertEqual(len(approved["features"]), 3)
            self.assertEqual(approved["features"][-1]["properties"]["source"], "manual_review")
            self.assertFalse((run_dir / "lines_gen.geojson").exists())
            self.assertTrue((run_dir / "lines_auto.geojson").exists())


if __name__ == "__main__":
    unittest.main()
