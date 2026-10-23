"""Isolated /test review workspaces; pipeline functions are used unchanged.

Immutable, content-addressed checkpoints keep later results available while an
earlier stage is edited. Each workspace runs the existing Flask API in its own
process so relative pipeline paths cannot touch another page's working files.
"""

import atexit
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid


def write_json(path, value):
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


class Checkpoints:
    def __init__(self, directory, stem):
        if not isinstance(stem, str) or not stem or stem in {".", ".."} or "/" in stem or "\\" in stem:
            raise ValueError("Invalid review project name")
        self.directory = Path(directory).resolve()
        self.stem = stem
        self.run = self.directory / "runs" / stem
        self.blobs = self.directory / "blobs"
        self.manifests = self.directory / "checkpoints"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "review-state.json"
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "step": None, "drafts": {}, "results": {str(step): "initial" for step in range(1, 10)}}

    def capture(self, name, source=None):
        source = Path(source or self.run)
        files = {}
        for path in source.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            blob = self.blobs / digest
            if not blob.exists():
                shutil.copy2(path, blob)
            stat = path.stat()
            files[path.relative_to(source).as_posix()] = {"blob": digest, "mtime_ns": stat.st_mtime_ns}
        write_json(self.manifests / f"{name}.json", files)
        self.save()

    def save(self):
        write_json(self.state_path, self.state)

    def restore(self, name):
        files = json.loads((self.manifests / f"{name}.json").read_text())
        staging = self.directory / "runs" / f".restore-{uuid.uuid4().hex}"
        staging.mkdir(parents=True)
        try:
            for relative, record in files.items():
                target = (staging / relative).resolve()
                if not target.is_relative_to(staging.resolve()):
                    raise ValueError("Invalid checkpoint path")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.blobs / record["blob"], target)
                os.utime(target, ns=(record["mtime_ns"], record["mtime_ns"]))
            # Only the named project's isolated workspace is ever replaced.
            if self.run.parent != self.directory / "runs" or self.run.name != self.stem:
                raise ValueError("Invalid review workspace")
            if self.run.exists():
                shutil.rmtree(self.run)
            staging.rename(self.run)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def checkout(self, step, keep=False):
        previous = self.state["step"]
        if previous == step:
            return
        if previous and (keep or self.state.get("working_dirty")):
            name = f"draft-{previous}"
            self.capture(name)
            self.state["drafts"][str(previous)] = name
        name = self.state["drafts"].get(str(step)) or self.state["results"][str(step)]
        self.restore(name)
        self.state["step"] = step
        self.state["working_dirty"] = str(step) in self.state["drafts"]
        self.save()

    def checkpoint(self, step, invalidate=False):
        name = f"result-{step}"
        self.capture(name)
        self.state["results"][str(step)] = name
        self.state["drafts"].pop(str(step), None)
        self.state["step"] = step
        stale = set(self.state.get("stale", []))
        stale.discard(step)
        if invalidate:
            stale.update(range(step + 1, 10))
        self.state["stale"] = sorted(stale)
        self.state["working_dirty"] = False
        self.save()


class ReviewProcess:
    def __init__(self, directory, stem, source_run, source_maps, source_config):
        self.directory = Path(directory).resolve()
        self.stem = stem
        self.lock = threading.Lock()
        self.store = Checkpoints(self.directory, stem)
        if not (self.store.manifests / "initial.json").exists():
            self.store.capture("initial", source_run)
            self.store.restore("initial")
        elif not self.store.run.exists():
            self.store.restore(self.store.state["results"].get(str(self.store.state["step"]), "initial"))
        self.update_stamp(source_run, source_maps)
        config = self.directory / "config"
        config.mkdir(exist_ok=True)
        if source_config.exists():
            shutil.copy2(source_config, config / "output_spec.json")
        self.log = (self.directory / "review-worker.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--worker",
             str(self.directory), stem, str(source_maps)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def call(self, payload):
        with self.lock:
            if self.process.poll() is not None:
                raise RuntimeError("The review workspace stopped. Reopen this project to reconnect.")
            self.process.stdin.write(json.dumps(payload) + "\n")
            self.process.stdin.flush()
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("The review workspace could not complete the request.")
            result = json.loads(line)
            if "error" in result:
                raise RuntimeError(result["error"])
            return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.log.close()
        self.process.stdin.close()
        self.process.stdout.close()

    def update_stamp(self, run, maps):
        write_json(self.directory / "source-stamp.json", source_stamp(run, maps, self.stem))


def source_stamp(run, maps, stem):
    files = [Path(run) / "step1_semantics.json", Path(run) / "step9_legend.png"]
    files.extend(path for path in Path(maps).iterdir() if path.is_file() and path.stem == stem)
    return {path.name: [path.stat().st_mtime_ns, path.stat().st_size] for path in files if path.exists()}


_sessions = {}
_session_lock = threading.Lock()
atexit.register(lambda: [session.close() for session in list(_sessions.values())])


def open_session(stem, runs, maps, config):
    with _session_lock:
        stamp = source_stamp(runs / stem, maps, stem)
        existing = next(((key, value) for key, value in _sessions.items()
                         if value.stem == stem and value.directory.parent.parent == runs.resolve()
                         and value.process.poll() is None), None)
        if existing:
            if json.loads((existing[1].directory / "source-stamp.json").read_text()) == stamp:
                return existing
            existing[1].close()
        # The folder is project-specific, persistent, and outside its live run.
        key = hashlib.sha256(f"{runs.resolve()}:{stem}".encode()).hexdigest()[:24]
        directory = runs / ".test-reviews" / key
        stamp_path = directory / "source-stamp.json"
        if stamp_path.exists() and json.loads(stamp_path.read_text()) != stamp:
            # Preserve the old history, but never reuse it for a newly run/uploaded map.
            directory.rename(directory.with_name(f"{key}-previous-{uuid.uuid4().hex[:8]}"))
        for old in _sessions.values():
            if old.process.poll() is None and old.stem != stem:
                try:
                    job = old.call({"path": f"/api/job/{old.stem}", "method": "GET"})
                    status = json.loads(base64.b64decode(job["body"]))
                except (RuntimeError, ValueError):
                    status = {}
                if status.get("status") != "running":
                    old.close()
        session = ReviewProcess(directory, stem, runs / stem, maps, config)
        _sessions[key] = session
        return key, session


def get_session(key):
    session = _sessions.get(key)
    if session is None:
        raise ValueError("Reopen this project to reconnect its review workspace.")
    return session


def allowed_api(path, stem, method):
    """No uploads, source deletion, project renaming, session keys, or shared spec writes."""
    from urllib.parse import unquote, urlsplit
    parts = unquote(urlsplit(path).path).strip("/").split("/")
    if parts[:1] != ["api"] or method not in {"GET", "POST"}:
        return False
    if parts == ["api", "maps"]:
        return method == "GET"
    if parts == ["api", "run"]:
        return method == "POST"
    if parts[:3] == ["api", "test", "step7-edit"]:
        return len(parts) == 4 and parts[3] == stem and method == "POST"
    services = {"artifact", "job", "maskreview", "mask-review", "legendreview", "labelreview",
                "linereview", "aggregation-review", "aggregation-preview", "step6params", "step6presets", "step6preset",
                "step7-review", "pattern-transforms", "pattern-assignments", "pattern-preview",
                "category-colors", "page-layout", "braille-labels", "braille-layout", "step8-review",
                "legend", "legend-swatch", "step9-review", "labelcrop", "download", "export", "export-pdf"}
    return len(parts) >= 3 and parts[1] in services and parts[2] == stem and ".." not in parts


def worker(directory, stem, maps):
    protocol = sys.stdout
    sys.stdout = sys.stderr  # Renderer diagnostics must not enter the RPC stream.
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from webui import server
    server.RUNS_DIR = Path(directory) / "runs"
    server.MAPS_DIR = Path(maps)
    server.PROJECT_ORDER_PATH = server.RUNS_DIR / ".project_order.json"
    os.chdir(directory)  # All unchanged mapgen relative defaults are isolated here.
    checkpoints = Checkpoints(directory, stem)
    for line in sys.stdin:
        try:
            payload = json.loads(line)
            operation = payload.get("operation")
            if operation in {"checkout", "checkpoint"}:
                if (server._jobs.get(stem) or {}).get("status") == "running":
                    raise ValueError("Wait for the current review step to finish.")
                step = payload.get("step")
                if type(step) is not int or not 1 <= step <= 9:
                    raise ValueError("Select a step from 1 to 9")
                if operation == "checkout":
                    checkpoints.checkout(step, payload.get("keep") is True)
                    server._jobs.clear()
                else:
                    checkpoints.checkpoint(step, payload.get("invalidate") is True)
                result = {"ok": True}
            else:
                path, method = payload["path"], payload["method"]
                if not allowed_api(path, stem, method):
                    raise ValueError("This API action is not available in a review workspace.")
                body = base64.b64decode(payload.get("body", ""))
                if path.split("?")[0] == "/api/run":
                    data = json.loads(body)
                    if data.get("stem") != stem or len(data.get("steps", [])) != 1:
                        raise ValueError("Review mode runs only the next individual step.")
                with server.app.test_client() as client:
                    with client.open(path, method=method, data=body,
                                     content_type=payload.get("content_type")) as response:
                        result = {"status": response.status_code,
                                  "headers": {key: value for key, value in response.headers.items()
                                              if key.lower() not in {"content-length", "transfer-encoding"}},
                                  "body": base64.b64encode(response.get_data()).decode()}
                        if method == "POST" and response.status_code < 400 and path.split("?")[0] != "/api/run":
                            checkpoints.state["working_dirty"] = True
                            checkpoints.save()
            protocol.write(json.dumps(result) + "\n")
            protocol.flush()
        except Exception as error:
            protocol.write(json.dumps({"error": str(error)}) + "\n")
            protocol.flush()


if __name__ == "__main__":
    worker(sys.argv[2], sys.argv[3], sys.argv[4])
