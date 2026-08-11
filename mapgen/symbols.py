"""Step 7 -- Tactile symbol assignment + first tactile master render.

Water always gets the sinusoidal wave and closes the wave group (only the
triangular wave remains usable when no water exists). Ordered data gets a
perceived-order texture ramp; qualitative groups are matched to texture
groups by meaning (small Gemini call, one DISTINCT texture group per class),
then all concrete SVG variants are chosen together to maximize the worst
embedding distance over adjacent patterned regions. Plain and pure-black
adjacencies are excluded. Non-thematic extras get patterns only if slots
remain, else plain. All region boundaries are embossed.

Artifacts per map, under runs/<name>/:
    symbols.json        pattern + line-style assignments with rationales
    overlay_labels.json final text positions in source px, tactile px, and mm
    step7_tactile.png   rendered tactile master preview (black = raised)
    step7_debug.png     generalized color map | tactile render
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field

from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .patterns import (GROUPS, ORDERED_RAMPS, PATTERNS,
                       optimize_adjacent_pattern_variants, render_pattern)
from .semantics import DEFAULT_MODEL, MapSemantics, _ensure_api_key

RENDER_PX_PER_MM = 5.0

LINE_STYLES = {  # tactile line symbology per feature kind
    "border": {"width_mm": 1.2, "dash_mm": None, "desc": "thick solid line"},
    "border_or_coast": {"width_mm": 1.2, "dash_mm": None, "desc": "thick solid line"},
    "river": {"width_mm": 0.6, "dash_mm": None, "desc": "thin solid line"},
    "road": {"width_mm": 0.8, "dash_mm": [3.0, 2.0], "desc": "dashed line"},
    "line": {"width_mm": 0.6, "dash_mm": [2.0, 2.0], "desc": "thin dashed line"},
}


# --------------------------------------------------------------------------- texture-group call

class TextureChoice(BaseModel):
    class_label: str
    texture_group: str = Field(description="one of the offered texture groups")
    rationale: str


class TexturePlan(BaseModel):
    choices: list[TextureChoice]


TEXTURE_PROMPT = """\
Assign each thematic map class below to ONE texture group for a tactile map.
Available texture groups (each may be used AT MOST ONCE):
{groups}

Choose by MEANING: e.g. granular dots suit deserts or scattered crops, solid
raised surface suits mountains or dense forest, line hatchings suit fields or
grasslands, grids suit built-up or structured areas. Every class gets exactly
one group; no group is used twice.

Map subject: {subject}

Classes:
{listing}
"""

GROUP_HINTS = {
    "dots": "granular dots (sparse to dense)",
    "lines": "parallel line hatching (horizontal / vertical / diagonal)",
    "grids": "square or diagonal grid",
    "solids": "solid raised surface",
    "waves": "triangular zig-zag lines",
}


def propose_textures(group_labels: list[str], available: list[str], sem: MapSemantics,
                     model: str | None) -> TexturePlan:
    from .semantics import generate_json

    prompt = TEXTURE_PROMPT.format(
        groups="\n".join(f"- {g}: {GROUP_HINTS[g]}" for g in available),
        subject=sem.subject,
        listing="\n".join(f"- {lb}" for lb in group_labels),
    )
    return generate_json([prompt], TexturePlan, model=model)


def assign_qualitative(groups: list[dict], available: list[str], sem: MapSemantics,
                       model: str | None, notes: list[str]) -> None:
    """Sets group['texture_group'] for each group, distinct, semantically chosen."""
    labels = [g["label"] for g in groups]
    chosen: dict[str, str] = {}
    try:
        plan = propose_textures(labels, available, sem, model)
        used: set[str] = set()
        for c in plan.choices:
            if c.class_label in labels and c.texture_group in available and c.texture_group not in used:
                chosen[c.class_label] = c.texture_group
                used.add(c.texture_group)
                for g in groups:
                    if g["label"] == c.class_label:
                        g["texture_rationale"] = c.rationale
    except Exception as exc:  # noqa: BLE001 - proposal is best-effort
        notes.append(f"texture proposal failed ({exc}); assigning greedily")
    remaining = [t for t in available if t not in chosen.values()]
    for g in groups:
        if g["label"] not in chosen:
            g["texture_group"] = remaining.pop(0) if remaining else "dots"
            g.setdefault("texture_rationale", "greedy assignment")
        else:
            g["texture_group"] = chosen[g["label"]]


# --------------------------------------------------------------------------- rendering

def _draw_dashed(canvas: np.ndarray, pts: np.ndarray, thickness: int, dash_px: list[float]) -> None:
    on, off = dash_px
    dist_on, drawing = 0.0, True
    prev = pts[0]
    for cur in pts[1:]:
        seg = float(np.linalg.norm(cur - prev))
        t = 0.0
        while t < seg:
            span = (on if drawing else off) - dist_on
            step = min(span, seg - t)
            a = prev + (cur - prev) * (t / seg)
            b = prev + (cur - prev) * ((t + step) / seg)
            if drawing:
                cv2.line(canvas, tuple(np.int32(a)), tuple(np.int32(b)), 0, thickness)
            dist_on += step
            if dist_on >= (on if drawing else off) - 1e-6:
                drawing, dist_on = not drawing, 0.0
            t += step
        prev = cur


def render_tactile(label_map: np.ndarray, idx_to_group: dict[int, int],
                   group_patterns: dict[int, str], lines: list[dict],
                   mm_per_px: float, spec: OutputSpec,
                   include_region_boundaries: bool = True) -> np.ndarray:
    h, w = label_map.shape
    scale = mm_per_px * RENDER_PX_PER_MM
    W2, H2 = int(round(w * scale)), int(round(h * scale))
    lm2 = cv2.resize((label_map + 1).astype(np.uint8), (W2, H2), interpolation=cv2.INTER_NEAREST)

    lut = np.full(256, -1, np.int16)
    for idx, gid in idx_to_group.items():
        lut[idx + 1] = gid
    gmap = lut[lm2]

    canvas = np.full((H2, W2), 255, np.uint8)
    for gid, pid in group_patterns.items():
        region = gmap == gid
        if not region.any() or pid == "plain":
            continue
        rendered_pattern = render_pattern(pid, (H2, W2), RENDER_PX_PER_MM)
        canvas[region] = rendered_pattern[region]

    if include_region_boundaries:
        # Kept for alternate/legacy callers.  Canonical boundaries are now
        # selected globally and rendered by Step 8.
        edge = np.zeros((H2, W2), np.uint8)
        edge[:-1][gmap[:-1] != gmap[1:]] = 255
        edge[:, :-1][gmap[:, :-1] != gmap[:, 1:]] = 255
        bw = max(2, int(round(spec.constants.min_line_width_mm * RENDER_PX_PER_MM)))
        edge = cv2.dilate(edge, np.ones((bw, bw), np.uint8))
        canvas[edge > 0] = 0

    for ln in lines:
        if (not include_region_boundaries
                and ln["kind"] in {"border", "border_or_coast"}):
            # Step 8 owns every area/coast boundary in the canonical branch;
            # drawing extracted boundary ink here would bypass its exceptions.
            continue
        style = LINE_STYLES.get(ln["kind"], LINE_STYLES["line"])
        pts = np.array(ln["points"], np.float64) * scale
        thickness = max(2, int(round(style["width_mm"] * RENDER_PX_PER_MM)))
        if style["dash_mm"]:
            _draw_dashed(canvas, pts, thickness,
                         [d * RENDER_PX_PER_MM for d in style["dash_mm"]])
        else:
            cv2.polylines(canvas, [np.int32(pts)], False, 0, thickness)
    return canvas


def _point(values: list[float] | tuple[float, float]) -> list[float]:
    return [float(values[0]), float(values[1])]


def _text_position(label: dict) -> tuple[list[float], str]:
    """Read the explicit Step 3 position, upgrading legacy label records."""
    if label.get("text_position") is not None:
        return _point(label["text_position"]), label.get("text_position_source", "unknown")
    quad = label.get("quad")
    if quad:
        pts = np.asarray(quad, np.float64)
        return [float(pts[:, 0].mean()), float(pts[:, 1].mean())], "quad_centroid_legacy"
    x0, y0, x1, y1 = label["box"]
    return [(float(x0) + float(x1)) / 2, (float(y0) + float(y1)) / 2], "box_center_legacy"


def _feature_position(label: dict) -> tuple[list[float] | None, str | None]:
    if label.get("feature_position") is not None:
        return _point(label["feature_position"]), label.get("feature_position_source")
    if label.get("anchor_source") == "point_symbol" and label.get("anchor") is not None:
        return _point(label["anchor"]), "point_symbol_legacy"
    return None, None


def build_overlay_labels(labels_json: dict, source_shape: tuple[int, int],
                         canvas_shape: tuple[int, int], mm_per_px: float) -> dict:
    """Transform Step 3 label positions into final map-only tactile space.

    The tactile canvas has no page margin at this stage, so its origin is the
    map origin. A later page-layout step can add one offset to every position.
    """
    source_h, source_w = source_shape
    canvas_h, canvas_w = canvas_shape
    sx = canvas_w / max(1, source_w)
    sy = canvas_h / max(1, source_h)

    def tactile_point(point: list[float]) -> list[float]:
        return [round(point[0] * sx, 3), round(point[1] * sy, 3)]

    def mm_point(point: list[float]) -> list[float]:
        return [round(point[0] * mm_per_px, 3), round(point[1] * mm_per_px, 3)]

    def tactile_box(box: list[float]) -> list[float]:
        return [round(float(box[0]) * sx, 3), round(float(box[1]) * sy, 3),
                round(float(box[2]) * sx, 3), round(float(box[3]) * sy, 3)]

    def tactile_quad(quad: list[list[float]] | None) -> list[list[float]] | None:
        return [tactile_point(_point(p)) for p in quad] if quad else None

    output_labels = []
    for label in labels_json.get("labels", []):
        text_pos, text_source = _text_position(label)
        feature_pos, feature_source = _feature_position(label)
        output_labels.append({
            "text": label["text"],
            "kind": label["kind"],
            "priority": label.get("priority"),
            "recognition_status": label.get("recognition_status"),
            "text_position_source": text_source,
            "text_position_source_px": [round(v, 3) for v in text_pos],
            "text_position_tactile_px": tactile_point(text_pos),
            "text_position_mm": mm_point(text_pos),
            "feature_position_source": feature_source,
            "feature_position_source_px": ([round(v, 3) for v in feature_pos]
                                            if feature_pos is not None else None),
            "feature_position_tactile_px": (tactile_point(feature_pos)
                                             if feature_pos is not None else None),
            "feature_position_mm": (mm_point(feature_pos)
                                     if feature_pos is not None else None),
            "box_source_px": [float(v) for v in label["box"]],
            "box_tactile_px": tactile_box(label["box"]),
            "quad_source_px": label.get("quad"),
            "quad_tactile_px": tactile_quad(label.get("quad")),
        })

    return {
        "coordinate_contract": {
            "origin": "top-left of map; x right, y down",
            "source_space": "map_area.png / label_map_gen.png pixels",
            "source_size_px": [source_w, source_h],
            "tactile_space": "step7_tactile.png pixels (map-only canvas)",
            "tactile_size_px": [canvas_w, canvas_h],
            "map_origin_tactile_px": [0, 0],
            "map_origin_mm": [0, 0],
            "source_to_tactile_scale": [round(sx, 6), round(sy, 6)],
            "source_mm_per_px": mm_per_px,
            "note": "Add the future page-layout map origin to tactile positions after placing the map on a page.",
        },
        "labels": output_labels,
    }


def write_overlay_labels(out_dir: Path) -> dict:
    """Write the final coordinate export from reviewed labels when available."""
    from .labelreview import REVIEW_VERSION, labels_fingerprint

    label_map = imread(out_dir / "label_map_gen.png")[..., 0]
    canvas = imread(out_dir / "step7_tactile.png")
    summary5 = json.loads((out_dir / "step5_summary.json").read_text(encoding="utf-8"))
    raw_path = out_dir / "labels.json"
    raw_json = (json.loads(raw_path.read_text(encoding="utf-8"))
                if raw_path.exists() else {"labels": []})
    approved_path = out_dir / "approved_labels.json"
    approved_json = (json.loads(approved_path.read_text(encoding="utf-8"))
                     if approved_path.exists() else None)
    approved_is_current = bool(
        approved_json
        and approved_json.get("review", {}).get("version") == REVIEW_VERSION
        and approved_json.get("review", {}).get("labels_fingerprint")
        == labels_fingerprint(raw_json)
    )
    labels_json = approved_json if approved_is_current else raw_json
    labels_path = approved_path if approved_is_current else raw_path
    result = build_overlay_labels(
        labels_json,
        source_shape=label_map.shape[:2],
        canvas_shape=canvas.shape[:2],
        mm_per_px=float(summary5["scale_mm_per_px"]),
    )
    result["review_source"] = labels_path.name
    (out_dir / "overlay_labels.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return result


# --------------------------------------------------------------------------- runner

def run_step7(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .aggregate import effective_aggregation, run_step6

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "aggregation.json").exists():
        run_step6(image_path, model=model, runs_dir=runs_dir)

    spec = OutputSpec.load_or_create()
    sem = MapSemantics.model_validate_json((out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    agg = json.loads((out_dir / "aggregation.json").read_text(encoding="utf-8"))
    agg = effective_aggregation(out_dir, agg)
    classes = json.loads((out_dir / "classes_gen.json").read_text(encoding="utf-8"))["classes"]
    summary5 = json.loads((out_dir / "step5_summary.json").read_text(encoding="utf-8"))
    lines = [f["properties"] | {"points": f["geometry"]["coordinates"]}
             for f in json.loads((out_dir / "lines_gen.geojson").read_text(encoding="utf-8"))["features"]]
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1

    notes: list[str] = list(agg.get("notes", []))
    groups = [dict(g) for g in agg["groups"]]
    water = agg["water"]
    assignments: list[dict] = []
    idx_to_group: dict[int, int] = {}
    group_patterns: dict[int, str] = {}
    pattern_candidates: dict[int, tuple[str, ...]] = {}
    gid = 0

    if water:
        group_patterns[gid] = "04_waves_sine"
        pattern_candidates[gid] = ("04_waves_sine",)
        for idx in water["members"]:
            idx_to_group[idx] = gid
        assignments.append({"label": water["label"], "members": water["members"],
                            "pattern": "04_waves_sine",
                            "pattern_desc": PATTERNS["04_waves_sine"]["desc"],
                            "rationale": "water always gets the wavy pattern", "is_thematic": False})
        gid += 1

    if sem.data_ordering.value == "ordered":
        ramp = ORDERED_RAMPS[min(len(groups), 5)]
        for g, pid in zip(groups, ramp):
            g["pattern"] = pid
            g["texture_rationale"] = "perceived-order texture ramp (ordered data)"
    else:
        available = ["dots", "lines", "grids", "solids"] + ([] if water else ["waves"])
        assign_qualitative(groups, available, sem, model, notes)
        for g in groups:
            tg = g["texture_group"]
            candidates = (("04_waves_triangle",) if tg == "waves"
                          else tuple(GROUPS[tg]))
            g["pattern_candidates"] = candidates
            g["pattern"] = candidates[0]

    for g in groups:
        candidates = tuple(g.get("pattern_candidates", (g["pattern"],)))
        group_patterns[gid] = g["pattern"]
        pattern_candidates[gid] = candidates
        for idx in g["members"]:
            idx_to_group[idx] = gid
        assignments.append({"label": g["label"], "members": g["members"], "pattern": g["pattern"],
                            "pattern_desc": PATTERNS[g["pattern"]]["desc"],
                            "rationale": g.get("texture_rationale", g.get("rationale", "")),
                            "is_thematic": True})
        gid += 1

    # non-thematic extras: pattern only if slots remain, else plain (still bounded)
    remaining_slots = spec.constants.max_area_textures - len(
        [a for a in assignments if a["pattern"] != "plain"])
    extras = sorted(agg["non_thematic_extra"], key=lambda e: (e["priority"] is None, e["priority"]))
    used_families = {
        PATTERNS[assignment["pattern"]]["group"]
        for assignment in assignments
        if PATTERNS[assignment["pattern"]]["group"] != "none"
    }
    unused_groups = [t for t in ["dots", "lines", "grids", "solids"]
                     if t not in used_families]
    for e in extras:
        if remaining_slots > 0 and unused_groups:
            family = unused_groups.pop(0)
            candidates = tuple(GROUPS[family])
            pid = candidates[0]
            remaining_slots -= 1
            rationale = "spare texture slot assigned by priority"
        else:
            candidates = ("plain",)
            pid = "plain"
            rationale = "no texture slots left"
        group_patterns[gid] = pid
        pattern_candidates[gid] = candidates
        idx_to_group[e["index"]] = gid
        assignments.append({"label": e["label"], "members": [e["index"]], "pattern": pid,
                            "pattern_desc": PATTERNS[pid]["desc"], "rationale": rationale,
                            "is_thematic": False})
        gid += 1

    # any surviving class not covered keeps its own plain region (boundaries still embossed)
    for c in classes:
        if c["area_px"] > 0 and c["index"] not in idx_to_group:
            group_patterns[gid] = "plain"
            pattern_candidates[gid] = ("plain",)
            idx_to_group[c["index"]] = gid
            assignments.append({"label": c["label"], "members": [c["index"]], "pattern": "plain",
                                "pattern_desc": PATTERNS["plain"]["desc"],
                                "rationale": "uncovered class kept plain", "is_thematic": False})
            gid += 1

    assignment_group_map = np.full(label_map.shape, -1, np.int32)
    for class_index, group_id in idx_to_group.items():
        assignment_group_map[label_map == class_index] = group_id
    group_patterns, pattern_optimization = optimize_adjacent_pattern_variants(
        assignment_group_map, pattern_candidates,
    )
    for group_id, assignment in enumerate(assignments):
        chosen_pattern = group_patterns[group_id]
        candidates = pattern_candidates[group_id]
        assignment["pattern"] = chosen_pattern
        assignment["pattern_desc"] = PATTERNS[chosen_pattern]["desc"]
        assignment["pattern_family"] = PATTERNS[chosen_pattern]["group"]
        assignment["pattern_candidates"] = list(candidates)
        if len(candidates) > 1:
            base_rationale = assignment.get("rationale", "").rstrip("; ")
            selection_rationale = "variant selected by global adjacent-pattern maximin distance"
            assignment["rationale"] = (
                f"{base_rationale}; {selection_rationale}"
                if base_rationale else selection_rationale
            )

    line_styles = {k: LINE_STYLES.get(k, LINE_STYLES["line"]) for k in {ln["kind"] for ln in lines}}
    (out_dir / "symbols.json").write_text(json.dumps({
        "area_assignments": assignments,
        "pattern_optimization": pattern_optimization,
        "line_styles": line_styles,
        "render_px_per_mm": RENDER_PX_PER_MM,
        "notes": notes,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    canvas = render_tactile(label_map, idx_to_group, group_patterns, lines,
                            summary5["scale_mm_per_px"], spec)
    imwrite(out_dir / "step7_tactile.png", canvas)
    overlay_labels = write_overlay_labels(out_dir)

    # debug: generalized color map | tactile render
    recon = np.full((*label_map.shape, 3), 255, np.uint8)
    for c in classes:
        if c["area_px"] > 0:
            recon[label_map == c["index"]] = np.uint8(c["rgb"][::-1])
    recon = cv2.resize(recon, (canvas.shape[1], canvas.shape[0]), interpolation=cv2.INTER_NEAREST)
    dbg = np.hstack([recon, cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)])
    if dbg.shape[1] > 2200:
        s = 2200 / dbg.shape[1]
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step7_debug.png", dbg)

    return {"out_dir": out_dir, "assignments": assignments, "notes": notes,
            "canvas_px": [canvas.shape[1], canvas.shape[0]],
            "overlay_labels": len(overlay_labels["labels"])}
