"""Step 4 -- Segmentation, text removal, and line extraction (merged).

Pure CV, no API calls. Every content pixel of map_area.png is assigned to a
class: legend swatch colors from Step 2 seed the assignment in Lab space;
pixels no seed explains are clustered into new non-thematic candidate classes
(sea not in the legend, border ink, ...). Thin elongated components are pulled
out as line features and skeletonized to polylines; text (Step 3 mask), line
pixels, speckle, and furniture gaps are then filled by nearest-region growing,
so the final area map is continuous under everything that was removed.

Artifacts per map, under runs/<name>/:
    label_map.png        uint8 class index + 1 per pixel (0 = outside map)
    classes_final.json   index -> class metadata incl. area share and origin
    regions.geojson      area polygons (map_area pixel coords, y down)
    lines.geojson        line centerline polylines with kind
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


def interior_line_kind(sem: MapSemantics) -> str:
    kinds = [ln.kind.value for ln in sem.lines if ln.kind.value not in ("coastline", "graticule")]
    for preferred in ("river", "road", "border"):
        if preferred in kinds:
            return preferred
    return "line"


HALO_TOL = 12.0
INK_DARKER_BY = 12.0


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
            halfw = float(cv2.distanceTransform(comp, cv2.DIST_L2, 3).max())
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
            skel = cv2.ximgproc.thinning(comp * 255)
            for pl in _trace_polylines(skel):
                records.append({
                    "kind": kind, "source_class": seeds[idx]["label"],
                    "points": [[int(px + x), int(py + y)] for px, py in pl],
                })
            line_mask[y:y + ch, x:x + cw] |= comp
    return line_mask, halo_mask, records


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
    labels_json = json.loads((out_dir / "labels.json").read_text(encoding="utf-8"))

    img = imread(out_dir / "map_area.png")
    h, w = img.shape[:2]
    mask = imread(out_dir / "map_mask.png")[..., 0]
    text_mask = imread(out_dir / "text_mask.png")[..., 0]

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

    # labels whose strokes were not isolated -- or only partially (a few
    # letters), which would leave legible ghosts -- erase their whole box.
    # ONLY for compact city/capital labels: their boxes are small and reliable.
    # Sprawling river/region labels with bad boxes must NOT be box-filled (an
    # offset box would erase real map content); their leftover ink is absorbed
    # by the mean-shift prefilter and the sliver dissolution below.
    for lb in labels_json["labels"]:
        if lb["kind"] not in ("city", "capital"):
            continue
        bx0, by0, bx1, by1 = lb["box"]
        bx0, by0 = max(0, bx0), max(0, by0)
        box_area = max(1, (bx1 - bx0) * (by1 - by0))
        coverage = np.count_nonzero(text_mask[by0:by1, bx0:bx1]) / box_area
        if not lb["mask_found"] or coverage < 0.04:
            text_mask[by0:by1, bx0:bx1] = 255

    # flatten scan noise before any color work: median kills salt-and-pepper,
    # mean-shift posterizes JPEG gradients and anti-aliasing into flat regions
    smoothed = cv2.pyrMeanShiftFiltering(cv2.medianBlur(img, 3), sp=6, sr=14)
    lab_img = to_lab(smoothed)
    # widen the text mask: letter anti-aliasing fringes extend past the strokes
    # and would survive as legible ghost outlines in blend-color classes
    text_mask = cv2.dilate(text_mask, np.ones((5, 5), np.uint8))
    removed = (text_mask > 0) | (furniture_hole > 0)
    valid = (mask > 0) & ~removed

    seeds = build_seeds(classes_json)
    notes: list[str] = []
    label_map = assign_pixels(lab_img, seeds, valid)
    notes += cluster_unassigned(lab_img, label_map, valid, seeds)

    line_mask, halo_mask, line_records = extract_lines(label_map, lab_img, mask, seeds, sem)
    label_map[(line_mask > 0) | (halo_mask > 0)] = -1

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
    (out_dir / "lines.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "features": [{
            "type": "Feature",
            "properties": {"kind": r["kind"], "source_class": r["source_class"]},
            "geometry": {"type": "LineString", "coordinates": r["points"]},
        } for r in line_records],
    }), encoding="utf-8")

    # ---- debug: original | reconstruction ----
    # extracted ink lines are intentionally NOT drawn: they are not part of the
    # tactile output (lines.geojson still records them for Step 5's opt-in)
    recon = np.full((h, w, 3), 255, np.uint8)
    for i, s in enumerate(seeds):
        recon[label_map == i] = np.uint8(s["rgb"][::-1])
    dbg = np.hstack([img, recon])
    if dbg.shape[1] > 2000:
        s = 2000 / dbg.shape[1]
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step4_debug.png", dbg)

    kinds: dict[str, int] = {}
    for r in line_records:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    return {
        "out_dir": out_dir, "classes": classes_final, "notes": notes,
        "polygons": len(feats), "polylines": len(line_records), "line_kinds": kinds,
        "filled_px": n_filled, "speckles": speckle,
    }
