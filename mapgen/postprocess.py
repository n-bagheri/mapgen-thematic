"""Canonical Steps 5-6: aggregate Step 4, then simplify the groups.

Step 5 changes only category identities in the untouched Step 4 raster.
Once those groups are approved, Step 6 passes that aggregated raster
through the same physical area-generalization operations used by the former Step 5.

"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np

from .aggregate import (aggregation_fingerprint, effective_aggregation,
                        load_aggregation_review, rebin_ordered)
from .aggregation_policy import propose_complete_aggregation
from .generalize import (LINE_JOIN_FAR_MM, LINE_JOIN_NEAR_MM,
                         LINE_POLICY_VERSION, SIMPLIFICATION_PRESETS, SIMPLIFY_MM,
                         compute_scale,
                         drop_redundant_boundary_lines, generalize_area_raster,
                         line_length, merge_lines, preset_params,
                         semantic_default_line_kinds, simplify_line)
from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .segment import polygonize
from .semantics import MapSemantics, load_pipeline_semantics, require_pipeline_eligible


STEP6_METHOD_VERSION = 4
STEP6_DEFAULT_PARAMS = {
    "method_version": STEP6_METHOD_VERSION,
    "simplification_level": 3,
    "min_texture_area_side_mm": None,
    "smooth_mm": None,
    "preserve_share": None,
    "keep_line_kinds": [],
    "line_policy_version": LINE_POLICY_VERSION,
    "protected_classes": [],
}

STEP6_PRESET_ARTIFACTS = (
    "label_map_gen.png",
    "label_map_gen_preview.png",
    "classes_gen.json",
    "regions_gen.geojson",
    "lines_gen.geojson",
    "step6_summary.json",
    "step6_debug.png",
    "step6_changes.png",
    "step6_transitions.json",
)

def _source_digest(label_map: np.ndarray, classes: list[dict]) -> str:
    digest = hashlib.sha256()
    digest.update(label_map.tobytes())
    digest.update(json.dumps(
        [{"index": item["index"], "label": item["label"]} for item in classes],
        sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return digest.hexdigest()


def _is_water(cl: dict) -> bool:
    return (cl.get("source") == "water-heuristic"
            or (not cl.get("is_thematic")
                and "water" in cl.get("label", "").lower()))


def is_white_background_water(water_classes: list[dict]) -> bool:
    """True when detected water is the unprinted white page background.

    White sea around an island/coastal map establishes the coastline but is not
    a thematic water area.  It must not consume a texture slot or receive the
    default sine-wave fill used for visibly coloured water.
    """
    if not water_classes:
        return False
    colours = [cl.get("rgb") for cl in water_classes]
    if any(not isinstance(rgb, list) or len(rgb) < 3 for rgb in colours):
        return False
    return all(min(int(value) for value in rgb[:3]) >= 235
               and max(int(value) for value in rgb[:3]) - min(int(value) for value in rgb[:3]) <= 25
               for rgb in colours)


def _mean_rgb(members: list[int], by_index: dict[int, dict]) -> list[int]:
    colours = [by_index[index].get("rgb", [180, 180, 180])
               for index in members if index in by_index]
    if not colours:
        return [180, 180, 180]
    return np.mean(np.asarray(colours, np.float32), axis=0).round().astype(int).tolist()


def build_group_definitions(aggregation: dict, classes: list[dict]) -> list[dict]:
    """Give every surviving Step 4 class one deterministic final group id."""
    by_index = {int(cl["index"]): cl for cl in classes}
    definitions: list[dict] = []
    covered: set[int] = set()

    def add(label: str, members: list[int], thematic: bool, rationale: str) -> None:
        clean = [int(index) for index in members if int(index) in by_index]
        if not clean:
            return
        definitions.append({
            "group_id": len(definitions),
            "label": label,
            "members": clean,
            "member_labels": [by_index[index]["label"] for index in clean],
            "rgb": _mean_rgb(clean, by_index),
            "is_thematic": thematic,
            "rationale": rationale,
        })
        covered.update(clean)

    water = aggregation.get("water")
    if water:
        add(water["label"], water["members"], False, "water kept as one final group")
    for group in aggregation.get("groups", []):
        add(group["label"], group["members"], True,
            group.get("rationale", "reviewed thematic aggregation"))
    for group in aggregation.get("plain_thematic", []):
        add(group["label"], group["members"], True,
            group.get("rationale", "thematic group kept plain"))
    # Pixels Step 4 could not associate with a legend class are background,
    # not additional map categories. Keep them in one raster group so they
    # remain a single no-fill area through generalization, rather than making
    # artificial regions or consuming texture/legend capacity downstream.
    background_members = [int(extra["index"])
                          for extra in aggregation.get("non_thematic_extra", [])]
    if background_members:
        add("background / no fill", background_members, False,
            "non-thematic unlabelled pixels kept as one no-fill background")
    for cl in sorted(classes, key=lambda item: int(item["index"])):
        index = int(cl["index"])
        if cl.get("area_px", 0) > 0 and index not in covered:
            add(cl["label"], [index], bool(cl.get("is_thematic")),
                "uncovered Step 4 class kept separate")
    return definitions


def group_raster(source: np.ndarray, definitions: list[dict]) -> tuple[np.ndarray, dict[int, int]]:
    result = np.full(source.shape, -1, np.int16)
    source_to_group: dict[int, int] = {}
    for group in definitions:
        gid = int(group["group_id"])
        for index in group["members"]:
            source_to_group[int(index)] = gid
            result[source == int(index)] = gid
    return result, source_to_group


def _render_groups(group_map: np.ndarray, definitions: list[dict]) -> np.ndarray:
    preview = np.full((*group_map.shape, 3), 255, np.uint8)
    for group in definitions:
        preview[group_map == int(group["group_id"])] = np.uint8(group["rgb"][::-1])
    return preview


def materialize_step5_output(out_dir: Path, aggregation: dict | None = None) -> dict:
    """Write the approved, unsimplified grouped Step 4 raster.

    Adjacent source categories assigned to one final group receive the same
    integer id, so their internal border ceases to exist from this point on.
    Disconnected pieces keep the same category id but remain separate pieces
    of geography until Step 6 processes them.
    """
    aggregation_path = out_dir / "aggregation.json"
    if aggregation is None:
        if not aggregation_path.exists():
            raise FileNotFoundError("run Step 5 before materializing its output")
        aggregation = json.loads(aggregation_path.read_text(encoding="utf-8"))
    effective = effective_aggregation(out_dir, aggregation)
    classes = json.loads((out_dir / "classes_final.json").read_text(
        encoding="utf-8"))["classes"]
    source = imread(out_dir / "label_map.png")[..., 0].astype(np.int16) - 1
    if _source_digest(source, classes) != effective.get("source_digest"):
        raise RuntimeError("Step 4 changed after Step 5; rerun Step 5")

    definitions = build_group_definitions(effective, classes)
    grouped, source_to_group = group_raster(source, definitions)
    if np.any((source >= 0) & (grouped < 0)):
        raise RuntimeError("approved aggregation does not cover every Step 4 class")

    imwrite(out_dir / "group_map_source.png", (grouped + 1).astype(np.uint8))
    imwrite(out_dir / "step5_aggregation_preview.png",
            _render_groups(grouped, definitions))
    (out_dir / "groups.json").write_text(
        json.dumps({"groups": definitions}, indent=2, ensure_ascii=False),
        encoding="utf-8")
    audit = {
        "source_digest": effective["source_digest"],
        "source_raster": "label_map.png",
        "source_classes": "classes_final.json",
        "output_raster": "group_map_source.png",
        "output_groups": "groups.json",
        "source_pixel_count": int(np.count_nonzero(source >= 0)),
        "geographic_pixels_changed": 0,
        "operation": "approved category-id lookup; no pixel moved or erased",
        "source_to_approved_group": source_to_group,
        "review_status": effective.get("review_status", "not_required"),
    }
    (out_dir / "step5_source_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"groups": definitions, "grouped": grouped, "audit": audit}


def run_step5(image_path: Path, model: str | None = None,
                  runs_dir: Path = Path("runs")) -> dict:
    """Propose aggregation from all Step 4 classes without simplifying them."""
    out_dir = runs_dir / image_path.stem
    required = (out_dir / "label_map.png", out_dir / "classes_final.json",
                out_dir / "step1_semantics.json")
    if not all(path.exists() for path in required):
        raise FileNotFoundError("run canonical Steps 1-4 before Step 5")

    spec = OutputSpec.load_or_create()
    sem = MapSemantics.model_validate_json(
        (out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    require_pipeline_eligible(sem, "Step 5")
    classes = json.loads((out_dir / "classes_final.json").read_text(
        encoding="utf-8"))["classes"]
    source = imread(out_dir / "label_map.png")[..., 0].astype(np.int16) - 1
    surviving = [cl for cl in classes if cl.get("area_px", 0) > 0]
    water = [cl for cl in surviving if _is_water(cl)]
    water_is_background = is_white_background_water(water)
    textured_water = bool(water) and not water_is_background
    slots = spec.texture_slots(water_present=textured_water)
    thematic = sorted((cl for cl in surviving if cl.get("is_thematic")),
                      key=lambda cl: int(cl["index"]))
    extras = [cl for cl in surviving if not cl.get("is_thematic") and cl not in water]

    merge_log: list[dict] = []
    if len(thematic) <= slots:
        mode = "identity"
        groups = [{
            "label": cl["label"], "members": [int(cl["index"])],
            "member_labels": [cl["label"]], "rationale": "kept as its original class",
        } for cl in thematic]
    elif sem.data_ordering.value == "ordered":
        mode = "ordered_rebin_proposal"
        groups = rebin_ordered(thematic, slots)
        for group in groups:
            group.pop("area", None)
    else:
        mode = "semantic_merge_proposal"
        groups, merge_log = propose_complete_aggregation(
            thematic, slots, {"reviewed": False, "pairs": []})

    review_required = any(len(group["members"]) > 1 for group in groups)
    aggregation = {
        "branch": "canonical",
        "stage": "aggregate_before_simplification",
        "source_artifacts": ["label_map.png", "classes_final.json"],
        "source_digest": _source_digest(source, classes),
        "mode": mode,
        "slots": slots,
        "texture_ceiling": spec.constants.max_area_textures,
        "texture_ceiling_is_target": False,
        "proposed_texture_count": len(groups) + (1 if textured_water else 0),
        "unused_texture_capacity": max(
            0, spec.constants.max_area_textures - len(groups) - (1 if textured_water else 0)),
        "review_required": review_required,
        "review_status": "needs_review" if review_required else "not_required",
        "water": ({"label": water[0]["label"],
                   "members": [int(cl["index"]) for cl in water],
                   "is_background": water_is_background} if water else None),
        "groups": groups,
        "plain_thematic": [],
        "source_classes": [{"index": int(cl["index"]), "label": cl["label"]}
                           for cl in thematic],
        "non_thematic_extra": [{
            "index": int(cl["index"]), "label": cl["label"],
            "priority": cl.get("priority"),
        } for cl in extras],
        "merge_log": merge_log,
        "notes": [],
    }
    aggregation["proposal_fingerprint"] = aggregation_fingerprint(aggregation)
    review = load_aggregation_review(out_dir, aggregation)
    if review:
        aggregation["review_status"] = review["status"]
    (out_dir / "aggregation.json").write_text(
        json.dumps(aggregation, indent=2, ensure_ascii=False), encoding="utf-8")

    definitions = build_group_definitions(aggregation, classes)
    proposed, source_to_group = group_raster(source, definitions)
    imwrite(out_dir / "step5_aggregation_preview.png",
            _render_groups(proposed, definitions))
    audit = {
        "source_digest": aggregation["source_digest"],
        "source_raster": "label_map.png",
        "source_classes": "classes_final.json",
        "source_pixel_count": int(np.count_nonzero(source >= 0)),
        "geographic_pixels_changed": 0,
        "operation": "identity-preserving lookup from source class to proposed group",
        "source_to_proposed_group": source_to_group,
    }
    (out_dir / "step5_source_audit.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    # Identity proposals and previously approved reviews are immediately valid
    # Step 5 outputs. New multi-class proposals remain previews until the
    # user approves them in the review UI.
    if not review_required or (review and review.get("approved")):
        materialize_step5_output(out_dir, aggregation)
    return {"out_dir": out_dir, "aggregation": aggregation, "audit": audit}


def load_step6_params(out_dir: Path) -> dict:
    params = dict(STEP6_DEFAULT_PARAMS)
    path = out_dir / "step6_params.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("method_version") == STEP6_METHOD_VERSION:
            params.update(saved)
    else:
        params["keep_line_kinds"] = semantic_default_line_kinds(out_dir)
    return params


def step6_preset_params(level: int, base: dict | None = None) -> dict:
    """Record the chosen canonical preset; do not duplicate its settings."""
    level = max(1, min(5, int(level)))
    params = dict(STEP6_DEFAULT_PARAMS)
    if base:
        params.update(base)
    params.update({
        "method_version": STEP6_METHOD_VERSION,
        "simplification_level": level,
        "min_texture_area_side_mm": None,
        "smooth_mm": None,
        "preserve_share": None,
    })
    return params


def step6_preset_artifact_name(level: int, canonical_name: str) -> str:
    return f"step6_preset_{int(level)}_{canonical_name}"


def _cache_step6_preset(out_dir: Path, level: int) -> None:
    for name in STEP6_PRESET_ARTIFACTS:
        shutil.copy2(out_dir / name, out_dir / step6_preset_artifact_name(level, name))


def activate_step6_preset(out_dir: Path, level: int) -> dict:
    level = max(1, min(5, int(level)))
    missing = [name for name in STEP6_PRESET_ARTIFACTS
               if not (out_dir / step6_preset_artifact_name(level, name)).exists()]
    if missing:
        raise FileNotFoundError(f"Step 6 preset {level} is not ready")
    for name in STEP6_PRESET_ARTIFACTS:
        shutil.copy2(out_dir / step6_preset_artifact_name(level, name), out_dir / name)
    summary = json.loads((out_dir / "step6_summary.json").read_text(encoding="utf-8"))
    (out_dir / "step6_params.json").write_text(
        json.dumps(summary["params"], indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def _group_area_audit(before: np.ndarray, after: np.ndarray,
                      definitions: list[dict]) -> dict:
    rows = []
    largest_change_share = 0.0
    for group in definitions:
        gid = int(group["group_id"])
        area_before = int(np.count_nonzero(before == gid))
        area_after = int(np.count_nonzero(after == gid))
        gained = int(np.count_nonzero((before != gid) & (after == gid)))
        lost = int(np.count_nonzero((before == gid) & (after != gid)))
        gain_share = gained / max(1, area_before)
        loss_share = lost / max(1, area_before)
        largest_change_share = max(largest_change_share, gain_share, loss_share)
        rows.append({
            "group_id": gid, "label": group["label"],
            "area_before_px": area_before, "area_after_px": area_after,
            "gained_px": gained, "lost_px": lost,
            "gain_share": round(gain_share, 4),
            "loss_share": round(loss_share, 4),
            "net_area_change_px": area_after - area_before,
            "net_area_change_share": round(
                (area_after - area_before) / max(1, area_before), 4),
        })
    return {
        "per_group": rows,
        "largest_gain_or_loss_share": round(largest_change_share, 4),
    }


def _generalize_lines(lines_in: list[dict], label_map: np.ndarray,
                      mm_per_px: float, params: dict, constants) -> tuple[list[dict], dict]:
    """Run the shared physical line operations against the grouped raster."""
    keep_kinds = set(params["keep_line_kinds"])
    kept = [feature["properties"] | {"points": feature["geometry"]["coordinates"]}
            for feature in lines_in
            if feature["properties"]["kind"] != "frame"
            and feature["properties"]["kind"] in keep_kinds]
    kept, redundant = drop_redundant_boundary_lines(kept, label_map)
    merged, joins = merge_lines(
        kept, near_px=LINE_JOIN_NEAR_MM / mm_per_px,
        far_px=LINE_JOIN_FAR_MM / mm_per_px)
    eps_px = max(1.0, SIMPLIFY_MM / mm_per_px)
    min_line_px = constants.min_line_length_mm / mm_per_px
    features = []
    dropped_short = 0
    for line in merged:
        points = simplify_line(line["pts"], eps_px)
        length = line_length(points)
        if length < min_line_px:
            dropped_short += 1
            continue
        features.append({
            "type": "Feature",
            "properties": {"kind": line["kind"],
                           "length_mm": round(length * mm_per_px, 1)},
            "geometry": {"type": "LineString",
                         "coordinates": [[round(float(x), 1), round(float(y), 1)]
                                         for x, y in points]},
        })
    return features, {
        "line_joins": joins,
        "lines_dropped_short": dropped_short,
        "lines_dropped_redundant": redundant,
    }


def _source_to_final_transition(source: np.ndarray, before_groups: np.ndarray,
                                final_groups: np.ndarray, classes: list[dict],
                                definitions: list[dict], source_to_group: dict[int, int]) -> dict:
    group_labels = {int(group["group_id"]): group["label"] for group in definitions}
    rows = []
    geographic_reassignment = 0
    for cl in classes:
        index = int(cl["index"])
        pixels = source == index
        count = int(np.count_nonzero(pixels))
        if not count:
            continue
        intended = source_to_group.get(index)
        values, counts = np.unique(final_groups[pixels], return_counts=True)
        destinations = [{
            "group_id": int(gid),
            "group_label": group_labels.get(int(gid), "outside"),
            "pixels": int(number),
            "share_of_source_class": round(int(number) / count, 4),
        } for gid, number in zip(values, counts)]
        moved = int(np.count_nonzero(final_groups[pixels] != intended))
        geographic_reassignment += moved
        rows.append({
            "source_index": index, "source_label": cl["label"],
            "source_pixels": count, "intended_group_id": intended,
            "intended_group_label": group_labels.get(intended, "unassigned"),
            "geographically_reassigned_px": moved,
            "destinations": destinations,
        })
    inside = int(np.count_nonzero(before_groups >= 0))
    return {
        "lineage": "Step 4 source class -> approved final group -> simplified final group",
        "source_raster": "label_map.png",
        "approved_unsimplified_group_raster": "group_map_source.png",
        "simplified_group_raster": "label_map_gen.png",
        "source_to_final_groups": rows,
        "geographically_reassigned_pixels": geographic_reassignment,
        "geographically_reassigned_share": round(geographic_reassignment / max(1, inside), 4),
    }


def run_step6(image_path: Path, model: str | None = None,
                  runs_dir: Path = Path("runs"), params_override: dict | None = None) -> dict:
    """Simplify the approved Step 5 grouped raster for touch."""
    out_dir = runs_dir / image_path.stem
    aggregation_path = out_dir / "aggregation.json"
    if not aggregation_path.exists():
        raise FileNotFoundError("run Step 5 manually before Step 6")
    load_pipeline_semantics(out_dir, "Step 6")
    aggregation = effective_aggregation(
        out_dir, json.loads(aggregation_path.read_text(encoding="utf-8")))
    required = (out_dir / "group_map_source.png", out_dir / "groups.json",
                out_dir / "step5_source_audit.json", out_dir / "lines.geojson")
    if not all(path.exists() for path in required):
        raise FileNotFoundError(
            "approve Step 5 so its aggregated Step 4 raster exists before Step 6")
    classes = json.loads((out_dir / "classes_final.json").read_text(
        encoding="utf-8"))["classes"]
    source = imread(out_dir / "label_map.png")[..., 0].astype(np.int16) - 1
    source_digest = _source_digest(source, classes)
    if source_digest != aggregation.get("source_digest"):
        raise RuntimeError("Step 4 changed after Step 5; rerun Step 5")

    definitions = json.loads((out_dir / "groups.json").read_text(
        encoding="utf-8"))["groups"]
    expected_definitions = build_group_definitions(aggregation, classes)
    if definitions != expected_definitions:
        raise RuntimeError("Step 5 review changed; save it again before Step 6")
    original = imread(out_dir / "group_map_source.png")[..., 0].astype(np.int16) - 1
    expected_original, source_to_group = group_raster(source, definitions)
    if not np.array_equal(original, expected_original):
        raise RuntimeError("Step 5 aggregated raster is stale; rerun Step 5")

    requested = (load_step6_params(out_dir) if params_override is None
                 else dict(params_override))
    level = max(1, min(5, int(requested.get("simplification_level", 3))))
    spec = OutputSpec.load_or_create()
    height, width = original.shape
    scale = compute_scale(spec, width, height)
    mm_per_px = scale["mm_per_px"]
    canonical_base = {}
    canonical_params_path = out_dir / "step6_params.json"
    if canonical_params_path.exists():
        canonical_base = json.loads(canonical_params_path.read_text(encoding="utf-8"))
    generalization_params = preset_params(spec, level, canonical_base)
    min_area_side_mm = (generalization_params["min_texture_area_side_mm"]
                        or spec.constants.min_texture_area_side_mm)
    min_area_px = (min_area_side_mm / mm_per_px) ** 2
    sigma = float(np.clip(generalization_params["smooth_mm"] / mm_per_px, 1.0, 6.0))

    total_before = max(1, int(np.count_nonzero(original >= 0)))
    group_input_classes = []
    for group in definitions:
        gid = int(group["group_id"])
        area = int(np.count_nonzero(original == gid))
        group_input_classes.append({
            "index": gid, "label": group["label"], "rgb": group["rgb"],
            "is_thematic": group["is_thematic"], "area_px": area,
            "area_share": round(area / total_before, 6),
        })
    protected_source = {int(index) for index in
                        generalization_params.get("protected_classes", [])}
    protected_groups = {int(group["group_id"]) for group in definitions
                        if protected_source.intersection(int(i) for i in group["members"])}
    label_map, mask, operation = generalize_area_raster(
        original, group_input_classes, min_area_px, sigma,
        preserve_share=generalization_params["preserve_share"],
        protected_classes=protected_groups)

    lines_in = json.loads((out_dir / "lines.geojson").read_text(
        encoding="utf-8"))["features"]
    line_features, line_stats = _generalize_lines(
        lines_in, label_map, mm_per_px, generalization_params, spec.constants)
    params = dict(generalization_params)
    params["method_version"] = STEP6_METHOD_VERSION
    params["input_raster"] = "group_map_source.png"
    params["algorithm"] = "canonical_step5_shared_area_generalization"
    area_audit = _group_area_audit(original, label_map, definitions)

    total = max(1, int(np.count_nonzero(label_map >= 0)))
    group_classes = []
    for group in definitions:
        gid = int(group["group_id"])
        before = int(np.count_nonzero(original == gid))
        after = int(np.count_nonzero(label_map == gid))
        group_classes.append({
            "index": gid, "label": group["label"], "rgb": group["rgb"],
            "is_thematic": group["is_thematic"], "source": "approved-aggregation",
            "priority": None, "members": group["members"],
            "member_labels": group["member_labels"],
            "area_px": after, "area_share_before": round(before / total, 4),
            "area_share": round(after / total, 4),
        })

    # Polygonize the already canonical raster. This does not alter its pixels;
    # it only supplies the vector boundary representation needed by the render.
    eps_px = max(1.0, SIMPLIFY_MM / mm_per_px)
    regions = []
    for cl in group_classes:
        if cl["area_px"]:
            regions += polygonize(label_map, cl["index"], cl["label"], eps_px)

    transition = _source_to_final_transition(
        source, original, label_map, classes, definitions, source_to_group)
    changed = original != label_map
    summary = {
        "branch": "canonical",
        "role": "approved_step4_aggregation_then_canonical_step5_simplification",
        "area_method": "canonical_step5_shared_area_generalization_on_approved_groups",
        "source_step": 4,
        "input_raster": "group_map_source.png",
        "aggregation_review_status": aggregation.get("review_status", "not_required"),
        "scale_mm_per_px": round(mm_per_px, 4),
        "orientation": scale["orientation"], "map_size_mm": scale["map_size_mm"],
        "page_mm": [spec.page_width_mm, spec.page_height_mm],
        "min_texture_area_px": round(min_area_px),
        "smoothing_mm": float(generalization_params["smooth_mm"]),
        "smoothing_sigma_px": round(sigma, 2),
        "dissolved_components": operation["dissolved_components"],
        "islands": operation["islands"],
        "groups_restored": operation["classes_restored"],
        "changed_pixels": int(np.count_nonzero(changed)),
        "changed_share": round(int(np.count_nonzero(changed)) /
                               max(1, int(np.count_nonzero(original >= 0))), 4),
        "per_group_area_audit": area_audit["per_group"],
        "largest_group_gain_or_loss_share": area_audit["largest_gain_or_loss_share"],
        "area_change_is_audit_only": True,
        "line_joins": line_stats["line_joins"],
        "lines_kept": len(line_features),
        "lines_dropped_short": line_stats["lines_dropped_short"],
        "lines_dropped_redundant": line_stats["lines_dropped_redundant"],
        "params": params,
    }

    imwrite(out_dir / "label_map_gen.png", (label_map + 1).astype(np.uint8))
    preview = _render_groups(label_map, definitions)
    imwrite(out_dir / "label_map_gen_preview.png", preview)
    (out_dir / "groups.json").write_text(
        json.dumps({"groups": definitions}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "classes_gen.json").write_text(
        json.dumps({"classes": group_classes}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "regions_gen.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm_per_px, 4), "features": regions,
    }), encoding="utf-8")
    (out_dir / "lines_gen.geojson").write_text(json.dumps({
        "type": "FeatureCollection", "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm_per_px, 4), "features": line_features,
    }), encoding="utf-8")
    (out_dir / "step6_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "step6_transitions.json").write_text(
        json.dumps(transition, indent=2, ensure_ascii=False), encoding="utf-8")

    changes = np.full((*label_map.shape, 3), 255, np.uint8)
    changes[original >= 0] = (225, 225, 225)
    for group in definitions:
        changes[changed & (label_map == int(group["group_id"]))] = np.uint8(group["rgb"][::-1])
    imwrite(out_dir / "step6_changes.png", changes)
    source_image = imread(out_dir / "map_area.png")
    debug = np.hstack([source_image, preview])
    if debug.shape[1] > 2000:
        factor = 2000 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=factor, fy=factor,
                           interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step6_debug.png", debug)
    return {"out_dir": out_dir, "summary": summary, "classes": group_classes,
            "polygons": len(regions), "transition": transition}


def run_step6_presets(image_path: Path, model: str | None = None,
                          runs_dir: Path = Path("runs")) -> dict:
    out_dir = runs_dir / image_path.stem
    base = load_step6_params(out_dir)
    params_path = out_dir / "step6_params.json"
    saved_is_current = False
    if params_path.exists():
        try:
            saved_is_current = (json.loads(params_path.read_text(encoding="utf-8")).get(
                "method_version") == STEP6_METHOD_VERSION)
        except json.JSONDecodeError:
            pass
    if not saved_is_current:
        canonical_params_path = out_dir / "step6_params.json"
        canonical_params = (json.loads(canonical_params_path.read_text(encoding="utf-8"))
                            if canonical_params_path.exists() else {})
        base["simplification_level"] = canonical_params.get("simplification_level", 3)
    selected = base.get("simplification_level")
    selected = int(selected) if selected in SIMPLIFICATION_PRESETS else 3
    for level in SIMPLIFICATION_PRESETS:
        run_step6(image_path, model=model, runs_dir=runs_dir,
                      params_override=step6_preset_params(level, base))
        _cache_step6_preset(out_dir, level)
    summary = activate_step6_preset(out_dir, selected)
    classes = json.loads((out_dir / "classes_gen.json").read_text(
        encoding="utf-8"))["classes"]
    regions = json.loads((out_dir / "regions_gen.geojson").read_text(
        encoding="utf-8"))["features"]
    return {"out_dir": out_dir, "summary": summary,
            "classes": classes, "polygons": len(regions)}

