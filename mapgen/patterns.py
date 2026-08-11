"""Step 7 tactile patterns backed by the Illustrator SVG pattern library.

The assignment layer still works with the same five semantic families used by
the pipeline: waves, dots/noise, lines, grids, and solids.  Regular textures
are rendered from the complete Illustrator ``<pattern>`` definitions; smooth
and pure-black fills are the two non-SVG special cases.

Haptic distance uses the supplied two-dimensional embedding in
``pattern_library/embedding.csv``. The SVG assets remain authoritative for
pattern geometry; the embedding is solely the pattern-choice metric.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from itertools import product
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
import xml.etree.ElementTree as ET

import cv2
import numpy as np

from pattern_library import PatternAsset, PatternLibrary, mm_to_pt


SVG_NAMESPACE = "http://www.w3.org/2000/svg"
PATTERN_DIRECTORY = Path(__file__).resolve().parents[1] / "pattern_library"
EMBEDDING_PATH = PATTERN_DIRECTORY / "embedding.csv"

# kind, orientation (degrees or None), approximate density (elements per cm),
# element size (mm), and human-readable legend description.  The metadata is
# used only for assignment/auditing; the actual marks always come from SVG.
PATTERNS: dict[str, dict] = {
    "01_noise_dots": {
        "group": "dots", "kind": "dot", "orient": None,
        "density": 1.7, "size": 1.5,
        "desc": "random dot noise",
    },
    "01_noise_splash": {
        "group": "dots", "kind": "dot", "orient": None,
        "density": 3.3, "size": 1.2,
        "desc": "dense splash noise",
    },
    "02_grid_checkers": {
        "group": "grids", "kind": "grid", "orient": 0,
        "density": 2.0, "size": 0.8,
        "desc": "checker grid",
    },
    "02_grid_dots": {
        "group": "grids", "kind": "grid", "orient": 0,
        "density": 2.5, "size": 1.5,
        "desc": "regular dot grid",
    },
    "03_lines_diagonal": {
        "group": "lines", "kind": "line", "orient": 45,
        "density": 2.5, "size": 0.8,
        "desc": "diagonal parallel lines",
    },
    "03_lines_vertical": {
        "group": "lines", "kind": "line", "orient": 90,
        "density": 2.5, "size": 0.8,
        "desc": "vertical parallel lines",
    },
    "04_waves_sine": {
        "group": "waves", "kind": "wave", "orient": 0,
        "density": 2.0, "size": 0.8,
        "desc": "sinusoidal wavy lines (water)",
    },
    "04_waves_triangle": {
        "group": "waves", "kind": "wave", "orient": 0,
        "density": 2.0, "size": 0.8,
        "desc": "triangular zig-zag lines",
    },
    "solid_black": {
        "group": "solids", "kind": "solid", "orient": None,
        "density": 10.0, "size": 10.0,
        "desc": "solid raised surface",
    },
    "plain": {
        "group": "none", "kind": "none", "orient": None,
        "density": 0.0, "size": 0.0,
        "desc": "smooth (no pattern fill)",
    },
}

GROUPS: dict[str, list[str]] = {
    "waves": ["04_waves_sine", "04_waves_triangle"],
    "dots": ["01_noise_dots", "01_noise_splash"],
    "lines": ["03_lines_vertical", "03_lines_diagonal"],
    "grids": ["02_grid_dots", "02_grid_checkers"],
    "solids": ["solid_black"],
}

# Perceived-order ramps for classed-sequential data (low -> high).  A map may
# use at most one regular pattern from each semantic family, so these ramps do
# not pair two noise/dot patterns (or two patterns from any other family).
# ``solid_black`` remains the high end of every ordered sequence.
ORDERED_RAMPS = {
    1: ["solid_black"],
    2: ["01_noise_dots", "solid_black"],
    3: ["01_noise_dots", "02_grid_dots", "solid_black"],
    4: ["plain", "01_noise_dots", "02_grid_dots", "solid_black"],
    5: ["plain", "01_noise_dots", "02_grid_dots", "03_lines_vertical", "solid_black"],
}

# Old run artifacts and focused boundary/cleanup callers may still contain the
# former procedural IDs.  They render through the nearest corresponding SVG
# asset, but new assignments never emit these aliases.
LEGACY_PATTERN_ALIASES = {
    "wave_sine": "04_waves_sine",
    "wave_triangle": "04_waves_triangle",
    "dots_sparse": "01_noise_dots",
    "dots_medium": "02_grid_dots",
    "dots_dense": "01_noise_splash",
    "lines_horizontal": "03_lines_vertical",
    "lines_vertical": "03_lines_vertical",
    "lines_diagonal": "03_lines_diagonal",
    "grid_square": "02_grid_dots",
    "grid_cross": "02_grid_checkers",
}


@lru_cache(maxsize=1)
def svg_pattern_library() -> PatternLibrary:
    """Return the process-wide library of Illustrator pattern assets."""

    library = PatternLibrary(PATTERN_DIRECTORY)
    expected = set(PATTERNS) - {"plain", "solid_black"}
    missing = expected - set(library.names)
    if missing:
        raise RuntimeError(
            "Step 7 SVG pattern library is incomplete: " + ", ".join(sorted(missing))
        )
    return library


def canonical_pattern_id(pattern_id: str) -> str:
    """Resolve a legacy procedural ID to its SVG asset name."""

    return LEGACY_PATTERN_ALIASES.get(pattern_id, pattern_id)


def pattern_info(pattern_id: str) -> dict:
    """Return assignment metadata for an SVG or special fill ID."""

    return PATTERNS[canonical_pattern_id(pattern_id)]


def _pattern_svg(asset: PatternAsset, shape: tuple[int, int], px_per_mm: float) -> str:
    """Create a carrier canvas around an unmodified copied pattern definition."""

    height_px, width_px = shape
    width_pt = mm_to_pt(width_px / px_per_mm)
    height_pt = mm_to_pt(height_px / px_per_mm)

    root = ET.Element(
        f"{{{SVG_NAMESPACE}}}svg",
        {
            "version": "1.1",
            "width": str(width_px),
            "height": str(height_px),
            "viewBox": f"0 0 {width_pt:.12g} {height_pt:.12g}",
        },
    )
    ET.SubElement(
        root,
        f"{{{SVG_NAMESPACE}}}rect",
        {"width": str(width_pt), "height": str(height_pt), "fill": "white"},
    )
    defs = ET.SubElement(root, f"{{{SVG_NAMESPACE}}}defs")
    fill = svg_pattern_library().copy_to_defs(asset.name, defs)
    carrier_attributes = {
        "width": str(width_pt),
        "height": str(height_pt),
        "fill": fill,
    }
    if asset.fill_opacity is not None:
        carrier_attributes["fill-opacity"] = asset.fill_opacity
    if asset.opacity is not None:
        carrier_attributes["opacity"] = asset.opacity
    ET.SubElement(root, f"{{{SVG_NAMESPACE}}}rect", carrier_attributes)
    return ET.tostring(root, encoding="unicode")


def render_pattern(
    pattern_id: str,
    shape: tuple[int, int],
    px_per_mm: float,
) -> np.ndarray:
    """Render one exact SVG pattern as a white-to-black uint8 canvas.

    SVG user units are treated as points, so pattern scale is independent of
    preview resolution and follows 72 pt = 25.4 mm.  Illustrator transforms,
    including large translations, rotations, and non-uniform scales, pass
    unchanged to the SVG renderer.
    """

    if px_per_mm <= 0:
        raise ValueError("px_per_mm must be positive")
    if len(shape) != 2 or shape[0] <= 0 or shape[1] <= 0:
        raise ValueError("shape must contain positive height and width")

    canonical_id = canonical_pattern_id(pattern_id)
    if canonical_id == "plain":
        return np.full(shape, 255, np.uint8)
    if canonical_id == "solid_black":
        return np.zeros(shape, np.uint8)
    if canonical_id not in PATTERNS:
        raise KeyError(pattern_id)

    try:
        import resvg_py
    except ImportError as exc:
        raise RuntimeError(
            "Rendering Step 7 Illustrator patterns requires resvg_py; "
            "install the project requirements"
        ) from exc

    asset = svg_pattern_library().get(canonical_id)
    svg = _pattern_svg(asset, shape, px_per_mm)
    png_bytes = resvg_py.svg_to_bytes(
        svg_string=svg,
        width=int(shape[1]),
        height=int(shape[0]),
    )
    decoded = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_UNCHANGED)
    if decoded is None:
        raise RuntimeError(f"SVG renderer returned an invalid image for {canonical_id}")
    if decoded.ndim == 2:
        gray = decoded
    elif decoded.shape[2] == 4:
        gray = cv2.cvtColor(decoded, cv2.COLOR_BGRA2GRAY)
    else:
        gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    if gray.shape != shape:
        gray = cv2.resize(gray, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
    return gray


def pattern_field(pattern_id: str, shape: tuple[int, int], px_per_mm: float) -> np.ndarray:
    """Compatibility boolean field derived from the SVG pattern renderer."""

    return render_pattern(pattern_id, shape, px_per_mm) < 128


# --------------------------------------------------------------------------- haptic distance

@lru_cache(maxsize=1)
def haptic_embeddings() -> Mapping[str, tuple[float, float]]:
    """Load and validate the supplied two-dimensional pattern embedding."""

    try:
        # ``utf-8-sig`` accepts Illustrator/Excel-style UTF-8 CSV exports with
        # a byte-order mark without altering the source file.
        with EMBEDDING_PATH.open(newline="", encoding="utf-8-sig") as embedding_file:
            rows = list(csv.DictReader(embedding_file))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing haptic embedding file: {EMBEDDING_PATH}") from exc

    expected_fields = {"item", "x1", "x2"}
    if not rows or not expected_fields.issubset(rows[0]):
        raise ValueError(
            f"{EMBEDDING_PATH} must contain item, x1, and x2 columns"
        )

    embeddings: dict[str, tuple[float, float]] = {}
    for row in rows:
        pattern_id = (row.get("item") or "").strip()
        if not pattern_id or pattern_id in embeddings:
            raise ValueError(f"Invalid or duplicate embedding ID: {pattern_id!r}")
        try:
            embeddings[pattern_id] = (float(row["x1"]), float(row["x2"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid embedding coordinates for {pattern_id!r}") from exc

    svg_pattern_ids = set(PATTERNS) - {"plain", "solid_black"}
    missing = svg_pattern_ids - set(embeddings)
    unexpected = set(embeddings) - svg_pattern_ids
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if unexpected:
            details.append("unexpected: " + ", ".join(sorted(unexpected)))
        raise ValueError(f"Embedding IDs do not match SVG patterns ({'; '.join(details)})")
    return MappingProxyType(embeddings)


def haptic_distance(first: str, second: str) -> float:
    """Euclidean distance between two SVG pattern coordinates."""

    first_id = canonical_pattern_id(first)
    second_id = canonical_pattern_id(second)
    embeddings = haptic_embeddings()
    try:
        first_point = embeddings[first_id]
        second_point = embeddings[second_id]
    except KeyError as exc:
        raise ValueError(
            "Haptic distance is defined only for embedded SVG patterns; "
            f"received {first!r} and {second!r}"
        ) from exc
    return float(np.hypot(first_point[0] - second_point[0], first_point[1] - second_point[1]))


def adjacent_group_pairs(group_map: np.ndarray) -> set[tuple[int, int]]:
    """Return distinct 4-neighbour adjacencies between non-negative groups."""

    if group_map.ndim != 2:
        raise ValueError("group_map must be a two-dimensional array")
    pairs: set[tuple[int, int]] = set()
    for first, second in (
        (group_map[:, :-1], group_map[:, 1:]),
        (group_map[:-1, :], group_map[1:, :]),
    ):
        touching = (first != second) & (first >= 0) & (second >= 0)
        if not np.any(touching):
            continue
        for left, right in zip(first[touching], second[touching]):
            a, b = int(left), int(right)
            pairs.add((a, b) if a < b else (b, a))
    return pairs


def optimize_adjacent_pattern_variants(
    group_map: np.ndarray,
    candidates_by_group: Mapping[int, tuple[str, ...] | list[str]],
) -> tuple[dict[int, str], dict]:
    """Globally maximize the worst embedded distance on patterned adjacencies.

    All valid variant combinations are evaluated simultaneously. Adjacencies
    involving ``plain`` or ``solid_black`` are excluded because those fills
    intentionally accept any neighbour. The primary objective is the minimum
    Euclidean distance over every remaining adjacency; mean adjacency distance
    is a deterministic secondary objective.
    """

    normalized_candidates: dict[int, tuple[str, ...]] = {}
    regular_family_owner: dict[str, int] = {}
    for raw_group_id, raw_candidates in sorted(candidates_by_group.items()):
        group_id = int(raw_group_id)
        candidates = tuple(dict.fromkeys(
            canonical_pattern_id(pattern) for pattern in raw_candidates
        ))
        if not candidates:
            raise ValueError(f"Group {group_id} has no pattern candidates")
        unknown = [pattern for pattern in candidates if pattern not in PATTERNS]
        if unknown:
            raise ValueError(f"Unknown patterns for group {group_id}: {unknown}")
        families = {PATTERNS[pattern]["group"] for pattern in candidates}
        if len(families) != 1:
            raise ValueError(
                f"Group {group_id} candidates span multiple families: {sorted(families)}"
            )
        family = next(iter(families))
        if family not in {"none", "solids"}:
            previous_owner = regular_family_owner.get(family)
            if previous_owner is not None and previous_owner != group_id:
                raise ValueError(
                    f"Pattern family {family!r} is assigned to groups "
                    f"{previous_owner} and {group_id}"
                )
            regular_family_owner[family] = group_id
        normalized_candidates[group_id] = candidates

    present_groups = {int(group_id) for group_id in np.unique(group_map) if group_id >= 0}
    missing_candidates = present_groups - set(normalized_candidates)
    if missing_candidates:
        raise ValueError(
            "Missing pattern candidates for groups: "
            + ", ".join(str(group_id) for group_id in sorted(missing_candidates))
        )

    group_ids = tuple(normalized_candidates)
    adjacency_pairs = adjacent_group_pairs(group_map)
    embeddings = haptic_embeddings()
    best_assignment: dict[int, str] | None = None
    best_key: tuple[float, float] | None = None
    combinations_evaluated = 0

    for combination in product(*(normalized_candidates[group_id] for group_id in group_ids)):
        assignment = dict(zip(group_ids, combination))
        distances = [
            haptic_distance(assignment[first], assignment[second])
            for first, second in adjacency_pairs
            if assignment[first] in embeddings and assignment[second] in embeddings
        ]
        score = (
            min(distances) if distances else float("-inf"),
            float(np.mean(distances)) if distances else float("-inf"),
        )
        combinations_evaluated += 1
        if best_key is None or score > best_key:
            best_key = score
            best_assignment = assignment

    if best_assignment is None:
        raise RuntimeError("No valid pattern assignment combinations were evaluated")

    adjacency_records = []
    for first, second in sorted(adjacency_pairs):
        first_pattern = best_assignment[first]
        second_pattern = best_assignment[second]
        eligible = first_pattern in embeddings and second_pattern in embeddings
        adjacency_records.append({
            "group_a": first,
            "group_b": second,
            "pattern_a": first_pattern,
            "pattern_b": second_pattern,
            "eligible": eligible,
            "distance": (
                round(haptic_distance(first_pattern, second_pattern), 6)
                if eligible else None
            ),
        })
    eligible_distances = [
        record["distance"] for record in adjacency_records if record["eligible"]
    ]
    audit = {
        "method": "global_exhaustive_adjacent_pattern_maximin",
        "objective": "maximize minimum embedding distance over eligible adjacency pairs",
        "tie_breaker": "maximize mean eligible adjacency distance, then candidate order",
        "combinations_evaluated": combinations_evaluated,
        "adjacency_pairs": len(adjacency_records),
        "eligible_pattern_adjacencies": len(eligible_distances),
        "excluded_plain_or_black_adjacencies": (
            len(adjacency_records) - len(eligible_distances)
        ),
        "minimum_distance": min(eligible_distances) if eligible_distances else None,
        "mean_distance": (
            round(float(np.mean(eligible_distances)), 6)
            if eligible_distances else None
        ),
        "edges": adjacency_records,
    }
    return best_assignment, audit


def pick_pattern(group: str, used: list[str]) -> str:
    """Choose the maximin-distance candidate from ``group``.

    Only SVG patterns participate in the embedding score. ``plain`` and
    ``solid_black`` are fixed fills with no embedding coordinates, so they do
    not distort a choice among SVG alternatives.
    """

    candidates = [pattern for pattern in GROUPS[group] if pattern not in used] or GROUPS[group]
    if len(candidates) == 1:
        return candidates[0]
    embeddings = haptic_embeddings()
    embedded_used = [
        canonical_pattern_id(pattern)
        for pattern in used
        if canonical_pattern_id(pattern) in embeddings
    ]
    if not embedded_used:
        return candidates[0]
    return max(
        candidates,
        key=lambda pattern: min(
            haptic_distance(pattern, used_pattern) for used_pattern in embedded_used
        ),
    )


__all__ = [
    "GROUPS",
    "ORDERED_RAMPS",
    "PATTERNS",
    "adjacent_group_pairs",
    "canonical_pattern_id",
    "haptic_distance",
    "haptic_embeddings",
    "optimize_adjacent_pattern_variants",
    "pattern_field",
    "pattern_info",
    "pick_pattern",
    "render_pattern",
    "svg_pattern_library",
]
