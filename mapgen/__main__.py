"""CLI for the MapGen pipeline steps.

    python -m mapgen step0 [--force]        write config/output_spec.json (defaults)
    python -m mapgen step1 IMAGE [IMAGE...] semantic interpretation -> runs/<name>/step1_semantics.json
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .output_spec import DEFAULT_CONFIG_PATH, OutputSpec


def cmd_step0(args: argparse.Namespace) -> int:
    if DEFAULT_CONFIG_PATH.exists() and not args.force:
        spec = OutputSpec.load()
        print(f"exists: {DEFAULT_CONFIG_PATH} (medium={spec.medium}, "
              f"page={spec.page_width_mm}x{spec.page_height_mm} mm); use --force to reset to defaults")
        return 0
    path = OutputSpec().save()
    print(f"wrote {path} -- edit it to match your production setup, then re-run later steps")
    return 0


def cmd_step1(args: argparse.Namespace) -> int:
    from .semantics import interpret_map, save_semantics

    spec = OutputSpec.load_or_create()  # Step 0 must exist before Step 1
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"interpreting {image.name} ...")
        try:
            sem = interpret_map(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        out = save_semantics(sem, image)
        slots = spec.texture_slots(sem.water_present)
        print(f"  {sem.map_type.value} | ordering={sem.data_ordering.value} | in_scope={sem.in_scope}")
        print(f"  {len(sem.thematic_classes)} thematic classes, water={sem.water_present} "
              f"-> {slots} texture slots (aggregation needed: {len(sem.thematic_classes) > slots})")
        print(f"  -> {out}")
    return 1 if failures else 0


def cmd_step2(args: argparse.Namespace) -> int:
    from .isolate import run_step2

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"isolating {image.name} ...")
        try:
            r = run_step2(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        cached = " (layout cached)" if r["layout_cached"] else ""
        print(f"  map crop {r['map_crop']}{cached}; legend={'yes' if r['legend'] else 'no'}; "
              f"colors sampled for {r['classes_with_color']}/{r['classes_total']} classes")
        for warning in r["warnings"]:
            print(f"  WARN: {warning}")
        print(f"  -> {r['out_dir']}\\step2_debug.png")
    return 1 if failures else 0


def cmd_step3(args: argparse.Namespace) -> int:
    from .textdetect import run_step3

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"detecting text on {image.name} ...")
        try:
            r = run_step3(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        cached = " (detections cached)" if r["raw_cached"] else ""
        kinds = ", ".join(f"{k}={v}" for k, v in sorted(r["kinds"].items())) or "none"
        print(f"  {r['total']} labels ({kinds}); strokes masked for {r['masked']}/{r['total']}{cached}")
        for warning in r["warnings"]:
            print(f"  WARN: {warning}")
        print(f"  -> {r['out_dir']}\\step3_debug.png")
    return 1 if failures else 0


def cmd_step4(args: argparse.Namespace) -> int:
    from .segment import run_step4

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"segmenting {image.name} ...")
        try:
            r = run_step4(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        lines = ", ".join(f"{k}={v}" for k, v in sorted(r["line_kinds"].items())) or "none"
        print(f"  {r['polygons']} polygons, {r['polylines']} polylines ({lines}); "
              f"filled {r['filled_px']} px (text/lines/speckle x{r['speckles']})")
        for c in sorted(r["classes"], key=lambda c: -c["area_share"]):
            if c["area_share"] >= 0.001:
                flag = "thematic" if c["is_thematic"] else c["source"]
                print(f"    {c['area_share']*100:5.1f}%  {c['label'][:46]} [{flag}]")
        for note in r["notes"]:
            print(f"  NOTE: {note}")
        print(f"  -> {r['out_dir']}\\step4_debug.png")
    return 1 if failures else 0


def cmd_step5(args: argparse.Namespace) -> int:
    from .generalize import run_step5_presets

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"generalizing {image.name} ...")
        try:
            r = run_step5_presets(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        s = r["summary"]
        print(f"  scale {s['scale_mm_per_px']} mm/px ({s['orientation']}, "
              f"map {s['map_size_mm'][0]}x{s['map_size_mm'][1]} mm on {s['page_mm'][0]}x{s['page_mm'][1]} mm)")
        print(f"  dissolved {s['dissolved_components']} small components; islands: "
              f"{s['islands']['dropped']} dropped, {s['islands']['exaggerated']} exaggerated")
        print(f"  lines: {s['lines_kept']} kept ({s['line_joins']} joins, "
              f"{s['lines_dropped_short']} dropped short); {r['polygons']} polygons")
        if s["classes_vanished"]:
            print(f"  vanished classes: {', '.join(s['classes_vanished'])}")
        print(f"  -> {r['out_dir']}\\step5_debug.png")
    return 1 if failures else 0


def cmd_step6(args: argparse.Namespace) -> int:
    from .aggregate import run_step6

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"aggregating {image.name} ...")
        try:
            r = run_step6(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        a = r["aggregation"]
        print(f"  mode={a['mode']}; {len(a['groups'])} groups in {a['slots']} slots; "
              f"water={'yes' if a['water'] else 'no'}")
        for g in a["groups"]:
            print(f"    {g['label']}  <-  {', '.join(g['member_labels'])}")
        for n in a["notes"]:
            print(f"  NOTE: {n}")
    return 1 if failures else 0


def cmd_step7(args: argparse.Namespace) -> int:
    from .symbols import run_step7

    OutputSpec.load_or_create()
    failures = 0
    for image in args.images:
        image = Path(image)
        if not image.exists():
            print(f"missing: {image}", file=sys.stderr)
            failures += 1
            continue
        print(f"assigning symbols for {image.name} ...")
        try:
            r = run_step7(image, model=args.model)
        except Exception as exc:  # noqa: BLE001 - CLI boundary
            print(f"  FAILED: {exc}", file=sys.stderr)
            failures += 1
            continue
        for a in r["assignments"]:
            print(f"    {a['label'][:38]:40} -> {a['pattern_desc']}")
        for n in r["notes"]:
            print(f"  NOTE: {n}")
        print(f"  tactile render {r['canvas_px'][0]}x{r['canvas_px'][1]} px "
              f"-> {r['out_dir']}\\step7_tactile.png")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mapgen")
    sub = parser.add_subparsers(dest="command", required=True)

    p0 = sub.add_parser("step0", help="write the output specification (no AI)")
    p0.add_argument("--force", action="store_true", help="overwrite an existing config with defaults")
    p0.set_defaults(func=cmd_step0)

    p1 = sub.add_parser("step1", help="semantic interpretation via Gemini")
    p1.add_argument("images", nargs="+", help="map image file(s)")
    p1.add_argument("--model", default=None,
                    help="override model id (default: GEMINI_MODEL env or Gemma 4)")
    p1.set_defaults(func=cmd_step1)

    p2 = sub.add_parser("step2", help="isolate map area + legend, sample class colors")
    p2.add_argument("images", nargs="+", help="map image file(s)")
    p2.add_argument("--model", default=None, help="override model id for the layout call")
    p2.set_defaults(func=cmd_step2)

    p3 = sub.add_parser("step3", help="detect, classify and mask overlay text")
    p3.add_argument("images", nargs="+", help="map image file(s)")
    p3.add_argument("--model", default=None, help="override model id for the text pass")
    p3.set_defaults(func=cmd_step3)

    p4 = sub.add_parser("step4", help="segment areas, remove text, extract lines (no AI)")
    p4.add_argument("images", nargs="+", help="map image file(s)")
    p4.add_argument("--model", default=None, help="model id if earlier steps must be run first")
    p4.set_defaults(func=cmd_step4)

    p5 = sub.add_parser("step5", help="minimum-size generalization + simplification (no AI)")
    p5.add_argument("images", nargs="+", help="map image file(s)")
    p5.add_argument("--model", default=None, help="model id if earlier steps must be run first")
    p5.set_defaults(func=cmd_step5)

    p6 = sub.add_parser("step6", help="class aggregation into texture slots")
    p6.add_argument("images", nargs="+", help="map image file(s)")
    p6.add_argument("--model", default=None, help="override model id for merge proposals")
    p6.set_defaults(func=cmd_step6)

    p7 = sub.add_parser("step7", help="tactile symbol assignment + master render")
    p7.add_argument("images", nargs="+", help="map image file(s)")
    p7.add_argument("--model", default=None, help="override model id for texture proposals")
    p7.set_defaults(func=cmd_step7)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
