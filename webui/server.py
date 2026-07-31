"""Local web UI backend for the MapGen pipeline (Steps 0-4).

Run:    .venv\\Scripts\\python.exe webui\\server.py
Open:   http://127.0.0.1:5001

Design: the pipeline stays in mapgen/*; this server only wraps it. Steps run
in a background thread (one job per map at a time) and the frontend polls the
job log. Artifacts are served straight from runs/<stem>/.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # mapgen uses relative config/ runs/ paths

from flask import Flask, abort, jsonify, request, send_from_directory  # noqa: E402

from mapgen.output_spec import DEFAULT_CONFIG_PATH, OutputSpec, PhysicalConstants  # noqa: E402
from mapgen.semantics import (  # noqa: E402
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    semantics_artifact_is_current,
)

MAPS_DIR = ROOT / "maps"
RUNS_DIR = ROOT / "runs"
IMG_EXTS = {".png", ".jpg", ".jpeg"}
SAFE_NAME = re.compile(r"^[\w.\- ()\[\]äöüÄÖÜéèêàçñ]+$")

app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"), static_url_path="")

_lock = threading.Lock()
_jobs: dict[str, dict] = {}  # stem -> job record

STEP_ARTIFACTS = {
    1: ("step1_semantics.json",),
    2: ("step2_layout_debug.png", "step2_debug.png", "map_text_input.png",
        "classes.json", "geometry.json"),
    3: ("step3_debug.png", "labels.json"),
    4: ("step4_debug.png", "classes_final.json", "regions.geojson", "lines.geojson"),
    5: ("step5_debug.png", "classes_gen.json", "regions_gen.geojson",
        "lines_gen.geojson", "step5_summary.json"),
    6: ("aggregation.json",),
    7: ("symbols.json", "step7_tactile.png", "step7_debug.png"),
}
# additional files a reset must clear (cached model calls, intermediates)
STEP_EXTRA = {
    1: (),
    2: ("step2_layout.json", "map_area.png", "map_mask.png", "map_text_input.png",
        "legend.png"),
    3: ("step3_raw.json", "step3_craft.json", "text_mask.png"),
    4: ("label_map.png",),
    5: ("label_map_gen.png",),
    6: (),
    7: (),
}


def map_files() -> list[Path]:
    if not MAPS_DIR.exists():
        return []
    return sorted((p for p in MAPS_DIR.iterdir() if p.suffix.lower() in IMG_EXTS),
                  key=lambda p: (-p.stat().st_mtime_ns, p.name.lower()))


def find_map(stem: str) -> Path | None:
    return next((p for p in map_files() if p.stem == stem), None)


def step_done(stem: str, step: int) -> bool:
    paths = [RUNS_DIR / stem / a for a in STEP_ARTIFACTS[step]]
    if not all(path.exists() for path in paths):
        return False
    if step == 1:
        return semantics_artifact_is_current(paths[0])
    return True


# --------------------------------------------------------------------------- jobs

def _run_single_step(step: int, image: Path, log, model: str) -> None:
    if step == 1:
        from mapgen.semantics import interpret_map, save_semantics
        sem = interpret_map(image, model=model, status=log)
        save_semantics(sem, image)
        log(f"{sem.map_type.value} | ordering={sem.data_ordering.value} | "
            f"language={sem.map_language} | "
            f"{len(sem.thematic_classes)} thematic classes | water={sem.water_present}")
    elif step == 2:
        from mapgen.isolate import run_step2
        r = run_step2(image, model=model)
        log(f"legend={'yes' if r['legend'] else 'no'}; colors sampled "
            f"{r['classes_with_color']}/{r['classes_total']}")
        for w in r["warnings"]:
            log("WARN: " + w)
    elif step == 3:
        from mapgen.textdetect import run_step3
        r = run_step3(image, model=model)
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(r["kinds"].items())) or "none"
        log(f"{r['total']} labels ({kinds}); strokes masked {r['masked']}/{r['total']}")
        for w in r["warnings"]:
            log("WARN: " + w)
    elif step == 4:
        from mapgen.segment import run_step4
        r = run_step4(image, model=model)
        lines = ", ".join(f"{k}={v}" for k, v in sorted(r["line_kinds"].items())) or "none"
        log(f"{r['polygons']} polygons; {r['polylines']} polylines ({lines})")
        for n in r["notes"]:
            log("NOTE: " + n)
    elif step == 5:
        from mapgen.generalize import run_step5_presets
        r = run_step5_presets(image, model=model)
        s = r["summary"]
        log(f"scale {s['scale_mm_per_px']} mm/px ({s['orientation']}); "
            f"map {s['map_size_mm'][0]}x{s['map_size_mm'][1]} mm")
        log(f"dissolved {s['dissolved_components']}; islands dropped {s['islands']['dropped']}, "
            f"exaggerated {s['islands']['exaggerated']}; lines kept {s['lines_kept']} "
            f"({s['line_joins']} joins, {s['lines_dropped_short']} too short)")
        if s["classes_vanished"]:
            log("NOTE: vanished classes: " + ", ".join(s["classes_vanished"]))
    elif step == 6:
        from mapgen.aggregate import run_step6
        r = run_step6(image, model=model)
        a = r["aggregation"]
        log(f"mode={a['mode']}; {len(a['groups'])} groups in {a['slots']} slots; "
            f"water={'yes' if a['water'] else 'no'}")
        for g in a["groups"]:
            log(f"  {g['label']} <- {', '.join(g['member_labels'])}")
        for n in a["notes"]:
            log("NOTE: " + n)
    elif step == 7:
        from mapgen.symbols import run_step7
        r = run_step7(image, model=model)
        for a in r["assignments"]:
            log(f"  {a['label']} -> {a['pattern_desc']}")
        for n in r["notes"]:
            log("NOTE: " + n)
    else:
        raise ValueError(f"unknown step {step}")


def _job_worker(stem: str, image: Path, steps: list[int], model: str) -> None:
    rec = _jobs[stem]

    def log(msg: str) -> None:
        with _lock:
            rec["log"].append(msg)

    try:
        log(f"model: {model}")
        for s in steps:
            with _lock:
                rec["current"] = s
            log(f"--- step {s} ---")
            _run_single_step(s, image, log, model)
        with _lock:
            rec["status"] = "done"
            rec["current"] = None
    except Exception as exc:  # noqa: BLE001 - job boundary
        log("ERROR: " + "".join(traceback.format_exception_only(exc)).strip())
        with _lock:
            rec["status"] = "failed"
            rec["error"] = str(exc)
            rec["current"] = None


# --------------------------------------------------------------------------- api

@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/api/maps")
def api_maps():
    with _lock:
        snapshot = {k: {"status": v["status"], "current": v["current"],
                        "steps": v["steps"], "model": v["model"]}
                    for k, v in _jobs.items()}
    maps = []
    for p in map_files():
        maps.append({
            "name": p.name,
            "stem": p.stem,
            "steps": {str(s): step_done(p.stem, s) for s in STEP_ARTIFACTS},
            "job": snapshot.get(p.stem),
        })
    return jsonify({"maps": maps})


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "no file")
    name = Path(f.filename).name
    if Path(name).suffix.lower() not in IMG_EXTS:
        abort(400, "only png/jpg maps")
    name = re.sub(r"[^\w.\-]", "_", name)
    dest = MAPS_DIR / name
    i = 1
    while dest.exists():
        dest = MAPS_DIR / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    MAPS_DIR.mkdir(exist_ok=True)
    f.save(dest)
    return jsonify({"ok": True, "name": dest.name})


@app.delete("/api/maps/<stem>")
def api_delete_map(stem: str):
    """Delete an uploaded map and every artifact produced for it."""
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "cannot delete a project while its job is running")

    image.unlink()
    run_dir = RUNS_DIR / stem
    if run_dir.is_dir():
        shutil.rmtree(run_dir)
    with _lock:
        _jobs.pop(stem, None)
    return jsonify({"ok": True, "deleted": image.name})


@app.post("/api/run")
def api_run():
    data = request.get_json(force=True)
    stem = data.get("stem", "")
    steps = sorted({int(s) for s in data.get("steps", [])})
    if not steps or any(s not in STEP_ARTIFACTS for s in steps):
        abort(400, "steps must be within 1-7")
    model = str(data.get("model") or DEFAULT_MODEL)
    if model not in {model_id for model_id, _ in AVAILABLE_MODELS}:
        abort(400, "unsupported model")
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is already running for this map")
        _jobs[stem] = {"status": "running", "steps": steps, "current": None,
                       "model": model, "log": [], "error": None}
    threading.Thread(target=_job_worker, args=(stem, image, steps, model), daemon=True).start()
    return jsonify({"ok": True})


@app.get("/api/models")
def api_models():
    return jsonify({
        "default": DEFAULT_MODEL,
        "models": [{"id": model_id, "label": label} for model_id, label in AVAILABLE_MODELS],
    })


@app.get("/api/job/<stem>")
def api_job(stem: str):
    with _lock:
        job = _jobs.get(stem)
        return jsonify(dict(job) if job else {"status": "idle"})


@app.post("/api/reset")
def api_reset():
    data = request.get_json(force=True)
    stem = data.get("stem", "")
    from_step = int(data.get("from_step", 1))
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    removed = []
    run_dir = RUNS_DIR / stem
    for s in range(from_step, 8):
        for name in STEP_ARTIFACTS[s] + STEP_EXTRA[s]:
            p = run_dir / name
            if p.exists():
                p.unlink()
                removed.append(name)
    if from_step <= 5:
        for p in run_dir.glob("step5_preset_*"):
            if p.is_file():
                p.unlink()
                removed.append(p.name)
    return jsonify({"ok": True, "removed": removed})


@app.get("/api/artifact/<stem>/<name>")
def api_artifact(stem: str, name: str):
    if not SAFE_NAME.match(name) or "/" in name or "\\" in name:
        abort(400)
    d = (RUNS_DIR / stem).resolve()
    if not d.is_dir() or RUNS_DIR.resolve() not in d.parents:
        abort(404)
    response = send_from_directory(d, name)
    # Pipeline artifacts are intentionally overwritten when a step is rerun.
    # Never let a browser reuse an earlier preview under the same filename.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.get("/api/mapimg/<name>")
def api_mapimg(name: str):
    if "/" in name or "\\" in name:
        abort(400)
    return send_from_directory(MAPS_DIR.resolve(), name)


@app.get("/api/geocounts/<stem>")
def api_geocounts(stem: str):
    import json as _json
    gen = request.args.get("gen") == "1"
    suffix = "_gen" if gen else ""
    out = {"polygons": None, "polylines": None, "line_kinds": {}}
    rp = RUNS_DIR / stem / f"regions{suffix}.geojson"
    lp = RUNS_DIR / stem / f"lines{suffix}.geojson"
    if rp.exists():
        out["polygons"] = len(_json.loads(rp.read_text(encoding="utf-8"))["features"])
    if lp.exists():
        feats = _json.loads(lp.read_text(encoding="utf-8"))["features"]
        out["polylines"] = len(feats)
        for f in feats:
            k = f["properties"]["kind"]
            out["line_kinds"][k] = out["line_kinds"].get(k, 0) + 1
    return jsonify(out)


@app.get("/api/step5params/<stem>")
def api_step5params_get(stem: str):
    import json as _json
    from mapgen.generalize import DEFAULT_PARAMS, load_params
    run_dir = RUNS_DIR / stem
    params = load_params(run_dir) if run_dir.exists() else dict(DEFAULT_PARAMS)
    classes = []
    cf = run_dir / "classes_final.json"
    if cf.exists():
        for c in _json.loads(cf.read_text(encoding="utf-8"))["classes"]:
            if c["area_share"] >= 0.001:
                classes.append({"index": c["index"], "label": c["label"],
                                "is_thematic": c["is_thematic"], "share": c["area_share"]})
    spec = OutputSpec.load_or_create()
    return jsonify({
        "params": params,
        "classes": classes,
        "defaults": {
            "min_texture_area_side_mm": spec.constants.min_texture_area_side_mm,
        },
    })


@app.post("/api/step5params/<stem>")
def api_step5params_post(stem: str):
    import json as _json
    from mapgen.generalize import ALL_LINE_KINDS, DEFAULT_PARAMS
    data = request.get_json(force=True)
    params = {}
    try:
        level = data.get("simplification_level", DEFAULT_PARAMS["simplification_level"])
        params["simplification_level"] = (
            None if level in (None, "") else max(1, min(5, int(level)))
        )
        v = data.get("min_texture_area_side_mm")
        params["min_texture_area_side_mm"] = None if v in (None, "", 0) else max(3.0, float(v))
        params["smooth_mm"] = max(0.0, float(data.get("smooth_mm", DEFAULT_PARAMS["smooth_mm"])))
        params["preserve_share"] = max(0.0, float(data.get("preserve_share",
                                                           DEFAULT_PARAMS["preserve_share"])))
        params["keep_line_kinds"] = [k for k in data.get("keep_line_kinds", ALL_LINE_KINDS)
                                     if k in ALL_LINE_KINDS]
        params["protected_classes"] = [int(i) for i in data.get("protected_classes", [])]
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    run_dir = RUNS_DIR / stem
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "step5_params.json").write_text(_json.dumps(params, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/step5presets/<stem>")
def api_step5presets_get(stem: str):
    import json as _json
    from mapgen.generalize import (PRESET_ARTIFACTS, SIMPLIFICATION_PRESETS,
                                   load_params, preset_artifact_name)
    run_dir = RUNS_DIR / stem
    variants = {}
    for level in SIMPLIFICATION_PRESETS:
        files = {name: run_dir / preset_artifact_name(level, name)
                 for name in PRESET_ARTIFACTS}
        if not all(p.exists() for p in files.values()):
            continue
        summary = _json.loads(files["step5_summary.json"].read_text(encoding="utf-8"))
        classes = _json.loads(files["classes_gen.json"].read_text(encoding="utf-8"))["classes"]
        regions = _json.loads(files["regions_gen.geojson"].read_text(encoding="utf-8"))["features"]
        lines = _json.loads(files["lines_gen.geojson"].read_text(encoding="utf-8"))["features"]
        variants[str(level)] = {
            "debug_artifact": files["step5_debug.png"].name,
            "summary": summary,
            "classes": classes,
            "polygons": len(regions),
            "polylines": len(lines),
        }
    params = load_params(run_dir) if run_dir.exists() else {}
    active = params.get("simplification_level")
    return jsonify({
        "ready": len(variants) == len(SIMPLIFICATION_PRESETS),
        "active_level": active if active in SIMPLIFICATION_PRESETS else None,
        "variants": variants,
    })


@app.post("/api/step5preset/<stem>")
def api_step5preset_post(stem: str):
    from mapgen.generalize import activate_preset
    if find_map(stem) is None:
        abort(404, "unknown map")
    try:
        level = int(request.get_json(force=True).get("level"))
    except (TypeError, ValueError):
        abort(400, "level must be from 1 to 5")
    if level not in range(1, 6):
        abort(400, "level must be from 1 to 5")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    try:
        activate_preset(RUNS_DIR / stem, level)
    except FileNotFoundError:
        abort(409, "generate the five previews first")
    return jsonify({"ok": True, "active_level": level})


@app.get("/api/spec")
def api_spec_get():
    spec = OutputSpec.load_or_create()
    return jsonify({"path": str(DEFAULT_CONFIG_PATH),
                    "spec": DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")})


@app.post("/api/spec")
def api_spec_post():
    import json as _json
    try:
        data = _json.loads(request.get_json(force=True)["spec"])
        constants = PhysicalConstants(**data.pop("constants", {}))
        spec = OutputSpec(constants=constants, **data)
        spec.save()
    except Exception as exc:  # noqa: BLE001 - validation boundary
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True})


if __name__ == "__main__":
    print(f"MapGen UI on http://127.0.0.1:5001  (project root: {ROOT})")
    app.run(host="127.0.0.1", port=5001, threaded=True)
