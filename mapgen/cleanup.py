"""Step 8A -- SVG-style component layer cleanup.

Steps 1--8 remain untouched.  This step reconstructs the Step 8 owner strokes
and then repaints only solid-black components above them, exactly like a top
SVG fill layer.  Plain and patterned areas remain below the strokes.  A
centered stroke is still computed at its original 5 mm / 1 mm dimensions; the
portion beneath a solid-black top component is merely hidden.

Artifacts per map, under runs/<name>/:
    step8a_cleanup.json  layer ownership and repaint audit
    step8a_cleanup.png   cleaned layer-composited tactile raster
    step8a_debug.png     unchanged Step 8 | Step 8A cleanup
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .boundaries import (apply_boundary_strokes, build_group_map,
                         open_endpoint_count, _render_step8_base)
from .isolate import imread, imwrite
from .symbols import RENDER_PX_PER_MM


def _owner_centerline(group_map: np.ndarray,
                      owner_groups: set[int]) -> tuple[np.ndarray, int]:
    """Build closed contours for Step 8A owners without changing Step 8."""
    edge = np.zeros(group_map.shape, np.uint8)
    contour_count = 0
    for group_id in sorted(owner_groups):
        region = (group_map == group_id).astype(np.uint8)
        if not region.any():
            continue
        padded = cv2.copyMakeBorder(
            region, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0,
        )
        contours, _ = cv2.findContours(
            padded, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE,
        )
        for contour in contours:
            shifted = contour.copy()
            shifted[:, :, 0] -= 1
            shifted[:, :, 1] -= 1
            cv2.drawContours(edge, [shifted], -1, 255, 1, lineType=cv2.LINE_8)
        contour_count += len(contours)
    if edge.any():
        edge = cv2.ximgproc.thinning(
            edge, thinningType=cv2.ximgproc.THINNING_ZHANGSUEN,
        )
    return edge, contour_count


def compose_component_layers(base: np.ndarray, group_map: np.ndarray,
                             group_patterns: dict[int, str],
                             px_per_mm: float = RENDER_PX_PER_MM) -> tuple[np.ndarray, dict]:
    """Render owner strokes, then repaint only solid-black fills on top.

    Plain is the bottom fill layer, but still owns a complete contour. Every
    non-black component is below its boundary strokes and cannot repaint them.
    """
    owner_groups = {
        group_id for group_id, pattern in group_patterns.items()
        if pattern != "solid_black"
    }
    centerline, contour_count = _owner_centerline(group_map, owner_groups)
    open_endpoints = open_endpoint_count(centerline)
    if open_endpoints:
        raise RuntimeError(
            f"refusing Step 8A cleanup with {open_endpoints} open owner endpoint(s)"
        )
    stroked, white_px, black_px = apply_boundary_strokes(
        base, centerline, px_per_mm,
    )
    result = stroked.copy()
    repainted_groups = []
    repainted_components = 0
    restored_pixels = 0
    for group_id in sorted(group_patterns):
        if group_patterns[group_id] != "solid_black":
            continue
        mask = group_map == group_id
        if not mask.any():
            continue
        count, _ = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
        component_count = max(0, count - 1)
        changed = int(np.count_nonzero(result[mask] != base[mask]))
        result[mask] = base[mask]
        repainted_components += component_count
        restored_pixels += changed
        repainted_groups.append({
            "group_id": group_id,
            "pattern": group_patterns[group_id],
            "components": component_count,
            "restored_pixels": changed,
        })
    return result, {
        "owner_group_ids": sorted(owner_groups),
        "owner_contours": contour_count,
        "open_owner_endpoints": open_endpoints,
        "repainted_groups": repainted_groups,
        "repainted_components": repainted_components,
        "restored_pixels": restored_pixels,
        "white_stroke_px": white_px,
        "black_stroke_px": black_px,
    }


def run_step8a(image_path: Path, model: str | None = None,
               runs_dir: Path = Path("runs")) -> dict:
    """Apply the component-layer cleanup without modifying Steps 1--8."""
    from .boundaries import run_step8

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "step8_boundaries.json").exists():
        run_step8(image_path, model=model, runs_dir=runs_dir)

    symbols = json.loads((out_dir / "symbols.json").read_text(encoding="utf-8"))
    boundary_audit = json.loads(
        (out_dir / "step8_boundaries.json").read_text(encoding="utf-8")
    )
    assignments = symbols["area_assignments"]
    group_patterns = {
        group_id: assignment["pattern"]
        for group_id, assignment in enumerate(assignments)
    }
    step8_active_patterns = set(
        boundary_audit.get("active_priority_patterns", [])
    )
    boundary_fills = {
        pattern for pattern in group_patterns.values()
        if pattern != "solid_black"
    }
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int32) - 1
    base = _render_step8_base(out_dir, symbols)
    group_map = build_group_map(label_map, assignments, base.shape[:2])
    result, audit = compose_component_layers(
        base, group_map, group_patterns,
        float(symbols.get("render_px_per_mm", RENDER_PX_PER_MM)),
    )

    labels = {group_id: assignment.get("label", f"group {group_id}")
              for group_id, assignment in enumerate(assignments)}
    audit.update({
        "method": "svg_component_layers_solid_black_fill_on_top",
        "boundary_fills": sorted(boundary_fills),
        "step8_active_priority_patterns": sorted(step8_active_patterns),
        "owner_groups": [
            {"group_id": group_id, "label": labels[group_id],
             "pattern": group_patterns[group_id]}
            for group_id in audit.pop("owner_group_ids")
        ],
        "step8_artifacts_modified": False,
        "step7_artifacts_modified": False,
    })
    for item in audit["repainted_groups"]:
        item["label"] = labels[item["group_id"]]
    (out_dir / "step8a_cleanup.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    imwrite(out_dir / "step8a_cleanup.png", result)

    step8 = imread(out_dir / "step8_boundaries.png")[..., 0]
    debug = np.hstack((step8, result))
    if debug.shape[1] > 2200:
        scale = 2200 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step8a_debug.png", debug)
    return {
        "out_dir": out_dir,
        "canvas_px": [result.shape[1], result.shape[0]],
        "owner_groups": len(audit["owner_groups"]),
        "repainted_components": audit["repainted_components"],
        "restored_pixels": audit["restored_pixels"],
    }
