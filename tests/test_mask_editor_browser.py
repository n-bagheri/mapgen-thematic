"""Run the /test mask regression fixture on real canvases, without a backend.

Run: .venv_local/Scripts/python.exe -m unittest tests.test_mask_editor_browser
The browser is optional; no packages or browser downloads are required.
"""

import http.server
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit


ROOT = pathlib.Path(__file__).resolve().parents[1]


class FixtureHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        route = unquote(urlsplit(self.path).path)
        target = (ROOT / route.lstrip("/")).resolve()
        allowed = target == ROOT / "tests" / "test_mask_editor.html" or (
            target.is_relative_to(ROOT / "webui" / "static")
        )
        if not allowed:
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, *_args):
        pass


class TestMaskBrowserTests(unittest.TestCase):
    def test_canvas_edits_undo_and_reset(self):
        candidates = [shutil.which(name) for name in ("chromium", "chromium-browser", "google-chrome")]
        for directory in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            root = os.environ.get(directory)
            if root:
                candidates.extend(str(pathlib.Path(root) / suffix) for suffix in (
                    "Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe"))
        browser = next((candidate for candidate in candidates
                        if candidate and pathlib.Path(candidate).is_file()), None)
        if not browser:
            self.skipTest("No installed Chromium browser available for the canvas fixture")
        with http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory(prefix="test-mask-browser-") as profile:
                    result = subprocess.run([
                        browser, "--headless=new", "--disable-gpu", "--no-first-run",
                        "--no-default-browser-check", f"--user-data-dir={profile}",
                        "--dump-dom", "--virtual-time-budget=10000",
                        f"http://127.0.0.1:{server.server_port}/tests/test_mask_editor.html",
                    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    summary = re.search(r'<p id="summary">([^<]+)</p>', result.stdout)
                    failures = re.findall(r'<li class="fail">([^<]+)</li>', result.stdout)
                    self.assertEqual(result.returncode, 0, result.stderr[-1500:])
                    self.assertIsNotNone(summary, result.stderr[-1500:])
                    self.assertRegex(summary.group(1), r"^[1-9]\d* passed, 0 failed$",
                                     "\n".join(failures) or summary.group(1))
            finally:
                server.shutdown()
                thread.join(timeout=5)
