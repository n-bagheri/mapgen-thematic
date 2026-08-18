"""Step 8 -- Select and render tactile area boundaries.

The selection is deliberately global.  A regular patterned area is initially
bounded only where it touches another regular pattern.  If a pattern occurs in
any such pattern-pattern adjacency, that pattern receives a boundary along all
of its region edges, including edges against plain, pure-black, and outside
areas.

Artifacts per map, under runs/<name>/:
    step8_boundaries.json  adjacency decisions and physical stroke settings
    step8_boundaries.png   rebuilt pattern layer with selective compound strokes
    step8_debug.png        unchanged Step 7 render | selected Step 8 result

Step 7 artifacts are inputs for assignments and visual comparison only; this
module never rewrites them.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .isolate import imread, imwrite, thinning
from .semantics import load_pipeline_semantics
from .symbols import RENDER_PX_PER_MM

WHITE_STROKE_MM = 5.0
BLACK_STROKE_MM = 1.0
NON_PATTERN_IDS = frozenset({"plain", "solid_black"})
OUTSIDE_GROUP = -1
# A patterned area narrower than this cannot retain a distinct, embossed
# boundary at the 5 px/mm render scale.  Excluding it from contouring avoids
# OpenCV producing a one-pixel dangling contour around rasterization slivers.
MIN_CONTOUR_COMPONENT_SPAN_PX = 4


def is_regular_pattern(pattern_id: str | None) -> bool:
    """True for textures governed by the pattern-pattern boundary rule."""
    return bool(pattern_id and pattern_id not in NON_PATTERN_IDS)


def build_group_map(label_map: np.ndarray, assignments: list[dict],
                    output_shape: tuple[int, int]) -> np.ndarray:
    """Map source class indices to Step 7 assignment groups at render size."""
    height, width = output_shape
    resized = cv2.resize(
        (label_map + 1).astype(np.uint16), (width, height),
        interpolation=cv2.INTER_NEAREST,
    )
    group_map = np.full((height, width), OUTSIDE_GROUP, np.int16)
    for group_id, assignment in enumerate(assignments):
        for class_index in assignment.get("members", []):
            group_map[resized == int(class_index) + 1] = group_id
    return group_map


def _pair(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def adjacency_contacts(group_map: np.ndarray) -> dict[tuple[int, int], int]:
    """Return distinct 4-neighbour interfaces, including the canvas outside."""
    contacts: dict[tuple[int, int], int] = {}

    def add(left: np.ndarray, right: np.ndarray) -> None:
        changed = left != right
        if not changed.any():
            return
        pairs = np.stack((left[changed], right[changed]), axis=1).astype(np.int32)
        pairs.sort(axis=1)
        unique, counts = np.unique(pairs, axis=0, return_counts=True)
        for values, count in zip(unique, counts):
            key = (int(values[0]), int(values[1]))
            contacts[key] = contacts.get(key, 0) + int(count)

    add(group_map[:-1, :], group_map[1:, :])
    add(group_map[:, :-1], group_map[:, 1:])

    # Treat the space beyond the raster as no-fill.  This matters when the
    # geographic map reaches an image edge instead of carrying an outside rim.
    for edge in (group_map[0, :], group_map[-1, :],
                 group_map[:, 0], group_map[:, -1]):
        outside = np.full(edge.shape, OUTSIDE_GROUP, dtype=group_map.dtype)
        add(outside, edge)
    contacts.pop((OUTSIDE_GROUP, OUTSIDE_GROUP), None)
    return contacts


def select_boundary_pairs(group_map: np.ndarray,
                          group_patterns: dict[int, str]) -> tuple[
                              set[tuple[int, int]], set[str], dict[tuple[int, int], int]]:
    """Apply base exceptions, then the map-wide pattern-priority override."""
    contacts = adjacency_contacts(group_map)
    active_patterns: set[str] = set()

    # Pass 1 is global: discover every pattern that participates anywhere in a
    # regular pattern-pattern interface before deciding any individual edge.
    for first, second in contacts:
        first_pattern = group_patterns.get(first)
        second_pattern = group_patterns.get(second)
        if is_regular_pattern(first_pattern) and is_regular_pattern(second_pattern):
            active_patterns.update((first_pattern, second_pattern))

    selected: set[tuple[int, int]] = set()
    for pair in contacts:
        first, second = pair
        first_pattern = group_patterns.get(first)
        second_pattern = group_patterns.get(second)
        base_case = is_regular_pattern(first_pattern) and is_regular_pattern(second_pattern)
        priority_case = (
            is_regular_pattern(first_pattern) and first_pattern in active_patterns
        ) or (
            is_regular_pattern(second_pattern) and second_pattern in active_patterns
        )
        if base_case or priority_case:
            selected.add(pair)
    return selected, active_patterns, contacts


def boundary_centerline(group_map: np.ndarray,
                        selected_pairs: set[tuple[int, int]]) -> np.ndarray:
    """Rasterize selected interfaces as one-pixel centerline seeds."""
    edge = np.zeros(group_map.shape, np.uint8)

    def selected(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        result = np.zeros(a.shape, dtype=bool)
        for first, second in selected_pairs:
            result |= ((a == first) & (b == second)) | ((a == second) & (b == first))
        return result

    edge[:-1, :][selected(group_map[:-1, :], group_map[1:, :])] = 255
    edge[:, :-1][selected(group_map[:, :-1], group_map[:, 1:])] = 255

    outside_pairs = {group for first, second in selected_pairs
                     for group in (first, second)
                     if OUTSIDE_GROUP in (first, second) and group != OUTSIDE_GROUP}
    if outside_pairs:
        edge[0, np.isin(group_map[0, :], list(outside_pairs))] = 255
        edge[-1, np.isin(group_map[-1, :], list(outside_pairs))] = 255
        edge[np.isin(group_map[:, 0], list(outside_pairs)), 0] = 255
        edge[np.isin(group_map[:, -1], list(outside_pairs)), -1] = 255
    return edge


def closed_pattern_centerline(group_map: np.ndarray,
                              group_patterns: dict[int, str],
                              active_patterns: set[str]) -> tuple[
                                  np.ndarray, int, set[tuple[int, int]], int]:
    """Draw complete contours for every occurrence of each priority pattern.

    Pairwise raster comparisons can leave open ends at outside contacts and
    multi-region junctions.  Contouring each priority-pattern region on a
    one-pixel padded raster guarantees a closed loop, including for regions
    touching the canvas edge and for holes.  Shared interfaces initially have
    one trace from each region; topology-preserving thinning merges those into
    one closed, one-pixel centerline before physical stroke widths are applied.
    """
    edge = np.zeros(group_map.shape, np.uint8)
    active_groups = {
        group_id for group_id, pattern in group_patterns.items()
        if pattern in active_patterns
    }
    carrier_masks = [(group_map == group_id).astype(np.uint8)
                     for group_id in sorted(active_groups)]
    active_mask = np.isin(group_map, list(active_groups)).astype(np.uint8)
    closure_pairs: set[tuple[int, int]] = set()
    black_closure_components = 0

    # A selected pattern-black boundary can otherwise appear to run into the
    # solid fill at a pattern/black/outside junction.  Close only the connected
    # black component that actually touches an active pattern, then carry its
    # compound outline around its complete perimeter, including the outside.
    cross = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    for group_id, pattern in group_patterns.items():
        if pattern != "solid_black":
            continue
        component_count, components = cv2.connectedComponents(
            (group_map == group_id).astype(np.uint8), connectivity=8,
        )
        for component_id in range(1, component_count):
            component = (components == component_id).astype(np.uint8)
            if not np.any((cv2.dilate(component, cross) > 0) & (active_mask > 0)):
                continue
            carrier_masks.append(component)
            black_closure_components += 1

            # Record every adjacency gained solely to close this component.
            neighbours: set[int] = set()
            mask = component > 0
            for shifted_group, shifted_mask in (
                (group_map[:-1, :], mask[1:, :]),
                (group_map[1:, :], mask[:-1, :]),
                (group_map[:, :-1], mask[:, 1:]),
                (group_map[:, 1:], mask[:, :-1]),
            ):
                neighbours.update(int(value) for value in np.unique(shifted_group[shifted_mask]))
            neighbours.discard(group_id)
            if (mask[0, :].any() or mask[-1, :].any()
                    or mask[:, 0].any() or mask[:, -1].any()):
                neighbours.add(OUTSIDE_GROUP)
            closure_pairs.update(_pair(group_id, neighbour) for neighbour in neighbours)

    contour_count = 0
    for region in carrier_masks:
        if not region.any():
            continue
        # Work per connected component: a tiny rasterization sliver can have
        # an open OpenCV contour despite the padded mask.  It is below the
        # physical boundary resolution, so it must not create a dangling
        # embossed stroke or block the whole Step 7 job.
        component_count, components, stats, _ = cv2.connectedComponentsWithStats(
            region, connectivity=8,
        )
        for component_id in range(1, component_count):
            _, _, width, height, _ = stats[component_id]
            if min(width, height) < MIN_CONTOUR_COMPONENT_SPAN_PX:
                continue
            component = (components == component_id).astype(np.uint8)
            padded = cv2.copyMakeBorder(
                component, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0,
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
    repaired_gaps = 0
    if edge.any():
        edge = thinning(edge)
        # OpenCV can turn a closed 8-connected raster contour into a tiny
        # dangling spur at a diagonal pinch.  Repair only within that existing
        # connected contour network; never bridge separate map regions.
        edge, repaired_gaps = repair_tiny_centerline_gaps(edge)
    return edge, contour_count, closure_pairs, black_closure_components, repaired_gaps


def open_endpoint_count(centerline: np.ndarray) -> int:
    """Count true one-neighbour ends in an expected closed contour network."""
    binary = (centerline > 0).astype(np.uint8)
    neighbours = cv2.filter2D(
        binary, -1, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT,
    ) - binary
    return int(np.count_nonzero((binary > 0) & (neighbours < 2)))


def repair_tiny_centerline_gaps(centerline: np.ndarray,
                                max_gap_px: int = 6) -> tuple[np.ndarray, int]:
    """Reconnect a short raster-only break within one contour component.

    The input is composed exclusively from padded, closed contours.  A nearby
    connection inside the same 8-connected network is therefore a rendering
    defect (usually a diagonal pinch), not a geographic gap.  The limit keeps
    this repair strictly below the physical boundary resolution.
    """
    repaired = (centerline > 0).astype(np.uint8) * 255
    bridges = 0
    for _ in range(4):
        component_count, components = cv2.connectedComponents(
            (repaired > 0).astype(np.uint8), connectivity=8)
        if component_count <= 1:
            break
        points = np.argwhere(repaired > 0)
        point_set = set(map(tuple, points.tolist()))
        endpoints = []
        for y, x in point_set:
            degree = sum(
                (y + dy, x + dx) in point_set
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                if dy or dx
            )
            if degree < 2:
                endpoints.append((y, x))
        if not endpoints:
            break

        def branch_from_endpoint(start: tuple[int, int]) -> set[tuple[int, int]]:
            """Return the existing dangling branch, including its junction.

            Euclidean proximity alone is not enough: the next pixel along the
            same dangling branch is also nearby.  Excluding that branch makes
            a bridge close an actual pinch instead of retracing the spur.
            """
            branch: set[tuple[int, int]] = set()
            previous: tuple[int, int] | None = None
            current = start
            while current not in branch:
                branch.add(current)
                y, x = current
                forward = [
                    (y + dy, x + dx)
                    for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                    if (dy or dx) and (y + dy, x + dx) in point_set
                    and (y + dy, x + dx) != previous
                ]
                if len(forward) != 1:
                    break
                previous, current = current, forward[0]
            return branch

        bridged = False
        for y, x in endpoints:
            component = components[y, x]
            if component == 0:
                continue
            branch = branch_from_endpoint((y, x))
            endpoint_targets = [
                (oy, ox) for oy, ox in endpoints
                if (oy, ox) != (y, x)
                and (oy, ox) not in branch
                and components[oy, ox] == component
                and 2.0 < float(np.hypot(ox - x, oy - y)) <= max_gap_px
            ]
            if endpoint_targets:
                ty, tx = min(endpoint_targets,
                             key=lambda point: float(np.hypot(point[1] - x, point[0] - y)))
            elif len(endpoints) == 1:
                # A lone end is the same defect seen at a diagonal pinch.
                ys, xs = np.where(components == component)
                distances = np.hypot(xs - x, ys - y)
                candidates = [index for index, distance in enumerate(distances)
                              if 2.0 < distance <= max_gap_px
                              and (int(ys[index]), int(xs[index])) not in branch]
                if not candidates:
                    continue
                target = min(candidates, key=lambda index: distances[index])
                ty, tx = int(ys[target]), int(xs[target])
            else:
                continue
            cv2.line(repaired, (int(x), int(y)), (int(tx), int(ty)), 255, 1, cv2.LINE_8)
            bridges += 1
            bridged = True
            break
        if not bridged:
            break
    return repaired, bridges


def discard_open_centerline_branches(centerline: np.ndarray) -> tuple[np.ndarray, int]:
    """Trim residual open raster branches rather than blocking map production.

    This layer is built from padded, closed region contours, so an endpoint is
    a thinning/raster artifact, never an intentional geographic line. Closed
    portions are retained; a dangling branch (or fully open remnant) is
    removed as the final contour-safety fallback.
    """
    result = (centerline > 0).astype(np.uint8) * 255
    removed = 0
    for _ in range(int(np.count_nonzero(result)) + 1):
        points = set(map(tuple, np.argwhere(result > 0).tolist()))
        endpoints = []
        for y, x in points:
            degree = sum(
                (y + dy, x + dx) in points
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                if dy or dx
            )
            if degree < 2:
                endpoints.append((y, x))
        if not endpoints:
            break
        branch: list[tuple[int, int]] = []
        previous: tuple[int, int] | None = None
        current = endpoints[0]
        while current in points and current not in branch:
            branch.append(current)
            cy, cx = current
            forward = [
                (cy + dy, cx + dx)
                for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                if (dy or dx) and (cy + dy, cx + dx) in points
                and (cy + dy, cx + dx) != previous
            ]
            if len(forward) != 1:
                if len(forward) > 1:  # retain the first junction pixel
                    branch.pop()
                break
            previous, current = current, forward[0]
        for y, x in branch:
            if result[y, x]:
                result[y, x] = 0
                removed += 1
    return result, removed


def apply_boundary_strokes(canvas: np.ndarray, centerline: np.ndarray,
                           px_per_mm: float = RENDER_PX_PER_MM,
                           white_mm: float = WHITE_STROKE_MM,
                           black_mm: float = BLACK_STROKE_MM) -> tuple[np.ndarray, int, int]:
    """Overlay a wide white clearance and centered narrow black stroke."""
    white_px = max(1, int(round(white_mm * px_per_mm)))
    black_px = max(1, int(round(black_mm * px_per_mm)))
    if black_px > white_px:
        raise ValueError("black boundary stroke cannot be wider than white stroke")

    def expanded(width: int) -> np.ndarray:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (width, width))
        return cv2.dilate(centerline, kernel)

    result = canvas.copy()
    if centerline.any():
        result[expanded(white_px) > 0] = 255
        result[expanded(black_px) > 0] = 0
    return result, white_px, black_px


def _side_record(group_id: int, assignments: list[dict],
                 group_patterns: dict[int, str]) -> dict:
    if group_id == OUTSIDE_GROUP:
        return {"group_id": None, "label": "outside / no fill", "pattern": "plain"}
    assignment = assignments[group_id]
    return {
        "group_id": group_id,
        "label": assignment.get("label", f"group {group_id}"),
        "pattern": group_patterns[group_id],
    }


def _render_step8_base(out_dir: Path, symbols: dict) -> np.ndarray:
    """Build a boundary-free base without reading or modifying Step 7's raster."""
    from .output_spec import OutputSpec
    from .symbols import render_tactile

    assignments = symbols["area_assignments"]
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
        group_id: assignment.get("transform")
        for group_id, assignment in enumerate(assignments)
    }
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int16) - 1
    summary_path = (out_dir / "step6_summary.json"
                    if (out_dir / "step6_summary.json").exists()
                    else out_dir / "step5_summary.json")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    line_data = json.loads((out_dir / "lines_gen.geojson").read_text(encoding="utf-8"))
    lines = [
        feature["properties"] | {"points": feature["geometry"]["coordinates"]}
        for feature in line_data.get("features", [])
    ]
    return render_tactile(
        label_map, idx_to_group, group_patterns, lines,
        float(summary["scale_mm_per_px"]), OutputSpec.load_or_create(),
        include_region_boundaries=False,
        group_transforms=group_transforms,
    )


def run_step8(image_path: Path, model: str | None = None,
              runs_dir: Path = Path("runs")) -> dict:
    """Analyze all map adjacencies and add the selected compound boundaries."""
    from .symbols import run_step7

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "step7_tactile.png").exists():
        run_step7(image_path, model=model, runs_dir=runs_dir)
    load_pipeline_semantics(out_dir, "Step 8")

    symbols = json.loads((out_dir / "symbols.json").read_text(encoding="utf-8"))
    assignments = symbols["area_assignments"]
    group_patterns = {
        group_id: assignment["pattern"]
        for group_id, assignment in enumerate(assignments)
    }
    label_map = imread(out_dir / "label_map_gen.png")[..., 0].astype(np.int32) - 1
    step7_canvas = imread(out_dir / "step7_tactile.png")[..., 0]
    canvas = _render_step8_base(out_dir, symbols)
    group_map = build_group_map(label_map, assignments, canvas.shape[:2])
    selected, active_patterns, contacts = select_boundary_pairs(group_map, group_patterns)
    centerline, closed_contours, closure_pairs, black_closure_components, repaired_gaps = closed_pattern_centerline(
        group_map, group_patterns, active_patterns,
    )
    open_endpoints = open_endpoint_count(centerline)
    discarded_open_outline_pixels = 0
    if open_endpoints:
        centerline, discarded_open_outline_pixels = discard_open_centerline_branches(centerline)
        open_endpoints = open_endpoint_count(centerline)
    result, white_px, black_px = apply_boundary_strokes(
        canvas, centerline, float(symbols.get("render_px_per_mm", RENDER_PX_PER_MM)),
    )
    white_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (white_px, white_px))
    black_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (black_px, black_px))
    white_stroke_mask = cv2.dilate(centerline, white_kernel)
    black_stroke_mask = cv2.dilate(centerline, black_kernel)

    adjacency_audit = []
    for pair, contact_px in sorted(contacts.items()):
        first_pattern = group_patterns.get(pair[0])
        second_pattern = group_patterns.get(pair[1])
        if pair in closure_pairs and pair not in selected:
            reason = "closed_black_component_perimeter"
        elif is_regular_pattern(first_pattern) and is_regular_pattern(second_pattern):
            reason = "pattern_pattern"
        elif pair in selected:
            reason = "global_pattern_priority"
        elif "solid_black" in (first_pattern, second_pattern):
            reason = "pattern_pure_black_exception"
        else:
            reason = "pattern_no_pattern_exception"
        adjacency_audit.append({
            "side_a": _side_record(pair[0], assignments, group_patterns),
            "side_b": _side_record(pair[1], assignments, group_patterns),
            "contact_px": contact_px,
            "boundary_drawn": pair in selected or pair in closure_pairs,
            "reason": reason,
        })

    report = {
        "white_stroke_mm": WHITE_STROKE_MM,
        "black_stroke_mm": BLACK_STROKE_MM,
        "render_px_per_mm": float(symbols.get("render_px_per_mm", RENDER_PX_PER_MM)),
        "white_stroke_px": white_px,
        "black_stroke_px": black_px,
        "active_priority_patterns": sorted(active_patterns),
        "selected_adjacencies": len(selected | closure_pairs),
        "closed_priority_contours": closed_contours,
        "black_closure_components": black_closure_components,
        "repaired_centerline_gap_bridges": repaired_gaps,
        "open_contour_endpoints": open_endpoints,
        "discarded_open_outline_pixels": discarded_open_outline_pixels,
        "boundary_centerline_px": int(np.count_nonzero(centerline)),
        "step7_artifacts_modified": False,
        "adjacencies": adjacency_audit,
    }
    (out_dir / "step8_boundaries.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    imwrite(out_dir / "step8_boundaries.png", result)
    imwrite(out_dir / "step8_white_stroke_mask.png", white_stroke_mask)
    imwrite(out_dir / "step8_black_stroke_mask.png", black_stroke_mask)

    debug = np.hstack((step7_canvas, result))
    if debug.shape[1] > 2200:
        scale = 2200 / debug.shape[1]
        debug = cv2.resize(debug, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step8_debug.png", debug)
    return {
        "out_dir": out_dir,
        "canvas_px": [result.shape[1], result.shape[0]],
        "active_patterns": sorted(active_patterns),
        "selected_adjacencies": len(selected | closure_pairs),
    }
