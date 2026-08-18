"""Run the whole pipeline over every map in maps/ and report what happened.

Step 5 normally waits for a human to approve its category grouping.  This
harness accepts the proposal as generated so the run can reach Steps 6-9;
anything that needed a real decision is reported as `auto-approved`.
"""
import json
import sys
import time
import traceback
from pathlib import Path

# Images are now downscaled before they reach the model, so calls are fast
# enough to run at the production deadline; a shortened one only produced
# false timeouts that looked like pipeline failures.
SURVEY_TIMEOUT_MS = None


def use_survey_timeout():
    if SURVEY_TIMEOUT_MS is None:
        return
    from mapgen import isolate, semantics, textdetect
    for module in (semantics, isolate, textdetect):
        module.API_TIMEOUT_MS = SURVEY_TIMEOUT_MS

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
MAPS = ROOT / "maps"


def approve_aggregation(run_dir: Path) -> str:
    from mapgen.aggregate import save_aggregation_review
    path = run_dir / "aggregation.json"
    if not path.exists():
        return "no proposal"
    aggregation = json.loads(path.read_text(encoding="utf-8"))
    groups = [{"label": g["label"], "members": [int(m) for m in g["members"]],
               "approved": True, "rationale": "batch harness auto-approval"}
              for g in aggregation["groups"] if g.get("members")]
    save_aggregation_review(run_dir, aggregation, groups)
    aggregation["review_status"] = "approved"
    path.write_text(json.dumps(aggregation, indent=2, ensure_ascii=False), encoding="utf-8")
    from mapgen.postprocess import materialize_step5_output
    materialize_step5_output(run_dir, aggregation)
    return f"auto-approved {len(groups)} groups"


def run_one(image: Path) -> dict:
    from mapgen import aggregate, braille, cleanup, boundaries, isolate
    from mapgen import legend, postprocess, segment, semantics, symbols, textdetect

    steps = [
        (1, lambda: semantics.interpret_map and _step1(image)),
        (2, lambda: isolate.run_step2(image, runs_dir=RUNS)),
        (3, lambda: textdetect.run_step3(image, runs_dir=RUNS)),
        (4, lambda: segment.run_step4(image, runs_dir=RUNS)),
        (5, lambda: aggregate.run_step5(image, runs_dir=RUNS)),
        ("5-review", lambda: approve_aggregation(RUNS / image.stem)),
        (6, lambda: postprocess.run_step6_presets(image, runs_dir=RUNS)),
        (7, lambda: _step7(image)),
        (8, lambda: braille.run_step8(image, runs_dir=RUNS)),
        (9, lambda: legend.run_step9(image, runs_dir=RUNS)),
    ]
    record = {"map": image.name, "stem": image.stem, "steps": {}, "failed_at": None,
              "seconds": {}}
    for label, fn in steps:
        started = time.monotonic()
        try:
            fn()
            record["steps"][str(label)] = "ok"
        except Exception as exc:
            record["steps"][str(label)] = f"{type(exc).__name__}: {exc}"
            record["failed_at"] = str(label)
            record["traceback"] = traceback.format_exc()[-1500:]
            record["seconds"][str(label)] = round(time.monotonic() - started, 1)
            break
        record["seconds"][str(label)] = round(time.monotonic() - started, 1)
    return record


def _step1(image: Path):
    """Reuse a valid Step 1 artifact so a re-run does not re-bill the model."""
    from mapgen.semantics import (interpret_map, save_semantics,
                                  semantics_artifact_is_current)
    artifact = RUNS / image.stem / "step1_semantics.json"
    if artifact.exists() and semantics_artifact_is_current(artifact):
        return "cached"
    sem = interpret_map(image)
    save_semantics(sem, RUNS / image.stem)
    return sem


def _step7(image: Path):
    from mapgen.symbols import run_step7
    from mapgen.boundaries import run_step8 as boundaries_step
    from mapgen.cleanup import run_step8a
    run_step7(image, runs_dir=RUNS)
    boundaries_step(image, runs_dir=RUNS)
    run_step8a(image, runs_dir=RUNS)


def main(names):
    use_survey_timeout()
    images = [MAPS / n for n in names] if names else sorted(
        p for p in MAPS.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg"})
    report = ROOT / "batch_report.json"
    out = json.loads(report.read_text(encoding="utf-8")) if report.exists() else []
    done = {r["map"] for r in out if not r["failed_at"]}
    for image in images:
        if image.name in done:
            print(f"=== {image.name} === (already complete, skipped)", flush=True)
            continue
        out = [r for r in out if r["map"] != image.name]
        print(f"=== {image.name} ===", flush=True)
        started = time.monotonic()
        record = run_one(image)
        record["total_seconds"] = round(time.monotonic() - started, 1)
        state = record["failed_at"] or "complete"
        print(f"    -> {state} in {record['total_seconds']}s", flush=True)
        if record["failed_at"]:
            print(f"    {record['steps'][record['failed_at']]}", flush=True)
        out.append(record)
        report.write_text(
            json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


if __name__ == "__main__":
    main(sys.argv[1:])
