"""Local web UI backend for the MapGen pipeline (Steps 0-4).

Run:    .venv\\Scripts\\python.exe webui\\server.py
Open:   http://127.0.0.1:5001

Design: the pipeline stays in mapgen/*; this server only wraps it. Steps run
in a background thread (one job per map at a time) and the frontend polls the
job log. Artifacts are served straight from runs/<stem>/.
"""

from __future__ import annotations

import io
import json
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

from flask import Flask, abort, jsonify, request, send_file, send_from_directory  # noqa: E402

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
    8: ("step8_boundaries.json", "step8_boundaries.png", "step8_debug.png"),
    "8a": ("step8a_cleanup.json", "step8a_cleanup.png", "step8a_debug.png"),
}
# additional files a reset must clear (cached model calls, intermediates)
STEP_EXTRA = {
    1: (),
    2: ("step2_layout.json", "map_area.png", "map_mask.png", "map_text_input.png",
        "legend.png"),
    3: ("step3_raw.json", "step3_raw.sha256", "step3_craft.json",
        "step3_craft.sha256", "step3_lines_raw.json", "step3_lines_raw.sha256",
        "line_guidance.json", "text_mask.png", "label_review.json",
        "approved_labels.json", "text_removal_mask.png", "text_removal_mask.json"),
    4: ("label_map.png", "label_map_preview.png", "step4_text_removed_input.png",
        "step4_lines_preview.png", "coastline_cleanup_mask.png", "river_cleanup_mask.png",
        "line_extraction.json", "lines_auto.geojson",
        "approved_lines.geojson", "line_review.json"),
    5: ("label_map_gen.png", "label_map_gen_preview.png"),
    6: ("aggregation_review.json",),
    7: ("overlay_labels.json",),
    8: (),
    "8a": (),
}

CANONICAL_STEP_ORDER = (1, 2, 3, 4, 5, 6, 7, 8, "8a")


def _canonical_step(value) -> int | str:
    if str(value).strip().lower() == "8a":
        return "8a"
    return int(value)


def _canonical_steps_from(step: int | str) -> tuple[int | str, ...]:
    normalized = _canonical_step(step)
    return CANONICAL_STEP_ORDER[CANONICAL_STEP_ORDER.index(normalized):]

ALT_STEP_ARTIFACTS = {
    5: ("alt_aggregation.json", "alt_step5_aggregation_preview.png",
        "alt_step5_source_audit.json"),
    6: ("alt_label_map_gen.png", "alt_label_map_gen_preview.png",
        "alt_classes_gen.json", "alt_regions_gen.geojson", "alt_lines_gen.geojson", "alt_step6_summary.json",
        "alt_step6_debug.png", "alt_step6_transitions.json"),
    7: ("alt_symbols.json", "alt_step7_tactile.png", "alt_step7_debug.png",
        "alt_step7_generalization.json", "alt_group_map_tactile.png",
        "alt_step7_regions_preview.png"),
}
ALT_STEP_EXTRA = {
    5: ("alt_aggregation_review.json", "alt_group_map_source.png", "alt_groups.json"),
    6: ("alt_step6_changes.png", "alt_step6_params.json"),
    7: ("alt_overlay_labels.json", "step7_comparison.png"),
}


def map_files() -> list[Path]:
    if not MAPS_DIR.exists():
        return []
    return sorted((p for p in MAPS_DIR.iterdir() if p.suffix.lower() in IMG_EXTS),
                  key=lambda p: (-p.stat().st_mtime_ns, p.name.lower()))


def find_map(stem: str) -> Path | None:
    return next((p for p in map_files() if p.stem == stem), None)


def step_done(stem: str, step: int | str) -> bool:
    paths = [RUNS_DIR / stem / a for a in STEP_ARTIFACTS[step]]
    if not all(path.exists() for path in paths):
        return False
    if step == 1:
        return semantics_artifact_is_current(paths[0])
    if step == 7:
        aggregation_path = RUNS_DIR / stem / "aggregation.json"
        if aggregation_path.exists():
            from mapgen.aggregate import effective_aggregation
            try:
                effective_aggregation(
                    RUNS_DIR / stem,
                    json.loads(aggregation_path.read_text(encoding="utf-8")))
            except RuntimeError:
                return False
    if step == 8:
        run_dir = RUNS_DIR / stem
        inputs = [run_dir / name for name in
                  ("step7_tactile.png", "symbols.json", "label_map_gen.png")]
        if not all(path.exists() for path in inputs):
            return False
    if step == "8a":
        run_dir = RUNS_DIR / stem
        inputs = [run_dir / name for name in
                  ("step8_boundaries.json", "symbols.json", "label_map_gen.png")]
        if not all(path.exists() for path in inputs):
            return False
        if min(path.stat().st_mtime_ns for path in paths) < max(
                path.stat().st_mtime_ns for path in inputs):
            return False
        if min(path.stat().st_mtime_ns for path in paths) < max(
                path.stat().st_mtime_ns for path in inputs):
            return False
    return True


def step6_review_ready(stem: str) -> bool:
    path = RUNS_DIR / stem / "aggregation.json"
    if not path.exists():
        return False
    from mapgen.aggregate import effective_aggregation
    try:
        effective_aggregation(
            RUNS_DIR / stem, json.loads(path.read_text(encoding="utf-8")))
    except RuntimeError:
        return False
    return True


def alt_step_done(stem: str, step: int) -> bool:
    run_dir = RUNS_DIR / stem
    if not all((run_dir / name).exists() for name in ALT_STEP_ARTIFACTS[step]):
        return False
    if step in (6, 7):
        summary_path = run_dir / "alt_step6_summary.json"
        if not summary_path.exists():
            return False
        from mapgen.alt_mapgen import ALT_STEP6_METHOD_VERSION
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if summary.get("params", {}).get("method_version") != ALT_STEP6_METHOD_VERSION:
            return False
    if step == 7:
        aggregation_path = run_dir / "alt_aggregation.json"
        if not aggregation_path.exists():
            return False
        from mapgen.alt_aggregate import effective_aggregation
        try:
            effective_aggregation(
                run_dir, json.loads(aggregation_path.read_text(encoding="utf-8")))
        except RuntimeError:
            return False
    return True


def alt_step5_review_ready(stem: str) -> bool:
    run_dir = RUNS_DIR / stem
    path = run_dir / "alt_aggregation.json"
    if not path.exists():
        return False
    if not all((run_dir / name).exists()
               for name in ("alt_group_map_source.png", "alt_groups.json")):
        return False
    from mapgen.alt_aggregate import effective_aggregation
    try:
        effective_aggregation(
            run_dir, json.loads(path.read_text(encoding="utf-8")))
    except RuntimeError:
        return False
    return True


def _remove_alt_from(run_dir: Path, from_step: int,
                     remove_relationships: bool = False) -> list[str]:
    removed = []
    for step in range(max(5, from_step), 8):
        for name in ALT_STEP_ARTIFACTS[step] + ALT_STEP_EXTRA[step]:
            path = run_dir / name
            if path.exists():
                path.unlink()
                removed.append(name)
    if from_step <= 6:
        for path in run_dir.glob("alt_step6_preset_*"):
            if path.is_file():
                path.unlink()
                removed.append(path.name)
    if from_step <= 5:
        for path in run_dir.glob("alt_step5_preset_*"):
            if path.is_file():
                path.unlink()
                removed.append(path.name)
        for name in ("alt_step5_params.json", "alt_step5_summary.json",
                     "alt_step5_debug.png", "alt_step5_changes.png",
                     "alt_step5_transitions.json", "alt_step5_merge_log.json",
                     "alt_class_relationships.json"):
            path = run_dir / name
            if path.exists():
                path.unlink()
                removed.append(name)
    if remove_relationships:
        path = run_dir / "alt_class_relationships.json"
        if path.exists():
            path.unlink()
            removed.append(path.name)
    return removed


# --------------------------------------------------------------------------- jobs

def _run_single_step(step: int | str, image: Path, log, model: str) -> None:
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
    elif step == 8:
        from mapgen.boundaries import run_step8
        r = run_step8(image, model=model)
        log(f"{r['selected_adjacencies']} selected adjacency type(s); "
            f"priority patterns={len(r['active_patterns'])}")
    elif step == "8a":
        from mapgen.cleanup import run_step8a
        r = run_step8a(image, model=model)
        log(f"{r['owner_groups']} boundary-owner group(s); "
            f"{r['repainted_components']} top component layer(s); "
            f"{r['restored_pixels']} pixels restored")
    elif step == "alt5":
        from mapgen.alt_mapgen import run_alt_step5
        r = run_alt_step5(image, model=model)
        a = r["aggregation"]
        log(f"aggregation from untouched Step 4: {len(a['groups'])} final thematic "
            f"group(s), maximum {a['texture_ceiling']}; review={a['review_status']}")
        log("geographic pixels changed: 0")
    elif step == "alt6":
        from mapgen.alt_mapgen import run_alt_step6_presets
        r = run_alt_step6_presets(image, model=model)
        s = r["summary"]
        log(f"approved Alt Step 5 groups simplified with canonical Step 5's algorithm; changed "
            f"{s['changed_share'] * 100:.2f}% of pixels")
        log(f"dissolved {s['dissolved_components']} components; "
            f"smoothing {s['smoothing_mm']} mm")
    elif step == "alt7":
        from mapgen.alt_symbols import run_alt_step7
        r = run_alt_step7(image, model=model)
        for assignment in r["assignments"]:
            log(f"  {assignment['label']} -> {assignment['pattern_desc']} "
                f"(minimum width {assignment.get('minimum_width_mm')} mm)")
        for note in r["notes"]:
            log("NOTE: " + note)
    else:
        raise ValueError(f"unknown step {step}")


def _job_worker(stem: str, image: Path, steps: list[int | str], model: str) -> None:
    rec = _jobs[stem]

    def log(msg: str) -> None:
        with _lock:
            rec["log"].append(msg)

    try:
        log(f"model: {model}")
        for s in steps:
            with _lock:
                rec["current"] = s
            log(f"--- {('step ' + str(s)) if isinstance(s, int) else s} ---")
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
            "step6_review_ready": step6_review_ready(p.stem),
            "alt_steps": {str(s): alt_step_done(p.stem, s) for s in ALT_STEP_ARTIFACTS},
            "alt_step5_review_ready": alt_step5_review_ready(p.stem),
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
    try:
        requested = {_canonical_step(step) for step in data.get("steps", [])}
        steps = [step for step in CANONICAL_STEP_ORDER if step in requested]
    except (TypeError, ValueError):
        steps = []
    if not steps or any(step not in STEP_ARTIFACTS for step in steps):
        abort(400, "steps must be within 1-8A")
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


@app.post("/api/run-alt")
def api_run_alt():
    data = request.get_json(force=True)
    stem = data.get("stem", "")
    requested = sorted({int(step) for step in data.get("steps", [])})
    if not requested or any(step not in ALT_STEP_ARTIFACTS for step in requested):
        abort(400, "alternate steps must be within 5-7")
    model = str(data.get("model") or DEFAULT_MODEL)
    if model not in {model_id for model_id, _ in AVAILABLE_MODELS}:
        abort(400, "unsupported model")
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    steps = [f"alt{step}" for step in requested]
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is already running for this map")
        if 5 in requested:
            _remove_alt_from(RUNS_DIR / stem, 5, remove_relationships=False)
        elif 6 in requested:
            _remove_alt_from(RUNS_DIR / stem, 7, remove_relationships=False)
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
    try:
        from_step = _canonical_step(data.get("from_step", 1))
        reset_steps = _canonical_steps_from(from_step)
    except (TypeError, ValueError):
        abort(400, "reset step must be within 1-8A")
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    removed = []
    run_dir = RUNS_DIR / stem
    for s in reset_steps:
        for name in STEP_ARTIFACTS[s] + STEP_EXTRA[s]:
            p = run_dir / name
            if p.exists():
                p.unlink()
                removed.append(name)
    numeric_from = from_step if isinstance(from_step, int) else 9
    if numeric_from <= 5:
        for p in run_dir.glob("step5_preset_*"):
            if p.is_file():
                p.unlink()
                removed.append(p.name)
    if numeric_from <= 4:
        removed.extend(_remove_alt_from(run_dir, 5, remove_relationships=True))
    return jsonify({"ok": True, "removed": removed})


@app.post("/api/reset-alt")
def api_reset_alt():
    data = request.get_json(force=True)
    stem = data.get("stem", "")
    from_step = int(data.get("from_step", 5))
    if from_step not in ALT_STEP_ARTIFACTS:
        abort(400, "alternate reset step must be within 5-7")
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    removed = _remove_alt_from(RUNS_DIR / stem, from_step,
                               remove_relationships=bool(data.get("remove_relationships")))
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


@app.get("/api/labelreview/<stem>")
def api_labelreview_get(stem: str):
    """Return raw label occurrences with any current review decisions."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    labels_path = run_dir / "labels.json"
    if not labels_path.exists():
        abort(409, "run Step 3 before reviewing labels")
    from mapgen.labelreview import review_view

    labels_json = json.loads(labels_path.read_text(encoding="utf-8"))
    review_path = run_dir / "label_review.json"
    review_json = (json.loads(review_path.read_text(encoding="utf-8"))
                   if review_path.exists() else None)
    return jsonify(review_view(labels_json, review_json))


@app.post("/api/labelreview/<stem>")
def api_labelreview_post(stem: str):
    """Save occurrence-level decisions and refresh the final coordinate export."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir = RUNS_DIR / stem
    labels_path = run_dir / "labels.json"
    if not labels_path.exists():
        abort(409, "run Step 3 before reviewing labels")
    from mapgen.labelreview import (apply_review, removal_signature, review_view,
                                    write_text_removal_mask)

    labels_json = json.loads(labels_path.read_text(encoding="utf-8"))
    previous_review_path = run_dir / "label_review.json"
    previous_review = (json.loads(previous_review_path.read_text(encoding="utf-8"))
                       if previous_review_path.exists() else None)
    previous_removal = removal_signature(previous_review, labels_json)
    data = request.get_json(force=True)
    try:
        review_json, approved_json = apply_review(labels_json, data.get("decisions", []))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    (run_dir / "label_review.json").write_text(
        json.dumps(review_json, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    (run_dir / "approved_labels.json").write_text(
        json.dumps(approved_json, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    write_text_removal_mask(run_dir)

    removal_changed = previous_removal != removal_signature(review_json, labels_json)
    invalidated = []
    if removal_changed and any(step_done(stem, step) for step in _canonical_steps_from(4)):
        for step in _canonical_steps_from(4):
            for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
                path = run_dir / name
                if path.exists():
                    path.unlink()
                    invalidated.append(name)
        for path in run_dir.glob("step5_preset_*"):
            if path.is_file():
                path.unlink()
                invalidated.append(path.name)
        invalidated.extend(_remove_alt_from(run_dir, 5, remove_relationships=True))

    overlay_updated = False
    overlay_warning = None
    overlay_inputs = ("label_map_gen.png", "step7_tactile.png", "step5_summary.json")
    if all((run_dir / name).exists() for name in overlay_inputs):
        try:
            from mapgen.symbols import write_overlay_labels
            write_overlay_labels(run_dir)
            overlay_updated = True
        except Exception as exc:  # review is still safely saved
            overlay_warning = f"review saved, but overlay export refresh failed: {exc}"
    response = review_view(labels_json, review_json)
    response.update({
        "ok": True,
        "approved": len(approved_json["labels"]),
        "excluded": len(labels_json.get("labels", [])) - len(approved_json["labels"]),
        "overlay_updated": overlay_updated,
        "segmentation_invalidated": bool(invalidated),
        "invalidated": invalidated,
        "warning": overlay_warning,
    })
    return jsonify(response)


@app.get("/api/labelcrop/<stem>/<int:index>")
def api_labelcrop(stem: str, index: int):
    """Serve a contextual crop for one detected physical label occurrence."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    labels_path = run_dir / "labels.json"
    image_path = run_dir / "map_text_input.png"
    if not labels_path.exists() or not image_path.exists():
        abort(404, "label review inputs are unavailable")
    labels = json.loads(labels_path.read_text(encoding="utf-8")).get("labels", [])
    if index < 0 or index >= len(labels):
        abort(404, "unknown label occurrence")

    import cv2
    from mapgen.isolate import imread

    image = imread(image_path)
    height, width = image.shape[:2]
    x0, y0, x1, y1 = [int(round(v)) for v in labels[index]["box"]]
    box_w, box_h = max(1, x1 - x0), max(1, y1 - y0)
    pad_x = max(14, int(box_w * 0.7))
    pad_y = max(14, int(box_h * 0.9))
    x0, x1 = max(0, x0 - pad_x), min(width, x1 + pad_x)
    y0, y1 = max(0, y0 - pad_y), min(height, y1 + pad_y)
    crop = image[y0:y1, x0:x1]
    if crop.size == 0:
        abort(404, "empty label crop")
    ok, encoded = cv2.imencode(".png", crop)
    if not ok:
        abort(500, "could not encode label crop")
    response = send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.get("/api/linereview/<stem>")
def api_linereview_get(stem: str):
    """Return automatic river segments with any current review edits."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    auto_path = run_dir / "lines_auto.geojson"
    image_path = run_dir / "map_area.png"
    if not auto_path.exists() or not image_path.exists():
        abort(409, "run Step 4 before reviewing rivers")
    from mapgen.isolate import imread
    from mapgen.linereview import review_view

    lines_json = json.loads(auto_path.read_text(encoding="utf-8"))
    review_path = run_dir / "line_review.json"
    review_json = (json.loads(review_path.read_text(encoding="utf-8"))
                   if review_path.exists() else None)
    height, width = imread(image_path).shape[:2]
    return jsonify(review_view(lines_json, review_json, width, height))


@app.post("/api/linereview/<stem>")
def api_linereview_post(stem: str):
    """Save reviewed river geometry and invalidate downstream line consumers."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir = RUNS_DIR / stem
    auto_path = run_dir / "lines_auto.geojson"
    image_path = run_dir / "map_area.png"
    mask_path = run_dir / "map_mask.png"
    if not auto_path.exists() or not image_path.exists() or not mask_path.exists():
        abort(409, "run Step 4 before reviewing rivers")

    from mapgen.isolate import imread, imwrite
    from mapgen.linereview import apply_review, review_view
    from mapgen.segment import render_lines_preview

    automatic = json.loads(auto_path.read_text(encoding="utf-8"))
    height, width = imread(image_path).shape[:2]
    try:
        review_json, approved = apply_review(
            automatic, request.get_json(force=True), width, height)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    (run_dir / "line_review.json").write_text(
        json.dumps(review_json, indent=2, ensure_ascii=False), encoding="utf-8")
    for filename in ("approved_lines.geojson", "lines.geojson"):
        (run_dir / filename).write_text(
            json.dumps(approved, indent=2, ensure_ascii=False), encoding="utf-8")

    preview_records = [{
        **feature.get("properties", {}),
        "points": feature.get("geometry", {}).get("coordinates", []),
    } for feature in approved.get("features", [])]
    mask = imread(mask_path)[..., 0]
    imwrite(run_dir / "step4_lines_preview.png", render_lines_preview(preview_records, mask))

    invalidated = []
    for step in _canonical_steps_from(5):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            path = run_dir / name
            if path.exists():
                path.unlink()
                invalidated.append(name)
    for path in run_dir.glob("step5_preset_*"):
        if path.is_file():
            path.unlink()
            invalidated.append(path.name)
    invalidated.extend(_remove_alt_from(run_dir, 5, remove_relationships=False))

    response = review_view(automatic, review_json, width, height)
    response.update({
        "ok": True,
        "automatic_rivers_kept": len(review_json["include_auto_ids"]),
        "manual_rivers": len(review_json["manual_rivers"]),
        "downstream_invalidated": bool(invalidated),
        "invalidated": invalidated,
    })
    return jsonify(response)


@app.get("/api/mapimg/<name>")
def api_mapimg(name: str):
    if "/" in name or "\\" in name:
        abort(400)
    return send_from_directory(MAPS_DIR.resolve(), name)


@app.get("/api/geocounts/<stem>")
def api_geocounts(stem: str):
    import json as _json
    alt = request.args.get("alt") == "1"
    gen = request.args.get("gen") == "1"
    suffix = "_gen" if gen else ""
    out = {"polygons": None, "polylines": None, "line_kinds": {}}
    prefix = "alt_" if alt else ""
    rp = RUNS_DIR / stem / f"{prefix}regions{suffix}.geojson"
    lp = RUNS_DIR / stem / f"{prefix}lines{suffix}.geojson"
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
    invalidated = []
    run_dir = RUNS_DIR / stem
    for step in (6, 7, 8, "8a"):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    return jsonify({"ok": True, "active_level": level, "invalidated": invalidated})


@app.get("/api/alt-aggregation-review/<stem>")
def api_alt_aggregation_review_get(stem: str):
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "alt_aggregation.json"
    if not path.exists():
        abort(409, "run Alt Step 5 to generate an aggregation proposal")
    from mapgen.alt_aggregate import load_aggregation_review
    aggregation = json.loads(path.read_text(encoding="utf-8"))
    review = load_aggregation_review(run_dir, aggregation)
    return jsonify({
        "proposal": aggregation,
        "review": review,
        "effective_groups": review["groups"] if review else aggregation["groups"],
    })


@app.get("/api/aggregation-review/<stem>")
def api_aggregation_review_get(stem: str):
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "aggregation.json"
    if not path.exists():
        abort(409, "run Step 6 to generate an aggregation proposal")
    from mapgen.aggregate import load_aggregation_review
    aggregation = json.loads(path.read_text(encoding="utf-8"))
    review = load_aggregation_review(run_dir, aggregation)
    return jsonify({
        "proposal": aggregation,
        "review": review,
        "effective_groups": review["groups"] if review else aggregation["groups"],
    })


@app.post("/api/aggregation-review/<stem>")
def api_aggregation_review_post(stem: str):
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "aggregation.json"
    if not path.exists():
        abort(409, "run Step 6 before reviewing its aggregation proposal")
    from mapgen.aggregate import save_aggregation_review
    aggregation = json.loads(path.read_text(encoding="utf-8"))
    try:
        review = save_aggregation_review(
            run_dir, aggregation, request.get_json(force=True).get("groups", []))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    aggregation["review_status"] = review["status"]
    path.write_text(json.dumps(aggregation, indent=2, ensure_ascii=False), encoding="utf-8")
    invalidated = []
    for step in (7, 8, "8a"):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    return jsonify({
        "ok": True, "review": review,
        "downstream_invalidated": bool(invalidated), "invalidated": invalidated,
    })


@app.post("/api/alt-aggregation-review/<stem>")
def api_alt_aggregation_review_post(stem: str):
    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "alt_aggregation.json"
    if not path.exists():
        abort(409, "run Alt Step 5 before reviewing its aggregation proposal")
    from mapgen.alt_aggregate import save_aggregation_review
    aggregation = json.loads(path.read_text(encoding="utf-8"))
    try:
        review = save_aggregation_review(
            run_dir, aggregation, request.get_json(force=True).get("groups", []))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    aggregation["review_status"] = review["status"]
    path.write_text(json.dumps(aggregation, indent=2, ensure_ascii=False), encoding="utf-8")
    invalidated = _remove_alt_from(run_dir, 6, remove_relationships=False)
    if review.get("approved"):
        from mapgen.alt_mapgen import materialize_alt_step5_output
        materialize_alt_step5_output(run_dir, aggregation)
    else:
        for name in ("alt_group_map_source.png", "alt_groups.json"):
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
    return jsonify({
        "ok": True, "review": review,
        "downstream_invalidated": bool(invalidated), "invalidated": invalidated,
    })


@app.get("/api/alt-step6params/<stem>")
def api_alt_step6params_get(stem: str):
    from mapgen.alt_mapgen import ALT_STEP6_DEFAULT_PARAMS, load_alt_step6_params
    run_dir = RUNS_DIR / stem
    params = (load_alt_step6_params(run_dir) if run_dir.exists()
              else dict(ALT_STEP6_DEFAULT_PARAMS))
    classes = []
    path = run_dir / "classes_final.json"
    if path.exists():
        for cl in json.loads(path.read_text(encoding="utf-8"))["classes"]:
            if cl["area_share"] >= 0.001:
                classes.append({"index": cl["index"], "label": cl["label"],
                                "is_thematic": cl["is_thematic"],
                                "share": cl["area_share"]})
    return jsonify({"params": params, "classes": classes})


@app.post("/api/alt-step6params/<stem>")
def api_alt_step6params_post(stem: str):
    from mapgen.alt_mapgen import ALT_STEP6_DEFAULT_PARAMS
    data = request.get_json(force=True)
    try:
        level = max(1, min(5, int(data.get(
            "simplification_level", ALT_STEP6_DEFAULT_PARAMS["simplification_level"]))))
        params = {
            "method_version": ALT_STEP6_DEFAULT_PARAMS["method_version"],
            "simplification_level": level,
            "min_texture_area_side_mm": None,
            "smooth_mm": None,
            "preserve_share": None,
        }
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    run_dir = RUNS_DIR / stem
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "alt_step6_params.json").write_text(
        json.dumps(params, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/alt-step6presets/<stem>")
def api_alt_step6presets_get(stem: str):
    from mapgen.alt_mapgen import (ALT_STEP6_PRESET_ARTIFACTS,
                                   alt_step6_preset_artifact_name,
                                   load_alt_step6_params)
    from mapgen.generalize import SIMPLIFICATION_PRESETS
    run_dir = RUNS_DIR / stem
    variants = {}
    for level in SIMPLIFICATION_PRESETS:
        files = {name: run_dir / alt_step6_preset_artifact_name(level, name)
                 for name in ALT_STEP6_PRESET_ARTIFACTS}
        if not all(path.exists() for path in files.values()):
            continue
        summary = json.loads(files["alt_step6_summary.json"].read_text(encoding="utf-8"))
        classes = json.loads(files["alt_classes_gen.json"].read_text(encoding="utf-8"))["classes"]
        regions = json.loads(files["alt_regions_gen.geojson"].read_text(encoding="utf-8"))["features"]
        variants[str(level)] = {
            "debug_artifact": files["alt_step6_debug.png"].name,
            "preview_artifact": files["alt_label_map_gen_preview.png"].name,
            "changes_artifact": files["alt_step6_changes.png"].name,
            "summary": summary,
            "classes": classes,
            "polygons": len(regions),
        }
    params = load_alt_step6_params(run_dir) if run_dir.exists() else {}
    active = params.get("simplification_level")
    return jsonify({
        "ready": len(variants) == len(SIMPLIFICATION_PRESETS),
        "active_level": active if active in SIMPLIFICATION_PRESETS else None,
        "variants": variants,
    })


@app.post("/api/alt-step6preset/<stem>")
def api_alt_step6preset_post(stem: str):
    from mapgen.alt_mapgen import activate_alt_step6_preset
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
        activate_alt_step6_preset(RUNS_DIR / stem, level)
    except FileNotFoundError:
        abort(409, "generate the five alternate previews first")
    invalidated = _remove_alt_from(RUNS_DIR / stem, 7, remove_relationships=False)
    return jsonify({"ok": True, "active_level": level, "invalidated": invalidated})


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
