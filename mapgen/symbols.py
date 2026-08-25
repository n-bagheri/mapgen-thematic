"""Step 7 -- Tactile symbol assignment + first tactile master render.

Visible water gets the sinusoidal wave; water represented by the unprinted
white background remains no-fill. Ordered data gets a
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
import re
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field

from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .patterns import (DEFAULT_PATTERN_TRANSFORM, GROUPS, ORDERED_RAMPS, PATTERNS,
                       canonical_pattern_id, normalize_pattern_transform,
                       optimize_adjacent_pattern_variants,
                       optimize_user_pattern_change, render_pattern)
from .semantics import DEFAULT_MODEL, MapSemantics, _ensure_api_key, require_pipeline_eligible

RENDER_PX_PER_MM = 5.0

LINE_STYLES = {  # tactile line symbology per feature kind
    "border": {"width_mm": 1.2, "dash_mm": None, "desc": "thick solid line"},
    "border_or_coast": {"width_mm": 1.2, "dash_mm": None, "desc": "thick solid line"},
    "coastline": {"width_mm": 1.2, "dash_mm": None, "desc": "thick solid line"},
    "graticule": {"width_mm": 0.6, "dash_mm": [4.0, 2.0], "desc": "thin dashed line"},
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
    """Set distinct texture families without blocking the rendering pipeline.

    Step 5 has already produced the reviewed semantic grouping.  Calling the
    language model again here made Step 7 wait for a network timeout (and a
    retry) before falling back to the same greedy assignment.  Keep that
    optional proposal behind an explicit opt-in, while the normal pipeline is
    deterministic and local.
    """
    labels = [g["label"] for g in groups]
    chosen: dict[str, str] = {}
    use_model = os.environ.get("MAPGEN_STEP7_AI_TEXTURES", "").lower() in {"1", "true", "yes"}
    if use_model:
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
            notes.append(f"texture proposal failed ({exc}); assigning deterministically")
    else:
        notes.append("texture proposal skipped in Step 7; deterministic assignment used after reviewed aggregation")
    remaining = [t for t in available if t not in chosen.values()]
    for g in groups:
        if g["label"] not in chosen:
            g["texture_group"] = remaining.pop(0) if remaining else "dots"
            g.setdefault("texture_rationale", "deterministic assignment after reviewed aggregation")
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
                   include_region_boundaries: bool = True,
                   group_transforms: dict[int, dict] | None = None) -> np.ndarray:
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
        rendered_pattern = render_pattern(
            pid, (H2, W2), RENDER_PX_PER_MM,
            (group_transforms or {}).get(gid),
        )
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


def _hex_to_bgr(value: object) -> tuple[int, int, int] | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[index:index + 2], 16) for index in (5, 3, 1))


def render_hybrid_from_tactile(out_dir: Path, tactile: np.ndarray,
                               output_name: str) -> bool:
    """Colour regions underneath a saved black tactile layer."""
    symbols = json.loads((out_dir / "symbols.json").read_text(encoding="utf-8"))
    assignments = symbols["area_assignments"]
    colors = {group: _hex_to_bgr(item.get("color"))
              for group, item in enumerate(assignments)}
    if not any(colors.values()):
        (out_dir / output_name).unlink(missing_ok=True)
        return False
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1
    idx_to_group = {int(index): group for group, item in enumerate(assignments)
                    for index in item.get("members", [])}
    scaled = cv2.resize((label_map + 1).astype(np.uint8),
                        (tactile.shape[1], tactile.shape[0]), interpolation=cv2.INTER_NEAREST)
    lut = np.full(256, -1, np.int16)
    for index, group in idx_to_group.items():
        lut[index + 1] = group
    groups = lut[scaled]
    hybrid = np.full((*tactile.shape, 3), 255, np.uint8)
    for group, color in colors.items():
        if color is not None:
            hybrid[groups == group] = color
    # Exact print stack: category colour under the *final* tactile raster.
    # Step 8A may deliberately repaint pure-black components over Step 8's
    # compound clearance.  The saved masks describe the earlier boundary
    # geometry, so they may colour only pixels whose final relief value still
    # agrees with that stroke.  Otherwise the hybrid would resurrect white
    # outlines that the relief map has already removed.
    hybrid[tactile < 128] = (0, 0, 0)
    white_mask_path = out_dir / "step8_white_stroke_mask.png"
    if white_mask_path.exists():
        white_mask = imread(white_mask_path)[..., 0]
        if white_mask.shape != tactile.shape:
            white_mask = cv2.resize(white_mask, (tactile.shape[1], tactile.shape[0]),
                                    interpolation=cv2.INTER_NEAREST)
        hybrid[(white_mask > 0) & (tactile >= 128)] = (255, 255, 255)
    black_mask_path = out_dir / "step8_black_stroke_mask.png"
    if black_mask_path.exists():
        black_mask = imread(black_mask_path)[..., 0]
        if black_mask.shape != tactile.shape:
            black_mask = cv2.resize(black_mask, (tactile.shape[1], tactile.shape[0]),
                                    interpolation=cv2.INTER_NEAREST)
        hybrid[(black_mask > 0) & (tactile < 128)] = (0, 0, 0)
    imwrite(out_dir / output_name, hybrid)
    return True


def rerender_hybrid_artifacts(out_dir: Path) -> list[str]:
    """Refresh colour layers without rebuilding unchanged tactile geometry."""
    rendered: list[str] = []
    for tactile_name, hybrid_name in (
        ("step7_tactile.png", "step7_hybrid.png"),
        ("step8a_cleanup.png", "step8a_hybrid.png"),
    ):
        tactile_path = out_dir / tactile_name
        if not tactile_path.exists():
            continue
        tactile = imread(tactile_path)
        if tactile.ndim == 3:
            tactile = tactile[..., 0]
        if render_hybrid_from_tactile(out_dir, tactile, hybrid_name):
            rendered.append(hybrid_name)
    return rendered


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
    summary_path = (out_dir / "step6_summary.json"
                    if (out_dir / "step6_summary.json").exists()
                    else out_dir / "step5_summary.json")
    summary6 = json.loads(summary_path.read_text(encoding="utf-8"))
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
        mm_per_px=float(summary6["scale_mm_per_px"]),
    )
    result["review_source"] = labels_path.name
    (out_dir / "overlay_labels.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    return result


# --------------------------------------------------------------------------- runner

def resolve_group_raster_indices(classes: list[dict], source_members: list[int]) -> list[int]:
    """Map Step 4 class ids to the corresponding aggregated Step 6 raster id."""
    wanted = {int(index) for index in source_members}
    matches = [int(cl["index"]) for cl in classes
               if {int(index) for index in cl.get("members", [cl["index"]])} == wanted]
    return matches or [int(index) for index in source_members]


def _assignment_transform_key(assignment: dict) -> tuple[int, ...]:
    return tuple(sorted(int(index) for index in assignment.get(
        "source_members", assignment.get("members", []))))


def load_pattern_transforms(out_dir: Path) -> dict[tuple[int, ...], dict[str, float]]:
    """Load saved per-area transforms, indexed by stable Step 4 membership."""

    path = out_dir / "pattern_transforms.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            tuple(sorted(int(index) for index in item.get("source_members", []))):
                normalize_pattern_transform(item.get("transform"))
            for item in payload.get("groups", [])
            if item.get("source_members")
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def save_pattern_transforms(out_dir: Path, assignments: list[dict]) -> dict:
    """Persist transforms separately so SVG source assets remain immutable."""

    payload = {
        "version": 1,
        "coordinate_contract": {
            "scale": "percent layered over the Illustrator SVG patternTransform",
            "move": "millimetres in final tactile-map coordinates",
            "rotate": "degrees clockwise in SVG canvas coordinates",
        },
        "groups": [{
            "group_id": group_id,
            "label": assignment.get("label", f"group {group_id}"),
            "pattern": assignment["pattern"],
            "source_members": list(_assignment_transform_key(assignment)),
            "transform": normalize_pattern_transform(assignment.get("transform")),
        } for group_id, assignment in enumerate(assignments)],
    }
    (out_dir / "pattern_transforms.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def reassign_step7_pattern(out_dir: Path, symbols: dict, group_id: int,
                           pattern_id: str,
                           preserve_haptic_distances: bool = True) -> dict:
    """Apply one user choice, optionally re-optimizing every other area."""

    assignments = symbols.get("area_assignments", [])
    if group_id < 0 or group_id >= len(assignments):
        raise ValueError(f"Unknown Step 7 area: {group_id}")
    if pattern_id not in PATTERNS:
        raise ValueError(f"Unknown pattern: {pattern_id}")

    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1
    assignment_group_map = np.full(label_map.shape, -1, np.int32)
    for assignment_id, assignment in enumerate(assignments):
        for class_index in assignment.get("members", []):
            assignment_group_map[label_map == int(class_index)] = assignment_id

    current = {
        assignment_id: assignment["pattern"]
        for assignment_id, assignment in enumerate(assignments)
    }
    if preserve_haptic_distances:
        water_group_ids = {
            assignment_id for assignment_id, assignment in enumerate(assignments)
            if assignment.get("is_water") is True
            or (not assignment.get("is_thematic", True)
                and not assignment.get("is_background", False)
                and canonical_pattern_id(assignment.get("pattern", "plain"))
                    == "04_waves_sine")
        }
        optimized, audit = optimize_user_pattern_change(
            assignment_group_map, current, group_id, pattern_id,
            water_group_ids=water_group_ids,
        )
    else:
        optimized = dict(current)
        optimized[group_id] = pattern_id
        audit = {
            "method": "independent_user_pattern_assignment",
            "preserve_haptic_distances": False,
            "user_constraint": {"group_id": group_id, "pattern": pattern_id},
        }
    for assignment_id, assignment in enumerate(assignments):
        old_pattern = assignment["pattern"]
        chosen_pattern = optimized[assignment_id]
        family = PATTERNS[chosen_pattern]["group"]
        assignment.setdefault("pipeline_rationale", assignment.get("rationale", ""))
        assignment["pattern"] = chosen_pattern
        assignment["pattern_desc"] = PATTERNS[chosen_pattern]["desc"]
        assignment["pattern_family"] = family
        assignment["pattern_candidates"] = (
            [chosen_pattern] if assignment_id == group_id else
            list(GROUPS[family]) if preserve_haptic_distances and family != "none"
            else [chosen_pattern]
        )
        assignment["user_locked"] = bool(
            assignment.get("user_locked") and not preserve_haptic_distances
        ) or assignment_id == group_id
        if assignment_id == group_id:
            assignment["rationale"] = ("user-selected pattern; all remaining pattern variants "
                "were globally reoptimized for adjacent-area haptic distance"
                if preserve_haptic_distances else
                "user-selected pattern; other category patterns were left unchanged")
        elif preserve_haptic_distances and chosen_pattern != old_pattern:
            assignment["rationale"] = (
                "reassigned after a user pattern change to preserve unique "
                "pattern families and maximize adjacent-area haptic distance"
            )
        else:
            assignment["rationale"] = assignment["pipeline_rationale"]

    symbols["pattern_optimization"] = audit
    symbols["last_user_pattern_change"] = {
        "group_id": group_id,
        "pattern": pattern_id,
        "preserve_haptic_distances": preserve_haptic_distances,
    }
    return symbols


def rerender_step7_artifacts(out_dir: Path, symbols: dict | None = None) -> dict:
    """Re-render saved Step 7 assignments locally, without another model call."""

    symbols_path = out_dir / "symbols.json"
    if symbols is None:
        symbols = json.loads(symbols_path.read_text(encoding="utf-8"))
    assignments = symbols["area_assignments"]
    for assignment in assignments:
        assignment["transform"] = normalize_pattern_transform(assignment.get("transform"))
    symbols_path.write_text(
        json.dumps(symbols, indent=2, ensure_ascii=False), encoding="utf-8")
    save_pattern_transforms(out_dir, assignments)

    classes = json.loads((out_dir / "classes_gen.json").read_text(encoding="utf-8"))["classes"]
    summary6 = json.loads((out_dir / "step6_summary.json").read_text(encoding="utf-8"))
    lines = [feature["properties"] | {"points": feature["geometry"]["coordinates"]}
             for feature in json.loads(
                 (out_dir / "lines_gen.geojson").read_text(encoding="utf-8"))["features"]]
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1
    idx_to_group = {
        int(class_index): group_id
        for group_id, assignment in enumerate(assignments)
        for class_index in assignment.get("members", [])
    }
    group_patterns = {
        group_id: assignment["pattern"]
        for group_id, assignment in enumerate(assignments)
    }
    group_transforms = {
        group_id: assignment["transform"]
        for group_id, assignment in enumerate(assignments)
    }
    canvas = render_tactile(
        label_map, idx_to_group, group_patterns, lines,
        summary6["scale_mm_per_px"], OutputSpec.load_or_create(),
        group_transforms=group_transforms,
    )
    imwrite(out_dir / "step7_tactile.png", canvas)
    render_hybrid_from_tactile(out_dir, canvas, "step7_hybrid.png")
    overlay_labels = write_overlay_labels(out_dir)

    recon = np.full((*label_map.shape, 3), 255, np.uint8)
    for category in classes:
        if category["area_px"] > 0:
            recon[label_map == category["index"]] = np.uint8(category["rgb"][::-1])
    recon = cv2.resize(recon, (canvas.shape[1], canvas.shape[0]),
                       interpolation=cv2.INTER_NEAREST)
    debug = np.hstack([recon, cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)])
    if debug.shape[1] > 2200:
        scale = 2200 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step7_debug.png", debug)
    return {
        "canvas_px": [canvas.shape[1], canvas.shape[0]],
        "overlay_labels": len(overlay_labels["labels"]),
    }


def run_step7(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .aggregate import effective_aggregation, run_step5
    from .postprocess import run_step6_presets

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "aggregation.json").exists():
        run_step5(image_path, model=model, runs_dir=runs_dir)
    if not (out_dir / "label_map_gen.png").exists():
        run_step6_presets(image_path, model=model, runs_dir=runs_dir)

    spec = OutputSpec.load_or_create()
    sem = MapSemantics.model_validate_json((out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    require_pipeline_eligible(sem, "Step 7")
    agg = json.loads((out_dir / "aggregation.json").read_text(encoding="utf-8"))
    agg = effective_aggregation(out_dir, agg)
    classes = json.loads((out_dir / "classes_gen.json").read_text(encoding="utf-8"))["classes"]
    summary6 = json.loads((out_dir / "step6_summary.json").read_text(encoding="utf-8"))
    lines = [f["properties"] | {"points": f["geometry"]["coordinates"]}
             for f in json.loads((out_dir / "lines_gen.geojson").read_text(encoding="utf-8"))["features"]]
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1

    notes: list[str] = list(agg.get("notes", []))
    saved_transforms = load_pattern_transforms(out_dir)
    groups = [dict(g) for g in agg["groups"]]
    water = agg["water"]
    assignments: list[dict] = []
    idx_to_group: dict[int, int] = {}
    group_patterns: dict[int, str] = {}
    pattern_candidates: dict[int, tuple[str, ...]] = {}
    gid = 0

    textured_water = bool(water) and not bool(water.get("is_background"))
    if water:
        raster_ids = resolve_group_raster_indices(classes, water["members"])
        water_pattern = "04_waves_sine" if textured_water else "plain"
        group_patterns[gid] = water_pattern
        pattern_candidates[gid] = (water_pattern,)
        for idx in raster_ids:
            idx_to_group[idx] = gid
        assignments.append({"label": water["label"], "members": raster_ids,
                            "source_members": water["members"],
                            "pattern": water_pattern,
                            "pattern_desc": PATTERNS[water_pattern]["desc"],
                            "rationale": ("visible water uses the wavy pattern" if textured_water
                                          else "white background water is intentionally unfilled"),
                            "is_water": True,
                            "is_thematic": False, "is_background": not textured_water,
                            "legend_visible": textured_water})
        gid += 1

    if sem.data_ordering.value == "ordered":
        ramp = ORDERED_RAMPS[min(len(groups), 5)]
        for g, pid in zip(groups, ramp):
            g["pattern"] = pid
            g["texture_rationale"] = "perceived-order texture ramp (ordered data)"
    else:
        available = ["dots", "lines", "grids", "solids"] + ([] if textured_water else ["waves"])
        assign_qualitative(groups, available, sem, model, notes)
        for g in groups:
            tg = g["texture_group"]
            candidates = (("04_waves_triangle",) if tg == "waves"
                          else tuple(GROUPS[tg]))
            g["pattern_candidates"] = candidates
            g["pattern"] = candidates[0]

    for g in groups:
        candidates = tuple(g.get("pattern_candidates", (g["pattern"],)))
        raster_ids = resolve_group_raster_indices(classes, g["members"])
        group_patterns[gid] = g["pattern"]
        pattern_candidates[gid] = candidates
        for idx in raster_ids:
            idx_to_group[idx] = gid
        assignments.append({"label": g["label"], "members": raster_ids,
                            "source_members": g["members"], "pattern": g["pattern"],
                            "pattern_desc": PATTERNS[g["pattern"]]["desc"],
                            "rationale": g.get("texture_rationale", g.get("rationale", "")),
                            "is_water": False,
                            "is_thematic": True, "legend_visible": True})
        gid += 1

    # Every non-thematic class other than deliberately retained water is the
    # page/map background. It is smooth, uses no texture slot, and is hidden
    # from the legend. Thematic ``plain`` assignments above stay legendable.
    background_ids = [int(c["index"]) for c in classes
                      if c["area_px"] > 0 and not c.get("is_thematic")
                      and int(c["index"]) not in idx_to_group]
    if background_ids:
        group_patterns[gid] = "plain"
        pattern_candidates[gid] = ("plain",)
        for idx in background_ids:
            idx_to_group[idx] = gid
        assignments.append({"label": "background / no fill", "members": background_ids,
                            "source_members": [int(c["index"]) for c in classes
                                               if int(c["index"]) in background_ids],
                            "pattern": "plain", "pattern_desc": PATTERNS["plain"]["desc"],
                            "rationale": "non-thematic background is intentionally unfilled",
                            "is_water": False,
                            "is_thematic": False, "is_background": True,
                            "legend_visible": False})
        gid += 1

    # any surviving class not covered keeps its own plain region (boundaries still embossed)
    for c in classes:
        if c["area_px"] > 0 and c["index"] not in idx_to_group:
            group_patterns[gid] = "plain"
            pattern_candidates[gid] = ("plain",)
            idx_to_group[c["index"]] = gid
            assignments.append({"label": c["label"], "members": [c["index"]], "pattern": "plain",
                                "pattern_desc": PATTERNS["plain"]["desc"],
                                "rationale": "uncovered thematic class kept plain", "is_water": False,
                                "is_thematic": True,
                                "legend_visible": True})
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

        assignment["transform"] = saved_transforms.get(
            _assignment_transform_key(assignment), dict(DEFAULT_PATTERN_TRANSFORM))

    color_path = out_dir / "category_colors.json"
    saved_colors = (json.loads(color_path.read_text(encoding="utf-8")).get("colors", {})
                    if color_path.exists() else {})
    for assignment in assignments:
        color = saved_colors.get(assignment.get("label"))
        if _hex_to_bgr(color) is not None:
            assignment["color"] = color.upper()

    line_styles = {k: LINE_STYLES.get(k, LINE_STYLES["line"]) for k in {ln["kind"] for ln in lines}}
    symbols = {
        "area_assignments": assignments,
        "pattern_optimization": pattern_optimization,
        "line_styles": line_styles,
        "render_px_per_mm": RENDER_PX_PER_MM,
        "notes": notes,
    }
    render_result = rerender_step7_artifacts(out_dir, symbols)

    return {"out_dir": out_dir, "assignments": assignments, "notes": notes,
            **render_result}
