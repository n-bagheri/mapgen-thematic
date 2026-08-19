import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.postprocess import (STEP6_METHOD_VERSION, STEP6_PRESET_ARTIFACTS,
                                step6_preset_artifact_name)
from webui import server


def semantics_payload():
    return {
        "map_type": "area_class_chorochromatic",
        "in_scope": True,
        "data_ordering": "qualitative",
        "map_language": "English",
        "subject": "Synthetic map",
        "description": "Synthetic Step 6 review map.",
        "title": None,
        "legend_present": True,
        "legend_title": "Classes",
        "legend_entries": [{
            "label": "Forest", "color_hint": "green", "is_thematic": True,
        }],
        "water_present": False,
        "thematic_classes": [{
            "label": "Forest", "priority": 1, "approx_area_share_percent": 100,
        }],
        "non_thematic": [],
        "lines": [],
        "overlay_text": {
            "has_city_labels": False, "capital_city": None,
            "has_region_labels": False, "has_line_labels": False, "notes": "",
        },
    }


class Step6ReviewApiTests(unittest.TestCase):
    def test_activating_a_preset_records_the_decision_and_clears_every_later_step(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps_dir / "sample.png"),
                        np.full((8, 8, 3), 255, np.uint8))
            (run_dir / "step1_semantics.json").write_text(
                json.dumps(semantics_payload()), encoding="utf-8")

            level = 4
            summary = {"params": {
                "method_version": STEP6_METHOD_VERSION,
                "simplification_level": level,
            }}
            for name in STEP6_PRESET_ARTIFACTS:
                path = run_dir / step6_preset_artifact_name(level, name)
                path.write_bytes(json.dumps(summary).encode("utf-8")
                                 if name == "step6_summary.json" else b"preset")
            downstream = []
            for step in (7, 8, 9):
                for name in server.STEP_ARTIFACTS[step] + server.STEP_EXTRA[step]:
                    path = run_dir / name
                    path.write_bytes(b"stale")
                    downstream.append(path)

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                client = server.app.test_client()
                response = client.post("/api/step6preset/sample", json={"level": level})
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
                review = json.loads((run_dir / "step6_review.json").read_text(
                    encoding="utf-8"))
                self.assertTrue(review["approved"])
                self.assertEqual(review["level"], level)
                self.assertTrue(server.step6_review_ready("sample"))
                map_record = client.get("/api/maps").get_json()["maps"][0]
                self.assertTrue(map_record["step6_review_ready"])
                self.assertTrue(all(not path.exists() for path in downstream))

                with patch.object(server.threading.Thread, "start"):
                    rerun = client.post("/api/run", json={
                        "stem": "sample", "steps": [6], "model": server.DEFAULT_MODEL,
                    })
                self.assertEqual(rerun.status_code, 200, rerun.get_data(as_text=True))
                self.assertFalse((run_dir / "step6_review.json").exists())
                server._jobs.pop("sample", None)


if __name__ == "__main__":
    unittest.main()
