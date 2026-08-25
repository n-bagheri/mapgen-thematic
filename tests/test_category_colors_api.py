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

    def test_color_change_only_rerenders_hybrid_layers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            runs_dir = root / "runs"
            maps_dir.mkdir()
            run_dir = runs_dir / "demo"
            run_dir.mkdir(parents=True)
            Image.new("RGB", (2, 2), "white").save(maps_dir / "demo.png")
            (run_dir / "symbols.json").write_text(json.dumps({
                "area_assignments": [{
                    "label": "Forest", "pattern": "01_noise_dots", "members": [0],
                }],
            }), encoding="utf-8")

            with (patch.object(server, "MAPS_DIR", maps_dir),
                  patch.object(server, "RUNS_DIR", runs_dir),
                  patch("mapgen.symbols.rerender_hybrid_artifacts") as rerender_hybrid,
                  patch("mapgen.symbols.rerender_step7_artifacts") as rerender_tactile,
                  patch("mapgen.boundaries.run_step8") as rerender_boundaries,
                  patch("mapgen.cleanup.run_step8a") as rerender_cleanup):
                response = server.app.test_client().post(
                    "/api/category-colors/demo",
                    json={"colors": {"Forest": "#112233"}},
                )

            self.assertEqual(response.status_code, 200)
            rerender_hybrid.assert_called_once_with(run_dir)
            rerender_tactile.assert_not_called()
            rerender_boundaries.assert_not_called()
            rerender_cleanup.assert_not_called()
            saved_symbols = json.loads((run_dir / "symbols.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_symbols["area_assignments"][0]["color"], "#112233")


if __name__ == "__main__":
    unittest.main()
