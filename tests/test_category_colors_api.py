import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from mapgen.aggregate import save_aggregation_review
from webui import server


class CategoryColorsApiTests(unittest.TestCase):
    @staticmethod
    def proposal():
        return {
            "slots": 2,
            "review_required": True,
            "source_classes": [
                {"index": 0, "label": "Forest"},
                {"index": 1, "label": "Cropland"},
            ],
            "groups": [
                {"label": "Forest", "members": [0], "member_labels": ["Forest"]},
                {"label": "Cropland", "members": [1], "member_labels": ["Cropland"]},
            ],
        }

    def test_colors_require_approval_and_persist_by_category(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            runs_dir = root / "runs"
            maps_dir.mkdir()
            run_dir = runs_dir / "demo"
            run_dir.mkdir(parents=True)
            Image.new("RGB", (2, 2), "white").save(maps_dir / "demo.png")
            proposal = self.proposal()
            (run_dir / "aggregation.json").write_text(
                json.dumps(proposal), encoding="utf-8",
            )

            with (patch.object(server, "MAPS_DIR", maps_dir),
                  patch.object(server, "RUNS_DIR", runs_dir)):
                client = server.app.test_client()
                blocked = client.post("/api/category-colors/demo", json={
                    "colors": {"Forest": "#59F7FF"},
                })
                self.assertEqual(blocked.status_code, 409)

                save_aggregation_review(run_dir, proposal, [
                    {"label": "Forest", "members": [0], "approved": True},
                    {"label": "Cropland", "members": [1], "approved": True},
                ])
                saved = client.post("/api/category-colors/demo", json={
                    "colors": {"Forest": "#59f7ff"},
                })
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.get_json()["colors"], {"Forest": "#59F7FF"})
                self.assertEqual(client.get("/api/category-colors/demo").get_json(),
                                 {"colors": {"Forest": "#59F7FF"}})


if __name__ == "__main__":
    unittest.main()
