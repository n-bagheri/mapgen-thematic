"""Alt MapGen Step 7 -- direct render of Alt Step 6's final group raster."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .patterns import (GROUPS, ORDERED_RAMPS, PATTERNS,
                       optimize_adjacent_pattern_variants, pattern_info)
from .semantics import MapSemantics
from .symbols import (LINE_STYLES, RENDER_PX_PER_MM, build_overlay_labels,
                      render_tactile)


def tactile_minimum_for_pattern(pattern: str, base_min_mm: float = 13.0) -> dict:
    """Return the physical footprint needed to recognise an area pattern."""
    try:
        info = pattern_info(pattern)
    except KeyError:
        info = PATTERNS["plain"]
    family = info["group"]
    if family == "solids":
        width_mm = max(10.0, base_min_mm * 0.75)
        reason = "solid surface needs a stable bounded patch"
    elif family == "none":
        width_mm = max(10.0, base_min_mm * 0.75)
        reason = "smooth area needs enough space to distinguish no texture"
    else:
        # Pattern density is elements per centimetre. Three repetitions are
        # needed so a texture is felt as a pattern instead of an isolated mark.
        pitch_mm = 10.0 / max(float(info["density"]), 1e-6)
        width_mm = max(float(base_min_mm), 3.0 * pitch_mm)
        reason = "fits at least three repetitions of the assigned pattern"
    width_mm = round(width_mm, 1)
    return {
        "pattern": pattern,
        "pattern_family": family,
        "min_width_mm": width_mm,
        "min_area_mm2": round(width_mm * width_mm, 1),
        "reason": reason,
    }


def assign_alt_qualitative(groups: list[dict], available: list[str]) -> None:
    """Assign stable, distinct texture families without a network/model call."""
    preferences = {
        "forest": "solids", "wood": "solids",
        "grass": "lines", "pasture": "lines",
        "field": "grids", "cereal": "grids", "cropland": "grids",
        "specialty": "dots", "vine": "dots", "olive": "dots", "garden": "dots",
    }
    unused = list(available)
    for group in groups:
        lower = group["label"].lower()
        preferred = next((texture for word, texture in preferences.items()
                          if word in lower and texture in unused), None)
        texture = preferred or unused[0]
        unused.remove(texture)
        group["texture_group"] = texture
        group["texture_rationale"] = "stable semantic texture assignment"


def generalize_group_geometry(group_map: np.ndarray, min_area_px: float | None,
                              mm_per_px: float, level: int,
                              group_patterns: dict[int, str] | None = None,
                              base_min_mm: float | None = None,
                              group_labels: dict[int, str] | None = None) -> tuple[np.ndarray, dict]:
    """Make approved final categories readable at the configured print scale.

    The test is performed after aggregation and texture assignment. A region
    must have both enough total area and enough usable width for its particular
    texture. Complete failing regions are offered to the touching neighbour
    with the longest shared boundary. Per-group budgets limit distortion and
    the largest occurrence of each final category is never silently deleted.
    """
    result = group_map.copy()
    original = group_map.copy()
    inside = result >= 0
    kernel = np.ones((3, 3), np.uint8)
    level = max(1, min(5, int(level)))
    max_change_share = {1: 0.08, 2: 0.12, 3: 0.16, 4: 0.22, 5: 0.28}[level]
    original_area = {int(gid): int(np.count_nonzero(original == gid))
                     for gid in np.unique(original[original >= 0])}
    budget = {gid: max(1, int(area * max_change_share))
              for gid, area in original_area.items()}
    gained = {gid: 0 for gid in original_area}
    lost = {gid: 0 for gid in original_area}
    retained_for_budget = 0

    if base_min_mm is None:
        base_min_mm = (float(np.sqrt(max(float(min_area_px or 1.0), 1.0)))
                       * mm_per_px)
    patterns = group_patterns or {gid: "plain" for gid in original_area}
    labels = group_labels or {}
    minima = {
        gid: tactile_minimum_for_pattern(patterns.get(gid, "plain"), base_min_mm)
        for gid in original_area
    }
    for minimum in minima.values():
        minimum["min_area_px"] = int(round(
            minimum["min_area_mm2"] / max(mm_per_px * mm_per_px, 1e-9)))

    def component_metrics(piece: np.ndarray, gid: int) -> dict:
        area_px = int(np.count_nonzero(piece))
        distance = cv2.distanceTransform(piece.astype(np.uint8), cv2.DIST_L2, 5)
        usable_width_mm = float(distance.max()) * 2.0 * mm_per_px
        minimum = minima[gid]
        return {
            "area_px": area_px,
            "area_mm2": area_px * mm_per_px * mm_per_px,
            "usable_width_mm": usable_width_mm,
            "fails_area": area_px < minimum["min_area_px"],
            "fails_width": usable_width_mm < minimum["min_width_mm"],
        }

    merged = 0
    merge_records: list[dict] = []

    def semantic_family(gid: int) -> str:
        label = labels.get(gid, "").lower()
        if any(word in label for word in ("water", "sea", "lake", "ocean")):
            return "water"
        if any(word in label for word in ("forest", "wood")):
            return "forest"
        if any(word in label for word in ("specialty", "vine", "olive", "garden")):
            return "specialty"
        if any(word in label for word in ("field", "crop", "cereal")):
            return "field"
        if any(word in label for word in ("grass", "pasture")):
            return "grass"
        return "other"

    def semantic_cost(source: int, target: int) -> int | None:
        """Directional guard against conspicuously false tactile expansion."""
        if not labels:
            return 0
        source_family, target_family = semantic_family(source), semantic_family(target)
        if "water" in (source_family, target_family):
            return None
        # A tiny forest may be omitted into surrounding land, but forest must
        # not expand over agricultural land merely to consume a small patch.
        if target_family == "forest" and source_family != "forest":
            return None
        pair = frozenset((source_family, target_family))
        costs = {
            frozenset(("field", "grass")): 0,
            frozenset(("field", "specialty")): 1,
            frozenset(("grass", "specialty")): 2,
            frozenset(("forest", "grass")): 3,
            frozenset(("forest", "field")): 4,
            frozenset(("forest", "specialty")): 4,
        }
        return costs.get(pair, 5)

    def merge_undersized(max_iterations: int) -> int:
        nonlocal retained_for_budget, merged, result
        pass_merges = 0
        for _ in range(max_iterations):
            protected: set[tuple[int, int]] = set()
            for gid in np.unique(result[result >= 0]):
                count, components, stats, _ = cv2.connectedComponentsWithStats(
                    (result == gid).astype(np.uint8), connectivity=8)
                if count <= 1:
                    continue
                component = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                y, x = np.argwhere(components == component)[0]
                protected.add((int(y), int(x)))

            candidates = []
            for gid in np.unique(result[result >= 0]):
                count, components, _, _ = cv2.connectedComponentsWithStats(
                    (result == gid).astype(np.uint8), connectivity=8)
                for component in range(1, count):
                    piece = components == component
                    metrics = component_metrics(piece, int(gid))
                    if not (metrics["fails_area"] or metrics["fails_width"]):
                        continue
                    if any(piece[y, x] for y, x in protected):
                        continue
                    candidates.append((metrics["area_px"], int(gid), piece, metrics))
            if not candidates:
                break

            changed = 0
            for _, source, piece_snapshot, metrics in sorted(
                    candidates, key=lambda item: item[0]):
                piece = piece_snapshot & (result == source)
                if not piece.any():
                    continue
                ring = ((cv2.dilate(piece.astype(np.uint8), kernel) > 0)
                        & ~piece & inside)
                neighbours, counts = np.unique(
                    result[ring & (result >= 0)], return_counts=True)
                valid = [(int(count), int(gid)) for gid, count in zip(neighbours, counts)
                         if int(gid) != source]
                if not valid:
                    continue
                area = int(np.count_nonzero(piece))
                if lost[source] + area > budget[source]:
                    retained_for_budget += 1
                    continue
                eligible_targets = []
                for shared, target in valid:
                    cost = semantic_cost(source, target)
                    if cost is None or gained[target] + area > budget[target]:
                        continue
                    eligible_targets.append((cost, -shared, target, shared))
                if not eligible_targets:
                    retained_for_budget += 1
                    continue
                _, _, target, shared_boundary = min(eligible_targets)
                result[piece] = target
                lost[source] += area
                gained[target] += area
                merged += 1
                pass_merges += 1
                changed += 1
                merge_records.append({
                    "source_group": source, "target_group": target,
                    "area_px": area,
                    "area_mm2": round(metrics["area_mm2"], 2),
                    "usable_width_mm": round(metrics["usable_width_mm"], 2),
                    "failed_area": metrics["fails_area"],
                    "failed_width": metrics["fails_width"],
                    "shared_boundary_px": shared_boundary,
                    "semantic_source_family": semantic_family(source),
                    "semantic_target_family": semantic_family(target),
                    "reason": "whole region below its assigned texture minimum",
                })
            if not changed:
                break
        return pass_merges

    pre_smoothing_merges = merge_undersized(8)

    # This second, stronger boundary pass is intentionally delayed until the
    # final approved groups exist. Internal boundaries of merged classes have
    # already vanished, so smoothing cannot make one source class consume its
    # newly aggregated semantic partner.
    sigma_mm = {1: 1.0, 2: 1.5, 3: 2.0, 4: 2.6, 5: 3.2}[level]
    sigma_px = max(0.6, sigma_mm / max(mm_per_px, 1e-6))
    gids = [int(gid) for gid in np.unique(result[result >= 0])]
    best = np.full(result.shape, -1.0, np.float32)
    winner = result.copy()
    for gid in gids:
        score = cv2.GaussianBlur((result == gid).astype(np.float32), (0, 0), sigma_px)
        update = inside & (score > best)
        winner[update] = gid
        best[update] = score[update]
    winner[~inside] = -1
    ys, xs = np.where(inside & (winner != result))
    order = np.argsort(-best[ys, xs], kind="stable")
    # Reserve part of every distortion budget for the post-smoothing removal
    # of tiny fragments. Otherwise innocuous boundary rounding can consume the
    # budget and leave sub-millimetre islands in the final tactile map.
    smoothing_lost_limit = {
        gid: lost[gid] + int((budget[gid] - lost[gid]) * 0.65) for gid in budget
    }
    smoothing_gain_limit = {
        gid: gained[gid] + int((budget[gid] - gained[gid]) * 0.65) for gid in budget
    }
    for position in order:
        y, x = int(ys[position]), int(xs[position])
        source, target = int(result[y, x]), int(winner[y, x])
        if (source == target or lost[source] >= smoothing_lost_limit[source]
                or gained[target] >= smoothing_gain_limit[target]
                or semantic_cost(source, target) is None):
            continue
        result[y, x] = target
        lost[source] += 1
        gained[target] += 1

    post_smoothing_merges = merge_undersized(8)
    remaining = []
    component_count = {}
    for gid in np.unique(result[result >= 0]):
        count, components, _, _ = cv2.connectedComponentsWithStats(
            (result == gid).astype(np.uint8), connectivity=8)
        component_count[int(gid)] = count - 1
        for component in range(1, count):
            metrics = component_metrics(components == component, int(gid))
            if metrics["fails_area"] or metrics["fails_width"]:
                remaining.append({
                    "group_id": int(gid),
                    "area_mm2": round(metrics["area_mm2"], 2),
                    "usable_width_mm": round(metrics["usable_width_mm"], 2),
                    "failed_area": metrics["fails_area"],
                    "failed_width": metrics["fails_width"],
                    "reason": "kept to preserve the category or distortion budget",
                })

    changed_pixels = int(np.count_nonzero(original != result))
    return result, {
        "method_version": 2,
        "method": "pattern_specific_area_and_width_plus_bounded_smoothing",
        "whole_components_merged": merged,
        "pre_smoothing_components_merged": pre_smoothing_merges,
        "post_smoothing_components_merged": post_smoothing_merges,
        "small_components_retained_for_area_budget": retained_for_budget,
        "remaining_below_pattern_minimum": len(remaining),
        "remaining_components": remaining,
        "component_count_by_group": component_count,
        "changed_pixels": changed_pixels,
        "changed_share": round(changed_pixels / max(1, int(np.count_nonzero(inside))), 4),
        "base_texture_minimum_mm": round(float(base_min_mm), 1),
        "pattern_minima": minima,
        "boundary_smoothing_mm": sigma_mm,
        "max_group_gain_loss_share": max_change_share,
        "per_group_gained_px": gained,
        "per_group_lost_px": lost,
        "merge_records": merge_records,
    }


def write_alt_overlay_labels(out_dir: Path) -> dict:
    from .labelreview import REVIEW_VERSION, labels_fingerprint

    label_map = imread(out_dir / "alt_label_map_gen.png")[..., 0]
    canvas = imread(out_dir / "alt_step7_tactile.png")
    summary = json.loads((out_dir / "alt_step6_summary.json").read_text(encoding="utf-8"))
    raw_path = out_dir / "labels.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8")) if raw_path.exists() else {"labels": []}
    approved_path = out_dir / "approved_labels.json"
    approved = (json.loads(approved_path.read_text(encoding="utf-8"))
                if approved_path.exists() else None)
    current = bool(
        approved
        and approved.get("review", {}).get("version") == REVIEW_VERSION
        and approved.get("review", {}).get("labels_fingerprint") == labels_fingerprint(raw)
    )
    labels = approved if current else raw
    result = build_overlay_labels(
        labels,
        source_shape=label_map.shape[:2],
        canvas_shape=canvas.shape[:2],
        mm_per_px=float(summary["scale_mm_per_px"]),
    )
    result["review_source"] = approved_path.name if current else raw_path.name
    result["coordinate_contract"]["source_space"] = "map_area.png / alt_label_map_gen.png pixels"
    result["coordinate_contract"]["tactile_space"] = (
        "alt_step7_tactile.png pixels (alternate map-only canvas)")
    (out_dir / "alt_overlay_labels.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def run_alt_step7(image_path: Path, model: str | None = None,
                  runs_dir: Path = Path("runs")) -> dict:
    from .alt_aggregate import effective_aggregation

    out_dir = runs_dir / image_path.stem
    required = (out_dir / "alt_aggregation.json", out_dir / "alt_label_map_gen.png",
                out_dir / "alt_groups.json", out_dir / "alt_step6_summary.json")
    if not all(path.exists() for path in required):
        raise FileNotFoundError("run Alt Step 6 manually before Alt Step 7")

    spec = OutputSpec.load_or_create()
    sem = MapSemantics.model_validate_json(
        (out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    aggregation = json.loads((out_dir / "alt_aggregation.json").read_text(
        encoding="utf-8"))
    aggregation = effective_aggregation(out_dir, aggregation)
    classes = json.loads((out_dir / "alt_classes_gen.json").read_text(
        encoding="utf-8"))["classes"]
    final_groups = json.loads((out_dir / "alt_groups.json").read_text(
        encoding="utf-8"))["groups"]
    summary = json.loads((out_dir / "alt_step6_summary.json").read_text(
        encoding="utf-8"))
    lines = [feature["properties"] | {"points": feature["geometry"]["coordinates"]}
             for feature in json.loads((out_dir / "alt_lines_gen.geojson").read_text(
                 encoding="utf-8"))["features"]]
    label_map = imread(out_dir / "alt_label_map_gen.png")[..., 0].astype(np.int16) - 1

    notes = list(aggregation.get("notes", []))
    thematic_groups = [dict(group) for group in final_groups if group.get("is_thematic")]
    water_members = set((aggregation.get("water") or {}).get("members", []))
    water_group = next((group for group in final_groups
                        if water_members and water_members == set(group["members"])), None)
    assignments: list[dict] = []
    group_patterns: dict[int, str] = {}
    pattern_candidates: dict[int, tuple[str, ...]] = {}

    if water_group:
        group_id = int(water_group["group_id"])
        group_patterns[group_id] = "04_waves_sine"
        pattern_candidates[group_id] = ("04_waves_sine",)
        assignments.append({
            "group_id": group_id,
            "label": water_group["label"], "members": water_group["members"],
            "pattern": "04_waves_sine",
            "pattern_desc": PATTERNS["04_waves_sine"]["desc"],
            "rationale": "water always gets the wavy pattern", "is_thematic": False,
        })

    if thematic_groups and sem.data_ordering.value == "ordered":
        ramp = ORDERED_RAMPS[min(len(thematic_groups), 5)]
        for group, pattern in zip(thematic_groups, ramp):
            group["pattern"] = pattern
            group["texture_rationale"] = "perceived-order texture ramp (ordered data)"
    elif thematic_groups:
        available = ["dots", "lines", "grids", "solids"] + ([] if water_group else ["waves"])
        assign_alt_qualitative(thematic_groups, available)
        for group in thematic_groups:
            texture_group = group["texture_group"]
            candidates = (("04_waves_triangle",) if texture_group == "waves"
                          else tuple(GROUPS[texture_group]))
            group["pattern_candidates"] = candidates
            group["pattern"] = candidates[0]

    for group in thematic_groups:
        group_id = int(group["group_id"])
        pattern = group["pattern"]
        group_patterns[group_id] = pattern
        pattern_candidates[group_id] = tuple(
            group.get("pattern_candidates", (pattern,))
        )
        assignments.append({
            "group_id": group_id,
            "label": group["label"], "members": group["members"],
            "pattern": pattern, "pattern_desc": PATTERNS[pattern]["desc"],
            "rationale": group.get("texture_rationale", group.get("rationale", "")),
            "is_thematic": True,
        })

    extras = [group for group in final_groups
              if not group.get("is_thematic") and group is not water_group]
    for extra in extras:
        # A maximum is not a target. Non-thematic extras remain bounded but do
        # not receive textures merely because capacity happens to be unused.
        pattern = "plain"
        rationale = "non-thematic extra kept plain; unused texture capacity stays unused"
        group_id = int(extra["group_id"])
        group_patterns[group_id] = pattern
        pattern_candidates[group_id] = (pattern,)
        assignments.append({
            "group_id": group_id,
            "label": extra["label"], "members": extra["members"],
            "pattern": pattern, "pattern_desc": PATTERNS[pattern]["desc"],
            "rationale": rationale, "is_thematic": False,
        })

    group_map = label_map
    group_patterns, pattern_optimization = optimize_adjacent_pattern_variants(
        group_map, pattern_candidates,
    )
    assignments_by_group = {
        int(assignment["group_id"]): assignment for assignment in assignments
    }
    for group_id, chosen_pattern in group_patterns.items():
        assignment = assignments_by_group[group_id]
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

    # Alt Step 7 deliberately performs no geographic generalization.  It audits
    # texture footprints, then renders Alt Step 6's exact indexed group raster.
    remaining_components = []
    pattern_minima = {}
    for assignment in assignments:
        gid = int(assignment["group_id"])
        minimum = tactile_minimum_for_pattern(
            assignment["pattern"], float(spec.constants.min_texture_area_side_mm))
        minimum["min_area_px"] = int(round(
            minimum["min_area_mm2"] /
            max(float(summary["scale_mm_per_px"]) ** 2, 1e-9)))
        pattern_minima[gid] = minimum
        count, components, _, _ = cv2.connectedComponentsWithStats(
            (group_map == gid).astype(np.uint8), connectivity=8)
        for component in range(1, count):
            piece = components == component
            area_px = int(np.count_nonzero(piece))
            distance = cv2.distanceTransform(piece.astype(np.uint8), cv2.DIST_L2, 5)
            width_mm = float(distance.max()) * 2.0 * float(summary["scale_mm_per_px"])
            if area_px < minimum["min_area_px"] or width_mm < minimum["min_width_mm"]:
                remaining_components.append({
                    "group_id": gid, "area_px": area_px,
                    "usable_width_mm": round(width_mm, 2),
                    "failed_area": area_px < minimum["min_area_px"],
                    "failed_width": width_mm < minimum["min_width_mm"],
                    "reason": "reported only; Step 7 does not alter Alt Step 6 geometry",
                })
    geometry_audit = {
        "method": "direct_render_of_alt_step6_group_raster",
        "geography_changed_in_step7": False,
        "whole_components_merged": 0,
        "remaining_below_pattern_minimum": len(remaining_components),
        "remaining_components": remaining_components,
        "pattern_minima": pattern_minima,
        "changed_pixels": 0, "changed_share": 0.0,
        "boundary_smoothing_mm": summary.get("smoothing_mm", 0),
        "alt_step6_detail_level": int(summary.get("params", {}).get(
            "simplification_level", 3)),
    }

    for assignment in assignments:
        minimum = geometry_audit["pattern_minima"].get(assignment["group_id"], {})
        assignment["minimum_width_mm"] = minimum.get("min_width_mm")
        assignment["minimum_area_mm2"] = minimum.get("min_area_mm2")
        assignment["remaining_subminimum_regions"] = sum(
            1 for item in geometry_audit["remaining_components"]
            if item["group_id"] == assignment["group_id"])

    remaining_count = geometry_audit["remaining_below_pattern_minimum"]
    if remaining_count:
        notes.append(
            f"{remaining_count} final regions are below their assigned pattern's "
            "recommended footprint. They are reported without changing the approved "
            "Alt Step 6 geometry.")

    line_styles = {kind: LINE_STYLES.get(kind, LINE_STYLES["line"])
                   for kind in {line["kind"] for line in lines}}
    (out_dir / "alt_symbols.json").write_text(json.dumps({
        "branch": "alternate",
        "aggregation_review_status": aggregation.get("review_status", "not_required"),
        "texture_count": len({assignment["pattern"] for assignment in assignments
                              if assignment["pattern"] != "plain"}),
        "texture_ceiling": spec.constants.max_area_textures,
        "texture_ceiling_is_target": False,
        "map_size_mm": summary["map_size_mm"],
        "source_mm_per_px": summary["scale_mm_per_px"],
        "area_assignments": assignments,
        "pattern_optimization": pattern_optimization,
        "line_styles": line_styles,
        "render_px_per_mm": RENDER_PX_PER_MM,
        "generalization_summary": {
            "whole_regions_merged": summary.get("dissolved_components", 0),
            "remaining_below_pattern_minimum": remaining_count,
            "changed_share": summary.get("changed_share", 0),
            "boundary_smoothing_mm": geometry_audit["boundary_smoothing_mm"],
        },
        "notes": notes,
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    # Exact final grouped geometry, before hatching, for a comprehensible
    # visual audit beside the tactile render.
    region_preview = np.full((*group_map.shape, 3), 255, np.uint8)
    for assignment in assignments:
        group = next(item for item in final_groups
                     if int(item["group_id"]) == int(assignment["group_id"]))
        rgb = np.asarray(group.get("rgb", [180, 180, 180]), np.uint8)
        region_preview[group_map == assignment["group_id"]] = rgb[::-1]
    imwrite(out_dir / "alt_step7_regions_preview.png", region_preview)
    imwrite(out_dir / "alt_group_map_tactile.png", (group_map + 1).astype(np.uint8))
    identity_groups = {gid: gid for gid in group_patterns}
    canvas = render_tactile(group_map, identity_groups, group_patterns, lines,
                            summary["scale_mm_per_px"], spec)
    imwrite(out_dir / "alt_step7_tactile.png", canvas)
    (out_dir / "alt_step7_generalization.json").write_text(
        json.dumps(geometry_audit, indent=2), encoding="utf-8")
    overlay_labels = write_alt_overlay_labels(out_dir)

    reconstruction = cv2.resize(
        region_preview, (canvas.shape[1], canvas.shape[0]), interpolation=cv2.INTER_NEAREST)
    debug = np.hstack([reconstruction, cv2.cvtColor(canvas, cv2.COLOR_GRAY2BGR)])
    if debug.shape[1] > 2200:
        factor = 2200 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=factor, fy=factor,
                           interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "alt_step7_debug.png", debug)

    comparison_path = None
    canonical_path = out_dir / "step7_tactile.png"
    if canonical_path.exists():
        canonical = imread(canonical_path)
        if canonical.ndim == 3:
            canonical = canonical[..., 0]
        if canonical.shape != canvas.shape:
            canonical = cv2.resize(canonical, (canvas.shape[1], canvas.shape[0]),
                                   interpolation=cv2.INTER_NEAREST)
        comparison = np.hstack([canonical, canvas])
        imwrite(out_dir / "step7_comparison.png", comparison)
        comparison_path = out_dir / "step7_comparison.png"

    return {
        "out_dir": out_dir, "assignments": assignments, "notes": notes,
        "canvas_px": [canvas.shape[1], canvas.shape[0]],
        "overlay_labels": len(overlay_labels["labels"]),
        "comparison": comparison_path,
    }
