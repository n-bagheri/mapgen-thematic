"""Tactile pattern library: 5 groups (waves, dots, lines, grids, solids).

Patterns are procedural: `pattern_field` returns a boolean raster (True =
raised/black) for any canvas size, parameterized in millimetres so they render
identically at any resolution. Spacings respect the Step 0 minimum-gap
constant (~3 mm between raised elements).

The haptic feature vectors and distance are a documented PLACEHOLDER pending
the pattern-similarity matrix from discriminability literature or a user
study (open question 1 in PIPELINE.md). Structure: kind, orientation,
density, element size.
"""

from __future__ import annotations

import numpy as np

# kind, orientation (deg or None), density (elements per cm along main axis),
# element size (mm), human-readable description (legend text)
PATTERNS: dict[str, dict] = {
    "wave_sine":       {"group": "waves",  "kind": "wave",  "orient": 0,    "density": 2.0, "size": 0.8,
                        "desc": "sinusoidal wavy lines (water)"},
    "wave_triangle":   {"group": "waves",  "kind": "wave",  "orient": 0,    "density": 2.0, "size": 0.8,
                        "desc": "triangular zig-zag lines"},
    "dots_sparse":     {"group": "dots",   "kind": "dot",   "orient": None, "density": 1.7, "size": 1.5,
                        "desc": "sparse dots (6 mm grid)"},
    "dots_medium":     {"group": "dots",   "kind": "dot",   "orient": None, "density": 2.5, "size": 1.5,
                        "desc": "medium dots (4 mm grid)"},
    "dots_dense":      {"group": "dots",   "kind": "dot",   "orient": None, "density": 3.3, "size": 1.2,
                        "desc": "dense dots (3 mm grid)"},
    "lines_horizontal": {"group": "lines", "kind": "line",  "orient": 0,    "density": 2.5, "size": 0.8,
                        "desc": "horizontal lines (4 mm apart)"},
    "lines_vertical":  {"group": "lines",  "kind": "line",  "orient": 90,   "density": 2.5, "size": 0.8,
                        "desc": "vertical lines (4 mm apart)"},
    "lines_diagonal":  {"group": "lines",  "kind": "line",  "orient": 45,   "density": 2.5, "size": 0.8,
                        "desc": "diagonal lines (4 mm apart)"},
    "grid_square":     {"group": "grids",  "kind": "grid",  "orient": 0,    "density": 2.0, "size": 0.8,
                        "desc": "square grid (5 mm)"},
    "grid_cross":      {"group": "grids",  "kind": "grid",  "orient": 45,   "density": 2.0, "size": 0.8,
                        "desc": "diagonal cross-hatch (5 mm)"},
    "solid_black":     {"group": "solids", "kind": "solid", "orient": None, "density": 10,  "size": 10,
                        "desc": "solid raised surface"},
    "plain":           {"group": "none",   "kind": "none",  "orient": None, "density": 0,   "size": 0,
                        "desc": "smooth (no texture)"},
}

GROUPS: dict[str, list[str]] = {
    "waves": ["wave_sine", "wave_triangle"],
    "dots": ["dots_sparse", "dots_medium", "dots_dense"],
    "lines": ["lines_horizontal", "lines_vertical", "lines_diagonal"],
    "grids": ["grid_square", "grid_cross"],
    "solids": ["solid_black"],
}

# perceived-order ramps for classed-sequential data (low -> high)
ORDERED_RAMPS = {
    1: ["solid_black"],
    2: ["dots_sparse", "solid_black"],
    3: ["dots_sparse", "dots_dense", "solid_black"],
    4: ["dots_sparse", "dots_medium", "dots_dense", "solid_black"],
    5: ["plain", "dots_sparse", "dots_medium", "dots_dense", "solid_black"],
}


# --------------------------------------------------------------------------- fields

def _grids_mm(shape: tuple[int, int], px_per_mm: float) -> tuple[np.ndarray, np.ndarray]:
    h, w = shape
    y, x = np.mgrid[0:h, 0:w]
    return x / px_per_mm, y / px_per_mm


def _line_field(xx, yy, angle_deg: float, spacing: float, width: float) -> np.ndarray:
    a = np.deg2rad(angle_deg)
    t = xx * np.sin(a) + yy * np.cos(a)
    return (t % spacing) < width


def _dot_field(xx, yy, spacing: float, radius: float) -> np.ndarray:
    dx = (xx % spacing) - spacing / 2
    dy = (yy % spacing) - spacing / 2
    return dx * dx + dy * dy <= radius * radius


def _wave_field(xx, yy, spacing: float, width: float, amp: float, wavelen: float,
                triangular: bool) -> np.ndarray:
    if triangular:
        saw = np.abs((xx / wavelen) % 1.0 - 0.5) * 2  # 0..1 triangle
        off = amp * (saw * 2 - 1)
    else:
        off = amp * np.sin(2 * np.pi * xx / wavelen)
    return ((yy + off) % spacing) < width


def pattern_field(pid: str, shape: tuple[int, int], px_per_mm: float) -> np.ndarray:
    """Boolean raster (True = raised) for pattern `pid` at the given resolution."""
    xx, yy = _grids_mm(shape, px_per_mm)
    if pid == "plain":
        return np.zeros(shape, bool)
    if pid == "solid_black":
        return np.ones(shape, bool)
    if pid == "wave_sine":
        return _wave_field(xx, yy, spacing=5.0, width=0.8, amp=1.2, wavelen=8.0, triangular=False)
    if pid == "wave_triangle":
        return _wave_field(xx, yy, spacing=5.0, width=0.8, amp=1.2, wavelen=8.0, triangular=True)
    if pid == "dots_sparse":
        return _dot_field(xx, yy, spacing=6.0, radius=0.75)
    if pid == "dots_medium":
        return _dot_field(xx, yy, spacing=4.0, radius=0.75)
    if pid == "dots_dense":
        return _dot_field(xx, yy, spacing=3.0, radius=0.6)
    if pid == "lines_horizontal":
        return _line_field(xx, yy, 0, spacing=4.0, width=0.8)
    if pid == "lines_vertical":
        return _line_field(xx, yy, 90, spacing=4.0, width=0.8)
    if pid == "lines_diagonal":
        return _line_field(xx, yy, 45, spacing=4.0, width=0.8)
    if pid == "grid_square":
        return _line_field(xx, yy, 0, 5.0, 0.8) | _line_field(xx, yy, 90, 5.0, 0.8)
    if pid == "grid_cross":
        return _line_field(xx, yy, 45, 5.0, 0.8) | _line_field(xx, yy, -45, 5.0, 0.8)
    raise KeyError(pid)


# --------------------------------------------------------------------------- haptic distance

_KINDS = ["dot", "line", "grid", "wave", "solid", "none"]


def _features(pid: str) -> np.ndarray:
    p = PATTERNS[pid]
    kind_vec = [3.0 if p["kind"] == k else 0.0 for k in _KINDS]
    if p["orient"] is None:
        orient = [0.0, 0.0]
    else:
        a = np.deg2rad(p["orient"] * 2)  # orientation is pi-periodic for lines
        orient = [1.5 * np.cos(a), 1.5 * np.sin(a)]
    return np.array(kind_vec + orient + [0.6 * p["density"], 0.3 * p["size"]])


def haptic_distance(a: str, b: str) -> float:
    return float(np.linalg.norm(_features(a) - _features(b)))


def pick_pattern(group: str, used: list[str]) -> str:
    """Pattern from `group` maximizing the minimum haptic distance to `used`."""
    candidates = [p for p in GROUPS[group] if p not in used] or GROUPS[group]
    if not used:
        return candidates[0]
    return max(candidates, key=lambda p: min(haptic_distance(p, u) for u in used))
