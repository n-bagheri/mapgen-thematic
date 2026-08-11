"""Step 4 -- Segmentation, text removal, and line extraction (merged).

Pure CV, no API calls. Every content pixel of map_area.png is assigned to a
class: legend swatch colors from Step 2 seed the assignment in Lab space;
pixels no seed explains are clustered into new non-thematic candidate classes
(sea not in the legend, border ink, ...). Coastlines are vectorized from the
geographic mask and dark river ridges are selected near reviewed river labels;
text, line pixels, speckle, and furniture gaps are then filled by nearest-region
growing so the final area map is continuous under everything removed.

Artifacts per map, under runs/<name>/:
    label_map.png        uint8 class index + 1 per pixel (0 = outside map)
    classes_final.json   index -> class metadata incl. area share and origin
    regions.geojson      area polygons (map_area pixel coords, y down)
    lines.geojson        line centerline polylines with kind
    coastline_cleanup_mask.png printed dark outline pixels excluded from areas
    river_cleanup_mask.png exact pixel-supported river ink excluded from areas
    step4_lines_preview.png extracted line centerlines on a separate canvas
    step4_text_removed_input.png pixels excluded before colour assignment
    step4_debug.png      original | reconstructed class map side by side
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from .isolate import imread, imwrite, to_lab, NAMED_COLORS, _NAMED_LAB
from .semantics import MapSemantics

UNSEEDED_DELTA = 16.0   # pixel farther than this from every seed -> needs a new cluster
MERGE_DELTA = 10.0      # new cluster centers closer than this join an existing class
SPECKLE_PX = 30         # smaller area components are noise -> refill
LINE_HALF_WIDTH = 4.0   # max distance-transform half-width for a line feature
MIN_LINE_LEN = 25       # px of estimated centerline
WATER_EDGE_DELTA = 18.0
RIVER_RIDGE_QUANTILE = 0.94
RIVER_MIN_COMPONENT_PX = 12
RIVER_LABEL_SEARCH_PAD = 18
RIVER_MIN_AXIS_ALIGNMENT = 0.45
RIVER_SECONDARY_SCORE_RATIO = 0.62
COAST_INNER_BAND_RATIO = 0.006
COAST_INNER_BAND_MIN = 3
COAST_INNER_BAND_MAX = 10
RIVER_INK_FRINGE_RADIUS = 2


# --------------------------------------------------------------------------- assignment

def build_seeds(classes_json: dict) -> list[dict]:
    seeds = []
    for c in classes_json["classes"]:
        if c.get("lab") is None:
            continue
        seeds.append({
            "label": c["label"], "lab": np.float32(c["lab"]), "rgb": c["rgb"],
            "is_thematic": bool(c["is_thematic"]), "priority": c.get("priority"),
            "source": "legend",
        })
    return seeds


def assign_pixels(lab_img: np.ndarray, seeds: list[dict], valid: np.ndarray) -> np.ndarray:
    h, w = lab_img.shape[:2]
    label_map = np.full((h, w), -1, np.int16)
    best = np.full((h, w), np.float32(np.inf))
    for i, s in enumerate(seeds):
        d = np.linalg.norm(lab_img - s["lab"], axis=2)
        upd = (d < best) & valid
        label_map[upd] = i
        best[upd] = d[upd]
    label_map[valid & (best > UNSEEDED_DELTA)] = -1
    return label_map


def _lab_to_rgb(lab: np.ndarray) -> list[int]:
    bgr = cv2.cvtColor(lab.reshape(1, 1, 3).astype(np.float32), cv2.COLOR_Lab2BGR).reshape(3)
    return [int(round(float(v) * 255)) for v in bgr[::-1]]


def cluster_unassigned(lab_img: np.ndarray, label_map: np.ndarray, valid: np.ndarray,
                       seeds: list[dict]) -> list[str]:
    """K-means the unexplained pixels into new candidate classes."""
    notes = []
    un = valid & (label_map < 0)
    n_un = int(np.count_nonzero(un))
    if n_un < SPECKLE_PX:
        return notes
    pix = lab_img[un].reshape(-1, 3).astype(np.float32)
    sample = pix[np.random.default_rng(0).choice(len(pix), min(len(pix), 80_000), replace=False)]
    k = min(6, max(1, len(sample) // 500))
    _, _, centers = cv2.kmeans(sample, k, None,
                               (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5),
                               4, cv2.KMEANS_PP_CENTERS)
    # merge near-duplicate centers, then map each center to an existing or new class
    kept: list[np.ndarray] = []
    for c in centers:
        if not any(np.linalg.norm(c - kc) < 6 for kc in kept):
            kept.append(c)
    target_idx = []
    for c in kept:
        d_seed = [float(np.linalg.norm(c - s["lab"])) for s in seeds]
        if d_seed and min(d_seed) < MERGE_DELTA:
            target_idx.append(int(np.argmin(d_seed)))
        else:
            name = min(_NAMED_LAB, key=lambda n: float(np.linalg.norm(c - _NAMED_LAB[n])))
            seeds.append({"label": f"unlabelled: {name}", "lab": c, "rgb": _lab_to_rgb(c),
                          "is_thematic": False, "priority": None, "source": "unseeded"})
            target_idx.append(len(seeds) - 1)
            notes.append(f"new non-legend class '{seeds[-1]['label']}'")
    dists = np.stack([np.linalg.norm(lab_img[un] - c, axis=1) for c in kept])
    label_map[un] = np.int16([target_idx[i] for i in np.argmin(dists, axis=0)])
    return notes


def reassign_water(label_map: np.ndarray, lab_img: np.ndarray, mask: np.ndarray,
                   seeds: list[dict], sem: MapSemantics) -> list[str]:
    """Big edge-touching components in water-like colors -> synthetic water class.

    Handles seas that are not legend entries (Iran) without stealing coastal
    land classes (color gate) on maps where water is the page background.
    """
    if not sem.water_present:
        return []
    hints = [n for n in NAMED_COLORS if any(wd in n for wd in ("blue", "cyan", "teal"))]
    water_words = ("water", "sea", "ocean", "lake", "gulf")
    for f in sem.non_thematic:
        if any(wd in f.name.lower() for wd in water_words) and f.color_hint in _NAMED_LAB:
            hints.append(f.color_hint)
    hint_labs = [_NAMED_LAB[n] for n in hints]

    # "outside the territory" = page background plus the dominant unseeded
    # surroundings class (e.g. white neighbouring countries inside a map frame)
    zone = (mask == 0).astype(np.uint8)
    total_px = max(1, int(np.count_nonzero(mask)))
    for i, s in enumerate(seeds):
        if s["source"] == "unseeded" and np.count_nonzero(label_map == i) > 0.15 * total_px:
            zone |= (label_map == i).astype(np.uint8)
    edge = cv2.dilate(zone, np.ones((9, 9), np.uint8)) > 0

    water_idx = None
    notes = []
    total = int(np.count_nonzero(mask))
    for cls_idx in range(len(seeds)):
        cls_mask = (label_map == cls_idx).astype(np.uint8)
        if not cls_mask.any():
            continue
        n, cc, stats, _ = cv2.connectedComponentsWithStats(cls_mask, connectivity=4)
        for i in range(1, n):
            x, y, cw, ch, area = stats[i]
            if area < 0.005 * total:
                continue
            comp = cc[y:y + ch, x:x + cw] == i
            edge_px = int(np.count_nonzero(edge[y:y + ch, x:x + cw] & comp))
            if edge_px < 300 and edge_px < 0.005 * area:
                continue
            mean_lab = lab_img[y:y + ch, x:x + cw][comp].mean(axis=0)
            if min(float(np.linalg.norm(mean_lab - hl)) for hl in hint_labs) > WATER_EDGE_DELTA:
                continue
            if water_idx is None:
                seeds.append({"label": "water (detected)", "lab": mean_lab.astype(np.float32),
                              "rgb": _lab_to_rgb(mean_lab), "is_thematic": False,
                              "priority": 1, "source": "water-heuristic"})
                water_idx = len(seeds) - 1
            sub = label_map[y:y + ch, x:x + cw]
            sub[comp] = water_idx
            notes.append(f"edge component ({area/total:.1%} of map) moved from "
                         f"'{seeds[cls_idx]['label']}' to water")
    return notes


# --------------------------------------------------------------------------- lines

def _trace_polylines(skel: np.ndarray) -> list[list[list[int]]]:
    """Skeleton raster -> list of polylines ([x, y] points), split at junctions."""
    pts = {(int(y), int(x)) for y, x in np.argwhere(skel > 0)}
    nbrs = {}
    for y, x in pts:
        nbrs[(y, x)] = [(y + dy, x + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                        if (dy or dx) and (y + dy, x + dx) in pts]
    deg = {p: len(v) for p, v in nbrs.items()}
    used: set[tuple] = set()
    lines = []

    def walk(a, b):
        path = [a, b]
        used.add((a, b)); used.add((b, a))
        while deg.get(path[-1], 0) == 2:
            nxt = [q for q in nbrs[path[-1]] if (path[-1], q) not in used]
            if not nxt:
                break
            used.add((path[-1], nxt[0])); used.add((nxt[0], path[-1]))
            path.append(nxt[0])
        return path

    starts = [p for p in pts if deg[p] != 2]
    for p in starts:
        for q in nbrs[p]:
            if (p, q) not in used:
                lines.append(walk(p, q))
    for p in pts:  # pure loops (no endpoint/junction)
        if deg[p] == 2 and not any((p, q) in used for q in nbrs[p]):
            lines.append(walk(p, nbrs[p][0]))

    out = []
    for path in lines:
        if len(path) < 4:
            continue
        arr = np.array([[x, y] for y, x in path], np.int32)
        approx = cv2.approxPolyDP(arr.reshape(-1, 1, 2), 1.2, False).reshape(-1, 2)
        if len(approx) >= 2:
            out.append(approx.tolist())
    return out


def _thin_component(component: np.ndarray) -> np.ndarray:
    """Skeletonize a cropped component with the zero border thinning needs."""
    padded = cv2.copyMakeBorder(
        (component > 0).astype(np.uint8) * 255, 1, 1, 1, 1,
        cv2.BORDER_CONSTANT, value=0)
    return cv2.ximgproc.thinning(padded)[1:-1, 1:-1]


def _component_half_width(component: np.ndarray) -> float:
    """Maximum interior radius, including a guaranteed exterior zero rim."""
    padded = cv2.copyMakeBorder(
        (component > 0).astype(np.uint8), 1, 1, 1, 1,
        cv2.BORDER_CONSTANT, value=0)
    return float(cv2.distanceTransform(padded, cv2.DIST_L2, 3).max())


def interior_line_kind(sem: MapSemantics) -> str:
    kinds = [ln.kind.value for ln in sem.lines if ln.kind.value not in ("coastline", "graticule")]
    for preferred in ("river", "road", "border"):
        if preferred in kinds:
            return preferred
    return "line"


HALO_TOL = 12.0
INK_DARKER_BY = 12.0

# BGR colours used only by the Step 4 line-layer preview.  The GeoJSON remains
# the authoritative output; this palette simply makes its feature kinds easy
# to distinguish without painting the lines back onto the segmented map.
LINE_PREVIEW_COLORS = {
    "river": (204, 105, 34),
    "road": (32, 145, 245),
    "border": (55, 55, 55),
    "border_or_coast": (55, 55, 55),
    "coastline": (92, 112, 48),
    "frame": (135, 135, 135),
    "line": (168, 87, 126),
}
LINE_PREVIEW_FALLBACK = (168, 87, 126)


def extract_lines(label_map: np.ndarray, lab_img: np.ndarray, mask: np.ndarray,
                  seeds: list[dict], sem: MapSemantics) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Thin components -> real ink lines (kept as polylines) or anti-aliasing
    halos between two regions (dissolved). Returns (line_mask, halo_mask, records)."""
    h, w = label_map.shape
    boundary_zone = cv2.dilate((mask == 0).astype(np.uint8), np.ones((11, 11), np.uint8)) > 0
    inner_kind = interior_line_kind(sem)
    line_mask = np.zeros((h, w), np.uint8)
    halo_mask = np.zeros((h, w), np.uint8)
    records: list[dict] = []
    for idx in range(len(seeds)):
        cls = (label_map == idx).astype(np.uint8)
        if not cls.any():
            continue
        n, cc, stats, _ = cv2.connectedComponentsWithStats(cls, connectivity=8)
        for i in range(1, n):
            x, y, cw, ch, area = stats[i]
            if area < 20:
                continue
            comp = (cc[y:y + ch, x:x + cw] == i).astype(np.uint8)
            halfw = _component_half_width(comp)
            length_est = area / max(1.0, 2 * halfw)
            if halfw > LINE_HALF_WIDTH or length_est < max(MIN_LINE_LEN, 10 * halfw):
                continue

            # neighbours: the two most common classes in a ring around the component
            px0, py0 = max(0, x - 4), max(0, y - 4)
            px1, py1 = min(w, x + cw + 4), min(h, y + ch + 4)
            pad_comp = np.zeros((py1 - py0, px1 - px0), np.uint8)
            pad_comp[y - py0:y - py0 + ch, x - px0:x - px0 + cw] = comp
            ring = (cv2.dilate(pad_comp, np.ones((7, 7), np.uint8)) > 0) & (pad_comp == 0)
            nb = label_map[py0:py1, px0:px1][ring]
            nb = nb[(nb >= 0) & (nb != idx)]
            comp_lab = lab_img[y:y + ch, x:x + cw][comp > 0].mean(axis=0)
            if len(nb):
                vals, counts = np.unique(nb, return_counts=True)
                order = np.argsort(-counts)
                a = seeds[int(vals[order[0]])]["lab"]
                b = seeds[int(vals[order[1]])]["lab"] if len(order) > 1 else a
                d_ca = float(np.linalg.norm(comp_lab - a))
                d_cb = float(np.linalg.norm(comp_lab - b))
                d_ab = float(np.linalg.norm(a - b))
                if d_ca + d_cb <= d_ab + HALO_TOL:
                    halo_mask[y:y + ch, x:x + cw] |= comp  # blend, not ink
                    continue
                if seeds[idx]["source"] == "legend" and \
                        comp_lab[0] > min(float(a[0]), float(b[0])) - INK_DARKER_BY:
                    continue  # a genuine narrow area of a legend class, keep it

            if cw > 0.92 * w and ch > 0.92 * h:
                kind = "frame"
            elif boundary_zone[y:y + ch, x:x + cw][comp > 0].mean() > 0.5:
                kind = "border_or_coast"
            else:
                kind = inner_kind
            skel = _thin_component(comp)
            for pl in _trace_polylines(skel):
                records.append({
                    "kind": kind, "source_class": seeds[idx]["label"],
                    "points": [[int(px + x), int(py + y)] for px, py in pl],
                })
            line_mask[y:y + ch, x:x + cw] |= comp
    return line_mask, halo_mask, records


def render_lines_preview(line_records: list[dict], mask: np.ndarray) -> np.ndarray:
    """Render extracted centerlines in their exact ``map_area.png`` positions.

    A very light outline supplies spatial context only.  It is deliberately
    much lighter than every extracted feature so it cannot be confused with a
    detected line.  This preview is diagnostic and is never consumed by a
    later pipeline step.
    """
    h, w = mask.shape[:2]
    preview = np.full((h, w, 3), 255, np.uint8)
    contours, _ = cv2.findContours(
        (mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(preview, contours, -1, (225, 229, 235), 1, cv2.LINE_AA)

    for record in line_records:
        points = np.asarray(record.get("points", []), dtype=np.int32).reshape(-1, 2)
        if len(points) < 2:
            continue
        color = LINE_PREVIEW_COLORS.get(record.get("kind"), LINE_PREVIEW_FALLBACK)
        cv2.polylines(preview, [points.reshape(-1, 1, 2)], False, color, 2, cv2.LINE_AA)
    return preview


def _boundary_line_kind(sem: MapSemantics) -> str | None:
    kinds = {line.kind.value for line in sem.lines}
    has_coast = "coastline" in kinds
    has_border = "border" in kinds
    if has_coast and has_border:
        return "border_or_coast"
    if has_coast:
        return "coastline"
    if has_border:
        return "border"
    return None


def _polyline_length(points: np.ndarray) -> float:
    if len(points) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(points.astype(np.float32), axis=0), axis=1).sum())


def extract_coastline_cleanup_mask(image: np.ndarray, mask: np.ndarray) \
        -> tuple[np.ndarray, dict]:
    """Dark printed outline pixels just inside the geographic silhouette.

    Geometry, not colour alone, supplies the safeguard: candidates must live
    in a narrow inward band and be connected to the actual outside edge. A
    legitimate dark thematic patch may enter the mask, but its pixels farther
    inward remain classified and therefore refill the cleaned rim as dark.
    """
    h, w = mask.shape[:2]
    diagonal = float(np.hypot(h, w))
    band_width = int(np.clip(round(COAST_INNER_BAND_RATIO * diagonal),
                             COAST_INNER_BAND_MIN, COAST_INNER_BAND_MAX))
    inside = mask > 0
    if not inside.any():
        return np.zeros((h, w), np.uint8), {
            "method": "adaptive-inner-dark-edge-band",
            "band_width_px": band_width,
            "pixels": 0,
        }

    distance = cv2.distanceTransform(inside.astype(np.uint8), cv2.DIST_L2, 5)
    inner_band = inside & (distance <= band_width)
    outside = (~inside).astype(np.uint8)
    touches_edge = inside & (cv2.dilate(outside, np.ones((3, 3), np.uint8)) > 0)

    smooth = cv2.medianBlur(image, 3)
    gray = cv2.cvtColor(smooth, cv2.COLOR_BGR2GRAY)
    spread = smooth.max(axis=2).astype(np.int16) - smooth.min(axis=2).astype(np.int16)
    # Very dark pixels can be mildly chromatic because of print/scan mixing;
    # lighter anti-aliasing is accepted only when nearly neutral.
    dark_core = inner_band & ((gray <= 100) | ((gray <= 150) & (spread <= 50)))
    dark_core = cv2.morphologyEx(
        dark_core.astype(np.uint8), cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))

    kept = np.zeros((h, w), np.uint8)
    count, components, stats, _ = cv2.connectedComponentsWithStats(dark_core, connectivity=8)
    for component_id in range(1, count):
        component = components == component_id
        if int(stats[component_id, cv2.CC_STAT_AREA]) >= 2 and np.any(component & touches_edge):
            kept[component] = 1

    # Include the gray antialiased rim immediately beside confirmed black ink,
    # without absorbing saturated coastal thematic colours.
    fringe_zone = cv2.dilate(kept, np.ones((3, 3), np.uint8)) > 0
    fringe = inner_band & fringe_zone & (gray <= 180) & (spread <= 70)
    cleanup = ((kept > 0) | fringe).astype(np.uint8) * 255
    return cleanup, {
        "method": "adaptive-inner-dark-edge-band",
        "band_width_px": band_width,
        "pixels": int(np.count_nonzero(cleanup)),
    }


def extract_river_cleanup_mask(image: np.ndarray, mask: np.ndarray,
                               supported_centerlines: np.ndarray) \
        -> tuple[np.ndarray, dict]:
    """Expand pixel-supported automatic rivers onto connected dark fringe ink.

    The one-pixel centerline is trusted because it came from an image ridge.
    Its two-pixel neighbourhood is accepted only where source pixels remain
    dark/neutral. Modelled graph bridges and reviewed/manual paths are absent
    from ``supported_centerlines`` and therefore cannot erase area colours.
    """
    h, w = mask.shape[:2]
    base = (supported_centerlines > 0) & (mask > 0)
    if not base.any():
        return np.zeros((h, w), np.uint8), {
            "method": "pixel-supported-dark-fringe",
            "fringe_radius_px": RIVER_INK_FRINGE_RADIUS,
            "centerline_pixels": 0,
            "fringe_pixels": 0,
            "pixels": 0,
        }
    smooth = cv2.medianBlur(image, 3)
    gray = cv2.cvtColor(smooth, cv2.COLOR_BGR2GRAY)
    spread = smooth.max(axis=2).astype(np.int16) - smooth.min(axis=2).astype(np.int16)
    ink_like = (((gray <= 100) & (spread <= 90)) |
                ((gray <= 175) & (spread <= 65)))
    size = 2 * RIVER_INK_FRINGE_RADIUS + 1
    expanded = cv2.dilate(
        base.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))) > 0
    fringe = expanded & ~base & ink_like & (mask > 0)
    cleanup = (base | fringe).astype(np.uint8) * 255
    return cleanup, {
        "method": "pixel-supported-dark-fringe",
        "fringe_radius_px": RIVER_INK_FRINGE_RADIUS,
        "centerline_pixels": int(np.count_nonzero(base)),
        "fringe_pixels": int(np.count_nonzero(fringe)),
        "pixels": int(np.count_nonzero(cleanup)),
    }


def _river_label_box(label: dict, width: int, height: int) -> tuple[int, int, int, int] | None:
    box = label.get("box")
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    x0, y0, x1, y1 = (int(round(float(value))) for value in box)
    x0, x1 = sorted((max(0, min(width, x0)), max(0, min(width, x1))))
    y0, y1 = sorted((max(0, min(height, y0)), max(0, min(height, y1))))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _component_axis(points_xy: np.ndarray) -> np.ndarray | None:
    """Principal direction of one skeleton component, or None if degenerate."""
    if len(points_xy) < 3:
        return None
    covariance = np.cov(points_xy.astype(np.float32).T)
    if covariance.shape != (2, 2) or not np.isfinite(covariance).all():
        return None
    values, vectors = np.linalg.eigh(covariance)
    return vectors[:, int(np.argmax(values))]


def _label_seeded_river_skeletons(image: np.ndarray, mask: np.ndarray,
                                  raw_text_mask: np.ndarray, labels: list[dict]) \
        -> tuple[np.ndarray, dict[int, list[dict]], list[dict]]:
    """Find dark, thin ridges only where reviewed river labels provide evidence.

    The ridge detector supplies pixel geometry; label boxes merely select nearby
    candidates and never draw a line.  This deliberately returns fragments when
    printed lettering interrupts a river instead of guessing a connection.
    """
    from skimage.filters import frangi
    from skimage.graph import route_through_array
    from skimage.morphology import skeletonize

    h, w = mask.shape[:2]
    river_labels = [label for label in labels if label.get("kind") == "river_label"]
    valid_labels = [(label, _river_label_box(label, w, h)) for label in river_labels]
    valid_labels = [(label, box) for label, box in valid_labels if box is not None]
    if not valid_labels:
        return np.zeros((h, w), np.uint8), {}, []

    gray = cv2.cvtColor(cv2.medianBlur(image, 3), cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    response = frangi(gray, sigmas=(0.8, 1.2, 1.8, 2.4), black_ridges=True)

    # Keep coast/border ink and every text glyph out of the candidate raster.
    valid = cv2.erode((mask > 0).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    valid &= ~(cv2.dilate((raw_text_mask > 0).astype(np.uint8),
                         np.ones((3, 3), np.uint8)) > 0)
    for label in labels:
        box = _river_label_box(label, w, h)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        valid[max(0, y0 - 2):min(h, y1 + 2), max(0, x0 - 2):min(w, x1 + 2)] = False

    values = response[valid]
    if not len(values) or float(values.max()) <= 0:
        return np.zeros((h, w), np.uint8), {}, [label for label, _ in valid_labels]
    threshold = max(0.01, float(np.quantile(values, RIVER_RIDGE_QUANTILE)))
    skeleton = skeletonize((response >= threshold) & valid).astype(np.uint8)
    count, components, stats, _ = cv2.connectedComponentsWithStats(skeleton, connectivity=8)

    component_points: dict[int, np.ndarray] = {}
    component_axes: dict[int, np.ndarray | None] = {}
    for component_id in range(1, count):
        if int(stats[component_id, cv2.CC_STAT_AREA]) < RIVER_MIN_COMPONENT_PX:
            continue
        ys, xs = np.nonzero(components == component_id)
        points = np.column_stack((xs, ys)).astype(np.float32)
        component_points[component_id] = points
        component_axes[component_id] = _component_axis(points)

    selected: dict[int, list[dict]] = {}
    bridge_paths: list[tuple[np.ndarray, dict]] = []
    unmatched: list[dict] = []
    for label, (x0, y0, x1, y1) in valid_labels:
        pad = max(RIVER_LABEL_SEARCH_PAD, int(round(0.45 * max(x1 - x0, y1 - y0))))
        xa, ya = max(0, x0 - pad), max(0, y0 - pad)
        xb, yb = min(w, x1 + pad), min(h, y1 + pad)
        ids, hits = np.unique(components[ya:yb, xa:xb], return_counts=True)

        box_w, box_h = x1 - x0, y1 - y0
        desired = (np.array([1.0, 0.0], np.float32) if box_w > 1.3 * box_h else
                   np.array([0.0, 1.0], np.float32) if box_h > 1.3 * box_w else None)
        candidates: list[tuple[float, int]] = []
        for component_id, local_hits in zip(ids.tolist(), hits.tolist()):
            if component_id == 0 or component_id not in component_points or local_hits < 2:
                continue
            axis = component_axes[component_id]
            alignment = (abs(float(axis @ desired))
                         if desired is not None and axis is not None else 0.7)
            if desired is not None and alignment < RIVER_MIN_AXIS_ALIGNMENT:
                continue
            length = int(stats[component_id, cv2.CC_STAT_AREA])
            score = 2.0 * local_hits + 0.08 * min(length, 200) + 20.0 * alignment
            candidates.append((score, component_id))

        if not candidates:
            unmatched.append(label)
            continue
        candidates.sort(reverse=True)
        best_score, best_id = candidates[0]
        chosen_ids = [best_id]

        # Text often cuts one physical river into two ridge components. Choose
        # a second, well-scored component only when it lies on the opposite
        # side of the label along the apparent line direction.
        center = np.array([(x0 + x1) / 2, (y0 + y1) / 2], np.float32)
        axis = desired if desired is not None else component_axes.get(best_id)
        if axis is not None:
            best_projection = float((component_points[best_id].mean(axis=0) - center) @ axis)
            for score, component_id in candidates[1:]:
                projection = float((component_points[component_id].mean(axis=0) - center) @ axis)
                if score >= RIVER_SECONDARY_SCORE_RATIO * best_score and \
                        best_projection * projection < 0:
                    chosen_ids.append(component_id)
                    break

        evidence = {
            "text": label.get("final_text") or label.get("text") or "river",
            "box": [x0, y0, x1, y1],
        }
        for component_id in chosen_ids:
            selected.setdefault(component_id, []).append(evidence)

        if len(chosen_ids) == 2:
            first = component_points[chosen_ids[0]]
            second = component_points[chosen_ids[1]]
            # Endpoints nearest the covered label are the bridge terminals.
            a = first[int(np.argmin(np.sum((first - center) ** 2, axis=1)))]
            b = second[int(np.argmin(np.sum((second - center) ** 2, axis=1)))]
            distance = float(np.linalg.norm(b - a))
            max_bridge = 2.2 * (max(x1 - x0, y1 - y0) + pad)
            if 3 < distance <= max_bridge:
                bx0, by0 = np.floor(np.minimum(a, b) - 8).astype(int)
                bx1, by1 = np.ceil(np.maximum(a, b) + 9).astype(int)
                bx0, by0 = max(0, bx0), max(0, by0)
                bx1, by1 = min(w, bx1), min(h, by1)
                if bx1 > bx0 and by1 > by0:
                    ridge_scale = max(0.01, float(np.quantile(response[valid], 0.99)))
                    likelihood = np.clip(response[by0:by1, bx0:bx1] / ridge_scale, 0, 1)
                    cost = 1.0 + 7.0 * (1.0 - likelihood)
                    cost[~(mask[by0:by1, bx0:bx1] > 0)] = 1_000.0
                    start = (int(round(a[1])) - by0, int(round(a[0])) - bx0)
                    end = (int(round(b[1])) - by0, int(round(b[0])) - bx0)
                    try:
                        route, _ = route_through_array(
                            cost, start, end, fully_connected=True, geometric=True)
                    except ValueError:
                        route = []
                    if route:
                        bridge_paths.append((np.asarray(
                            [[px + bx0, py + by0] for py, px in route], np.int32), evidence))

    selected_components = np.where(np.isin(components, list(selected)), components, 0)
    # Give each bridge a fresh component id so downstream tracing keeps its
    # provenance and can expose it as a separately reviewable segment.
    next_component_id = int(selected_components.max()) + 1
    for bridge, evidence in bridge_paths:
        bridge_canvas = np.zeros((h, w), np.uint8)
        cv2.polylines(bridge_canvas, [bridge.reshape(-1, 1, 2)], False, 1, 1, cv2.LINE_8)
        selected_components[bridge_canvas > 0] = next_component_id
        selected[next_component_id] = [{**evidence, "graph_bridge": True}]
        next_component_id += 1
    return selected_components.astype(np.int32), selected, unmatched


def extract_cartographic_lines(image: np.ndarray, mask: np.ndarray, raw_text_mask: np.ndarray,
                                sem: MapSemantics, labels: list[dict]) \
        -> tuple[np.ndarray, list[dict], dict]:
    """Extract actual linework independently from thematic colour regions.

    External coast/border geometry comes from the geographic mask. Interior
    rivers come from an image ridge detector, but are accepted only near a
    reviewed river-label occurrence. This prevents narrow thematic patches
    from being promoted to rivers across the map.
    """
    h, w = mask.shape[:2]
    diag = float(np.hypot(h, w))
    line_mask = np.zeros((h, w), np.uint8)
    records: list[dict] = []
    diagnostic = {
        "method": "mask-boundary-and-label-seeded-ridge-graph",
        "boundary_features": 0,
        "river_label_seeds": 0,
        "river_components": 0,
        "river_features": 0,
        "pixel_supported_river_features": 0,
        "graph_bridge_features": 0,
        "unmatched_river_labels": [],
        "omitted_unconfirmed_interior": False,
    }

    # A coastline/border is a property of the geographic silhouette, not a
    # dark thematic class. RETR_EXTERNAL keeps islands as separate outlines.
    boundary_kind = _boundary_line_kind(sem)
    if boundary_kind:
        contours, _ = cv2.findContours(
            (mask > 0).astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        for contour in contours:
            if cv2.arcLength(contour, True) < max(24.0, 0.025 * diag):
                continue
            approx = cv2.approxPolyDP(contour, 1.0, True).reshape(-1, 2)
            if len(approx) < 3:
                continue
            points = approx.tolist()
            points.append(points[0])
            records.append({
                "kind": boundary_kind,
                "source_class": "geographic mask boundary",
                "source": "map_mask",
                "confidence": "high",
                "label_evidence": None,
                "points": points,
            })
            diagnostic["boundary_features"] += 1

    semantic_kinds = {line.kind.value for line in sem.lines}
    river_labels = [label for label in labels if label.get("kind") == "river_label"]
    diagnostic["river_label_seeds"] = len(river_labels)
    if "river" not in semantic_kinds or not river_labels:
        diagnostic["omitted_unconfirmed_interior"] = "river" in semantic_kinds
        return line_mask, records, diagnostic

    selected_components, selected, unmatched = _label_seeded_river_skeletons(
        image, mask, raw_text_mask, labels)
    diagnostic["river_components"] = len(selected)
    diagnostic["unmatched_river_labels"] = [
        {"text": label.get("final_text") or label.get("text"), "box": label.get("box")}
        for label in unmatched
    ]
    diagnostic["omitted_unconfirmed_interior"] = bool(unmatched)
    for component_id, evidence in selected.items():
        component = (selected_components == component_id).astype(np.uint8)
        if not component.any():
            continue
        label_names = list(dict.fromkeys(item["text"] for item in evidence))
        is_graph_bridge = any(item.get("graph_bridge") for item in evidence)
        for points in _trace_polylines(component):
            points_array = np.asarray(points, np.int32)
            if _polyline_length(points_array) < max(10.0, 0.008 * diag):
                continue
            records.append({
                "kind": "river",
                "source_class": ("label-guided shortest path across a text gap"
                                 if is_graph_bridge else "label-seeded dark ridge skeleton"),
                "source": "image_processing",
                "confidence": "label-guided" if is_graph_bridge else "pixel-derived",
                "label_evidence": label_names,
                "points": points,
            })
            diagnostic["river_features"] += 1
            if is_graph_bridge:
                diagnostic["graph_bridge_features"] += 1
            else:
                # Only source-image-supported centerlines may influence area
                # segmentation. Graph bridges remain vector geometry only.
                cv2.polylines(line_mask, [points_array.reshape(-1, 1, 2)],
                              False, 255, 1, cv2.LINE_8)
                diagnostic["pixel_supported_river_features"] += 1

    return line_mask, records, diagnostic


# --------------------------------------------------------------------------- fill + vectorize

def fill_holes_nearest(label_map: np.ndarray, mask: np.ndarray) -> int:
    """Assign every masked-but-unlabelled pixel the class of its nearest labelled one."""
    hole = (mask > 0) & (label_map < 0)
    n_hole = int(np.count_nonzero(hole))
    if n_hole == 0:
        return 0
    src = (label_map < 0).astype(np.uint8)  # zero = known
    if not (src == 0).any():
        return 0
    _, lbls = cv2.distanceTransformWithLabels(src, cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
    zeros_yx = np.argwhere(src == 0)  # row-major order matches label numbering
    nearest_cls = label_map[zeros_yx[:, 0], zeros_yx[:, 1]]
    label_map[hole] = nearest_cls[lbls[hole] - 1]
    return n_hole


def polygonize(label_map: np.ndarray, idx: int, cls_label: str, epsilon: float = 1.0) -> list[dict]:
    m = (label_map == idx).astype(np.uint8)
    if not m.any():
        return []
    contours, hierarchy = cv2.findContours(m, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]
    feats = []
    outers: dict[int, list] = {}
    holes: dict[int, list] = {}
    for ci, c in enumerate(contours):
        if len(c) < 3:
            continue
        approx = cv2.approxPolyDP(c, epsilon, True).reshape(-1, 2)
        if len(approx) < 3:
            continue
        ring = approx.tolist() + [approx[0].tolist()]
        parent = hierarchy[ci][3]
        if parent == -1:
            outers[ci] = ring
        else:
            holes.setdefault(parent, []).append(ring)
    for ci, outer in outers.items():
        area = cv2.contourArea(contours[ci])
        if area < 9:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"class": cls_label, "class_index": idx, "area_px": float(area)},
            "geometry": {"type": "Polygon", "coordinates": [outer] + holes.get(ci, [])},
        })
    return feats


# --------------------------------------------------------------------------- runner

def run_step4(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .textdetect import run_step3

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "text_mask.png").exists():
        run_step3(image_path, model=model, runs_dir=runs_dir)

    sem = MapSemantics.model_validate_json((out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    geo = json.loads((out_dir / "geometry.json").read_text(encoding="utf-8"))
    classes_json = json.loads((out_dir / "classes.json").read_text(encoding="utf-8"))
    img = imread(out_dir / "map_area.png")
    h, w = img.shape[:2]
    mask = imread(out_dir / "map_mask.png")[..., 0]
    from .labelreview import write_text_removal_mask
    removal_mask, removal_metadata = write_text_removal_mask(out_dir)
    raw_text_mask = imread(out_dir / "text_mask.png")[..., 0]
    labels_path = out_dir / ("approved_labels.json" if (out_dir / "approved_labels.json").exists()
                             else "labels.json")
    labels_payload = json.loads(labels_path.read_text(encoding="utf-8"))
    line_labels = labels_payload.get("labels", [])

    # content mask: close interior holes (pale classes the bg threshold ate),
    # but keep furniture areas out and refill them from their surroundings
    filled = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    ff = np.zeros((h + 4, w + 4), np.uint8)
    cv2.floodFill(filled, ff, (0, 0), 255)  # 1px zero border: seed is never content
    mask = mask | cv2.bitwise_not(filled[1:-1, 1:-1])
    cx0, cy0 = geo["map_crop"][0], geo["map_crop"][1]
    furniture_hole = np.zeros((h, w), np.uint8)
    for f in geo["furniture"]:
        fx0, fy0, fx1, fy1 = f["box"]
        lx0, ly0 = max(0, fx0 - cx0 - 2), max(0, fy0 - cy0 - 2)
        lx1, ly1 = min(w, fx1 - cx0 + 2), min(h, fy1 - cy0 + 2)
        if lx1 > lx0 and ly1 > ly0:
            furniture_hole[ly0:ly1, lx0:lx1] = mask[ly0:ly1, lx0:lx1]

    # flatten scan noise before any color work: median kills salt-and-pepper,
    # mean-shift posterizes JPEG gradients and anti-aliasing into flat regions
    smoothed = cv2.pyrMeanShiftFiltering(cv2.medianBlur(img, 3), sp=6, sr=14)
    lab_img = to_lab(smoothed)
    if _boundary_line_kind(sem):
        coastline_cleanup, coastline_diagnostic = extract_coastline_cleanup_mask(img, mask)
    else:
        coastline_cleanup = np.zeros((h, w), np.uint8)
        coastline_diagnostic = {
            "method": "not-applicable-no-external-boundary",
            "band_width_px": 0,
            "pixels": 0,
        }
    imwrite(out_dir / "coastline_cleanup_mask.png", coastline_cleanup)
    removed = ((removal_mask > 0) | (furniture_hole > 0) |
               (coastline_cleanup > 0))
    valid = (mask > 0) & ~removed
    excluded_preview = img.copy()
    excluded_preview[mask == 0] = 255
    excluded_preview[removed] = 255
    imwrite(out_dir / "step4_text_removed_input.png", excluded_preview)

    seeds = build_seeds(classes_json)
    notes: list[str] = [
        f"text removal {removal_metadata['mode']}: "
        f"{removal_metadata['precise_stroke_labels']} precise, "
        f"{removal_metadata['whole_box_labels']} whole-box, "
        f"{removal_metadata['kept_labels']} kept",
        f"coastline ink cleanup: {coastline_diagnostic['pixels']} pixels in a "
        f"{coastline_diagnostic['band_width_px']} px inward band",
    ]
    label_map = assign_pixels(lab_img, seeds, valid)
    notes += cluster_unassigned(lab_img, label_map, valid, seeds)

    # Colour-component analysis is retained only for anti-aliasing halos. Its
    # former line records were false positives whenever a thematic region was
    # narrow and elongated. Actual linework is now extracted independently
    # from the original image and geographic mask.
    _, halo_mask, _ = extract_lines(label_map, lab_img, mask, seeds, sem)
    line_mask, line_records, line_diagnostic = extract_cartographic_lines(
        img, mask, raw_text_mask, sem, line_labels)
    river_cleanup, river_cleanup_diagnostic = extract_river_cleanup_mask(
        img, mask, line_mask)
    imwrite(out_dir / "river_cleanup_mask.png", river_cleanup)
    line_diagnostic["coastline_cleanup"] = coastline_diagnostic
    line_diagnostic["river_cleanup"] = river_cleanup_diagnostic
    label_map[(river_cleanup > 0) | (halo_mask > 0)] = -1
    notes.append(
        f"line extraction: {line_diagnostic['boundary_features']} mask-derived boundary, "
        f"{line_diagnostic['river_features']} label-seeded pixel river features"
    )
    notes.append(
        f"river ink cleanup: {river_cleanup_diagnostic['centerline_pixels']} supported "
        f"centerline pixels + {river_cleanup_diagnostic['fringe_pixels']} dark fringe pixels"
    )
    if line_diagnostic["omitted_unconfirmed_interior"]:
        notes.append("some interior river labels had no reliable nearby pixel ridge")

    # unseeded classes must earn their existence as coherent regions; their
    # scattered slivers are blend noise and dissolve into the classes around them
    sliver_max = max(300, int(2e-4 * np.count_nonzero(mask)))
    unseeded_slivers = 0
    for i, s in enumerate(seeds):
        if s["source"] != "unseeded":
            continue
        cls = (label_map == i).astype(np.uint8)
        if not cls.any():
            continue
        n, cc, stats, _ = cv2.connectedComponentsWithStats(cls, connectivity=8)
        for k in range(1, n):
            x, y, cw, ch, area = stats[k]
            if area < sliver_max:
                sub = label_map[y:y + ch, x:x + cw]
                sub[cc[y:y + ch, x:x + cw] == k] = -1
                unseeded_slivers += 1
    if unseeded_slivers:
        notes.append(f"dissolved {unseeded_slivers} unseeded sliver components")

    notes += reassign_water(label_map, lab_img, mask, seeds, sem)

    # speckle removal: tiny leftovers dissolve into their surroundings
    speckle = 0
    occupied = (label_map >= 0).astype(np.uint8)
    n, cc, stats, _ = cv2.connectedComponentsWithStats(occupied, connectivity=4)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < SPECKLE_PX:
            label_map[cc == i] = -1
            speckle += 1

    n_filled = fill_holes_nearest(label_map, mask)
    uncovered = int(np.count_nonzero((mask > 0) & (label_map < 0)))
    if uncovered:
        notes.append(f"{uncovered} content pixels left unassigned after fill")

    # ---- artifacts ----
    total = max(1, int(np.count_nonzero(mask)))
    classes_final = []
    for i, s in enumerate(seeds):
        area = int(np.count_nonzero(label_map == i))
        classes_final.append({
            "index": i, "label": s["label"], "rgb": s["rgb"],
            "is_thematic": s["is_thematic"], "priority": s["priority"],
            "source": s["source"], "area_px": area, "area_share": round(area / total, 4),
        })
    (out_dir / "classes_final.json").write_text(
        json.dumps({"classes": classes_final, "notes": notes}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    imwrite(out_dir / "label_map.png", (label_map + 1).astype(np.uint8))

    feats = []
    for i, s in enumerate(seeds):
        feats += polygonize(label_map, i, s["label"])
    (out_dir / "regions.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "features": feats,
    }), encoding="utf-8")
    automatic_lines = {
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "extraction": line_diagnostic,
        "features": [{
            "type": "Feature",
            "properties": {
                "kind": r["kind"],
                "source_class": r["source_class"],
                "source": r.get("source"),
                "confidence": r.get("confidence"),
                "label_evidence": r.get("label_evidence"),
            },
            "geometry": {"type": "LineString", "coordinates": r["points"]},
        } for r in line_records],
    }
    (out_dir / "lines_auto.geojson").write_text(
        json.dumps(automatic_lines, indent=2, ensure_ascii=False), encoding="utf-8")
    from .linereview import materialize_review
    review_path = out_dir / "line_review.json"
    saved_review = (json.loads(review_path.read_text(encoding="utf-8"))
                    if review_path.exists() else None)
    active_lines = materialize_review(automatic_lines, saved_review)
    (out_dir / "lines.geojson").write_text(
        json.dumps(active_lines, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "approved_lines.geojson").write_text(
        json.dumps(active_lines, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "line_extraction.json").write_text(
        json.dumps(line_diagnostic, indent=2, ensure_ascii=False), encoding="utf-8")
    active_line_records = [{
        **feature.get("properties", {}),
        "points": feature.get("geometry", {}).get("coordinates", []),
    } for feature in active_lines.get("features", [])]
    imwrite(out_dir / "step4_lines_preview.png", render_lines_preview(active_line_records, mask))

    # ---- debug: original | reconstruction ----
    # extracted ink lines are intentionally NOT drawn: they are not part of the
    # tactile output (lines.geojson still records them for Step 5's opt-in)
    recon = np.full((h, w, 3), 255, np.uint8)
    for i, s in enumerate(seeds):
        recon[label_map == i] = np.uint8(s["rgb"][::-1])
    # Human-readable rendering of the exact indexed raster consumed by Step 5.
    # This contains no source-image backdrop or debug annotations.
    imwrite(out_dir / "label_map_preview.png", recon)
    dbg = np.hstack([img, recon])
    if dbg.shape[1] > 2000:
        s = 2000 / dbg.shape[1]
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step4_debug.png", dbg)

    kinds: dict[str, int] = {}
    for r in active_line_records:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {
        "out_dir": out_dir, "classes": classes_final, "notes": notes,
        "polygons": len(feats), "polylines": len(active_line_records), "line_kinds": kinds,
        "filled_px": n_filled, "speckles": speckle,
    }
