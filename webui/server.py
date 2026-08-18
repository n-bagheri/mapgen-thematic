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

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # mapgen uses relative config/ runs/ paths

from flask import Flask, abort, jsonify, request, send_file, send_from_directory  # noqa: E402

from mapgen.output_spec import DEFAULT_CONFIG_PATH, OutputSpec, PhysicalConstants  # noqa: E402
from mapgen.semantics import (  # noqa: E402
    AVAILABLE_MODELS,
    DEFAULT_MODEL,
    MapSemantics,
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
    5: ("aggregation.json", "step5_aggregation_preview.png",
        "step5_source_audit.json"),
    6: ("step6_debug.png", "classes_gen.json", "regions_gen.geojson",
        "lines_gen.geojson", "step6_summary.json"),
    # Step 7 retains the intermediate symbol and boundary artifacts for audit,
    # but is complete only once component cleanup has produced its master.
    7: ("symbols.json", "step7_tactile.png", "step7_debug.png",
        "step8_boundaries.json", "step8_boundaries.png", "step8_debug.png",
        "step8a_cleanup.json", "step8a_cleanup.png", "step8a_debug.png"),
    8: ("braille_labels.json", "step8_braille.json", "step8_braille_base.png",
        "step8_braille.png"),
    9: ("legend_labels.json", "step9_legend.json", "step9_legend_base.png", "step9_legend.png"),
}
# additional files a reset must clear (cached model calls, intermediates)
STEP_EXTRA = {
    1: (),
    2: ("step2_layout.json", "map_area.png", "map_mask.png", "map_mask_auto.png",
        "map_mask_review.json", "map_text_input.png", "legend.png"),
    3: ("step3_raw.json", "step3_raw.sha256", "step3_craft.json",
        "step3_craft.sha256", "step3_lines_raw.json", "step3_lines_raw.sha256",
        "line_guidance.json", "text_mask.png", "label_review.json",
        "approved_labels.json", "text_removal_mask.png", "text_removal_mask.json"),
    4: ("label_map.png", "label_map_preview.png", "step4_text_removed_input.png",
        "step4_lines_preview.png", "coastline_cleanup_mask.png", "river_cleanup_mask.png",
        "line_extraction.json", "lines_auto.geojson",
        "approved_lines.geojson", "line_review.json"),
    5: ("aggregation_review.json", "group_map_source.png", "groups.json"),
    6: ("label_map_gen.png", "label_map_gen_preview.png", "step6_changes.png",
        "step6_transitions.json", "step6_params.json"),
    7: ("overlay_labels.json", "pattern_transforms.json", "page_layout.json",
        "step7_hybrid.png", "step8a_hybrid.png", "step8_white_stroke_mask.png",
        "step8_black_stroke_mask.png"),
    8: ("step8_hybrid.png",),
    9: ("step9_legend_hybrid.png",),
}

CANONICAL_STEP_ORDER = (1, 2, 3, 4, 5, 6, 7, 8, 9)
PROJECT_ORDER_PATH = RUNS_DIR / ".project_order.json"


def _canonical_step(value) -> int:
    return int(value)


def _canonical_steps_from(step: int) -> tuple[int, ...]:
    normalized = _canonical_step(step)
    return CANONICAL_STEP_ORDER[CANONICAL_STEP_ORDER.index(normalized):]

def _project_order() -> list[str]:
    try:
        value = json.loads(PROJECT_ORDER_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [str(stem) for stem in value if isinstance(stem, str)] if isinstance(value, list) else []


def _save_project_order(stems: list[str]) -> None:
    RUNS_DIR.mkdir(exist_ok=True)
    PROJECT_ORDER_PATH.write_text(json.dumps(stems, indent=2), encoding="utf-8")


def map_files() -> list[Path]:
    if not MAPS_DIR.exists():
        return []
    paths = [p for p in MAPS_DIR.iterdir() if p.suffix.lower() in IMG_EXTS]
    saved_order = _project_order()
    if not saved_order:
        return sorted(paths, key=lambda p: (-p.stat().st_mtime_ns, p.name.lower()))
    order = {stem: index for index, stem in enumerate(saved_order)}
    return sorted(paths, key=lambda p: (
        0 if p.stem in order else 1,
        order.get(p.stem, 0) if p.stem in order else -p.stat().st_mtime_ns,
        p.name.lower(),
    ))


def find_map(stem: str) -> Path | None:
    return next((p for p in map_files() if p.stem == stem), None)


def _current_semantics(stem: str) -> MapSemantics | None:
    path = RUNS_DIR / stem / "step1_semantics.json"
    if not semantics_artifact_is_current(path):
        return None
    try:
        return MapSemantics.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _step1_artifact_error(stem: str) -> str | None:
    """Explain why an existing Step 1 artifact cannot drive the pipeline."""
    path = RUNS_DIR / stem / "step1_semantics.json"
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "Step 1 produced an unreadable semantic result; rerun Step 1."
    if not isinstance(raw, dict) or not str(raw.get("map_language") or "").strip():
        return "Step 1 semantic result is incomplete; rerun Step 1."
    try:
        MapSemantics.model_validate(raw)
    except ValueError:
        return "Step 1 produced an invalid semantic result; rerun Step 1."
    return None


def _pipeline_error(semantics: MapSemantics | None) -> str | None:
    if semantics is not None and not semantics.legend_present:
        return (
            "The tactile-map pipeline cannot continue: no legend was detected; "
            "a visible legend is required to identify and symbolize the map classes."
        )
    return None


def step_done(stem: str, step: int | str) -> bool:
    semantics = _current_semantics(stem)
    if step == 1:
        return semantics is not None
    if semantics is None or not semantics.in_scope or _pipeline_error(semantics):
        return False
    paths = [RUNS_DIR / stem / a for a in STEP_ARTIFACTS[step]]
    if not all(path.exists() for path in paths):
        return False
    if step == 6:
        summary_path = RUNS_DIR / stem / "step6_summary.json"
        from mapgen.postprocess import STEP6_METHOD_VERSION
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if summary.get("params", {}).get("method_version") != STEP6_METHOD_VERSION:
            return False
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
        run_dir = RUNS_DIR / stem
        inputs = [run_dir / name for name in
                  ("step8_boundaries.json", "symbols.json", "label_map_gen.png")]
        if not all(path.exists() for path in inputs):
            return False
        final_paths = [run_dir / name for name in
                       ("step8a_cleanup.json", "step8a_cleanup.png", "step8a_debug.png")]
        if min(path.stat().st_mtime_ns for path in final_paths) < max(
                path.stat().st_mtime_ns for path in inputs):
            return False
    if step == 8:
        from mapgen.braille import BRAILLE_LAYOUT_VERSION
        run_dir = RUNS_DIR / stem
        source = run_dir / "step8a_cleanup.png"
        output = run_dir / "step8_braille.png"
        layout = run_dir / "braille_labels.json"
        if not all(path.exists() for path in (source, output, layout)):
            return False
        if output.stat().st_mtime_ns < max(source.stat().st_mtime_ns,
                                           layout.stat().st_mtime_ns):
            return False
        report_path = run_dir / "step8_braille.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if report.get("renderer_version") != BRAILLE_LAYOUT_VERSION:
            return False
    if step == 9:
        from mapgen.legend import LEGEND_VERSION
        run_dir = RUNS_DIR / stem
        source = run_dir / "symbols.json"
        output = run_dir / "step9_legend.png"
        layout = run_dir / "legend_labels.json"
        if not all(path.exists() for path in (source, output, layout)):
            return False
        if output.stat().st_mtime_ns < max(source.stat().st_mtime_ns, layout.stat().st_mtime_ns):
            return False
        try:
            report = json.loads((run_dir / "step9_legend.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if report.get("renderer_version") != LEGEND_VERSION:
            return False
    return True


def step5_review_ready(stem: str) -> bool:
    path = RUNS_DIR / stem / "aggregation.json"
    if not path.exists():
        return False
    from mapgen.aggregate import effective_aggregation
    try:
        effective_aggregation(
            RUNS_DIR / stem, json.loads(path.read_text(encoding="utf-8")))
    except RuntimeError:
        return False
    run_dir = RUNS_DIR / stem
    return all((run_dir / name).exists()
               for name in ("group_map_source.png", "groups.json"))


# --------------------------------------------------------------------------- jobs

def _run_single_step(step: int | str, image: Path, log,
                     model: str) -> MapSemantics | None:
    if step == 1:
        from mapgen.semantics import MissingLegendError, interpret_map, save_semantics
        sem = interpret_map(image, model=model, status=log)
        save_semantics(sem, image)
        log(f"{sem.map_type.value} | ordering={sem.data_ordering.value} | "
            f"language={sem.map_language} | "
            f"{len(sem.thematic_classes)} thematic classes | water={sem.water_present}")
        if not sem.in_scope:
            return sem
        if not sem.legend_present:
            raise MissingLegendError(
                "The tactile-map pipeline cannot continue: no legend was detected; "
                "a visible legend is required to identify and symbolize the map classes."
            )
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
        from mapgen.aggregate import run_step5
        r = run_step5(image, model=model)
        a = r["aggregation"]
        log(f"aggregation from untouched Step 4: {len(a['groups'])} final thematic "
            f"group(s), maximum {a['texture_ceiling']}; review={a['review_status']}")
        log("geographic pixels changed: 0")
    elif step == 6:
        from mapgen.postprocess import run_step6_presets
        r = run_step6_presets(image, model=model)
        s = r["summary"]
        log(f"approved Step 5 groups simplified with the shared area algorithm; changed "
            f"{s['changed_share'] * 100:.2f}% of pixels")
        log(f"dissolved {s['dissolved_components']} components; "
            f"smoothing {s['smoothing_mm']} mm")
    elif step == 7:
        from mapgen.symbols import run_step7
        from mapgen.boundaries import run_step8
        from mapgen.cleanup import run_step8a

        symbols = run_step7(image, model=model)
        for a in symbols["assignments"]:
            log(f"  {a['label']} -> {a['pattern_desc']}")
        for n in symbols["notes"]:
            log("NOTE: " + n)
        boundaries = run_step8(image, model=model)
        log(f"{boundaries['selected_adjacencies']} selected adjacency type(s); "
            f"priority patterns={len(boundaries['active_patterns'])}")
        cleanup = run_step8a(image, model=model)
        log(f"final cleanup: {cleanup['owner_groups']} boundary-owner group(s); "
            f"{cleanup['repainted_components']} top component layer(s); "
            f"{cleanup['restored_pixels']} pixels restored")
    elif step == 8:
        from mapgen.braille import run_step8

        result = run_step8(image, runs_dir=RUNS_DIR)
        log(f"Braille overlay: {result['enabled_labels']}/{result['total_labels']} "
            "labels enabled; local render, API calls=0")
    elif step == 9:
        from mapgen.legend import run_step9
        result = run_step9(image, runs_dir=RUNS_DIR)
        log(f"Braille legend: {result['entries']} pattern samples; local render, API calls=0")
    else:
        raise ValueError(f"unknown step {step}")
    return None


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
            retained_step1 = False
            try:
                result = _run_single_step(s, image, log, model)
            except Exception as step_exc:  # noqa: BLE001 - preserve a valid prior interpretation
                from mapgen.semantics import MissingLegendError
                cached = _current_semantics(stem) if s == 1 else None
                if cached is None or isinstance(step_exc, MissingLegendError):
                    raise
                retained_step1 = True
                result = cached
                log(
                    "WARN: Step 1 rerun failed, but the existing valid Step 1 "
                    f"result was retained ({type(step_exc).__name__}: {step_exc})"
                )
            if s == 1 and result is not None and not result.in_scope:
                log(
                    f"PIPELINE STOPPED: map type '{result.map_type.value}' is out of scope; "
                    "only chorochromatic and isopleth maps can continue"
                )
                break
            if retained_step1:
                log("PIPELINE STOPPED: no later steps were run because Step 1 produced no new result")
                break
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


@app.get("/minimal")
def minimal_index():
    """Serve the focused UI without duplicating the pipeline application."""
    return app.send_static_file("minimal.html")


@app.get("/api/maps")
def api_maps():
    with _lock:
        snapshot = {k: {"status": v["status"], "current": v["current"],
                        "steps": v["steps"], "model": v["model"]}
                    for k, v in _jobs.items()}
    maps = []
    for p in map_files():
        semantics = _current_semantics(p.stem)
        step1_error = _step1_artifact_error(p.stem)
        pipeline_error = _pipeline_error(semantics)
        maps.append({
            "name": p.name,
            "stem": p.stem,
            "in_scope": semantics.in_scope if semantics is not None else None,
            "step1_error": step1_error,
            "pipeline_error": pipeline_error,
            "steps": {str(s): step_done(p.stem, s) for s in STEP_ARTIFACTS},
            "step5_review_ready": step5_review_ready(p.stem),
            "job": snapshot.get(p.stem),
        })
    return jsonify({"maps": maps})


@app.post("/api/upload")
def api_upload():
    f = request.files.get("file")
    if f is None or not f.filename:
        abort(400, "no file")
    existing_order = [path.stem for path in map_files()]
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
    existing_order.append(dest.stem)
    _save_project_order(list(dict.fromkeys(existing_order)))
    return jsonify({"ok": True, "name": dest.name})


@app.patch("/api/maps/<stem>")
def api_rename_map(stem: str):
    """Rename a project while preserving all of its generated artifacts."""
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "cannot rename a project while its job is running")
    data = request.get_json(silent=True) or {}
    requested = str(data.get("name") or "").strip()
    if not requested:
        abort(400, "project name cannot be empty")
    requested_path = Path(requested).name
    requested_suffix = Path(requested_path).suffix
    new_stem = (requested_path.removesuffix(requested_suffix)
                if requested_suffix.lower() in IMG_EXTS else requested_path).strip()
    new_stem = re.sub(r"[^\w.\- ()\[\]]", "_", new_stem).strip(" .")
    if not new_stem:
        abort(400, "project name must contain at least one letter or number")
    destination = image.with_name(new_stem + image.suffix.lower())
    if destination != image and (destination.exists() or (RUNS_DIR / new_stem).exists()):
        abort(409, "another project already uses that name")
    image.rename(destination)
    old_run = RUNS_DIR / stem
    if old_run.is_dir() and old_run != RUNS_DIR / new_stem:
        old_run.rename(RUNS_DIR / new_stem)
    order = [new_stem if item == stem else item for item in _project_order()]
    _save_project_order(list(dict.fromkeys(order)))
    with _lock:
        if stem in _jobs:
            _jobs[new_stem] = _jobs.pop(stem)
    return jsonify({"ok": True, "name": destination.name, "stem": new_stem})


@app.put("/api/maps/order")
def api_reorder_maps():
    data = request.get_json(silent=True) or {}
    requested = data.get("stems")
    existing = [path.stem for path in map_files()]
    if not isinstance(requested, list) or set(requested) != set(existing) or len(requested) != len(existing):
        abort(400, "project order must contain every project exactly once")
    _save_project_order([str(stem) for stem in requested])
    return jsonify({"ok": True, "stems": requested})


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
    _save_project_order([item for item in _project_order() if item != stem])
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
        abort(400, "steps must be within 1-9")
    model = str(data.get("model") or DEFAULT_MODEL)
    if model not in {model_id for model_id, _ in AVAILABLE_MODELS}:
        abort(400, "unsupported model")
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    semantics = _current_semantics(stem)
    if semantics is not None and not semantics.in_scope and 1 not in steps:
        abort(409, "Step 1 classified this map as out of scope; only Step 1 can be rerun")
    pipeline_error = _pipeline_error(semantics)
    if pipeline_error is not None and 1 not in steps:
        abort(409, pipeline_error + " Only Step 1 can be rerun.")
    step1_error = _step1_artifact_error(stem)
    if step1_error is not None and 1 not in steps:
        abort(409, step1_error + " Rerun Step 1 before continuing.")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is already running for this map")
        run_dir = RUNS_DIR / stem
        invalidate_from = (6 if 5 in steps else 7 if 6 in steps else 8 if 7 in steps
                           else 9 if 8 in steps else None)
        if invalidate_from is not None:
            for later_step in _canonical_steps_from(invalidate_from):
                for name in STEP_ARTIFACTS[later_step] + STEP_EXTRA[later_step]:
                    artifact = run_dir / name
                    if artifact.exists():
                        artifact.unlink()
            if invalidate_from <= 6:
                for artifact in run_dir.glob("step6_preset_*"):
                    if artifact.is_file():
                        artifact.unlink()
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
        abort(400, "reset step must be within 1-8")
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
    if from_step <= 6:
        for p in run_dir.glob("step6_preset_*"):
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


def _mask_review_paths(stem: str) -> tuple[Path, Path, Path, Path]:
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    map_path = run_dir / "map_area.png"
    mask_path = run_dir / "map_mask.png"
    auto_path = run_dir / "map_mask_auto.png"
    geometry_path = run_dir / "geometry.json"
    if not map_path.exists() or not mask_path.exists() or not geometry_path.exists():
        abort(409, "run Step 2 before reviewing the geographic mask")
    # Older Step 2 results predate the review baseline.  Treat their existing
    # mask as the restore baseline rather than blocking a project mid-pipeline.
    if not auto_path.exists():
        shutil.copyfile(mask_path, auto_path)
    return run_dir, map_path, mask_path, auto_path


@app.get("/api/maskreview/<stem>")
def api_maskreview_get(stem: str):
    from mapgen.isolate import imread

    run_dir, map_path, mask_path, auto_path = _mask_review_paths(stem)
    image = imread(map_path)
    mask = imread(mask_path)[..., 0]
    automatic = imread(auto_path)[..., 0]
    review_path = run_dir / "map_mask_review.json"
    return jsonify({
        "width": int(image.shape[1]), "height": int(image.shape[0]),
        "kept_pixels": int((mask > 0).sum()),
        "automatic_pixels": int((automatic > 0).sum()),
        "reviewed": review_path.exists(),
    })


@app.post("/api/maskreview/<stem>")
def api_maskreview_post(stem: str):
    import cv2
    import numpy as np
    from mapgen.isolate import imread, imwrite, prepare_text_input

    run_dir, map_path, mask_path, auto_path = _mask_review_paths(stem)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) - {"strokes", "reset"}:
        abort(400, "mask review accepts strokes or reset")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
        mask = imread(mask_path)[..., 0]
        automatic = imread(auto_path)[..., 0]
        if data.get("reset"):
            mask = automatic.copy()
            (run_dir / "map_mask_review.json").unlink(missing_ok=True)
            stroke_count = 0
        else:
            strokes = data.get("strokes")
            if not isinstance(strokes, list) or not strokes or len(strokes) > 300:
                abort(400, "provide 1 to 300 mask-review strokes")
            height, width = mask.shape
            stroke_count = 0
            for stroke in strokes:
                if not isinstance(stroke, dict) or set(stroke) - {"mode", "points", "radius"}:
                    abort(400, "each stroke requires mode, points, and radius")
                mode = stroke.get("mode")
                points = stroke.get("points")
                radius = stroke.get("radius")
                if mode not in {"erase", "restore"} or not isinstance(points, list) or not points:
                    abort(400, "stroke mode must be erase or restore with at least one point")
                if len(points) > 5000:
                    abort(400, "a mask-review stroke contains too many points")
                try:
                    radius = int(round(float(radius)))
                except (TypeError, ValueError):
                    abort(400, "stroke radius must be a number")
                radius = min(100, max(1, radius))
                parsed = []
                for point in points:
                    if not isinstance(point, (list, tuple)) or len(point) != 2:
                        abort(400, "each stroke point must be [x, y]")
                    try:
                        x, y = float(point[0]), float(point[1])
                    except (TypeError, ValueError):
                        abort(400, "stroke point coordinates must be numbers")
                    if not np.isfinite(x) or not np.isfinite(y):
                        abort(400, "stroke point coordinates must be finite")
                    parsed.append((min(width - 1, max(0, round(x))),
                                   min(height - 1, max(0, round(y)))))
                stroke_mask = np.zeros_like(mask)
                for start, end in zip(parsed, parsed[1:]):
                    cv2.line(stroke_mask, start, end, 255, thickness=radius * 2,
                             lineType=cv2.LINE_AA)
                for point in parsed[:1]:
                    cv2.circle(stroke_mask, point, radius, 255, thickness=-1,
                               lineType=cv2.LINE_AA)
                if mode == "erase":
                    mask[stroke_mask > 0] = 0
                else:
                    # Human review can recover real geography that the
                    # automatic isolator omitted, not only undo an earlier
                    # manual removal.
                    mask[stroke_mask > 0] = 255
                stroke_count += 1
            (run_dir / "map_mask_review.json").write_text(json.dumps({
                "version": 1, "strokes_saved": stroke_count,
                "kept_pixels": int((mask > 0).sum()),
            }, indent=2), encoding="utf-8")

        imwrite(mask_path, mask)
        image = imread(map_path)
        geometry = json.loads((run_dir / "geometry.json").read_text(encoding="utf-8"))
        furniture = geometry.get("furniture", [])
        imwrite(run_dir / "map_text_input.png", prepare_text_input(
            image, geometry["map_crop"], furniture, mask))

        invalidated = []
        for step in _canonical_steps_from(3):
            for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
                path = run_dir / name
                if path.exists():
                    path.unlink()
                    invalidated.append(name)
        for path in run_dir.glob("step6_preset_*"):
            if path.is_file():
                path.unlink()
                invalidated.append(path.name)
    return jsonify({"ok": True, "kept_pixels": int((mask > 0).sum()),
                    "invalidated": invalidated, "downstream_invalidated": bool(invalidated)})


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
        for path in run_dir.glob("step6_preset_*"):
            if path.is_file():
                path.unlink()
                invalidated.append(path.name)

    overlay_updated = False
    overlay_warning = None
    overlay_inputs = ("label_map_gen.png", "step7_tactile.png", "step6_summary.json")
    if all((run_dir / name).exists() for name in overlay_inputs):
        try:
            from mapgen.symbols import write_overlay_labels
            write_overlay_labels(run_dir)
            overlay_updated = True
            if (run_dir / "braille_labels.json").exists():
                from mapgen.braille import run_step8 as run_braille_step8
                image = find_map(stem)
                if image is not None:
                    run_braille_step8(image, runs_dir=RUNS_DIR)
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
    """Return automatic overlaying-line segments with current review edits."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    auto_path = run_dir / "lines_auto.geojson"
    image_path = run_dir / "map_area.png"
    if not auto_path.exists() or not image_path.exists():
        abort(409, "run Step 4 before reviewing overlaying lines")
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
    """Save reviewed overlaying-line geometry and invalidate its consumers."""
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
        abort(409, "run Step 4 before reviewing overlaying lines")

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
    for path in run_dir.glob("step6_preset_*"):
        if path.is_file():
            path.unlink()
            invalidated.append(path.name)

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


@app.get("/api/aggregation-review/<stem>")
def api_aggregation_review_get(stem: str):
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "aggregation.json"
    if not path.exists():
        abort(409, "run Step 5 to generate an aggregation proposal")
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
        abort(409, "run Step 5 before reviewing its aggregation proposal")
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
    for step in (6, 7):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    for artifact in run_dir.glob("step6_preset_*"):
        if artifact.is_file():
            artifact.unlink()
            invalidated.append(artifact.name)
    if review.get("approved") and all(
            (run_dir / name).exists()
            for name in ("classes_final.json", "label_map.png")):
        from mapgen.postprocess import materialize_step5_output
        materialize_step5_output(run_dir, aggregation)
    else:
        for name in ("group_map_source.png", "groups.json"):
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
    return jsonify({
        "ok": True, "review": review,
        "downstream_invalidated": bool(invalidated), "invalidated": invalidated,
    })


@app.get("/api/step6params/<stem>")
def api_step6params_get(stem: str):
    from mapgen.postprocess import STEP6_DEFAULT_PARAMS, load_step6_params
    run_dir = RUNS_DIR / stem
    params = (load_step6_params(run_dir) if run_dir.exists()
              else dict(STEP6_DEFAULT_PARAMS))
    classes = []
    path = run_dir / "classes_final.json"
    if path.exists():
        for cl in json.loads(path.read_text(encoding="utf-8"))["classes"]:
            if cl["area_share"] >= 0.001:
                classes.append({"index": cl["index"], "label": cl["label"],
                                "is_thematic": cl["is_thematic"],
                                "share": cl["area_share"]})
    return jsonify({"params": params, "classes": classes})


@app.post("/api/step6params/<stem>")
def api_step6params_post(stem: str):
    from mapgen.postprocess import STEP6_DEFAULT_PARAMS
    from mapgen.generalize import ALL_LINE_KINDS, LINE_POLICY_VERSION
    data = request.get_json(force=True)
    try:
        level = max(1, min(5, int(data.get(
            "simplification_level", STEP6_DEFAULT_PARAMS["simplification_level"]))))
        params = {
            "method_version": STEP6_DEFAULT_PARAMS["method_version"],
            "simplification_level": level,
            "min_texture_area_side_mm": None,
            "smooth_mm": None,
            "preserve_share": None,
            "keep_line_kinds": [kind for kind in data.get("keep_line_kinds", ALL_LINE_KINDS)
                                if kind in ALL_LINE_KINDS],
            "line_policy_version": LINE_POLICY_VERSION,
            "protected_classes": [int(index)
                                  for index in data.get("protected_classes", [])],
        }
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    run_dir = RUNS_DIR / stem
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "step6_params.json").write_text(
        json.dumps(params, indent=2), encoding="utf-8")
    return jsonify({"ok": True})


@app.get("/api/step6presets/<stem>")
def api_step6presets_get(stem: str):
    from mapgen.postprocess import (STEP6_PRESET_ARTIFACTS,
                                   load_step6_params,
                                   step6_preset_artifact_name)
    from mapgen.generalize import SIMPLIFICATION_PRESETS
    run_dir = RUNS_DIR / stem
    variants = {}
    for level in SIMPLIFICATION_PRESETS:
        files = {name: run_dir / step6_preset_artifact_name(level, name)
                 for name in STEP6_PRESET_ARTIFACTS}
        if not all(path.exists() for path in files.values()):
            continue
        summary = json.loads(files["step6_summary.json"].read_text(encoding="utf-8"))
        classes = json.loads(files["classes_gen.json"].read_text(encoding="utf-8"))["classes"]
        regions = json.loads(files["regions_gen.geojson"].read_text(encoding="utf-8"))["features"]
        lines = json.loads(files["lines_gen.geojson"].read_text(encoding="utf-8"))["features"]
        variants[str(level)] = {
            "debug_artifact": files["step6_debug.png"].name,
            "preview_artifact": files["label_map_gen_preview.png"].name,
            "changes_artifact": files["step6_changes.png"].name,
            "summary": summary,
            "classes": classes,
            "polygons": len(regions),
            "polylines": len(lines),
        }
    params = load_step6_params(run_dir) if run_dir.exists() else {}
    active = params.get("simplification_level")
    return jsonify({
        "ready": len(variants) == len(SIMPLIFICATION_PRESETS),
        "active_level": active if active in SIMPLIFICATION_PRESETS else None,
        "variants": variants,
    })


@app.post("/api/step6preset/<stem>")
def api_step6preset_post(stem: str):
    from mapgen.postprocess import activate_step6_preset
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
        activate_step6_preset(RUNS_DIR / stem, level)
    except FileNotFoundError:
        abort(409, "generate the five previews first")
    invalidated = []
    run_dir = RUNS_DIR / stem
    for step in (7, 8):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    return jsonify({"ok": True, "active_level": level, "invalidated": invalidated})


def _step7_symbols(stem: str) -> tuple[Path, dict]:
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "symbols.json"
    if not path.exists():
        abort(409, "run Step 7 before editing its patterns")
    try:
        return run_dir, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(409, "Step 7 symbols are unreadable; rerun Step 7")


def _pattern_editor_payload(symbols: dict) -> dict:
    from mapgen.patterns import (DEFAULT_PATTERN_TRANSFORM, PATTERNS,
                                 PATTERN_TRANSFORM_LIMITS,
                                 normalize_pattern_transform)

    groups = []
    for group_id, assignment in enumerate(symbols.get("area_assignments", [])):
        pattern = assignment["pattern"]
        groups.append({
            "group_id": group_id,
            "label": assignment.get("label", f"group {group_id}"),
            "pattern": pattern,
            "pattern_desc": assignment.get("pattern_desc", PATTERNS[pattern]["desc"]),
            "pattern_family": PATTERNS[pattern]["group"],
            "rationale": assignment.get("rationale", ""),
            "editable": pattern not in {"plain", "solid_black"},
            "transform": normalize_pattern_transform(assignment.get("transform")),
        })
    library = [{
        "pattern": pattern,
        "pattern_desc": metadata["desc"],
        "pattern_family": metadata["group"],
        "editable": pattern not in {"plain", "solid_black"},
    } for pattern, metadata in PATTERNS.items()]
    return {
        "groups": groups,
        "library": library,
        "defaults": DEFAULT_PATTERN_TRANSFORM,
        "limits": {key: {"min": limits[0], "max": limits[1]}
                   for key, limits in PATTERN_TRANSFORM_LIMITS.items()},
    }


@app.get("/api/pattern-transforms/<stem>")
def api_pattern_transforms_get(stem: str):
    _, symbols = _step7_symbols(stem)
    return jsonify(_pattern_editor_payload(symbols))


@app.get("/api/pattern-library-preview/<pattern_id>")
def api_pattern_library_preview(pattern_id: str):
    import cv2
    from mapgen.patterns import PATTERNS, render_pattern

    if pattern_id not in PATTERNS:
        abort(404, "unknown tactile pattern")
    preview = render_pattern(pattern_id, (128, 128), 5.0)
    ok, encoded = cv2.imencode(".png", preview)
    if not ok:
        abort(500, "could not render pattern preview")
    response = send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/api/pattern-preview/<stem>/<int:group_id>")
def api_pattern_preview(stem: str, group_id: int):
    import cv2
    from mapgen.patterns import normalize_pattern_transform, render_pattern

    _, symbols = _step7_symbols(stem)
    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        abort(404, "unknown Step 7 area")
    assignment = assignments[group_id]
    preview = render_pattern(
        assignment["pattern"], (128, 128), 5.0,
        normalize_pattern_transform(assignment.get("transform")),
    )
    ok, encoded = cv2.imencode(".png", preview)
    if not ok:
        abort(500, "could not render pattern preview")
    response = send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


@app.post("/api/pattern-transforms/<stem>/<int:group_id>")
def api_pattern_transforms_post(stem: str, group_id: int):
    from mapgen.boundaries import run_step8
    from mapgen.cleanup import run_step8a
    from mapgen.patterns import normalize_pattern_transform
    from mapgen.symbols import rerender_step7_artifacts

    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir, symbols = _step7_symbols(stem)
    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        abort(404, "unknown Step 7 area")
    assignment = assignments[group_id]
    if assignment.get("pattern") in {"plain", "solid_black"}:
        abort(409, "plain and solid fills have no repeating pattern to transform")
    try:
        transform = normalize_pattern_transform(request.get_json(force=True))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    assignment["transform"] = transform
    rerender_step7_artifacts(run_dir, symbols)
    run_step8(image, runs_dir=RUNS_DIR)
    run_step8a(image, runs_dir=RUNS_DIR)
    if (run_dir / "braille_labels.json").exists():
        from mapgen.braille import run_step8 as run_braille_step8
        run_braille_step8(image, runs_dir=RUNS_DIR)
    return jsonify({
        "ok": True,
        "group_id": group_id,
        "transform": transform,
        "final_artifact": "step8a_cleanup.png",
    })


@app.post("/api/pattern-assignments/<stem>/<int:group_id>")
def api_pattern_assignments_post(stem: str, group_id: int):
    from mapgen.boundaries import run_step8
    from mapgen.cleanup import run_step8a
    from mapgen.patterns import PATTERNS
    from mapgen.symbols import reassign_step7_pattern, rerender_step7_artifacts

    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    data = request.get_json(force=True)
    pattern_id = data.get("pattern") if isinstance(data, dict) else None
    if not isinstance(pattern_id, str) or pattern_id not in PATTERNS:
        return jsonify({"ok": False, "error": "select a pattern from the Step 7 library"}), 400

    run_dir, symbols = _step7_symbols(stem)
    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        abort(404, "unknown Step 7 area")
    try:
        reassign_step7_pattern(run_dir, symbols, group_id, pattern_id)
    except (OSError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    rerender_step7_artifacts(run_dir, symbols)
    run_step8(image, runs_dir=RUNS_DIR)
    run_step8a(image, runs_dir=RUNS_DIR)
    if (run_dir / "braille_labels.json").exists():
        from mapgen.braille import run_step8 as run_braille_step8
        run_braille_step8(image, runs_dir=RUNS_DIR)
    if (run_dir / "legend_labels.json").exists():
        from mapgen.legend import run_step9
        run_step9(image, runs_dir=RUNS_DIR)
    return jsonify({
        "ok": True,
        "group_id": group_id,
        "pattern": pattern_id,
        "pattern_data": _pattern_editor_payload(symbols),
        "pattern_optimization": symbols.get("pattern_optimization", {}),
        "final_artifact": "step8a_cleanup.png",
    })


def _braille_layout(stem: str) -> tuple[Path, dict]:
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "braille_labels.json"
    if not path.exists():
        abort(409, "run Step 8 before editing Braille labels")
    try:
        return run_dir, json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        abort(409, "Step 8 label layout is unreadable; rerun Step 8")


@app.get("/api/page-layout/<stem>")
def api_page_layout_get(stem: str):
    from mapgen.braille import load_step7_page_layout

    if find_map(stem) is None:
        abort(404, "unknown map")
    try:
        return jsonify(load_step7_page_layout(RUNS_DIR / stem))
    except (FileNotFoundError, TypeError, ValueError) as exc:
        abort(409, str(exc))


@app.get("/api/north-marker.svg")
def api_north_marker():
    path = ROOT / "pattern_library" / "N.svg"
    if not path.is_file():
        abort(404, "north marker asset is missing")
    return send_file(path, mimetype="image/svg+xml", max_age=86400)


@app.post("/api/page-layout/<stem>")
def api_page_layout_post(stem: str):
    from mapgen.braille import run_step8 as rerender_braille
    from mapgen.braille import update_step7_page_layout

    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not data or set(data) - {"map_origin_px", "orientation", "furniture"}:
        abort(400, "page layout update accepts map_origin_px, orientation, and/or furniture")
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            layout = update_step7_page_layout(RUNS_DIR / stem, data.get("map_origin_px"),
                                              data.get("orientation"), data.get("furniture"))
            if (RUNS_DIR / stem / "braille_labels.json").exists():
                rerender_braille(image, runs_dir=RUNS_DIR)
    except (FileNotFoundError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "layout": layout})


@app.route("/api/category-colors/<stem>", methods=["GET", "POST"])
def api_category_colors(stem: str):
    image = find_map(stem)
    if image is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "category_colors.json"
    current = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"colors": {}})
    aggregation_path = run_dir / "aggregation.json"
    allowed_labels = None
    if aggregation_path.exists():
        from mapgen.aggregate import effective_aggregation
        try:
            effective = effective_aggregation(run_dir, json.loads(aggregation_path.read_text(encoding="utf-8")))
            allowed_labels = {group["label"] for group in effective.get("groups", [])}
        except RuntimeError:
            if request.method == "POST":
                abort(409, "approve Step 5 final categories before assigning colours")
    if request.method == "GET":
        return jsonify(current)
    colors = (request.get_json(silent=True) or {}).get("colors")
    if not isinstance(colors, dict) or any(not isinstance(label, str) or not isinstance(value, str)
                                           or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)
                                           for label, value in colors.items()):
        abort(400, "colors must map category names to #RRGGBB values")
    if allowed_labels is not None and set(colors) - allowed_labels:
        abort(400, "colours must be assigned to approved final categories")
    # A color update writes and re-renders several downstream artifacts. Keep
    # that whole operation atomic so quick picker changes cannot race.
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "wait for the running pipeline step before saving colours")
        current = {"colors": {label: value.upper() for label, value in colors.items()}}
        path.write_text(json.dumps(current, indent=2), encoding="utf-8")
        if (run_dir / "symbols.json").exists():
            from mapgen.symbols import rerender_step7_artifacts
            symbols = json.loads((run_dir / "symbols.json").read_text(encoding="utf-8"))
            for assignment in symbols.get("area_assignments", []):
                color = current["colors"].get(assignment.get("label"))
                assignment.pop("color", None)
                if color:
                    assignment["color"] = color
            rerender_step7_artifacts(run_dir, symbols)
            from mapgen.boundaries import run_step8
            from mapgen.cleanup import run_step8a
            run_step8(image, runs_dir=RUNS_DIR)
            run_step8a(image, runs_dir=RUNS_DIR)
            if (run_dir / "braille_labels.json").exists():
                from mapgen.braille import run_step8 as run_braille
                run_braille(image, runs_dir=RUNS_DIR)
            if (run_dir / "legend_labels.json").exists():
                from mapgen.legend import run_step9
                run_step9(image, runs_dir=RUNS_DIR)
    return jsonify(current)


@app.get("/api/download/<stem>")
def api_download_pdf(stem: str):
    """Download the completed tactile map and legend as one two-page PDF."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    hybrid = request.args.get("variant") == "hybrid"
    map_name = "step8_hybrid.png" if hybrid else "step8_braille.png"
    legend_name = "step9_legend_hybrid.png" if hybrid else "step9_legend.png"
    map_path, legend_path = run_dir / map_name, run_dir / legend_name
    if not map_path.exists() or not legend_path.exists():
        abort(409, "run Steps 8 and 9 before downloading the combined PDF")
    with Image.open(map_path) as map_image, Image.open(legend_path) as legend_image:
        payload = io.BytesIO()
        first = map_image.convert("RGB")
        second = legend_image.convert("RGB")
        first.save(payload, format="PDF", save_all=True, append_images=[second],
                   resolution=float(map_image.info.get("dpi", (127, 127))[0]))
    payload.seek(0)
    kind = "hybrid_map" if hybrid else "relief_map"
    return send_file(payload, mimetype="application/pdf", as_attachment=True,
                     download_name=f"{stem}_{kind}_and_legend.pdf")


@app.get("/api/braille-font")
def api_braille_font():
    from mapgen.braille import braille_font_path

    response = send_file(braille_font_path(), mimetype="font/ttf")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/api/braille-labels/<stem>")
def api_braille_labels_get(stem: str):
    _, layout = _braille_layout(stem)
    return jsonify(layout)


@app.post("/api/braille-labels/<stem>")
def api_braille_labels_add(stem: str):
    from mapgen.braille import add_braille_label

    run_dir, _ = _braille_layout(stem)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) - {"text"}:
        abort(400, "new label accepts only an optional text field")
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            label, report = add_braille_label(run_dir, data.get("text", ""))
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "label": label, "enabled_labels": report["enabled_labels"]})


@app.post("/api/braille-labels/<stem>/title")
def api_braille_title_post(stem: str):
    from mapgen.braille import update_braille_title

    run_dir, _ = _braille_layout(stem)
    data = request.get_json(force=True)
    allowed = {"text", "enabled", "align", "position_page_px", "box_width_px"}
    if not isinstance(data, dict) or set(data) - allowed:
        abort(400, "title update accepts only text, enabled, align, position_page_px, and box_width_px")
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            title, report = update_braille_title(run_dir, data)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "title": title, "enabled_labels": report["enabled_labels"]})


@app.post("/api/braille-labels/<stem>/<label_id>")
def api_braille_labels_post(stem: str, label_id: str):
    from mapgen.braille import update_braille_label

    run_dir, _ = _braille_layout(stem)
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        abort(400, "label update must be an object")
    allowed = {"text", "enabled", "position_px", "side"}
    unknown = set(data) - allowed
    if unknown:
        return jsonify({"ok": False, "error":
                        f"unknown label field(s): {', '.join(sorted(unknown))}"}), 400
    try:
        # Serialize file-and-raster updates so two quick UI changes cannot
        # overwrite one another with an older layout snapshot.
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            label, report = update_braille_label(run_dir, label_id, data)
    except KeyError:
        abort(404, "unknown Step 8 label")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "label": label,
        "enabled_labels": report["enabled_labels"],
        "final_artifact": "step8_braille.png",
    })


def _legend_layout(stem: str) -> tuple[Path, dict]:
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "legend_labels.json"
    if not path.exists():
        abort(409, "run Step 9 before editing its legend")
    return run_dir, json.loads(path.read_text(encoding="utf-8"))


@app.get("/api/legend/<stem>")
def api_legend_get(stem: str):
    _, layout = _legend_layout(stem)
    return jsonify(layout)


@app.get("/api/legend-swatch/<stem>/<target>")
def api_legend_swatch(stem: str, target: str):
    from mapgen.legend import legend_swatch, legend_swatch_hybrid

    _, layout = _legend_layout(stem)
    entry = next((item for item in layout.get("entries", []) if item.get("id") == target), None)
    if entry is None:
        abort(404, "unknown legend entry")
    image = (legend_swatch_hybrid(layout, entry)
             if request.args.get("variant") == "hybrid" else legend_swatch(layout, entry))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(layout["page"]["dpi"],) * 2)
    buffer.seek(0)
    response = send_file(buffer, mimetype="image/png")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/legend-page/<stem>")
def api_legend_page_post(stem: str):
    from mapgen.legend import update_legend_page_orientation

    run_dir, _ = _legend_layout(stem)
    data = request.get_json(force=True)
    if not isinstance(data, dict) or set(data) != {"orientation"}:
        abort(400, "legend page update accepts only orientation")
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            layout, report = update_legend_page_orientation(run_dir, data["orientation"])
    except (FileNotFoundError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "layout": layout, "report": report})


@app.post("/api/legend/<stem>/<target>")
def api_legend_post(stem: str, target: str):
    from mapgen.legend import update_legend
    run_dir, _ = _legend_layout(stem)
    data = request.get_json(force=True)
    allowed = {"text", "enabled", "align", "position_page_px", "box_width_px"}
    if not isinstance(data, dict) or set(data) - allowed:
        abort(400, "legend edit contains an unsupported field")
    try:
        with _lock:
            item, report = update_legend(run_dir, target, data)
    except KeyError:
        abort(404, "unknown legend item")
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "item": item, "entries": report["entries"]})


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
    port = int(os.environ.get("MAPGEN_PORT", "5001"))
    print(f"MapGen UI on http://127.0.0.1:{port}  (project root: {ROOT})")
    app.run(host="127.0.0.1", port=port, threaded=True)
