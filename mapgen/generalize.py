"""Shared physical simplification and minimum-size generalization for Step 6.

The output scale (mm per source pixel) is
fixed from the Step 0 page spec, and the Step 0 constants are converted into
pixel thresholds. Generalization and smoothing run on the RASTER label map,
where the partition cannot develop gaps or slivers; simplified vectors are
then extracted from the generalized raster. Lines are merged across the gaps
text removal left (tangent-aware bridging), simplified, and length-filtered.
The canonical Step 6 runner in ``postprocess.py`` applies these operations to
the approved Step 5 group raster.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import cv2
import numpy as np

from .isolate import imread, imwrite
from .output_spec import OutputSpec
from .segment import fill_holes_nearest, polygonize
from .semantics import MapSemantics, load_pipeline_semantics

SMOOTH_MM = 0.5          # boundary smoothing radius on the page
SIMPLIFY_MM = 0.25       # Douglas-Peucker tolerance on the page
ISLAND_KEEP_FRACTION = 0.2   # islands >= this share of min area get exaggerated, smaller are dropped
LINE_JOIN_NEAR_MM = 1.5      # endpoints closer than this always reconnect
LINE_JOIN_FAR_MM = 12.0      # bridge up to this far if the tangents agree
BOUNDARY_MIN_LINE_MM = 2.0   # short segments participate in a larger border network
BOUNDARY_LINE_KINDS = {"border", "border_or_coast", "coastline", "graticule"}


# --------------------------------------------------------------------------- scale

def compute_scale(spec: OutputSpec, w: int, h: int) -> dict:
    """mm per source pixel; picks the page orientation that yields the larger map."""
    dw, dh = spec.drawable_width_mm, spec.drawable_height_mm
    options = {"portrait": min(dw / w, dh / h), "landscape": min(dh / w, dw / h)}
    orientation = max(options, key=options.get)
    return {
        "mm_per_px": options[orientation],
        "orientation": orientation,
        "map_size_mm": [round(w * options[orientation], 1), round(h * options[orientation], 1)],
    }


# --------------------------------------------------------------------------- raster generalization

def dissolve_small(label_map: np.ndarray, mask: np.ndarray, min_area_px: float,
                   max_iter: int = 8, protected_classes: set[int] = frozenset()) -> int:
    """Merge every class component below the feelable minimum into its neighbours.

    Each content component (mainland, every island) is its own world: refills
    only draw from the SAME component, and a component's largest class patch
    always survives -- otherwise an island whose patches are all sub-minimum
    imports its class from across the water (the Corsica bug)."""
    total = 0
    n_w, world, wstats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8),
                                                             connectivity=8)
    for wi in range(1, n_w):
        x, y, cw, ch, _ = wstats[wi, :5]
        wm = world[y:y + ch, x:x + cw] == wi
        sub = label_map[y:y + ch, x:x + cw]
        local = np.where(wm, sub, -1).astype(np.int16)
        submask = np.where(wm, 255, 0).astype(np.uint8)

        # this world's dominant patch is untouchable
        prot = np.zeros(local.shape, bool)
        best_area = 0
        for idx in np.unique(local[local >= 0]):
            nn, cc2, st2, _ = cv2.connectedComponentsWithStats(
                (local == idx).astype(np.uint8), connectivity=8)
            for i in range(1, nn):
                if st2[i, cv2.CC_STAT_AREA] > best_area:
                    best_area = int(st2[i, cv2.CC_STAT_AREA])
                    prot = cc2 == i

        for _ in range(max_iter):
            marked = 0
            for idx in np.unique(local[local >= 0]):
                if int(idx) in protected_classes:
                    continue
                nn, cc2, st2, _ = cv2.connectedComponentsWithStats(
                    (local == idx).astype(np.uint8), connectivity=8)
                for i in range(1, nn):
                    bx, by, bw_, bh_, area = st2[i]
                    if area >= min_area_px:
                        continue
                    piece = cc2[by:by + bh_, bx:bx + bw_] == i
                    if prot[by:by + bh_, bx:bx + bw_][piece].any():
                        continue
                    local[by:by + bh_, bx:bx + bw_][piece] = -1
                    marked += 1
            if not marked:
                break
            fill_holes_nearest(local, submask)
            total += marked
        sub[wm] = local[wm]
    return total


def handle_islands(label_map: np.ndarray, mask: np.ndarray, min_area_px: float) -> dict:
    """Islands below minimum size: drop the negligible, exaggerate the notable."""
    out = {"dropped": 0, "exaggerated": 0}
    n, cc, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    if n <= 2:
        return out
    areas = stats[1:, cv2.CC_STAT_AREA]
    main = 1 + int(np.argmax(areas))
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if i == main or area >= min_area_px:
            continue
        x, y, cw, ch = stats[i, :4]
        if area < ISLAND_KEEP_FRACTION * min_area_px:
            sub_m = cc[y:y + ch, x:x + cw] == i
            label_map[y:y + ch, x:x + cw][sub_m] = -1
            mask[y:y + ch, x:x + cw][sub_m] = 0
            out["dropped"] += 1
            continue
        # exaggerate: dilate the island until it reaches the feelable minimum
        pad = int(np.ceil((np.sqrt(min_area_px) - np.sqrt(area)) / 2)) + 2
        x0, y0 = max(0, x - pad - 2), max(0, y - pad - 2)
        x1, y1 = min(mask.shape[1], x + cw + pad + 2), min(mask.shape[0], y + ch + pad + 2)
        comp = np.zeros((y1 - y0, x1 - x0), np.uint8)
        comp[y - y0:y - y0 + ch, x - x0:x - x0 + cw] = (cc[y:y + ch, x:x + cw] == i)
        grown = comp.copy()
        for _ in range(40):
            if int(grown.sum()) >= min_area_px:
                break
            grown = cv2.dilate(grown, np.ones((3, 3), np.uint8))
        new = (grown > 0) & (mask[y0:y1, x0:x1] == 0)
        mask[y0:y1, x0:x1][new] = 255
        # new ring pixels stay -1; the nearest labelled pixels are the island's own
        out["exaggerated"] += 1
    if out["exaggerated"]:
        fill_holes_nearest(label_map, mask)
    return out


PRESERVE_SHARE = 0.01  # thematic classes above this pre-gen share must survive


def preserve_thematic(label_map: np.ndarray, label_orig: np.ndarray, mask: np.ndarray,
                      classes: list[dict], min_area_px: float,
                      preserve_share: float = PRESERVE_SHARE) -> list[str]:
    """A significant thematic class must not vanish wholesale: bring back its
    largest original patch, exaggerated to the feelable minimum."""
    restored = []
    for cl in classes:
        idx = cl["index"]
        if not cl["is_thematic"] or cl["area_share"] < preserve_share:
            continue
        if np.count_nonzero(label_map == idx):
            continue
        n, cc, stats, _ = cv2.connectedComponentsWithStats(
            (label_orig == idx).astype(np.uint8), connectivity=8)
        if n < 2:
            continue
        i = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        x, y, cw, ch, area = stats[i]
        pad = int(np.ceil((np.sqrt(min_area_px) - np.sqrt(area)) / 2)) + 4
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(mask.shape[1], x + cw + pad), min(mask.shape[0], y + ch + pad)
        grown = np.zeros((y1 - y0, x1 - x0), np.uint8)
        grown[y - y0:y - y0 + ch, x - x0:x - x0 + cw] = (cc[y:y + ch, x:x + cw] == i)
        for _ in range(40):
            if int(grown.sum()) >= min_area_px:
                break
            grown = cv2.dilate(grown, np.ones((3, 3), np.uint8))
        paint = (grown > 0) & (mask[y0:y1, x0:x1] > 0)
        label_map[y0:y1, x0:x1][paint] = idx
        restored.append(cl["label"])
    return restored


def drop_redundant_boundary_lines(lines: list[dict], label_map: np.ndarray) -> tuple[list[dict], int]:
    """Border/coast ink that hugs a (smoothed) region boundary duplicates the
    embossed boundary line and violates the minimum-gap rule -> drop it."""
    edge = np.zeros(label_map.shape, np.uint8)
    edge[:-1][label_map[:-1] != label_map[1:]] = 1
    edge[:, :-1][label_map[:, :-1] != label_map[:, 1:]] = 1
    zone = cv2.dilate(edge, np.ones((9, 9), np.uint8)) > 0
    kept, dropped = [], 0
    h, w = label_map.shape
    for ln in lines:
        if ln["kind"] not in ("border", "border_or_coast", "coastline"):
            kept.append(ln)
            continue
        pts = np.array(ln["points"], int)
        pts = pts[(pts[:, 0] >= 0) & (pts[:, 0] < w) & (pts[:, 1] >= 0) & (pts[:, 1] < h)]
        frac = zone[pts[:, 1], pts[:, 0]].mean() if len(pts) else 1.0
        if frac > 0.7:
            dropped += 1
        else:
            kept.append(ln)
    return kept, dropped


def smooth_labels(label_map: np.ndarray, mask: np.ndarray, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    """One-hot blur + argmax over classes AND the outside: smooth boundaries and
    coastline with a guaranteed gap-free partition."""
    idxs = [int(v) for v in np.unique(label_map[label_map >= 0])]
    best = cv2.GaussianBlur((label_map < 0).astype(np.float32), (0, 0), sigma)
    winner = np.full(label_map.shape, -1, np.int16)
    for idx in idxs:
        score = cv2.GaussianBlur((label_map == idx).astype(np.float32), (0, 0), sigma)
        upd = score > best
        winner[upd] = idx
        best[upd] = score[upd]
    new_mask = np.where(winner >= 0, 255, 0).astype(np.uint8)
    return winner, new_mask


def generalize_area_raster(label_map: np.ndarray, classes: list[dict],
                           min_area_px: float, sigma: float,
                           preserve_share: float = PRESERVE_SHARE,
                           protected_classes: set[int] = frozenset()
                           ) -> tuple[np.ndarray, np.ndarray, dict]:
    """Run canonical Step 6's complete area algorithm on any class raster.

    Step 6 calls this after Step 5 aggregates the Step 4 class identities, so
    the same island handling, small-component dissolution, Gaussian label
    smoothing, second dissolution, and class preservation operate on the
    approved final categories.
    """
    result = label_map.astype(np.int16, copy=True)
    mask = np.where(result >= 0, 255, 0).astype(np.uint8)
    original = result.copy()
    islands = handle_islands(result, mask, min_area_px)
    dissolved = dissolve_small(
        result, mask, min_area_px, protected_classes=protected_classes)
    result, mask = smooth_labels(result, mask, sigma)
    dissolved += dissolve_small(
        result, mask, min_area_px, protected_classes=protected_classes)
    restored = preserve_thematic(
        result, original, mask, classes, min_area_px,
        preserve_share=preserve_share)
    return result, mask, {
        "dissolved_components": dissolved,
        "islands": islands,
        "classes_restored": restored,
    }


# --------------------------------------------------------------------------- lines

def _endpoint_tangent(pts: np.ndarray, at_start: bool) -> np.ndarray:
    k = min(5, len(pts) - 1)
    v = (pts[0] - pts[k]) if at_start else (pts[-1] - pts[-1 - k])
    n = np.linalg.norm(v)
    return v / n if n else v


def merge_lines(lines: list[dict], near_px: float, far_px: float) -> tuple[list[dict], int]:
    """Reconnect polylines of the same kind across small gaps (or larger ones
    when the endpoint tangents point at each other -- text-removal gaps)."""
    lines = [{"kind": ln["kind"], "pts": np.array(ln["points"], np.float64)} for ln in lines]
    joins = 0
    cos_lim = np.cos(np.deg2rad(40))
    changed = True
    while changed:
        changed = False
        for i in range(len(lines)):
            if lines[i] is None:
                continue
            for j in range(i + 1, len(lines)):
                if lines[j] is None or lines[j]["kind"] != lines[i]["kind"]:
                    continue
                a, b = lines[i]["pts"], lines[j]["pts"]
                best = None
                for ai, at_a_start in ((0, True), (len(a) - 1, False)):
                    for bi, at_b_start in ((0, True), (len(b) - 1, False)):
                        gap = float(np.linalg.norm(a[ai] - b[bi]))
                        if gap >= far_px:
                            continue
                        if gap >= near_px:
                            d = b[bi] - a[ai]
                            dn = d / (np.linalg.norm(d) or 1)
                            if float(_endpoint_tangent(a, at_a_start) @ dn) < cos_lim:
                                continue
                            if float(_endpoint_tangent(b, at_b_start) @ -dn) < cos_lim:
                                continue
                        if best is None or gap < best[0]:
                            best = (gap, at_a_start, at_b_start)
                if best is None:
                    continue
                _, a_start, b_start = best
                pa = a[::-1] if a_start else a          # joined end last
                pb = b if b_start else b[::-1]          # joined end first
                lines[i]["pts"] = np.vstack([pa, pb])
                lines[j] = None
                joins += 1
                changed = True
    return [ln for ln in lines if ln is not None], joins


def simplify_line(pts: np.ndarray, eps: float) -> np.ndarray:
    if len(pts) < 3:
        return pts
    approx = cv2.approxPolyDP(pts.astype(np.float32).reshape(-1, 1, 2), eps, False)
    return approx.reshape(-1, 2).astype(np.float64)


def line_length(pts: np.ndarray) -> float:
    return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))


# --------------------------------------------------------------------------- runner

ALL_LINE_KINDS = [
    "river", "road", "border", "border_or_coast", "coastline", "graticule", "line",
]
LINE_POLICY_VERSION = 1

SIMPLIFICATION_PRESETS = {
    1: {"side_factor": 0.55, "smooth_mm": 0.2, "preserve_share": 0.003},
    2: {"side_factor": 0.77, "smooth_mm": 0.35, "preserve_share": 0.006},
    3: {"side_factor": 1.0, "smooth_mm": 0.5, "preserve_share": 0.01},
    4: {"side_factor": 1.35, "smooth_mm": 0.8, "preserve_share": 0.018},
    5: {"side_factor": 1.7, "smooth_mm": 1.2, "preserve_share": 0.03},
}

PRESET_ARTIFACTS = (
    "label_map_gen.png",
    "label_map_gen_preview.png",
    "classes_gen.json",
    "regions_gen.geojson",
    "lines_gen.geojson",
    "step5_summary.json",
    "step5_debug.png",
)

DEFAULT_PARAMS = {
    # UI preset that produced the numeric parameters below. Step 5 itself uses
    # only the numeric values; null means the user has customized them.
    "simplification_level": 3,
    "min_texture_area_side_mm": None,  # null -> use the Step 0 spec constant
    "smooth_mm": SMOOTH_MM,
    "preserve_share": PRESERVE_SHARE,
    # Explicit borders/coastlines reported by Step 1 are selected contextually
    # by load_params(); other extracted line kinds remain opt-in.
    "keep_line_kinds": [],
    "line_policy_version": LINE_POLICY_VERSION,
    "protected_classes": [],           # class indices exempt from dissolution
}


def semantic_default_line_kinds(out_dir: Path) -> list[str]:
    """Keep explicitly visible political/coastal context without manual rescue."""
    path = out_dir / "step1_semantics.json"
    if not path.exists():
        return []
    try:
        sem = MapSemantics.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    kinds = {line.kind.value for line in sem.lines}
    if "border" in kinds and "coastline" in kinds:
        return ["border_or_coast"]
    if "border" in kinds:
        return ["border"]
    if "coastline" in kinds:
        return ["coastline"]
    return []


def load_params(out_dir: Path) -> dict:
    p = out_dir / "step5_params.json"
    params = dict(DEFAULT_PARAMS)
    if p.exists():
        saved = json.loads(p.read_text(encoding="utf-8"))
        params.update(saved)
        # Parameter files created before the preset slider represent manual
        # values, even when they happen to resemble the Balanced preset.
        if "simplification_level" not in saved:
            params["simplification_level"] = None
        # Older defaults silently discarded every extracted line.  Migrate
        # only that old empty default; a non-empty saved selection remains an
        # explicit user choice.
        if (int(saved.get("line_policy_version", 0)) < LINE_POLICY_VERSION
                and not saved.get("keep_line_kinds")):
            params["keep_line_kinds"] = semantic_default_line_kinds(out_dir)
    else:
        params["keep_line_kinds"] = semantic_default_line_kinds(out_dir)
    params["line_policy_version"] = LINE_POLICY_VERSION
    return params


def preset_params(spec: OutputSpec, level: int, base: dict | None = None) -> dict:
    """Resolve one slider position while retaining applied advanced exceptions."""
    level = max(1, min(5, int(level)))
    preset = SIMPLIFICATION_PRESETS[level]
    params = dict(DEFAULT_PARAMS)
    if base:
        params.update(base)
    params.update({
        "simplification_level": level,
        "min_texture_area_side_mm": round(
            max(3.0, spec.constants.min_texture_area_side_mm * preset["side_factor"]), 1),
        "smooth_mm": preset["smooth_mm"],
        "preserve_share": preset["preserve_share"],
    })
    return params


def preset_artifact_name(level: int, canonical_name: str) -> str:
    return f"step5_preset_{int(level)}_{canonical_name}"


def preset_cache_ready(out_dir: Path) -> bool:
    return all((out_dir / preset_artifact_name(level, name)).exists()
               for level in SIMPLIFICATION_PRESETS for name in PRESET_ARTIFACTS)


def _cache_preset(out_dir: Path, level: int) -> None:
    for name in PRESET_ARTIFACTS:
        shutil.copy2(out_dir / name, out_dir / preset_artifact_name(level, name))


def activate_preset(out_dir: Path, level: int) -> dict:
    """Make a pre-generated slider output canonical without recomputing it."""
    level = max(1, min(5, int(level)))
    missing = [name for name in PRESET_ARTIFACTS
               if not (out_dir / preset_artifact_name(level, name)).exists()]
    if missing:
        raise FileNotFoundError(f"preset {level} is not ready")
    for name in PRESET_ARTIFACTS:
        shutil.copy2(out_dir / preset_artifact_name(level, name), out_dir / name)
    summary = json.loads((out_dir / "step5_summary.json").read_text(encoding="utf-8"))
    (out_dir / "step5_params.json").write_text(
        json.dumps(summary["params"], indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def run_step5(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs"),
              params_override: dict | None = None) -> dict:
    from .segment import run_step4

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "label_map.png").exists() or not (out_dir / "classes_final.json").exists():
        run_step4(image_path, model=model, runs_dir=runs_dir)
    load_pipeline_semantics(out_dir, "Step 5")

    spec = OutputSpec.load_or_create()
    classes = json.loads((out_dir / "classes_final.json").read_text(encoding="utf-8"))["classes"]
    lines_in = json.loads((out_dir / "lines.geojson").read_text(encoding="utf-8"))["features"]

    label_map = imread(out_dir / "label_map.png")[..., 0].astype(np.int16) - 1
    h, w = label_map.shape
    share_before = {c["index"]: c["area_share"] for c in classes}

    params = load_params(out_dir) if params_override is None else dict(params_override)
    scale = compute_scale(spec, w, h)
    mm = scale["mm_per_px"]
    c = spec.constants
    side_mm = params["min_texture_area_side_mm"] or c.min_texture_area_side_mm
    min_area_px = (side_mm / mm) ** 2
    min_line_px = c.min_line_length_mm / mm
    sigma = float(np.clip(params["smooth_mm"] / mm, 1.0, 6.0))
    eps_px = max(1.0, SIMPLIFY_MM / mm)
    protected = set(int(i) for i in params["protected_classes"])

    # ---- areas: islands -> dissolve -> smooth -> dissolve -> preserve ----
    label_map, mask, area_result = generalize_area_raster(
        label_map, classes, min_area_px, sigma,
        preserve_share=params["preserve_share"], protected_classes=protected)
    islands = area_result["islands"]
    dissolved = area_result["dissolved_components"]
    restored = area_result["classes_restored"]

    # ---- lines: drop frame + redundant boundary ink, merge, simplify, filter ----
    keep_kinds = set(params["keep_line_kinds"])
    kept_lines = [f["properties"] | {"points": f["geometry"]["coordinates"]}
                  for f in lines_in
                  if f["properties"]["kind"] != "frame" and f["properties"]["kind"] in keep_kinds]
    kept_lines, redundant = drop_redundant_boundary_lines(kept_lines, label_map)
    # Administrative networks are already split at real junctions.  Generic
    # gap bridging is unsafe here: on a dense world map it connects unrelated
    # countries with long diagonal chords.  Preserve accurate source segments
    # and reserve reconnection for rivers/roads interrupted by text masks.
    boundary_lines = [line for line in kept_lines if line["kind"] in BOUNDARY_LINE_KINDS]
    reconnectable_lines = [line for line in kept_lines
                           if line["kind"] not in BOUNDARY_LINE_KINDS]
    merged, joins = merge_lines(
        reconnectable_lines,
        near_px=LINE_JOIN_NEAR_MM / mm,
        far_px=LINE_JOIN_FAR_MM / mm,
    )
    merged += [{"kind": line["kind"],
                "network_id": line.get("network_id"),
                "pts": np.asarray(line["points"], dtype=np.float64)}
               for line in boundary_lines]
    boundary_network_lengths: dict[str, float] = {}
    for line in merged:
        network_id = line.get("network_id")
        if network_id:
            boundary_network_lengths[network_id] = (
                boundary_network_lengths.get(network_id, 0.0)
                + line_length(line["pts"])
            )
    line_feats = []
    dropped_short = 0
    for ln in merged:
        pts = simplify_line(ln["pts"], eps_px)
        length = line_length(pts)
        network_id = ln.get("network_id")
        network_is_long = bool(
            network_id
            and boundary_network_lengths.get(network_id, 0.0) >= min_line_px
        )
        required_length = (
            0.0 if network_is_long
            else BOUNDARY_MIN_LINE_MM / mm
            if ln["kind"] in BOUNDARY_LINE_KINDS
            else min_line_px
        )
        if length < required_length:
            dropped_short += 1
            continue
        line_feats.append({
            "type": "Feature",
            "properties": {"kind": ln["kind"], "length_mm": round(length * mm, 1)},
            "geometry": {"type": "LineString",
                         "coordinates": [[round(float(x), 1), round(float(y), 1)] for x, y in pts]},
        })

    # ---- artifacts ----
    total = max(1, int(np.count_nonzero(mask)))
    classes_gen = []
    for cl in classes:
        area = int(np.count_nonzero(label_map == cl["index"]))
        classes_gen.append(cl | {
            "area_px": area,
            "area_share_before": share_before.get(cl["index"], 0.0),
            "area_share": round(area / total, 4),
        })
    survivors = [cl for cl in classes_gen if cl["area_px"] > 0]

    region_feats = []
    for cl in survivors:
        region_feats += polygonize(label_map, cl["index"], cl["label"], eps_px)

    summary = {
        "scale_mm_per_px": round(mm, 4),
        "orientation": scale["orientation"],
        "map_size_mm": scale["map_size_mm"],
        "page_mm": [spec.page_width_mm, spec.page_height_mm],
        "min_texture_area_px": round(min_area_px),
        "smoothing_sigma_px": round(sigma, 2),
        "dissolved_components": dissolved,
        "islands": islands,
        "classes_restored": restored,
        "line_joins": joins,
        "lines_kept": len(line_feats),
        "lines_dropped_short": dropped_short,
        "lines_dropped_redundant": redundant,
        "classes_vanished": [cl["label"] for cl in classes_gen if cl["area_px"] == 0],
        "params": params,
    }
    (out_dir / "step5_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                encoding="utf-8")
    (out_dir / "classes_gen.json").write_text(
        json.dumps({"classes": classes_gen}, indent=2, ensure_ascii=False), encoding="utf-8")
    imwrite(out_dir / "label_map_gen.png", (label_map + 1).astype(np.uint8))
    (out_dir / "regions_gen.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm, 4),
        "features": region_feats,
    }), encoding="utf-8")
    (out_dir / "lines_gen.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "coordinate_space": "map_area.png pixels, y down",
        "mm_per_px": round(mm, 4),
        "features": line_feats,
    }), encoding="utf-8")

    # ---- debug: original | generalized ----
    img = imread(out_dir / "map_area.png")
    recon = np.full((h, w, 3), 255, np.uint8)
    for cl in survivors:
        recon[label_map == cl["index"]] = np.uint8(cl["rgb"][::-1])
    # Human-readable rendering of the exact indexed raster consumed by Steps
    # 6 and 7. Lines are added only to the debug image below, not this preview.
    imwrite(out_dir / "label_map_gen_preview.png", recon)
    kind_col = {"border_or_coast": (0, 0, 0), "coastline": (48, 96, 64),
                "graticule": (170, 170, 170),
                "river": (255, 128, 0), "road": (0, 0, 200),
                "border": (0, 0, 0), "line": (200, 0, 200)}
    for f in line_feats:
        pts = np.array(f["geometry"]["coordinates"], np.int32)
        cv2.polylines(recon, [pts], False, kind_col.get(f["properties"]["kind"], (0, 0, 0)), 2)
    dbg = np.hstack([img, recon])
    if dbg.shape[1] > 2000:
        s = 2000 / dbg.shape[1]
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step5_debug.png", dbg)

    return {"out_dir": out_dir, "summary": summary,
            "polygons": len(region_feats), "classes": classes_gen}


def run_step5_presets(image_path: Path, model: str | None = None,
                      runs_dir: Path = Path("runs")) -> dict:
    """Generate all five slider previews, then activate the selected one."""
    out_dir = runs_dir / image_path.stem
    base = load_params(out_dir)
    selected = base.get("simplification_level")
    selected = int(selected) if selected in SIMPLIFICATION_PRESETS else 3
    spec = OutputSpec.load_or_create()

    for level in SIMPLIFICATION_PRESETS:
        run_step5(image_path, model=model, runs_dir=runs_dir,
                  params_override=preset_params(spec, level, base))
        _cache_preset(out_dir, level)

    summary = activate_preset(out_dir, selected)
    classes = json.loads((out_dir / "classes_gen.json").read_text(encoding="utf-8"))["classes"]
    polygons = len(json.loads((out_dir / "regions_gen.geojson").read_text(
        encoding="utf-8"))["features"])
    return {"out_dir": out_dir, "summary": summary,
            "polygons": polygons, "classes": classes}
