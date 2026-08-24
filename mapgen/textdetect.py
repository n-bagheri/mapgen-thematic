"""Step 3 -- Overlay text detection.

Gemini transcribes and classifies every label on the map area (tiled for large
images, primed with the Step 1 vocabulary); classic CV then extracts the exact
text strokes inside each detection to produce the oriented quad, the anchor
point, and the pixel-tight removal mask that Step 4 consumes. Furniture
regions from Step 2 (legend, title, scale bar...) are painted out before
detection, so only true overlay text is reported.

Artifacts per map, under runs/<name>/:
    step3_raw.json      merged Gemini detections (cached; delete to re-call)
    step3_raw.sha256    fingerprint tying the Gemini cache to the prepared input
    step3_craft.sha256  fingerprint tying the CRAFT cache to the prepared input
    labels.json         text, kind, priority, quad, anchor (map-area coords)
    text_mask.png       raw union of pixel-precise detected text strokes
    text_removal_mask.png exact reviewed/default mask consumed by Step 4
    step3_debug.png     annotated overlay for human review
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
from enum import Enum
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field

from .isolate import imread, imwrite, prepare_text_input, to_lab
from .semantics import (DEFAULT_MODEL, EmptyModelResponse, MapSemantics,
                        generate_json, require_pipeline_eligible)

# --------------------------------------------------------------------------- schema

class TextKind(str, Enum):
    capital = "capital"            # capital city name
    city = "city"                  # any other city/town name
    river_label = "river_label"    # name written along a river or other line
    region_label = "region_label"  # name of a thematic area or region
    line_label = "line_label"      # road/border annotations
    other = "other"


class TextItem(BaseModel):
    text: str = Field(description="The label exactly as written, keeping accents")
    kind: TextKind
    # Length bounds are load-bearing on the Gemma text-schema path, where
    # Pydantic is the only validator and the caller unpacks four values.
    box_2d: list[int] = Field(min_length=4, max_length=4,
                              description="[y_min, x_min, y_max, x_max] normalized to 0-1000 of THIS image")


class TextDetections(BaseModel):
    items: list[TextItem]


# priority for Step 9 clutter resolution (1 = keep longest)
KIND_PRIORITY = {
    TextKind.capital: 1, TextKind.region_label: 2, TextKind.river_label: 3,
    TextKind.line_label: 4, TextKind.city: 5, TextKind.other: 6,
}

TEXT_PROMPT = """\
You are the overlay-text detection stage of a pipeline converting thematic
maps into tactile maps. This image is a crop of the MAP AREA only (legend,
title and other furniture have been blanked out).

List EVERY piece of text overlaid on the map picture: city names, names
written along rivers or lines, region/area names, small annotations. Rules:

- One item per label phrase ("Le Mans" is one item, not two).
- Transcribe exactly, keeping accents and capitalization.
- kind: 'capital' only for the capital city{capital_hint}; 'city' for other
  settlements (usually marked with a dot); 'river_label' for names along
  rivers/streams; 'region_label' for names of areas or regions;
  'line_label' for road or border annotations; 'other' otherwise.
- box_2d must enclose the whole written label, including curved/tilted text.
- CRITICAL: only report text that is actually LEGIBLE in THIS image. The
  context below may mention legend text, scale bars, projection notes or
  labels that are not in this crop -- NEVER report text you cannot see here.
  If the image contains no overlay text, return an empty list.

Known context from earlier analysis (for classification only, not a list of
things to find):
{context}
"""


# --------------------------------------------------------------------------- gemini pass

MAX_TILE = 1400
TILE_OVERLAP = 0.12


def _context_from_sem(sem: MapSemantics) -> tuple[str, str]:
    lines = [f"- subject: {sem.subject}"]
    ot = sem.overlay_text
    lines.append(f"- expected: city labels={ot.has_city_labels}, region labels={ot.has_region_labels}, "
                 f"line labels={ot.has_line_labels}")
    if ot.notes:
        lines.append(f"- notes: {ot.notes}")
    for ln in sem.lines:
        lines.append(f"- line feature ({ln.kind.value}): {ln.description}")
    capital_hint = f" (known capital: {ot.capital_city})" if ot.capital_city else ""
    return "\n".join(lines), capital_hint


def _expects_overlay_text(sem: MapSemantics) -> bool:
    """Whether Step 1 found any text that can occur in the map area.

    The title, legend and source note are furniture and are deliberately
    blanked before this stage.  When Step 1 says none of the remaining text
    categories exists, sending a large map through the remote detector and
    EasyOCR is both unnecessary and prone to false positives.
    """
    overlay = sem.overlay_text
    return bool(
        overlay.has_city_labels
        or overlay.has_region_labels
        or overlay.has_line_labels
        or overlay.capital_city
    )


def _gemini_text(tile_bgr: np.ndarray, prompt: str, model: str | None) -> TextDetections:
    from google.genai import types

    # MAX_TILE decides how many pieces the map is cut into, not how big each
    # piece is, so a large scan produces multi-megapixel tiles.  The model only
    # returns boxes normalized to 0-1000 of whatever it is shown, and those are
    # mapped back using the tile's original width and height, so sending a
    # smaller copy costs no accuracy and keeps the upload from timing out.
    sent = tile_bgr
    longest = max(sent.shape[:2])
    if longest > MAX_TILE:
        scale = MAX_TILE / longest
        sent = cv2.resize(sent, (max(1, round(sent.shape[1] * scale)),
                                 max(1, round(sent.shape[0] * scale))),
                          interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", sent, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise ValueError("tile encode failed")
    # generate_json carries the shared deadline and retries, and avoids Gemma's
    # server-side response_schema stall by sending the schema as text instead.
    return generate_json(
        [types.Part.from_bytes(data=buf.tobytes(), mime_type="image/jpeg"), prompt],
        TextDetections, model=model, temperature=0.0)


def _tiles(img: np.ndarray) -> list[tuple[int, int, np.ndarray]]:
    h, w = img.shape[:2]
    if max(h, w) <= MAX_TILE:
        return [(0, 0, img)]
    nx = int(np.ceil(w / (MAX_TILE * (1 - TILE_OVERLAP))))
    ny = int(np.ceil(h / (MAX_TILE * (1 - TILE_OVERLAP))))
    nx, ny = min(nx, 3), min(ny, 3)  # cap the call count; Gemini copes with big tiles
    xs = np.linspace(0, w, nx + 1).astype(int)
    ys = np.linspace(0, h, ny + 1).astype(int)
    ov_x, ov_y = int(w / nx * TILE_OVERLAP), int(h / ny * TILE_OVERLAP)
    out = []
    for j in range(ny):
        for i in range(nx):
            x0, x1 = max(0, xs[i] - ov_x), min(w, xs[i + 1] + ov_x)
            y0, y1 = max(0, ys[j] - ov_y), min(h, ys[j + 1] + ov_y)
            out.append((x0, y0, img[y0:y1, x0:x1]))
    return out


def detect_text(map_bgr: np.ndarray, sem: MapSemantics, model: str | None) -> list[dict]:
    """Run the text-only Gemini pass and return pixel boxes."""
    if not _expects_overlay_text(sem):
        return []
    context, capital_hint = _context_from_sem(sem)
    prompt = TEXT_PROMPT.format(context=context, capital_hint=capital_hint)
    raw: list[dict] = []

    def collect(part, off_x, off_y, depth):
        th, tw = part.shape[:2]
        try:
            detected = _gemini_text(part, prompt, model)
        except EmptyModelResponse as exc:
            # Gemma's decoder can wedge on one crowded view, looping inside its
            # thinking channel until the token budget dies (temperature does
            # not reliably free it).  A narrower view usually reads fine, so
            # quarter the tile once; a quadrant that still wedges only costs
            # that region's Gemini boxes -- the CRAFT/EasyOCR fusion still
            # contributes its text, and the Step 3 review gate puts every
            # label in front of the reader.
            if depth >= 1 or min(th, tw) < 500:
                print(f"WARN: Gemini text skipped for tile at ({off_x},{off_y}): {exc}")
                return
            half_x, half_y = tw // 2, th // 2
            lap_x, lap_y = tw // 8, th // 8
            for qx, qy, quad in (
                    (0, 0, part[:half_y + lap_y, :half_x + lap_x]),
                    (half_x - lap_x, 0, part[:half_y + lap_y, half_x - lap_x:]),
                    (0, half_y - lap_y, part[half_y - lap_y:, :half_x + lap_x]),
                    (half_x - lap_x, half_y - lap_y,
                     part[half_y - lap_y:, half_x - lap_x:])):
                collect(quad, off_x + qx, off_y + qy, depth + 1)
            return
        for it in detected.items:
            y0, x0, y1, x1 = it.box_2d
            raw.append({
                "text": it.text.strip(), "kind": it.kind.value,
                "box": [off_x + int(x0 / 1000 * tw), off_y + int(y0 / 1000 * th),
                        off_x + int(x1 / 1000 * tw), off_y + int(y1 / 1000 * th)],
            })

    for ox, oy, tile in _tiles(map_bgr):
        collect(tile, int(ox), int(oy), 0)
    # merge duplicate text boxes from tile overlap
    raw.sort(key=lambda r: (r["box"][2] - r["box"][0]) * (r["box"][3] - r["box"][1]),
             reverse=True)
    merged: list[dict] = []
    for item in raw:
        rx = (item["box"][0] + item["box"][2]) / 2
        ry = (item["box"][1] + item["box"][3]) / 2
        duplicate = False
        for kept in merged:
            mx = (kept["box"][0] + kept["box"][2]) / 2
            my = (kept["box"][1] + kept["box"][3]) / 2
            near = max(kept["box"][2] - kept["box"][0],
                       kept["box"][3] - kept["box"][1])
            if (abs(rx - mx) < 0.7 * near and abs(ry - my) < 0.7 * near and
                    difflib.SequenceMatcher(
                        None, item["text"].lower(), kept["text"].lower()).ratio() > 0.7):
                duplicate = True
                break
        if not duplicate:
            merged.append(item)
    return merged


def _vocabulary(sem: MapSemantics) -> set[str]:
    """Proper names Step 1 mentioned; used to validate transcriptions."""
    text = " . ".join(
        [sem.overlay_text.notes or "", sem.overlay_text.capital_city or "", sem.subject]
        + [ln.description for ln in sem.lines]
    )
    names = set(re.findall(r"[A-ZÀ-Þ][\w'À-ÿ-]+(?:\s+[A-ZÀ-Þ][\w'À-ÿ-]+)*", text))
    return {n for n in names if len(n) > 2}


# --------------------------------------------------------------------------- craft localization

_READER = None
TEXT_CACHE_VERSION = 4


def _text_input_signature(clean_bgr: np.ndarray) -> str:
    """Fingerprint the exact pixels and detector-cache contract."""
    digest = hashlib.sha256()
    digest.update(f"textdetect-v{TEXT_CACHE_VERSION}\0".encode("ascii"))
    digest.update(str(clean_bgr.shape).encode("ascii"))
    digest.update(clean_bgr.tobytes())
    return digest.hexdigest()


def _cache_matches(signature_path: Path, signature: str) -> bool:
    try:
        return signature_path.read_text(encoding="ascii").strip() == signature
    except OSError:
        return False


def _craft_reader():
    global _READER
    if _READER is None:
        import easyocr
        _READER = easyocr.Reader(["en", "fr"], gpu=False, verbose=False)
    return _READER


def craft_detect(clean_bgr: np.ndarray) -> list[dict] | None:
    """Pixel-accurate word boxes from the CRAFT scene-text detector (EasyOCR).
    Returns None when easyocr/torch is not installed (Gemini boxes then apply).
    mag_ratio=2 is essential: map labels are ~8-10 px tall and CRAFT misses
    half of them at native scale (13 vs 21 detections on the France sample)."""
    try:
        reader = _craft_reader()
    except ImportError:
        return None
    out = []
    for quad, text, conf in reader.readtext(clean_bgr, paragraph=False, canvas_size=3000,
                                            mag_ratio=2.0, text_threshold=0.6, low_text=0.35):
        q = np.array(quad, np.float32)
        x0, y0 = q.min(axis=0)
        x1, y1 = q.max(axis=0)
        out.append({"text": str(text).strip(), "conf": float(conf),
                    "box": [int(x0), int(y0), int(x1), int(y1)]})
    return out


def fuse_detections(gemini_items: list[dict], craft_dets: list[dict] | None,
                    diag: float, vocab: set[str]) -> list[dict]:
    """Fuse Gemini with the CRAFT + EasyOCR detection/recognition result.

    Gemini supplies classification and the preferred final transcription;
    EasyOCR supplies its own reading/confidence while CRAFT supplies geometry.
    Preserve both readings and their agreement so review does not conflate
    text recognition with box localization.
    """
    if craft_dets is None:
        return [dict(
            it,
            localization="gemini",
            recognition_status="gemini-only",
            gemini_text=it["text"],
            easyocr_text=None,
            easyocr_conf=None,
            text_similarity=None,
        ) for it in gemini_items]
    pairs = []
    for gi, g in enumerate(gemini_items):
        gc = ((g["box"][0] + g["box"][2]) / 2, (g["box"][1] + g["box"][3]) / 2)
        for ci, c in enumerate(craft_dets):
            cc = ((c["box"][0] + c["box"][2]) / 2, (c["box"][1] + c["box"][3]) / 2)
            dist = ((gc[0] - cc[0]) ** 2 + (gc[1] - cc[1]) ** 2) ** 0.5
            ratio = difflib.SequenceMatcher(None, g["text"].lower(), c["text"].lower()).ratio()
            if ratio >= 0.5 or (dist < 0.05 * diag and ratio >= 0.25) or dist < 0.02 * diag:
                pairs.append((ratio - dist / (0.2 * diag), gi, ci, ratio))
    pairs.sort(key=lambda p: -p[0])
    used_g: set[int] = set()
    used_c: set[int] = set()
    fused = []
    for _, gi, ci, ratio in pairs:
        if gi in used_g or ci in used_c:
            continue
        used_g.add(gi)
        used_c.add(ci)
        c = craft_dets[ci]
        status = ("text-confirmed" if ratio >= 0.7
                  else "partial-text-match" if ratio >= 0.5
                  else "geometry-only")
        fused.append(dict(
            gemini_items[gi],
            box=list(c["box"]),
            localization="craft",
            recognition_status=status,
            gemini_text=gemini_items[gi]["text"],
            easyocr_text=c["text"],
            easyocr_conf=c["conf"],
            text_similarity=round(float(ratio), 3),
        ))
    for gi, g in enumerate(gemini_items):
        if gi not in used_g:
            fused.append(dict(
                g,
                localization="gemini-unverified",
                recognition_status="gemini-only",
                gemini_text=g["text"],
                easyocr_text=None,
                easyocr_conf=None,
                text_similarity=None,
            ))
    for ci, c in enumerate(craft_dets):
        if ci in used_c or c["conf"] < 0.45:
            continue
        if len([ch for ch in c["text"] if ch.isalnum()]) < 3:
            continue
        m = difflib.get_close_matches(c["text"], vocab, n=1, cutoff=0.7)
        fused.append({
            "text": m[0] if m else c["text"],
            "kind": "other",
            "box": list(c["box"]),
            "localization": "craft-only",
            "recognition_status": "easyocr-only",
            "gemini_text": None,
            "easyocr_text": c["text"],
            "easyocr_conf": c["conf"],
            "text_similarity": None,
        })
    return fused


# --------------------------------------------------------------------------- stroke extraction

STROKE_DELTA = 15.0     # Lab distance from local background to count as text
MAX_STROKE_WIDTH = 4.5  # px half-width; anything fatter is a map region, not a letter


def extract_strokes(map_bgr: np.ndarray, box: list[int]) -> dict:
    """Pixel-accurate text strokes inside a (padded) detection box."""
    h, w = map_bgr.shape[:2]
    bx0, by0, bx1, by1 = box
    # generous pad: CRAFT boxes are tight, and the background ring must not
    # touch the letters themselves or the k-means bg estimate absorbs them
    pad = max(6, int(0.6 * (by1 - by0)))
    x0, y0 = max(0, bx0 - pad), max(0, by0 - pad)
    x1, y1 = min(w, bx1 + pad), min(h, by1 + pad)
    sub = map_bgr[y0:y1, x0:x1]
    sh, sw = sub.shape[:2]
    if sh < 6 or sw < 6:
        return {"found": False}
    lab = to_lab(sub)

    # local background = dominant colors of the box border ring (k-means, k<=3)
    ring = np.concatenate([lab[:3].reshape(-1, 3), lab[-3:].reshape(-1, 3),
                           lab[:, :3].reshape(-1, 3), lab[:, -3:].reshape(-1, 3)])
    k = min(3, len(ring))
    _, _, centers = cv2.kmeans(ring.astype(np.float32), k, None,
                               (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0),
                               3, cv2.KMEANS_PP_CENTERS)
    dists = np.min(np.stack([np.linalg.norm(lab - c, axis=2) for c in centers]), axis=0)
    strokes = (dists > STROKE_DELTA).astype(np.uint8)

    # strokes must belong to THIS label: the wide window exists only for the
    # background estimate; neighbouring labels and patch edges outside the
    # core detection box (+15%) must not join the mask/quad
    core = np.zeros((sh, sw), bool)
    cp = max(2, int(0.15 * (by1 - by0)))
    core[max(0, by0 - cp - y0):min(sh, by1 + cp - y0),
         max(0, bx0 - cp - x0):min(sw, bx1 + cp - x0)] = True

    n, cc, stats, _ = cv2.connectedComponentsWithStats(strokes, connectivity=8)
    keep = np.zeros_like(strokes)
    for i in range(1, n):
        cx, cy, cw, ch, area = stats[i]
        if area < 4:
            continue
        # a line feature passing through spans the whole window; letters don't
        if (cw >= 0.96 * sw and cx <= 1 and cx + cw >= sw - 1) or \
           (ch >= 0.96 * sh and cy <= 1 and cy + ch >= sh - 1):
            continue
        comp = (cc == i).astype(np.uint8)
        if (comp.astype(bool) & core).sum() < 0.5 * area:
            continue  # mostly outside the label's own box
        if cv2.distanceTransform(comp, cv2.DIST_L2, 3).max() > MAX_STROKE_WIDTH:
            continue  # too fat to be a letter stroke
        keep |= comp
    if not keep.any():
        return {"found": False}

    pts = cv2.findNonZero(keep)
    quad = cv2.boxPoints(cv2.minAreaRect(pts))
    centroid = pts.reshape(-1, 2).mean(axis=0)
    mask = cv2.dilate(keep, np.ones((3, 3), np.uint8))
    n_comp = cv2.connectedComponents(keep, connectivity=8)[0] - 1
    return {
        "found": True,
        "mask": mask, "origin": (x0, y0),
        "quad": [[int(px + x0), int(py + y0)] for px, py in quad],
        "centroid": [float(centroid[0] + x0), float(centroid[1] + y0)],
        "px": int(keep.sum()), "n_components": n_comp,
        "window_area": sh * sw,
    }


def strokes_look_like_text(strokes: dict, text: str) -> bool:
    """Reject stroke sets that are clearly not letters: a detection box offset
    onto a map patch yields one big blob or edge fragments, not a word."""
    if not strokes["found"]:
        return False
    coverage = strokes["px"] / max(1, strokes["window_area"])
    if not 0.02 <= coverage <= 0.5:  # empty noise, or it swallowed a whole patch
        return False
    letters = len([c for c in text if c.isalnum()])
    return strokes["n_components"] >= min(3, max(2, letters // 3))


def find_point_symbol(map_bgr: np.ndarray, box: list[int], text_mask: np.ndarray) -> list[float] | None:
    """City dot/star near the label -- the braille anchor should sit on it."""
    h, w = map_bgr.shape[:2]
    bx0, by0, bx1, by1 = box
    grow = int(1.2 * (by1 - by0))
    x0, y0 = max(0, bx0 - grow), max(0, by0 - grow)
    x1, y1 = min(w, bx1 + grow), min(h, by1 + grow)
    sub = to_lab(map_bgr[y0:y1, x0:x1])
    dark = (sub[..., 0] < 55).astype(np.uint8)
    dark &= (text_mask[y0:y1, x0:x1] == 0).astype(np.uint8)  # the letters themselves don't count
    n, cc, stats, cents = cv2.connectedComponentsWithStats(dark, connectivity=8)
    best, best_d = None, 1e9
    cx0, cy0 = (bx0 + bx1) / 2, (by0 + by1) / 2
    for i in range(1, n):
        _, _, cw, ch, area = stats[i]
        if not 6 <= area <= 400 or not 0.4 <= cw / max(ch, 1) <= 2.5:
            continue
        if area / (cw * ch) < 0.5:
            continue
        px, py = cents[i][0] + x0, cents[i][1] + y0
        d = (px - cx0) ** 2 + (py - cy0) ** 2
        if d < best_d:
            best, best_d = [float(px), float(py)], d
    return best


# --------------------------------------------------------------------------- runner

KIND_COLORS = {  # BGR, for the debug overlay
    "capital": (0, 0, 255), "city": (0, 140, 255), "river_label": (255, 128, 0),
    "region_label": (0, 180, 0), "line_label": (128, 0, 128), "other": (128, 128, 128),
}


def run_step3(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .isolate import run_step2

    out_dir = runs_dir / image_path.stem
    if not (out_dir / "map_area.png").exists() or not (out_dir / "geometry.json").exists():
        run_step2(image_path, model=model, runs_dir=runs_dir)
    sem = MapSemantics.model_validate_json(
        (out_dir / "step1_semantics.json").read_text(encoding="utf-8"))
    require_pipeline_eligible(sem, "Step 3")
    geo = json.loads((out_dir / "geometry.json").read_text(encoding="utf-8"))

    map_bgr = imread(out_dir / "map_area.png")
    mh, mw = map_bgr.shape[:2]
    cx0, cy0 = geo["map_crop"][0], geo["map_crop"][1]

    # Step 2 saves the exact furniture-blanked input for review. Recreate it
    # here only for runs made before that preview artifact existed.
    text_input_path = out_dir / "map_text_input.png"
    clean = (imread(text_input_path) if text_input_path.exists()
             else prepare_text_input(
                 map_bgr, geo["map_crop"], geo["furniture"],
                 imread(out_dir / "map_mask.png")[..., 0],
             ))
    input_signature = _text_input_signature(clean)
    furniture_local = []
    for f in geo["furniture"]:
        fx0, fy0, fx1, fy1 = f["box"]
        lx0, ly0 = max(0, fx0 - cx0), max(0, fy0 - cy0)
        lx1, ly1 = min(mw, fx1 - cx0), min(mh, fy1 - cy0)
        if lx1 > lx0 and ly1 > ly0:
            furniture_local.append((lx0, ly0, lx1, ly1))

    raw_path = out_dir / "step3_raw.json"
    raw_signature_path = out_dir / "step3_raw.sha256"
    if raw_path.exists() and _cache_matches(raw_signature_path, input_signature):
        raw_payload = json.loads(raw_path.read_text(encoding="utf-8"))
        if isinstance(raw_payload, dict):
            items = raw_payload.get("items", [])
        else:  # defensive support for a manually preserved pre-v3 cache
            items = raw_payload
        raw_cached = True
    else:
        items = detect_text(clean, sem, model)
        raw_path.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
        raw_signature_path.write_text(input_signature, encoding="ascii")
        raw_cached = False

    vocab = _vocabulary(sem)

    # Pixel-true localization: CRAFT boxes replace Gemini's coarse ones.
    # Do not run OCR when Step 1 explicitly found no overlay text: it would
    # only manufacture false positives from map texture or linework.
    craft_path = out_dir / "step3_craft.json"
    craft_signature_path = out_dir / "step3_craft.sha256"
    if not _expects_overlay_text(sem):
        craft_dets = []
    elif craft_path.exists() and _cache_matches(craft_signature_path, input_signature):
        craft_dets = json.loads(craft_path.read_text(encoding="utf-8"))
    else:
        craft_dets = craft_detect(clean)
        if craft_dets is not None:
            craft_path.write_text(json.dumps(craft_dets, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
            craft_signature_path.write_text(input_signature, encoding="ascii")
    items = fuse_detections(items, craft_dets, float(np.hypot(mw, mh)), vocab)
    warnings: list[str] = []
    text_mask = np.zeros((mh, mw), np.uint8)
    labels: list[dict] = []
    margin = max(6, int(0.005 * max(mw, mh)))
    for it in items:
        bx = [max(0, it["box"][0]), max(0, it["box"][1]), min(mw, it["box"][2]), min(mh, it["box"][3])]
        if bx[2] - bx[0] < 6 or bx[3] - bx[1] < 6:
            continue  # degenerate box -- hallucination fingerprint
        ix, iy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
        if any(fx0 - margin <= ix <= fx1 + margin and fy0 - margin <= iy <= fy1 + margin
               for fx0, fy0, fx1, fy1 in furniture_local):
            continue  # furniture text that slipped through
        if it.get("localization") == "gemini-unverified" and it["kind"] not in ("city", "capital"):
            # no detector confirmation and no downstream consumer: extracting
            # strokes here can only erase real map content -- do nothing
            strokes = {"found": False}
        else:
            strokes = extract_strokes(clean, bx)
        if strokes["found"] and not strokes_look_like_text(strokes, it["text"]):
            warnings.append(f"strokes for '{it['text']}' do not look like text "
                            f"(detection box likely offset) -- discarded")
            strokes = {"found": False}
        entry = {
            "text": it["text"],
            "kind": it["kind"],
            "priority": KIND_PRIORITY[TextKind(it["kind"])],
            "matches_step1": any(
                difflib.SequenceMatcher(None, it["text"].casefold(), name.casefold()).ratio() >= 0.75
                for name in vocab
            ),
            "localization": it.get("localization", "gemini"),
            "recognition_status": it.get("recognition_status", "gemini-only"),
            "gemini_text": it.get("gemini_text", it["text"]),
            "easyocr_text": it.get("easyocr_text"),
            "easyocr_conf": it.get("easyocr_conf"),
            "text_similarity": it.get("text_similarity"),
            "box": bx,
            "quad": None,
            "text_position": [ix, iy],
            "text_position_source": "box_center",
            "feature_position": None,
            "feature_position_source": None,
            "anchor": [ix, iy],
            "anchor_source": "box_center",
            "mask_found": strokes["found"],
        }
        if strokes["found"]:
            ox, oy = strokes["origin"]
            m = strokes["mask"]
            text_mask[oy:oy + m.shape[0], ox:ox + m.shape[1]] |= m * 255
            entry["quad"] = strokes["quad"]
            entry["text_position"] = strokes["centroid"]
            entry["text_position_source"] = "stroke_centroid"
            entry["anchor"] = strokes["centroid"]
            entry["anchor_source"] = "stroke_centroid"
        else:
            if it["kind"] in ("city", "capital"):
                warnings.append(
                    f"no strokes extracted for '{it['text']}' -- Step 4 will fill its city box"
                )
            else:
                warnings.append(
                    f"no strokes extracted for '{it['text']}' -- no precise removal mask; "
                    "Step 4 will not fill this long-label box"
                )
        if it["kind"] in ("city", "capital"):
            dot = find_point_symbol(clean, bx, text_mask)
            if dot:
                entry["feature_position"] = dot
                entry["feature_position_source"] = "point_symbol"
                entry["anchor"] = dot
                entry["anchor_source"] = "point_symbol"
        labels.append(entry)

    unmatched = [lb["text"] for lb in labels if not lb["matches_step1"]]
    if unmatched:
        warnings.append("not in Step 1 vocabulary (verify on debug overlay): " + ", ".join(unmatched[:12]))

    (out_dir / "labels.json").write_text(json.dumps({
        "coordinate_space": "map_area.png pixels; add map_crop offset for original image",
        "map_crop_offset": [cx0, cy0],
        "labels": labels,
        "warnings": warnings,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    imwrite(out_dir / "text_mask.png", text_mask)
    from .labelreview import write_text_removal_mask
    write_text_removal_mask(out_dir)

    # Render the checkpoint over the exact furniture-blanked image used for
    # detection. Using map_area.png here made excluded legend/scale text appear
    # in the preview even though neither Gemini nor CRAFT received those pixels.
    dbg = clean.copy()
    overlay = dbg.copy()
    overlay[text_mask > 0] = (255, 0, 255)
    dbg = cv2.addWeighted(overlay, 0.45, dbg, 0.55, 0)
    for lb in labels:
        color = KIND_COLORS[lb["kind"]]
        if lb["quad"]:
            cv2.polylines(dbg, [np.array(lb["quad"], np.int32)], True, color, 2)
        else:
            # unverified/unused boxes drawn thin: recorded but nothing acts on them
            thin = 1 if lb["localization"] == "gemini-unverified" else 2
            bx0, by0, bx1, by1 = lb["box"]
            cv2.rectangle(dbg, (bx0, by0), (bx1, by1), color, thin)
        ax, ay = int(lb["anchor"][0]), int(lb["anchor"][1])
        cv2.circle(dbg, (ax, ay), 4, color, -1)
    if max(dbg.shape[:2]) > 1600:
        s = 1600 / max(dbg.shape[:2])
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step3_debug.png", dbg)

    kinds: dict[str, int] = {}
    for lb in labels:
        kinds[lb["kind"]] = kinds.get(lb["kind"], 0) + 1
    return {
        "out_dir": out_dir, "raw_cached": raw_cached,
        "total": len(labels),
        "kinds": kinds, "masked": sum(1 for lb in labels if lb["mask_found"]),
        "warnings": warnings,
    }
