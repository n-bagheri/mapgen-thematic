import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import cv2
import numpy as np

from webui import server


class CleanupApiTests(unittest.TestCase):
    def test_run_api_rejects_the_former_step8a_as_a_canonical_step(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            maps_dir.mkdir()
            cv2.imwrite(str(maps_dir / "sample.png"),
                        np.full((8, 8, 3), 255, np.uint8))
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"), \
                    patch.object(server.threading.Thread, "start"):
                client = server.app.test_client()
                response = client.post("/api/run", json={
                    "stem": "sample", "steps": ["8a"],
                    "model": server.DEFAULT_MODEL,
                })
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("sample", server._jobs)

    def test_reset_from_step7_removes_the_entire_final_render_stage(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir, run_dir = root / "maps", root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            cv2.imwrite(str(maps_dir / "sample.png"),
                        np.full((8, 8, 3), 255, np.uint8))
            for name in server.STEP_ARTIFACTS[7]:
                (run_dir / name).write_bytes(b"final")
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                response = server.app.test_client().post(
                    "/api/reset", json={"stem": "sample", "from_step": 7},
                )
            self.assertEqual(response.status_code, 200)
            self.assertTrue(all(not (run_dir / name).exists()
                                for name in server.STEP_ARTIFACTS[7]))


if __name__ == "__main__":
    unittest.main()
