"""Local web UI backend for the MapGen pipeline (Steps 0-4).

Run:    .venv\\Scripts\\python.exe webui\\server.py
Open:   http://127.0.0.1:5001

Design: the pipeline stays in mapgen/*; this server only wraps it. Steps run
in a background thread (one job per map at a time) and the frontend polls the
job log. Artifacts are served straight from runs/<stem>/.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from functools import lru_cache
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
_label_crop_cache_lock = threading.Lock()


@lru_cache(maxsize=4)
def _cached_label_crop_inputs(labels_name: str, labels_mtime_ns: int,
                              image_name: str, image_mtime_ns: int):
    """Decode one map's label-review inputs once per artifact version.

    A large map can have hundreds of label crops. Decoding the multi-megabyte
    source raster independently for each thumbnail starves unrelated preview
    requests. The modification times make this cache self-invalidating after a
    Step 3 rerun, while the small bound prevents old projects retaining memory.
    """
    from mapgen.isolate import imread

    labels = json.loads(Path(labels_name).read_text(encoding="utf-8")).get("labels", [])
    return labels, imread(Path(image_name))


def _label_crop_inputs(labels_path: Path, image_path: Path):
    key = (str(labels_path.resolve()), labels_path.stat().st_mtime_ns,
           str(image_path.resolve()), image_path.stat().st_mtime_ns)
    # functools.lru_cache may compute the same cold key more than once when
    # requests arrive concurrently. Serializing the cache call prevents the
    # Africa label list from launching several full-size decodes at once.
    with _label_crop_cache_lock:
        return _cached_label_crop_inputs(*key)

# The focused view in webui/static/minimal and this file are one contract: the
# page posts actions and reads review flags that only a server of the same
# vintage implements.  Bump this whenever an endpoint the focused view depends
# on gains or changes a field, so a page served from a newer checkout can name
# the mismatch instead of failing later with an "unsupported field" refusal.
UI_CONTRACT = 2

# Every source file this process was started from.  `app.run` deliberately runs
# without the reloader, so a pipeline job is never killed mid-step -- which also
# means updating the checkout while the server is up leaves the browser on the
# new static files and this process on the old Python.  That skew is the one
# failure the focused view cannot diagnose from its own side.
_SOURCE_FILES = (Path(__file__).resolve(), *sorted((ROOT / "mapgen").glob("*.py")))
_STALE_CHECK_SECONDS = 2.0


def _source_fingerprint() -> tuple | None:
    """Hash the loaded sources, ignoring harmless timestamp-only changes.

    Editors and file scanners can briefly replace or lock a source file. A
    failed read is inconclusive and must not permanently label the running
    server as stale.
    """
    marks = []
    for path in _SOURCE_FILES:
        try:
            digest = hashlib.sha256(path.read_bytes()).digest()
        except OSError:
            return None
        marks.append((path.relative_to(ROOT).as_posix(), digest))
    return tuple(marks)


_STARTED_FINGERPRINT = _source_fingerprint()
_stale_source_check: tuple[float, bool] = (0.0, False)


def restart_required() -> bool:
    """True while loaded source differs from the source currently on disk.

    The check is throttled because /api/maps is polled about once a second
    while a job runs.
    """
    global _stale_source_check
    checked_at, stale = _stale_source_check
    now = time.monotonic()
    if now - checked_at < _STALE_CHECK_SECONDS:
        return stale
    current = _source_fingerprint()
    # A transient read failure says nothing about source freshness. Keep the
    # last reliable result and try again after the normal throttle interval.
    if current is not None:
        stale = current != _STARTED_FINGERPRINT
    _stale_source_check = (now, stale)
    return stale

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
        "step6_transitions.json", "step6_params.json", "step6_review.json"),
    7: ("overlay_labels.json", "pattern_transforms.json", "page_layout.json",
        "step7_review.json",
        "step7_hybrid.png", "step8a_hybrid.png", "step8_white_stroke_mask.png",
        "step8_black_stroke_mask.png"),
    8: ("step8_hybrid.png", "step8_hybrid_base.png", "step8_review.json"),
    9: ("step9_legend_hybrid.png", "step9_review.json"),
}

CANONICAL_STEP_ORDER = (1, 2, 3, 4, 5, 6, 7, 8, 9)
PROJECT_ORDER_PATH = RUNS_DIR / ".project_order.json"


def _canonical_step(value) -> int:
    return int(value)


def _canonical_steps_from(step: int) -> tuple[int, ...]:
    normalized = _canonical_step(step)
    return CANONICAL_STEP_ORDER[CANONICAL_STEP_ORDER.index(normalized):]


def _invalidate_run_from(stem: str, from_step: int) -> list[str]:
    """Remove artifacts that depend on an earlier step's newly saved result."""
    run_dir = RUNS_DIR / stem
    invalidated: list[str] = []
    seen: set[Path] = set()
    for step in _canonical_steps_from(from_step):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact in seen:
                continue
            seen.add(artifact)
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    if from_step <= 6:
        for artifact in run_dir.glob("step6_preset_*"):
            if artifact.is_file():
                artifact.unlink()
                invalidated.append(artifact.name)
    return invalidated


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
    # A missing legend no longer blocks the pipeline: Step 2 derives the class
    # palette from the map's dominant colours instead.  Only the map type
    # (checked separately via in_scope) can stop a run after Step 1.
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
        if not step_done(stem, 8):
            return False
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


def step6_review_ready(stem: str) -> bool:
    """True only when the active simplification preset was chosen by a user."""
    run_dir = RUNS_DIR / stem
    review_path = run_dir / "step6_review.json"
    params_path = run_dir / "step6_params.json"
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
        params = json.loads(params_path.read_text(encoding="utf-8"))
        selected_level_matches = int(review.get("level", 0)) == int(
            params.get("simplification_level", -1))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    return bool(review.get("approved")) and selected_level_matches and step_done(stem, 6)


def _step7_review_payload(stem: str) -> dict:
    """Load the persisted Step 7 choices, with safe defaults for older runs."""
    path = RUNS_DIR / stem / "step7_review.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {
        "version": 1,
        "approved": bool(saved.get("approved", False)),
        "preserve_haptic_distances": bool(saved.get("preserve_haptic_distances", True)),
        "create_hybrid_map": bool(saved.get("create_hybrid_map", False)),
    }


def _save_step7_review(stem: str, payload: dict) -> dict:
    normalized = {
        "version": 1,
        "approved": bool(payload.get("approved", False)),
        "preserve_haptic_distances": bool(payload.get("preserve_haptic_distances", True)),
        "create_hybrid_map": bool(payload.get("create_hybrid_map", False)),
    }
    path = RUNS_DIR / stem / "step7_review.json"
    path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    if not normalized["approved"]:
        _invalidate_step8_review(stem)
        _invalidate_step9_review(stem)
    return normalized


def step7_review_ready(stem: str) -> bool:
    """True once the current pattern and hybrid choices were explicitly approved."""
    return _step7_review_payload(stem)["approved"] and step_done(stem, 7)


def _step8_review_payload(stem: str) -> dict:
    """Load the explicit label-and-page approval for Step 8."""
    path = RUNS_DIR / stem / "step8_review.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {"version": 1, "approved": bool(saved.get("approved", False))}


def _save_step8_review(stem: str, payload: dict) -> dict:
    normalized = {"version": 1, "approved": bool(payload.get("approved", False))}
    path = RUNS_DIR / stem / "step8_review.json"
    path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return normalized


def _invalidate_step8_review(stem: str) -> None:
    path = RUNS_DIR / stem / "step8_review.json"
    if not path.exists():
        return
    _save_step8_review(stem, {"approved": False})


def step8_review_ready(stem: str) -> bool:
    """True once the current Braille labels and page geometry were approved."""
    return _step8_review_payload(stem)["approved"] and step_done(stem, 8)


def _step9_review_payload(stem: str) -> dict:
    path = RUNS_DIR / stem / "step9_review.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved = {}
    return {"version": 1, "approved": bool(saved.get("approved", False))}


def _save_step9_review(stem: str, payload: dict) -> dict:
    normalized = {"version": 1, "approved": bool(payload.get("approved", False))}
    path = RUNS_DIR / stem / "step9_review.json"
    path.write_text(json.dumps(normalized, indent=2), encoding="utf-8")
    return normalized


def _invalidate_step9_review(stem: str) -> None:
    path = RUNS_DIR / stem / "step9_review.json"
    if path.exists():
        _save_step9_review(stem, {"approved": False})


def step9_review_ready(stem: str) -> bool:
    return _step9_review_payload(stem)["approved"] and step_done(stem, 9)


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
            log("no legend read on this map; Step 2 will derive classes from the map colours")
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
        from mapgen.braille import load_step7_page_layout
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
        # Step 7 now pauses for a decision before Braille is generated. Build
        # its paper contract here so the review already uses the configured
        # page size, margins, and orientation.
        load_step7_page_layout(RUNS_DIR / image.stem)
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
            if s == 1 and result is not None and not retained_step1:
                invalidated = _invalidate_run_from(stem, 2)
                if invalidated:
                    log(
                        f"retired {len(invalidated)} stale downstream artifact(s) "
                        "after the new Step 1 reading")
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


@app.get("/api/version")
def api_version():
    """What this process implements, and whether its own sources moved under it."""
    return jsonify({"contract": UI_CONTRACT, "restart_required": restart_required()})


@app.route("/api/<path:endpoint>",
           methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def api_unknown(endpoint: str):
    """Answer for an endpoint this build of the server has never had.

    Static files are served from the root (`static_url_path=""`), so without
    this the static rule matches an unknown /api/... path as well -- for GET
    only.  A POST to a missing endpoint then reports "the method is not allowed
    for the requested URL", which is true and names the wrong problem.  Say what
    is actually wrong, and say it as JSON so the focused view can show it.
    """
    detail = (" This page was served by a newer checkout than the running"
              " server; stop webui/server.py and start it again."
              if restart_required() else "")
    return jsonify({
        "error": f"this server has no /api/{endpoint} endpoint.{detail}",
        "contract": UI_CONTRACT,
        "restart_required": restart_required(),
    }), 404


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
            "step6_review_ready": step6_review_ready(p.stem),
            "step7_review_ready": step7_review_ready(p.stem),
            "step8_review_ready": step8_review_ready(p.stem),
            "step9_review_ready": step9_review_ready(p.stem),
            "job": snapshot.get(p.stem),
        })
    # The focused view checks these two on every poll: they are how a page from
    # a newer checkout recognises a server that has not been restarted yet.
    return jsonify({"maps": maps, "contract": UI_CONTRACT,
                    "restart_required": restart_required()})


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
    # A removed or interrupted project can leave a run directory behind. Do
    # not attach those artifacts to a new upload that happens to share its
    # filename; a new map must always start with an empty Run setup.
    while dest.exists() or (RUNS_DIR / dest.stem).exists():
        dest = MAPS_DIR / f"{Path(name).stem}_{i}{Path(name).suffix}"
        i += 1
    MAPS_DIR.mkdir(exist_ok=True)
    f.save(dest)
    # A freshly added map is the item the reader is about to work on, so keep
    # it immediately reachable at the top of the library.
    existing_order.insert(0, dest.stem)
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
        if 6 in steps:
            (run_dir / "step6_review.json").unlink(missing_ok=True)
        if 7 in steps:
            (run_dir / "step7_review.json").unlink(missing_ok=True)
        if 8 in steps:
            (run_dir / "step8_review.json").unlink(missing_ok=True)
        if 9 in steps:
            (run_dir / "step9_review.json").unlink(missing_ok=True)
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
    try:
        review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        review = {}
    return jsonify({
        "width": int(image.shape[1]), "height": int(image.shape[0]),
        "kept_pixels": int((mask > 0).sum()),
        "automatic_pixels": int((automatic > 0).sum()),
        "reviewed": int(review.get("strokes_saved", 0)) > 0,
        "approved": bool(review.get("approved", False)),
    })


@app.post("/api/maskreview/<stem>")
def api_maskreview_post(stem: str):
    import cv2
    import numpy as np
    from mapgen.isolate import imread, imwrite, prepare_text_input

    run_dir, map_path, mask_path, auto_path = _mask_review_paths(stem)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) - {"strokes", "reset", "approve"}:
        abort(400, "mask review accepts strokes, reset, or approve")
    actions = int("strokes" in data) + int(data.get("reset") is True) + int(data.get("approve") is True)
    if actions != 1:
        abort(400, "choose exactly one mask-review action")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
        mask = imread(mask_path)[..., 0]
        automatic = imread(auto_path)[..., 0]
        review_path = run_dir / "map_mask_review.json"
        if data.get("approve") is True:
            try:
                review = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
            except (OSError, json.JSONDecodeError):
                review = {}
            review.update({
                "version": 2,
                "approved": True,
                "strokes_saved": int(review.get("strokes_saved", 0)),
                "kept_pixels": int((mask > 0).sum()),
            })
            review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "approved": True,
                            "kept_pixels": int((mask > 0).sum()),
                            "invalidated": [], "downstream_invalidated": False})
        if data.get("reset"):
            mask = automatic.copy()
            review_path.unlink(missing_ok=True)
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
            review_path.write_text(json.dumps({
                "version": 2, "approved": False, "strokes_saved": stroke_count,
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
    labels, image = _label_crop_inputs(labels_path, image_path)
    if index < 0 or index >= len(labels):
        abort(404, "unknown label occurrence")

    import cv2
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


@app.post("/api/aggregation-preview/<stem>")
def api_aggregation_preview_post(stem: str):
    """Render unsaved Step 5 category assignments without changing the run."""
    import cv2
    import numpy as np
    from mapgen.aggregate import build_aggregation_review
    from mapgen.isolate import imread
    from mapgen.postprocess import build_group_definitions, group_raster, _render_groups

    if find_map(stem) is None:
        abort(404, "unknown map")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "a job is running for this map")
    run_dir = RUNS_DIR / stem
    aggregation_path = run_dir / "aggregation.json"
    classes_path = run_dir / "classes_final.json"
    label_map_path = run_dir / "label_map.png"
    if not all(path.exists() for path in (aggregation_path, classes_path, label_map_path)):
        abort(409, "run through Step 5 before previewing category assignments")
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) != {"groups"}:
        abort(400, "aggregation preview requires groups")
    aggregation = json.loads(aggregation_path.read_text(encoding="utf-8"))
    try:
        review = build_aggregation_review(aggregation, data["groups"])
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    preview_aggregation = dict(aggregation)
    preview_aggregation["groups"] = [
        {key: value for key, value in group.items() if key != "approved"}
        for group in review["groups"]
    ]
    classes = json.loads(classes_path.read_text(encoding="utf-8"))["classes"]
    source = imread(label_map_path)[..., 0].astype(np.int16) - 1
    definitions = build_group_definitions(preview_aggregation, classes)
    grouped, _ = group_raster(source, definitions)
    preview = _render_groups(grouped, definitions)
    ok, encoded = cv2.imencode(".png", preview)
    if not ok:
        abort(500, "could not render the category preview")
    response = send_file(io.BytesIO(encoded.tobytes()), mimetype="image/png")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response


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
    for step in (7, 8, 9):
        for name in STEP_ARTIFACTS[step] + STEP_EXTRA[step]:
            artifact = run_dir / name
            if artifact.exists():
                artifact.unlink()
                invalidated.append(name)
    (run_dir / "step6_review.json").write_text(json.dumps({
        "version": 1,
        "approved": True,
        "level": level,
    }, indent=2), encoding="utf-8")
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
            "is_water": bool(assignment.get("is_water", False)
                             or (not assignment.get("is_thematic", True)
                                 and not assignment.get("is_background", False)
                                 and pattern == "04_waves_sine")),
            "rationale": assignment.get("rationale", ""),
            "editable": pattern not in {"plain", "solid_black"},
            "transform": normalize_pattern_transform(assignment.get("transform")),
        })
    library = [{
        "pattern": pattern,
        "pattern_desc": metadata["desc"],
        "pattern_family": metadata["group"],
        "water_only": pattern == "04_waves_sine",
        "editable": pattern not in {"plain", "solid_black"},
    } for pattern, metadata in PATTERNS.items()]
    return {
        "groups": groups,
        "library": library,
        "defaults": DEFAULT_PATTERN_TRANSFORM,
        "limits": {key: {"min": limits[0], "max": limits[1]}
                   for key, limits in PATTERN_TRANSFORM_LIMITS.items()},
    }


@app.route("/api/step7-review/<stem>", methods=["GET", "POST"])
def api_step7_review(stem: str):
    """Persist the two pattern modes and the explicit Step 7 approval gate."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    if not (RUNS_DIR / stem / "symbols.json").exists():
        abort(409, "run Step 7 before reviewing its tactile patterns")
    current = _step7_review_payload(stem)
    if request.method == "GET":
        return jsonify(current)
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "wait for the running pipeline step before changing Step 7")

    data = request.get_json(silent=True) or {}
    allowed = {"preserve_haptic_distances", "create_hybrid_map", "approve"}
    if not isinstance(data, dict) or not data or set(data) - allowed:
        abort(400, "Step 7 review accepts pattern modes and/or approval")
    for key in allowed & set(data):
        if not isinstance(data[key], bool):
            abort(400, f"{key} must be true or false")
    if "preserve_haptic_distances" in data:
        current["preserve_haptic_distances"] = data["preserve_haptic_distances"]
        current["approved"] = False
    if "create_hybrid_map" in data:
        current["create_hybrid_map"] = data["create_hybrid_map"]
        current["approved"] = False
    if data.get("approve"):
        if not step_done(stem, 7):
            abort(409, "finish Step 7 before approving its tactile patterns")
        current["approved"] = True
    elif data.get("approve") is False:
        current["approved"] = False
    return jsonify(_save_step7_review(stem, current))


@app.get("/api/pattern-transforms/<stem>")
def api_pattern_transforms_get(stem: str):
    _, symbols = _step7_symbols(stem)
    return jsonify(_pattern_editor_payload(symbols))


@lru_cache(maxsize=256)
def _pattern_preview_png(pattern_id: str,
                         transform_items: tuple[tuple[str, float], ...] = ()) -> bytes:
    """Render one small editor swatch once per pattern/transform combination.

    The focused UI may mount the same swatch in both a category row and its
    edit dialog.  Rasterising the Illustrator SVG for every HTTP request made
    those identical views contend with the finished-map preview.  The cache
    key contains the complete normalized transform, so a user edit naturally
    gets a new image without explicit invalidation.
    """
    import cv2
    from mapgen.patterns import render_pattern

    transform = dict(transform_items) if transform_items else None
    preview = render_pattern(pattern_id, (128, 128), 5.0, transform)
    ok, encoded = cv2.imencode(".png", preview)
    if not ok:
        raise RuntimeError("could not render pattern preview")
    return encoded.tobytes()


@app.get("/api/pattern-library-preview/<pattern_id>")
def api_pattern_library_preview(pattern_id: str):
    from mapgen.patterns import PATTERNS

    if pattern_id not in PATTERNS:
        abort(404, "unknown tactile pattern")
    try:
        png = _pattern_preview_png(pattern_id)
    except RuntimeError:
        abort(500, "could not render pattern preview")
    response = send_file(io.BytesIO(png), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@app.get("/api/pattern-preview/<stem>/<int:group_id>")
def api_pattern_preview(stem: str, group_id: int):
    from mapgen.patterns import normalize_pattern_transform

    _, symbols = _step7_symbols(stem)
    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        abort(404, "unknown Step 7 area")
    assignment = assignments[group_id]
    transform = normalize_pattern_transform(assignment.get("transform"))
    try:
        png = _pattern_preview_png(assignment["pattern"], tuple(transform.items()))
    except RuntimeError:
        abort(500, "could not render pattern preview")
    response = send_file(io.BytesIO(png), mimetype="image/png")
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
    if (run_dir / "step8a_cleanup.png").exists():
        from mapgen.braille import load_step7_page_layout
        load_step7_page_layout(run_dir)
    review = _step7_review_payload(stem)
    review["approved"] = False
    _save_step7_review(stem, review)
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
    if not isinstance(data, dict):
        abort(400, "select a pattern from the Step 7 library")
    pattern_id = data.get("pattern")
    if not isinstance(pattern_id, str) or pattern_id not in PATTERNS:
        return jsonify({"ok": False, "error": "select a pattern from the Step 7 library"}), 400
    review = _step7_review_payload(stem)
    preserve_haptic_distances = data.get(
        "preserve_haptic_distances", review["preserve_haptic_distances"])
    if not isinstance(preserve_haptic_distances, bool):
        abort(400, "preserve_haptic_distances must be true or false")

    run_dir, symbols = _step7_symbols(stem)
    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        abort(404, "unknown Step 7 area")
    try:
        reassign_step7_pattern(
            run_dir, symbols, group_id, pattern_id, preserve_haptic_distances)
    except (OSError, TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    rerender_step7_artifacts(run_dir, symbols)
    run_step8(image, runs_dir=RUNS_DIR)
    run_step8a(image, runs_dir=RUNS_DIR)
    if (run_dir / "step8a_cleanup.png").exists():
        from mapgen.braille import load_step7_page_layout
        load_step7_page_layout(run_dir)
    review["approved"] = False
    review["preserve_haptic_distances"] = preserve_haptic_distances
    _save_step7_review(stem, review)
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
        "review": review,
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


@app.route("/api/step8-review/<stem>", methods=["GET", "POST"])
def api_step8_review(stem: str):
    """Expose the explicit Step 8 toolbox/page approval gate."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    if not (RUNS_DIR / stem / "braille_labels.json").exists():
        abort(409, "run Step 8 before reviewing its labels and page layout")
    current = _step8_review_payload(stem)
    if request.method == "GET":
        return jsonify(current)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) != {"approve"} or not isinstance(
            data.get("approve"), bool):
        abort(400, "Step 8 review accepts one boolean approve field")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "wait for the running pipeline step before approving Step 8")
        if data["approve"] and not step_done(stem, 8):
            abort(409, "finish Step 8 before approving its page layout")
        current["approved"] = data["approve"]
        return jsonify(_save_step8_review(stem, current))


@app.post("/api/braille-layout/<stem>")
def api_braille_layout_post(stem: str):
    from mapgen.braille import update_braille_toolbox

    run_dir, _ = _braille_layout(stem)
    data = request.get_json(force=True)
    allowed = {"all_text_enabled", "fix_text_to_map", "group_map_elements",
               "map_origin_px", "furniture"}
    if not isinstance(data, dict) or not data or set(data) - allowed:
        abort(400, "Step 8 layout update accepts text, grouping, map position, and furniture fields")
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            layout, report = update_braille_toolbox(run_dir, data)
            _invalidate_step8_review(stem)
    except (TypeError, ValueError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "layout": layout,
                    "enabled_labels": report["enabled_labels"]})


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
                _invalidate_step8_review(stem)
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
    symbols_path = run_dir / "symbols.json"
    if symbols_path.exists():
        try:
            symbols_for_labels = json.loads(symbols_path.read_text(encoding="utf-8"))
            allowed_labels = {
                str(assignment.get("label"))
                for assignment in symbols_for_labels.get("area_assignments", [])
                if assignment.get("label")
            }
        except (OSError, json.JSONDecodeError):
            allowed_labels = None
    elif aggregation_path.exists():
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
            from mapgen.symbols import rerender_hybrid_artifacts
            symbols = json.loads((run_dir / "symbols.json").read_text(encoding="utf-8"))
            for assignment in symbols.get("area_assignments", []):
                color = current["colors"].get(assignment.get("label"))
                assignment.pop("color", None)
                if color:
                    assignment["color"] = color
            (run_dir / "symbols.json").write_text(
                json.dumps(symbols, indent=2, ensure_ascii=False), encoding="utf-8")
            # Colours sit underneath the unchanged tactile raster. Rebuilding
            # pattern geometry, boundaries, and component cleanup here made a
            # single colour-picker change unnecessarily take several seconds.
            rerender_hybrid_artifacts(run_dir)
            review = _step7_review_payload(stem)
            review["approved"] = False
            _save_step7_review(stem, review)
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
    if not step9_review_ready(stem):
        abort(409, "approve the Step 9 legend before exporting the final PDF")
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
            _invalidate_step8_review(stem)
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
            _invalidate_step8_review(stem)
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
    allowed = {"text", "enabled", "position_px", "side", "callout", "pin_shape"}
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
            _invalidate_step8_review(stem)
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


@app.delete("/api/braille-labels/<stem>/<label_id>")
def api_braille_labels_delete(stem: str, label_id: str):
    from mapgen.braille import delete_braille_label

    run_dir, _ = _braille_layout(stem)
    try:
        with _lock:
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            deleted, report = delete_braille_label(run_dir, label_id)
            _invalidate_step8_review(stem)
    except KeyError:
        abort(404, "unknown Step 8 label")
    return jsonify({"ok": True, "deleted": deleted,
                    "enabled_labels": report["enabled_labels"]})


def _legend_layout(stem: str) -> tuple[Path, dict]:
    if find_map(stem) is None:
        abort(404, "unknown map")
    run_dir = RUNS_DIR / stem
    path = run_dir / "legend_labels.json"
    if not path.exists():
        abort(409, "run Step 9 before editing its legend")
    return run_dir, json.loads(path.read_text(encoding="utf-8"))


@app.route("/api/step9-review/<stem>", methods=["GET", "POST"])
def api_step9_review(stem: str):
    """Persist the explicit final legend approval that unlocks export."""
    if find_map(stem) is None:
        abort(404, "unknown map")
    if not (RUNS_DIR / stem / "legend_labels.json").exists():
        abort(409, "run Step 9 before reviewing the legend page")
    current = _step9_review_payload(stem)
    if request.method == "GET":
        return jsonify(current)
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or set(data) != {"approve"} or not isinstance(
            data.get("approve"), bool):
        abort(400, "Step 9 review accepts one boolean approve field")
    with _lock:
        job = _jobs.get(stem)
        if job and job["status"] == "running":
            abort(409, "wait for the running pipeline step before approving Step 9")
        if data["approve"] and not step_done(stem, 9):
            abort(409, "finish Step 9 before approving its legend")
        current["approved"] = data["approve"]
        return jsonify(_save_step9_review(stem, current))


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
            _invalidate_step9_review(stem)
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
            job = _jobs.get(stem)
            if job and job["status"] == "running":
                abort(409, "a job is running for this map")
            item, report = update_legend(run_dir, target, data)
            _invalidate_step9_review(stem)
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


@app.after_request
def _never_cache_the_application(response):
    """Serve the pages and their modules fresh.

    Only the entry module carries a version query, and an ES module's imports
    inherit the importing URL without it, so every other module would be cached
    under a bare path.  These files are read from local disk; there is nothing
    to gain by letting a browser hold an older copy of one of them.
    """
    path = request.path
    if path in ("/", "/minimal") or path.endswith((".js", ".css", ".html")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("MAPGEN_PORT", "5001"))
    print(f"MapGen UI on http://127.0.0.1:{port}  (project root: {ROOT})")
    print(f"UI contract {UI_CONTRACT}. Updating the checkout does not reload a "
          f"running server -- stop and start this process after a git pull.")
    app.run(host="127.0.0.1", port=port, threaded=True)
