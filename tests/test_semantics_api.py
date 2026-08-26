import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mapgen.semantics import MapSemantics
from webui import server


def _semantics(map_type: str, legend_present: bool = True) -> dict:
    return {
        "map_type": map_type,
        "in_scope": True,
        "data_ordering": "ordered",
        "map_language": "English",
        "subject": "Synthetic thematic map",
        "description": "Synthetic map used to verify the Step 1 gate.",
        "title": "Synthetic map",
        "legend_present": legend_present,
        "legend_title": "Values" if legend_present else None,
        "legend_entries": ([{
            "label": "Low",
            "color_hint": "yellow",
            "is_thematic": True,
            "kind": "area_fill",
        }] if legend_present else []),
        "water_present": False,
        "thematic_classes": [{
            "label": "Low",
            "priority": 1,
            "approx_area_share_percent": 100.0,
        }],
        "non_thematic": [],
        "lines": [],
        "overlay_text": {
            "has_city_labels": False,
            "capital_city": None,
            "has_region_labels": False,
            "has_line_labels": False,
            "notes": "",
        },
    }


class Step1GateApiTests(unittest.TestCase):
    def make_project(self, root: Path, semantics: dict):
        maps_dir = root / "maps"
        runs_dir = root / "runs"
        run_dir = runs_dir / "sample"
        maps_dir.mkdir()
        run_dir.mkdir(parents=True)
        (maps_dir / "sample.png").write_bytes(b"synthetic")
        (run_dir / "step1_semantics.json").write_text(
            json.dumps(semantics), encoding="utf-8")
        return maps_dir, runs_dir

    def test_out_of_scope_map_keeps_step1_but_blocks_later_steps(self):
        with TemporaryDirectory() as directory:
            maps_dir, runs_dir = self.make_project(
                Path(directory), _semantics("choropleth"))
            run_dir = runs_dir / "sample"
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", runs_dir):
                client = server.app.test_client()
                record = client.get("/api/maps").get_json()["maps"][0]
                self.assertTrue(record["steps"]["1"])
                self.assertFalse(record["in_scope"])
                self.assertIsNone(record["step1_error"])
                self.assertIsNone(record["pipeline_error"])
                self.assertTrue(all(not done for step, done in record["steps"].items()
                                    if step != "1"))
                self.assertNotIn("alt_steps", record)

                blocked = client.post("/api/run", json={
                    "stem": "sample", "steps": [2], "model": server.DEFAULT_MODEL,
                })
                self.assertEqual(blocked.status_code, 409)

                alt_removed = client.post("/api/run-alt", json={
                    "stem": "sample", "steps": [5], "model": server.DEFAULT_MODEL,
                })
                self.assertIn(alt_removed.status_code, (404, 405))

                with patch.object(server.threading.Thread, "start"):
                    rerun = client.post("/api/run", json={
                        "stem": "sample", "steps": [1],
                        "model": server.DEFAULT_MODEL,
                    })
                self.assertEqual(rerun.status_code, 200)
                server._jobs.pop("sample", None)

    def test_supported_map_types_can_start_step2(self):
        for map_type in ("area_class_chorochromatic", "isopleth"):
            with self.subTest(map_type=map_type), TemporaryDirectory() as directory:
                maps_dir, runs_dir = self.make_project(
                    Path(directory), _semantics(map_type))
                with patch.object(server, "MAPS_DIR", maps_dir), \
                        patch.object(server, "RUNS_DIR", runs_dir), \
                        patch.object(server.threading.Thread, "start"):
                    client = server.app.test_client()
                    record = client.get("/api/maps").get_json()["maps"][0]
                    self.assertTrue(record["steps"]["1"])
                    self.assertTrue(record["in_scope"])
                    self.assertIsNone(record["step1_error"])
                    self.assertIsNone(record["pipeline_error"])
                    response = client.post("/api/run", json={
                        "stem": "sample", "steps": [2],
                        "model": server.DEFAULT_MODEL,
                    })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(server._jobs["sample"]["steps"], [2])
                server._jobs.pop("sample", None)

    def test_run_remaining_continues_for_supported_map(self):
        record = {
            "status": "running", "steps": [1, 2, 3], "current": None,
            "model": server.DEFAULT_MODEL, "log": [], "error": None,
        }
        server._jobs["sample"] = record
        try:
            with patch.object(server, "_run_single_step", return_value=None) as run:
                server._job_worker(
                    "sample", Path("sample.png"), [1, 2, 3], server.DEFAULT_MODEL)
            self.assertEqual([call.args[0] for call in run.call_args_list], [1, 2, 3])
            self.assertEqual(record["status"], "done")
            self.assertIsNone(record["error"])
            self.assertFalse(any("PIPELINE STOPPED" in line for line in record["log"]))
        finally:
            server._jobs.pop("sample", None)

    def test_successful_step1_rerun_retires_stale_step2_and_later_artifacts(self):
        with TemporaryDirectory() as directory:
            maps_dir, runs_dir = self.make_project(
                Path(directory), _semantics("isopleth"))
            run_dir = runs_dir / "sample"
            stale = (
                "step2_layout.json", "classes.json", "geometry.json",
                "step9_legend.png", "step6_preset_3_step6_debug.png",
            )
            for name in stale:
                (run_dir / name).write_text("stale", encoding="utf-8")
            result = MapSemantics.model_validate(_semantics("isopleth"))
            record = {
                "status": "running", "steps": [1], "current": None,
                "model": server.DEFAULT_MODEL, "log": [], "error": None,
            }
            server._jobs["sample"] = record
            try:
                with patch.object(server, "RUNS_DIR", runs_dir), \
                        patch.object(server, "_run_single_step", return_value=result):
                    server._job_worker(
                        "sample", maps_dir / "sample.png", [1], server.DEFAULT_MODEL)

                self.assertTrue((run_dir / "step1_semantics.json").exists())
                self.assertTrue(all(not (run_dir / name).exists() for name in stale))
                self.assertTrue(any("stale downstream" in line for line in record["log"]))
            finally:
                server._jobs.pop("sample", None)

    def test_run_remaining_stops_after_successful_choropleth_step1(self):
        semantics = MapSemantics.model_validate(_semantics("choropleth"))
        record = {
            "status": "running", "steps": [1, 2, 3], "current": None,
            "model": server.DEFAULT_MODEL, "log": [], "error": None,
        }
        server._jobs["sample"] = record
        try:
            with patch.object(server, "_run_single_step", return_value=semantics) as run:
                server._job_worker(
                    "sample", Path("sample.png"), [1, 2, 3], server.DEFAULT_MODEL)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], 1)
            self.assertEqual(record["status"], "done")
            self.assertIsNone(record["error"])
            self.assertTrue(any("PIPELINE STOPPED" in line for line in record["log"]))
        finally:
            server._jobs.pop("sample", None)

    def test_failed_rerun_retains_existing_valid_choropleth_result(self):
        with TemporaryDirectory() as directory:
            maps_dir, runs_dir = self.make_project(
                Path(directory), _semantics("choropleth"))
            record = {
                "status": "running", "steps": [1, 2], "current": None,
                "model": server.DEFAULT_MODEL, "log": [], "error": None,
            }
            stale_step2 = runs_dir / "sample" / "classes.json"
            stale_step2.write_text('{"classes": []}', encoding="utf-8")
            server._jobs["sample"] = record
            try:
                with patch.object(server, "MAPS_DIR", maps_dir), \
                        patch.object(server, "RUNS_DIR", runs_dir), \
                        patch.object(server, "_run_single_step",
                                     side_effect=ValueError("truncated JSON")) as run:
                    server._job_worker(
                        "sample", maps_dir / "sample.png", [1, 2],
                        server.DEFAULT_MODEL)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(record["status"], "done")
                self.assertIsNone(record["error"])
                self.assertTrue(any("existing valid Step 1 result was retained" in line
                                    for line in record["log"]))
                self.assertTrue(any("choropleth" in line and "out of scope" in line
                                    for line in record["log"]))
                self.assertTrue(stale_step2.exists())
            finally:
                server._jobs.pop("sample", None)

    def test_missing_legend_completes_step1_and_does_not_block_the_pipeline(self):
        """A sheet without a legend still gets a tactile map: Step 2 derives
        the class palette from the map's dominant colours, so the UI must not
        report the run as blocked after Step 1."""
        with TemporaryDirectory() as directory:
            maps_dir, runs_dir = self.make_project(
                Path(directory), _semantics("isopleth", legend_present=False))
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", runs_dir):
                record = server.app.test_client().get(
                    "/api/maps").get_json()["maps"][0]
            self.assertTrue(record["steps"]["1"])
            self.assertTrue(record["in_scope"])
            self.assertIsNone(record["step1_error"])
            self.assertIsNone(record["pipeline_error"])
            self.assertTrue(all(not done for step, done in record["steps"].items()
                                if step != "1"))


if __name__ == "__main__":
    unittest.main()
