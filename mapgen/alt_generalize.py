"""Alternate Step 5 -- bounded local boundary simplification.

This experimental branch is deliberately independent from the canonical
Step 5. It keeps Step 4's label_map.png immutable, generalizes only complete
regions and narrow features that fail a physical tactile-size test, then uses
bounded Gaussian winner smoothing for shared boundaries. Independent class and
transition budgets prevent large thematic expansion. It never erases a patch
for nearest-pixel filling and needs no model call or relationship review.

Artifacts use an ``alt_`` prefix so both branches can coexist in one run
directory and be compared before either tactile render is selected.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field

from .generalize import (SIMPLIFICATION_PRESETS, SIMPLIFY_MM, compute_scale,
                         handle_islands)
from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .segment import polygonize
from .semantics import MapSemantics

RELATIONSHIP_VERSION = 1


class PairProposal(BaseModel):
    class_a: str
    class_b: str
    allow_a_to_b: bool = Field(
        description="Whether a small class_a region may be relabelled class_b")
    allow_b_to_a: bool = Field(
        description="Whether a small class_b region may be relabelled class_a")
    aggregation_compatible: bool = Field(
        description="Whether both classes may share one tactile legend group")
    rationale: str


class RelationshipProposal(BaseModel):
    pairs: list[PairProposal]


RELATIONSHIP_PROMPT = """\
You are proposing a conservative, human-reviewable semantic policy for
generalizing a tactile thematic map.  The pixel classification is already
known.  For EVERY unordered pair of thematic classes below, return one pair
record using the class labels exactly as written.

There are two different decisions:
1. Local replacement changes geography: allow A->B only when an undersized A
   patch can be represented as B without creating a materially false map.
   Direction matters.  Be conservative; false means safer than a dubious yes.
2. Global aggregation only gives two categories one tactile texture and legend
   group.  It may be allowed more broadly when the combined concept is honest.

Map subject: {subject}
Data ordering: {ordering}

Classes:
{classes}
"""


ALT_PRESET_ARTIFACTS = (
    "alt_label_map_gen.png",
    "alt_label_map_gen_preview.png",
    "alt_classes_gen.json",
    "alt_regions_gen.geojson",
    "alt_lines_gen.geojson",
    "alt_step5_summary.json",
    "alt_step5_debug.png",
    "alt_step5_changes.png",
    "alt_step5_transitions.json",
    "alt_step5_merge_log.json",
)

ALT_DEFAULT_PARAMS = {
    "method_version": 7,
    "simplification_level": 3,
    "min_texture_area_side_mm": None,
    "protected_classes": [],
}

SAFE_BOUNDARY_PRESETS = {
    1: {"min_feature_mm": 5.0, "smooth_mm": 0.50, "max_class_change_share": 0.06},
    2: {"min_feature_mm": 7.0, "smooth_mm": 0.80, "max_class_change_share": 0.09},
    3: {"min_feature_mm": 10.0, "smooth_mm": 1.20, "max_class_change_share": 0.12},
    # A visibly calmer boundary and region scale, while category budgets still
    # prevent the unbounded expansion seen in the canonical dissolve method.
    4: {"min_feature_mm": 13.0, "smooth_mm": 1.50, "max_class_change_share": 0.20},
    5: {"min_feature_mm": 16.0, "smooth_mm": 2.00, "max_class_change_share": 0.25},
}


def _class_fingerprint(classes: list[dict], sem: MapSemantics) -> str:
    payload = {
        "version": RELATIONSHIP_VERSION,
        "subject": sem.subject,
        "ordering": sem.data_ordering.value,
        "classes": [{"index": c["index"], "label": c["label"],
                     "is_thematic": c["is_thematic"]} for c in classes],
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _empty_pairs(thematic: list[dict]) -> list[dict]:
    pairs = []
    for pos, a in enumerate(thematic):
        for b in thematic[pos + 1:]:
            pairs.append({
                "a_index": a["index"], "a_label": a["label"],
                "b_index": b["index"], "b_label": b["label"],
                "allow_a_to_b": False, "allow_b_to_a": False,
                "aggregation_compatible": False,
                "rationale": "No reviewed compatibility decision",
            })
    return pairs


def _ordered_pairs(thematic: list[dict]) -> list[dict]:
    pairs = _empty_pairs(thematic)
    positions = {c["index"]: i for i, c in enumerate(thematic)}
    for pair in pairs:
        adjacent = abs(positions[pair["a_index"]] - positions[pair["b_index"]]) == 1
        if adjacent:
            pair.update({
                "allow_a_to_b": True,
                "allow_b_to_a": True,
                "aggregation_compatible": True,
                "rationale": "Adjacent ordered bins",
            })
    return pairs


def _apply_proposal(base_pairs: list[dict], proposal: RelationshipProposal) -> list[dict]:
    by_labels = {
        frozenset((p["a_label"], p["b_label"])): p for p in base_pairs
    }
    for proposed in proposal.pairs:
        pair = by_labels.get(frozenset((proposed.class_a, proposed.class_b)))
        if pair is None or proposed.class_a == proposed.class_b:
            continue
        same_order = proposed.class_a == pair["a_label"]
        pair["allow_a_to_b"] = (proposed.allow_a_to_b if same_order
                                  else proposed.allow_b_to_a)
        pair["allow_b_to_a"] = (proposed.allow_b_to_a if same_order
                                  else proposed.allow_a_to_b)
        pair["aggregation_compatible"] = proposed.aggregation_compatible
        pair["rationale"] = proposed.rationale
    return base_pairs


def build_relationships(classes: list[dict], sem: MapSemantics,
                        model: str | None = None) -> dict:
    """Create a deterministic legacy policy artifact without a network call."""
    thematic = sorted((c for c in classes if c["is_thematic"]), key=lambda c: c["index"])
    notes: list[str] = []
    if sem.data_ordering.value == "ordered":
        pairs = _ordered_pairs(thematic)
        status = "deterministic_ordered"
    else:
        pairs = _empty_pairs(thematic)
        status = "not_used_by_bounded_alt_step5"
    return {
        "version": RELATIONSHIP_VERSION,
        "classes_fingerprint": _class_fingerprint(classes, sem),
        "reviewed": sem.data_ordering.value == "ordered",
        "status": status,
        "data_ordering": sem.data_ordering.value,
        "pairs": pairs,
        "notes": notes,
    }


def load_or_create_relationships(out_dir: Path, classes: list[dict], sem: MapSemantics,
                                 model: str | None = None) -> dict:
    path = out_dir / "alt_class_relationships.json"
    fingerprint = _class_fingerprint(classes, sem)
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if (saved.get("version") == RELATIONSHIP_VERSION
                and saved.get("classes_fingerprint") == fingerprint):
            return saved
    result = build_relationships(classes, sem, model=model)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def save_relationship_review(out_dir: Path, classes: list[dict], sem: MapSemantics,
                             decisions: list[dict]) -> dict:
    """Validate and save a complete directed human review from the web API."""
    current = load_or_create_relationships(out_dir, classes, sem)
    by_key = {(p["a_index"], p["b_index"]): p for p in current["pairs"]}
    if len(decisions) != len(by_key):
        raise ValueError(f"review must include all {len(by_key)} thematic class pairs")
    seen: set[tuple[int, int]] = set()
    for decision in decisions:
        try:
            a, b = int(decision["a_index"]), int(decision["b_index"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("every relationship decision needs integer a_index and b_index") from exc
        key = (min(a, b), max(a, b))
        pair = by_key.get(key)
        if pair is None:
            raise ValueError(f"unknown thematic class pair {key}")
        if key in seen:
            raise ValueError(f"duplicate thematic class pair {key}")
        seen.add(key)
        same_order = a == pair["a_index"]
        ab = bool(decision.get("allow_a_to_b", False))
        ba = bool(decision.get("allow_b_to_a", False))
        pair["allow_a_to_b"] = ab if same_order else ba
        pair["allow_b_to_a"] = ba if same_order else ab
        pair["aggregation_compatible"] = bool(
            decision.get("aggregation_compatible", False))
        rationale = str(decision.get("rationale", pair.get("rationale", ""))).strip()
        pair["rationale"] = rationale[:500]
    current.update({"reviewed": True, "status": "human_reviewed"})
    (out_dir / "alt_class_relationships.json").write_text(
        json.dumps(current, indent=2, ensure_ascii=False), encoding="utf-8")
    return current


def _relationship_lookup(relationships: dict) -> dict[tuple[int, int], dict]:
    lookup = {}
    reviewed = bool(relationships.get("reviewed"))
    for pair in relationships.get("pairs", []):
        a, b = int(pair["a_index"]), int(pair["b_index"])
        lookup[(a, b)] = {
            "allowed": reviewed and bool(pair.get("allow_a_to_b")),
            "rationale": (pair.get("rationale", "") if reviewed
                          else "draft relationship not yet human reviewed"),
        }
        lookup[(b, a)] = {
            "allowed": reviewed and bool(pair.get("allow_b_to_a")),
            "rationale": (pair.get("rationale", "") if reviewed
                          else "draft relationship not yet human reviewed"),
        }
    return lookup


def _is_water(cl: dict) -> bool:
    return cl.get("source") == "water-heuristic" or "water" in cl.get("label", "").lower()


def merge_small_components(label_map: np.ndarray, mask: np.ndarray, classes: list[dict],
                           min_area_px: float, relationships: dict,
                           protected_classes: set[int] = frozenset(),
                           preserve_share: float = 0.01,
                           max_iter: int = 8) -> tuple[list[dict], list[dict]]:
    """Merge whole small patches through allowed directed class relationships.

    Returns (merge_log, unresolved_log).  No pixel in a thematic patch changes
    category unless one explicit merge_log entry accounts for the whole patch.
    """
    by_idx = {int(c["index"]): c for c in classes}
    original_area = {idx: max(1, int(c.get("area_px", 0))) for idx, c in by_idx.items()}
    relation = _relationship_lookup(relationships)
    merges: list[dict] = []
    unresolved_by_key: dict[tuple[int, int, int], dict] = {}

    n_worlds, worlds, world_stats, _ = cv2.connectedComponentsWithStats(
        (mask > 0).astype(np.uint8), connectivity=8)
    kernel = np.ones((3, 3), np.uint8)

    for world_id in range(1, n_worlds):
        x, y, width, height, _ = world_stats[world_id, :5]
        world_mask = worlds[y:y + height, x:x + width] == world_id
        local = label_map[y:y + height, x:x + width]

        # Preserve the largest original patch on each separate landmass.
        dominant = np.zeros(local.shape, bool)
        dominant_area = 0
        for idx in np.unique(local[world_mask & (local >= 0)]):
            n, cc, stats, _ = cv2.connectedComponentsWithStats(
                ((local == idx) & world_mask).astype(np.uint8), connectivity=8)
            for component_id in range(1, n):
                area = int(stats[component_id, cv2.CC_STAT_AREA])
                if area > dominant_area:
                    dominant_area = area
                    dominant = cc == component_id

        for iteration in range(max_iter):
            candidates_to_process = []
            for idx in np.unique(local[world_mask & (local >= 0)]):
                n, cc, stats, _ = cv2.connectedComponentsWithStats(
                    ((local == idx) & world_mask).astype(np.uint8), connectivity=8)
                for component_id in range(1, n):
                    area = int(stats[component_id, cv2.CC_STAT_AREA])
                    if area < min_area_px:
                        candidates_to_process.append((area, int(idx), cc == component_id))
            candidates_to_process.sort(key=lambda item: item[0])
            changed = 0

            for area_snapshot, source_idx, piece_snapshot in candidates_to_process:
                piece = piece_snapshot & (local == source_idx) & world_mask
                area = int(np.count_nonzero(piece))
                if area == 0 or area >= min_area_px:
                    continue
                source = by_idx[source_idx]
                reason = None
                if source_idx in protected_classes:
                    reason = "protected_class"
                elif np.any(dominant & piece):
                    reason = "dominant_patch_on_landmass"
                elif (_is_water(source)):
                    reason = "water_not_locally_relabelled"
                elif (source.get("is_thematic") and source.get("area_share", 0) >= preserve_share
                      and np.count_nonzero(local == source_idx) == area):
                    reason = "last_significant_thematic_patch"
                if reason:
                    anchor = int(np.flatnonzero(piece)[0])
                    unresolved_by_key[(world_id, source_idx, anchor)] = {
                        "world": world_id, "iteration": iteration + 1,
                        "source_index": source_idx, "source_label": source["label"],
                        "area_px": area, "reason": reason,
                        "anchor_px": [int(x) + int(anchor % width),
                                      int(y) + int(anchor // width)],
                    }
                    continue

                ring = (cv2.dilate(piece.astype(np.uint8), kernel) > 0) & ~piece & world_mask
                neighbour_values, counts = np.unique(local[ring & (local >= 0)], return_counts=True)
                scored = []
                for target_value, boundary_count in zip(neighbour_values, counts):
                    target_idx = int(target_value)
                    if target_idx == source_idx:
                        continue
                    target = by_idx[target_idx]
                    if source.get("is_thematic"):
                        decision = relation.get((source_idx, target_idx), {})
                        allowed = bool(decision.get("allowed"))
                        semantic_reason = decision.get("rationale", "no relationship")
                    else:
                        # Unseeded/non-thematic segmentation debris may be absorbed
                        # geometrically, but water and thematic pixels may not.
                        allowed = source.get("source") == "unseeded"
                        semantic_reason = "non-thematic unseeded cleanup"
                    current_target = int(np.count_nonzero(label_map == target_idx))
                    growth = max(0.0, (current_target - original_area[target_idx])
                                 / original_area[target_idx])
                    scored.append({
                        "target_index": target_idx,
                        "target_label": target["label"],
                        "allowed": allowed,
                        "shared_boundary_px": int(boundary_count),
                        "shared_boundary_fraction": round(float(boundary_count) /
                                                          max(1, int(counts.sum())), 4),
                        "target_growth_from_original": round(growth, 4),
                        "semantic_reason": semantic_reason,
                    })
                eligible = [candidate for candidate in scored if candidate["allowed"]]
                eligible.sort(key=lambda candidate: (
                    -candidate["shared_boundary_px"],
                    candidate["target_growth_from_original"],
                    by_idx[candidate["target_index"]].get("priority") is None,
                    by_idx[candidate["target_index"]].get("priority") or 10_000,
                    candidate["target_index"],
                ))
                if not eligible:
                    anchor = int(np.flatnonzero(piece)[0])
                    unresolved_by_key[(world_id, source_idx, anchor)] = {
                        "world": world_id, "iteration": iteration + 1,
                        "source_index": source_idx, "source_label": source["label"],
                        "area_px": area, "reason": "no_semantically_allowed_neighbour",
                        "anchor_px": [int(x) + int(anchor % width),
                                      int(y) + int(anchor // width)],
                        "candidates": scored,
                    }
                    continue

                selected = eligible[0]
                local[piece] = selected["target_index"]
                merges.append({
                    "world": world_id, "iteration": iteration + 1,
                    "source_index": source_idx, "source_label": source["label"],
                    "target_index": selected["target_index"],
                    "target_label": selected["target_label"],
                    "area_px": area,
                    "selection": "largest shared boundary, then lowest target area growth",
                    "selected_candidate": selected,
                    "candidates": scored,
                })
                changed += 1
            if changed == 0:
                break

    # Report final retained components, not every intermediate version of a
    # component that may have grown when adjacent debris was merged into it.
    history = list(unresolved_by_key.values())
    final_unresolved = []
    for source_idx in np.unique(label_map[(mask > 0) & (label_map >= 0)]):
        n, components, stats, _ = cv2.connectedComponentsWithStats(
            (label_map == source_idx).astype(np.uint8), connectivity=8)
        for component_id in range(1, n):
            area = int(stats[component_id, cv2.CC_STAT_AREA])
            if area >= min_area_px:
                continue
            matching = []
            for record in history:
                if record["source_index"] != int(source_idx):
                    continue
                ax, ay = record["anchor_px"]
                if components[ay, ax] == component_id:
                    matching.append(record)
            if matching:
                record = dict(max(matching, key=lambda item: item["iteration"]))
                record["area_px"] = area
            else:
                ys, xs = np.where(components == component_id)
                record = {
                    "world": int(worlds[ys[0], xs[0]]), "iteration": max_iter,
                    "source_index": int(source_idx),
                    "source_label": by_idx[int(source_idx)]["label"],
                    "area_px": area, "anchor_px": [int(xs[0]), int(ys[0])],
                    "reason": "retained_after_component_reconfiguration",
                }
            final_unresolved.append(record)
    return merges, final_unresolved


def transition_audit(original: np.ndarray, generalized: np.ndarray,
                     classes: list[dict]) -> dict:
    by_idx = {int(c["index"]): c["label"] for c in classes}
    values = sorted(set(int(v) for v in np.unique(original)) |
                    set(int(v) for v in np.unique(generalized)))
    matrix = []
    for source in values:
        for target in values:
            count = int(np.count_nonzero((original == source) & (generalized == target)))
            if count:
                matrix.append({
                    "from_index": source,
                    "from_label": by_idx.get(source, "outside"),
                    "to_index": target,
                    "to_label": by_idx.get(target, "outside"),
                    "pixels": count,
                })
    per_class = []
    for idx, label in sorted(by_idx.items()):
        before = int(np.count_nonzero(original == idx))
        after = int(np.count_nonzero(generalized == idx))
        retained = int(np.count_nonzero((original == idx) & (generalized == idx)))
        gained, lost = after - retained, before - retained
        per_class.append({
            "index": idx, "label": label, "before_px": before, "after_px": after,
            "retained_px": retained, "gained_px": gained, "lost_px": lost,
            "contamination_share": round(gained / max(1, after), 4),
            "retention_share": round(retained / max(1, before), 4),
        })
    changed = int(np.count_nonzero(original != generalized))
    inside = max(1, int(np.count_nonzero((original >= 0) | (generalized >= 0))))
    return {
        "changed_pixels": changed,
        "changed_share": round(changed / inside, 4),
        "matrix": matrix,
        "per_class": per_class,
    }


def simplify_boundaries_safely(label_map: np.ndarray, mask: np.ndarray,
                               classes: list[dict], mm_per_px: float, level: int,
                               protected_classes: set[int] = frozenset()) -> dict:
    """Enforce a physical tactile minimum, then smooth shared boundaries.

    Only complete connected regions that fail the configured area/width test
    may be merged. Their owner is selected from touching neighbours (semantic
    family first, longest shared boundary second); nearest-pixel flood filling
    is never used. Gaussian winner smoothing then follows the successful
    canonical ``Simple`` approach, but every class has independent gain/loss
    budgets so the large thematic expansions cannot recur.
    """
    level = max(1, min(5, int(level)))
    preset = SAFE_BOUNDARY_PRESETS[level]
    min_feature_mm = float(preset["min_feature_mm"])
    min_area_mm2 = min_feature_mm * min_feature_mm
    max_share = float(preset["max_class_change_share"])
    indices = [int(value) for value in np.unique(label_map[label_map >= 0])]
    if not indices:
        return {
            "method": "physical_minimum_plus_bounded_gaussian_smoothing",
            "min_feature_mm": min_feature_mm, "min_area_mm2": min_area_mm2,
            "smooth_mm": preset["smooth_mm"], "smoothing_sigma_px": 0.0,
            "max_class_change_share": max_share,
            "whole_regions_merged": 0, "below_minimum_retained": 0,
            "post_smoothing_whole_regions_merged": 0,
            "merge_records": [],
            "candidate_pixels": 0, "accepted_pixels": 0, "per_class_budget_px": {},
        }
    by_idx = {int(cl["index"]): cl for cl in classes}
    original_area = {idx: int(np.count_nonzero(label_map == idx)) for idx in indices}
    budget = {
        idx: (0 if idx in protected_classes else max(1, int(original_area[idx] * max_share)))
        for idx in indices
    }
    gained = {idx: 0 for idx in indices}
    lost = {idx: 0 for idx in indices}
    kernel = np.ones((3, 3), np.uint8)

    def family(idx: int) -> str | None:
        label = by_idx.get(idx, {}).get("label", "").lower()
        if any(word in label for word in ("forest", "wood")):
            return "forest"
        if any(word in label for word in ("grass", "pasture", "meadow")):
            return "grassland"
        if any(word in label for word in ("cropland", "cereal", "wheat", "corn", "beet", "field")):
            return "field_crops"
        if any(word in label for word in ("vine", "olive", "garden", "fruit", "vegetable", "flower")):
            return "specialty_crops"
        return None

    pair_used: dict[tuple[int, int], int] = {}

    def pair_limit(source: int, target: int) -> int:
        if source < 0 or target < 0:
            return 10**12
        source_family, target_family = family(source), family(target)
        smaller = min(original_area[source], original_area[target])
        if source_family and source_family == target_family:
            return max(1, int(smaller * max_share))
        if ({source_family, target_family} <= {"field_crops", "specialty_crops"}
                and source_family and target_family):
            return max(1, int(smaller * 0.06))
        return max(1, int(smaller * 0.02))

    def pair_allows(source: int, target: int, amount: int) -> bool:
        return pair_used.get((source, target), 0) + amount <= pair_limit(source, target)

    def record_pair(source: int, target: int, amount: int) -> None:
        pair_used[(source, target)] = pair_used.get((source, target), 0) + amount

    def detectable(piece: np.ndarray) -> tuple[bool, float, float]:
        area_mm2 = float(np.count_nonzero(piece)) * mm_per_px * mm_per_px
        radius_px = float(cv2.distanceTransform(piece.astype(np.uint8), cv2.DIST_L2, 5).max())
        diameter_mm = 2.0 * radius_px * mm_per_px
        return area_mm2 >= min_area_mm2 and diameter_mm >= min_feature_mm, area_mm2, diameter_mm

    merged_records = []
    retained_for_budget = 0
    for iteration in range(8):
        candidates = []
        for idx in indices:
            if idx in protected_classes or not np.any(label_map == idx):
                continue
            count, components, stats, _ = cv2.connectedComponentsWithStats(
                (label_map == idx).astype(np.uint8), connectivity=8)
            if count <= 1:
                continue
            largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            for component in range(1, count):
                piece = components == component
                passes, area_mm2, diameter_mm = detectable(piece)
                if passes or component == largest:
                    continue
                candidates.append((int(stats[component, cv2.CC_STAT_AREA]), idx, piece,
                                   area_mm2, diameter_mm))
        if not candidates:
            break
        changed = 0
        for area, source, piece_snapshot, area_mm2, diameter_mm in sorted(
                candidates, key=lambda item: item[0]):
            piece = piece_snapshot & (label_map == source)
            if not piece.any():
                continue
            ring = (cv2.dilate(piece.astype(np.uint8), kernel) > 0) & ~piece & (mask > 0)
            neighbours, counts = np.unique(label_map[ring & (label_map >= 0)], return_counts=True)
            choices = []
            source_family = family(source)
            for target, boundary in zip(neighbours, counts):
                target = int(target)
                if target == source:
                    continue
                target_family = family(target)
                semantic = 2 if source_family and source_family == target_family else 0
                if ({source_family, target_family} <= {"field_crops", "specialty_crops"}
                        and source_family and target_family):
                    semantic = max(semantic, 1)
                choices.append((semantic, int(boundary), -gained[target], target))
            if not choices:
                continue
            current_area = int(np.count_nonzero(piece))
            selected = next((choice for choice in sorted(choices, reverse=True)
                             if lost[source] + current_area <= budget[source]
                             and gained[choice[3]] + current_area <= budget[choice[3]]
                             and pair_allows(source, choice[3], current_area)), None)
            if selected is None:
                retained_for_budget += 1
                continue
            _, boundary, _, target = selected
            label_map[piece] = target
            lost[source] += current_area
            gained[target] += current_area
            record_pair(source, target, current_area)
            merged_records.append({
                "iteration": iteration + 1, "source_index": source,
                "source_label": by_idx[source]["label"], "target_index": target,
                "target_label": by_idx[target]["label"], "area_px": current_area,
                "area_mm2": round(area_mm2, 2), "max_width_mm": round(diameter_mm, 2),
                "shared_boundary_px": boundary,
            })
            changed += 1
        if not changed:
            break

    # Connected regions may still have long branches or necks narrower than
    # the tactile minimum.  A physical opening identifies only those narrow
    # parts; pixels are offered to the nearest surviving class core, subject to
    # the same gain/loss budgets.  This is deliberately not an unrestricted
    # nearest-pixel fill because broad regions are never erased.
    tactile_radius_px = max(1, int(round(
        (min_feature_mm / 2.0) / max(mm_per_px, 1e-6))))
    tactile_kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (tactile_radius_px * 2 + 1, tactile_radius_px * 2 + 1))
    cores: dict[int, np.ndarray] = {}
    opened: dict[int, np.ndarray] = {}
    distances = []
    distance_indices = []
    for idx in indices:
        region = (label_map == idx).astype(np.uint8)
        core = cv2.erode(region, tactile_kernel) > 0
        if not core.any():
            continue
        cores[idx] = core
        opened[idx] = cv2.dilate(core.astype(np.uint8), tactile_kernel) > 0
        distances.append(cv2.distanceTransform((~core).astype(np.uint8), cv2.DIST_L2, 5))
        distance_indices.append(idx)
    narrow_candidates = np.zeros(label_map.shape, bool)
    for idx, opened_region in opened.items():
        narrow_candidates |= (label_map == idx) & ~opened_region
    narrow_changed = 0
    if distances:
        distance_stack = np.stack(distances)
        nearest_pos = np.argmin(distance_stack, axis=0)
        nearest = np.asarray(distance_indices, np.int16)[nearest_pos]
        nearest_distance = np.min(distance_stack, axis=0)
        current_distance = np.full(label_map.shape, np.inf, np.float32)
        for pos, idx in enumerate(distance_indices):
            current_distance[label_map == idx] = distance_stack[pos][label_map == idx]
        improvement = current_distance - nearest_distance
        eligible = (narrow_candidates & (nearest != label_map)
                    & (nearest_distance <= tactile_radius_px * 2.0))
        ys, xs = np.where(eligible)
        for position in np.argsort(-improvement[ys, xs], kind="stable"):
            y, x = int(ys[position]), int(xs[position])
            source, target = int(label_map[y, x]), int(nearest[y, x])
            if (lost[source] >= budget[source] or gained[target] >= budget[target]
                    or not pair_allows(source, target, 1)):
                continue
            label_map[y, x] = target
            lost[source] += 1
            gained[target] += 1
            record_pair(source, target, 1)
            narrow_changed += 1

    # Canonical-style one-hot Gaussian smoothing, constrained by independent
    # class and directional transition budgets.
    sigma = float(np.clip(float(preset["smooth_mm"]) / max(mm_per_px, 1e-6), 1.0, 8.0))
    values = [-1] + indices
    score_stack = np.stack([
        cv2.GaussianBlur(((label_map < 0) if idx < 0 else (label_map == idx)).astype(np.float32),
                         (0, 0), sigma)
        for idx in values
    ])
    winner_pos = np.argmax(score_stack, axis=0)
    winner = np.asarray(values, np.int16)[winner_pos]
    best = np.max(score_stack, axis=0)
    current = np.zeros(label_map.shape, np.float32)
    for pos, idx in enumerate(values):
        current[(label_map < 0) if idx < 0 else (label_map == idx)] = score_stack[pos][
            (label_map < 0) if idx < 0 else (label_map == idx)]
    confidence = best - current
    candidate = winner != label_map
    ys, xs = np.where(candidate)
    order = np.argsort(-confidence[ys, xs], kind="stable")
    accepted = 0
    for position in order:
        y, x = int(ys[position]), int(xs[position])
        source, target = int(label_map[y, x]), int(winner[y, x])
        if source == target:
            continue
        if source >= 0 and lost[source] >= budget[source]:
            continue
        if target >= 0 and gained[target] >= budget[target]:
            continue
        if not pair_allows(source, target, 1):
            continue
        label_map[y, x] = target
        if source >= 0:
            lost[source] += 1
        if target >= 0:
            gained[target] += 1
        record_pair(source, target, 1)
        accepted += 1
    mask[:] = np.where(label_map >= 0, 255, 0).astype(np.uint8)

    # Smoothing can create a few new sub-minimum fragments. Remove those as
    # whole objects as well, while preserving the largest occurrence of every
    # category and enforcing an absolute 20% distortion ceiling.
    hard_budget = {idx: max(budget[idx], int(original_area[idx] * min(0.20, max_share + 0.04)))
                   for idx in indices}
    final_whole_merges = 0
    for _ in range(6):
        final_candidates = []
        for idx in indices:
            if idx in protected_classes or not np.any(label_map == idx):
                continue
            count, components, stats, _ = cv2.connectedComponentsWithStats(
                (label_map == idx).astype(np.uint8), connectivity=8)
            if count <= 2:
                continue
            largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            for component in range(1, count):
                if component == largest:
                    continue
                piece = components == component
                if not detectable(piece)[0]:
                    final_candidates.append((int(stats[component, cv2.CC_STAT_AREA]), idx, piece))
        if not final_candidates:
            break
        changed = 0
        for _, source, piece_snapshot in sorted(final_candidates, key=lambda item: item[0]):
            piece = piece_snapshot & (label_map == source)
            area = int(np.count_nonzero(piece))
            if not area:
                continue
            ring = (cv2.dilate(piece.astype(np.uint8), kernel) > 0) & ~piece & (mask > 0)
            neighbours, counts = np.unique(label_map[ring & (label_map >= 0)], return_counts=True)
            ranked = []
            for target, boundary in zip(neighbours, counts):
                target = int(target)
                if target == source:
                    continue
                semantic = 2 if family(source) and family(source) == family(target) else 0
                if ({family(source), family(target)} <= {"field_crops", "specialty_crops"}
                        and family(source) and family(target)):
                    semantic = max(semantic, 1)
                ranked.append((semantic, int(boundary), target))
            # A redundant sub-minimum occurrence may be removed even when its
            # source class has already lost its soft budget; the class's largest
            # occurrence remains protected. Target growth stays hard-capped,
            # which is the safeguard against renewed orange-style expansion.
            selected = next((target for _, _, target in sorted(ranked, reverse=True)
                             if gained[target] + area <= hard_budget[target]
                             and pair_allows(source, target, area)), None)
            if selected is None:
                continue
            label_map[piece] = selected
            lost[source] += area
            gained[selected] += area
            record_pair(source, selected, area)
            final_whole_merges += 1
            changed += 1
        if not changed:
            break

    below_minimum = 0
    for idx in indices:
        count, components, _, _ = cv2.connectedComponentsWithStats(
            (label_map == idx).astype(np.uint8), connectivity=8)
        for component in range(1, count):
            if not detectable(components == component)[0]:
                below_minimum += 1
    return {
        "method": "physical_minimum_plus_bounded_gaussian_smoothing",
        "min_feature_mm": min_feature_mm,
        "min_area_mm2": min_area_mm2,
        "smooth_mm": preset["smooth_mm"],
        "smoothing_sigma_px": round(sigma, 2),
        "max_class_change_share": max_share,
        "whole_regions_merged": len(merged_records),
        "post_smoothing_whole_regions_merged": final_whole_merges,
        "narrow_feature_pixels_changed": narrow_changed,
        "tactile_radius_px": tactile_radius_px,
        "merge_records": merged_records,
        "regions_retained_for_area_budget": retained_for_budget,
        "below_minimum_retained": below_minimum,
        "candidate_pixels": int(len(order)),
        "accepted_pixels": accepted,
        "per_class_budget_px": budget,
    }


def absorb_unseeded_debris(label_map: np.ndarray, mask: np.ndarray,
                           classes: list[dict]) -> list[dict]:
    """Remove segmentation-only colours that are not real legend categories."""
    records = []
    kernel = np.ones((3, 3), np.uint8)
    for cl in classes:
        if cl.get("is_thematic") or cl.get("source") != "unseeded":
            continue
        source = int(cl["index"])
        count, components, _, _ = cv2.connectedComponentsWithStats(
            (label_map == source).astype(np.uint8), connectivity=8)
        for component in range(1, count):
            piece = components == component
            ring = (cv2.dilate(piece.astype(np.uint8), kernel) > 0) & ~piece & (mask > 0)
            neighbours, counts = np.unique(label_map[ring & (label_map >= 0)], return_counts=True)
            valid = [(int(n), int(target)) for target, n in zip(neighbours, counts)
                     if int(target) != source]
            if not valid:
                continue
            _, target = max(valid)
            area = int(np.count_nonzero(piece))
            label_map[piece] = target
            records.append({
                "source_index": source, "source_label": cl["label"],
                "target_index": target, "area_px": area,
                "reason": "unseeded segmentation debris absorbed by longest boundary",
            })
    return records


def load_alt_params(out_dir: Path) -> dict:
    params = dict(ALT_DEFAULT_PARAMS)
    path = out_dir / "alt_step5_params.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("method_version") == ALT_DEFAULT_PARAMS["method_version"]:
            params.update(saved)
    return params


def alt_preset_params(spec: OutputSpec, level: int, base: dict | None = None) -> dict:
    level = max(1, min(5, int(level)))
    preset = SAFE_BOUNDARY_PRESETS[level]
    params = dict(ALT_DEFAULT_PARAMS)
    if base:
        params.update(base)
    params.update({
        "simplification_level": level,
        "min_texture_area_side_mm": preset["min_feature_mm"],
    })
    return params


def alt_preset_artifact_name(level: int, canonical_name: str) -> str:
    return f"alt_step5_preset_{int(level)}_{canonical_name.removeprefix('alt_')}"


def _cache_alt_preset(out_dir: Path, level: int) -> None:
    for name in ALT_PRESET_ARTIFACTS:
        shutil.copy2(out_dir / name, out_dir / alt_preset_artifact_name(level, name))


def activate_alt_preset(out_dir: Path, level: int) -> dict:
    level = max(1, min(5, int(level)))
    missing = [name for name in ALT_PRESET_ARTIFACTS
               if not (out_dir / alt_preset_artifact_name(level, name)).exists()]
    if missing:
        raise FileNotFoundError(f"alternate preset {level} is not ready")
    for name in ALT_PRESET_ARTIFACTS:
        shutil.copy2(out_dir / alt_preset_artifact_name(level, name), out_dir / name)
    summary = json.loads((out_dir / "alt_step5_summary.json").read_text(encoding="utf-8"))
    (out_dir / "alt_step5_params.json").write_text(
        json.dumps(summary["params"], indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def run_alt_step5(image_path: Path, model: str | None = None,
                  runs_dir: Path = Path("runs"), params_override: dict | None = None) -> dict:
    from .segment import run_step4

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "label_map.png").exists() or not (out_dir / "classes_final.json").exists():
        run_step4(image_path, model=model, runs_dir=runs_dir)

    spec = OutputSpec.load_or_create()
    classes = json.loads((out_dir / "classes_final.json").read_text(encoding="utf-8"))["classes"]
    label_map = imread(out_dir / "label_map.png")[..., 0].astype(np.int16) - 1
    original = label_map.copy()
    height, width = label_map.shape
    mask = np.where(label_map >= 0, 255, 0).astype(np.uint8)

    params = load_alt_params(out_dir) if params_override is None else dict(params_override)
    scale = compute_scale(spec, width, height)
    mm_per_px = scale["mm_per_px"]
    side_mm = (params.get("min_texture_area_side_mm")
               or spec.constants.min_texture_area_side_mm)
    min_area_px = (side_mm / mm_per_px) ** 2
    eps_px = max(1.0, SIMPLIFY_MM / mm_per_px)
    protected = set(int(value) for value in params.get("protected_classes", []))

    islands = {"dropped": 0, "exaggerated": 0}
    cleanup = absorb_unseeded_debris(label_map, mask, classes)
    smoothing = simplify_boundaries_safely(
        label_map, mask, classes, mm_per_px,
        int(params.get("simplification_level", 3)), protected_classes=protected)
    merges = cleanup + smoothing["merge_records"]
    unresolved = []
    audit = transition_audit(original, label_map, classes)
    for merge in merges:
        merge["area_mm2"] = round(merge["area_px"] * mm_per_px * mm_per_px, 2)
    for item in unresolved:
        item["area_mm2"] = round(item["area_px"] * mm_per_px * mm_per_px, 2)

    total = max(1, int(np.count_nonzero(label_map >= 0)))
    classes_alt = []
    for cl in classes:
        area = int(np.count_nonzero(label_map == cl["index"]))
        classes_alt.append(cl | {
            "area_px": area,
            "area_share_before": cl.get("area_share", 0.0),
            "area_share": round(area / total, 4),
        })
    survivors = [cl for cl in classes_alt if cl["area_px"] > 0]
    regions = []
    for cl in survivors:
        regions += polygonize(label_map, cl["index"], cl["label"], eps_px)

    empty_lines = {
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm_per_px, 4),
        "features": [],
    }
    summary = {
        "branch": "alternate",
        "role": "preaggregation_geographic_cleanup",
        "touch_readiness_finalized_in_step": 7,
        "scale_mm_per_px": round(mm_per_px, 4),
        "orientation": scale["orientation"],
        "map_size_mm": scale["map_size_mm"],
        "page_mm": [spec.page_width_mm, spec.page_height_mm],
        "min_texture_area_px": round(min_area_px),
        "min_texture_area_side_mm": side_mm,
        "whole_component_merges": (smoothing["whole_regions_merged"]
                                   + smoothing["post_smoothing_whole_regions_merged"]),
        "unresolved_small_components": smoothing["below_minimum_retained"],
        "islands": islands,
        "method": smoothing["method"],
        "tactile_min_feature_mm": smoothing["min_feature_mm"],
        "preaggregation_min_feature_mm": smoothing["min_feature_mm"],
        "tactile_min_area_mm2": smoothing["min_area_mm2"],
        "boundary_smoothing_mm": smoothing["smooth_mm"],
        "smoothing_sigma_px": smoothing["smoothing_sigma_px"],
        "max_class_change_share": smoothing["max_class_change_share"],
        "candidate_pixels": smoothing["candidate_pixels"],
        "unseeded_components_cleaned": len(cleanup),
        "changed_pixels": audit["changed_pixels"],
        "changed_share": audit["changed_share"],
        "classes_vanished": [cl["label"] for cl in classes_alt if cl["area_px"] == 0],
        "lines_kept": 0,
        "params": params,
    }

    (out_dir / "alt_step5_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "alt_classes_gen.json").write_text(
        json.dumps({"classes": classes_alt}, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "alt_step5_transitions.json").write_text(
        json.dumps(audit, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "alt_step5_merge_log.json").write_text(
        json.dumps({"method": smoothing, "merges": merges, "unresolved": unresolved}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    imwrite(out_dir / "alt_label_map_gen.png", (label_map + 1).astype(np.uint8))
    (out_dir / "alt_regions_gen.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm_per_px, 4),
        "features": regions,
    }), encoding="utf-8")
    (out_dir / "alt_lines_gen.geojson").write_text(
        json.dumps(empty_lines), encoding="utf-8")

    source_image = imread(out_dir / "map_area.png")
    reconstruction = np.full((height, width, 3), 255, np.uint8)
    for cl in survivors:
        reconstruction[label_map == cl["index"]] = np.uint8(cl["rgb"][::-1])
    imwrite(out_dir / "alt_label_map_gen_preview.png", reconstruction)
    debug = np.hstack([source_image, reconstruction])
    if debug.shape[1] > 2000:
        factor = 2000 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=factor, fy=factor,
                           interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "alt_step5_debug.png", debug)

    changed_preview = np.full((height, width, 3), 255, np.uint8)
    inside = (original >= 0) | (label_map >= 0)
    changed_preview[inside] = (225, 225, 225)
    changed = original != label_map
    for cl in classes_alt:
        changed_preview[changed & (label_map == cl["index"])] = np.uint8(cl["rgb"][::-1])
    imwrite(out_dir / "alt_step5_changes.png", changed_preview)

    return {"out_dir": out_dir, "summary": summary, "classes": classes_alt,
            "polygons": len(regions), "merges": merges, "unresolved": unresolved}


def run_alt_step5_presets(image_path: Path, model: str | None = None,
                          runs_dir: Path = Path("runs")) -> dict:
    out_dir = runs_dir / image_path.stem
    base = load_alt_params(out_dir)
    selected = base.get("simplification_level")
    selected = int(selected) if selected in SIMPLIFICATION_PRESETS else 3
    spec = OutputSpec.load_or_create()
    for level in SIMPLIFICATION_PRESETS:
        run_alt_step5(image_path, model=model, runs_dir=runs_dir,
                      params_override=alt_preset_params(spec, level, base))
        _cache_alt_preset(out_dir, level)
    summary = activate_alt_preset(out_dir, selected)
    classes = json.loads((out_dir / "alt_classes_gen.json").read_text(encoding="utf-8"))["classes"]
    polygons = len(json.loads((out_dir / "alt_regions_gen.geojson").read_text(
        encoding="utf-8"))["features"])
    return {"out_dir": out_dir, "summary": summary,
            "classes": classes, "polygons": polygons}
