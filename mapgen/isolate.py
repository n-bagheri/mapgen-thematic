"""Step 2 -- Isolating the main map area and legend.

Hybrid approach: Gemini supplies coarse layout bounding boxes (map components
including islands, legend, furniture); classic CV refines them to pixel
precision, builds the map content mask, detects legend swatches, and samples
their colors. Legend labels are NOT re-OCR'd here: Step 1 already transcribed
the legend entries in reading order, so detected swatches (ordered
column-major) are zipped with those labels.

Artifacts per map, under runs/<name>/:
    step2_layout.json   raw VLM boxes (cached; delete to force a fresh call)
    step2_layout_debug.png raw VLM boxes before any CV refinement
    map_area.png        cropped map content
    map_mask.png        binary content mask, same crop
    map_text_input.png  map crop with furniture blanked (Step 3 input)
    legend.png          cropped legend (when present)
    classes.json        label -> sampled color table + warnings (Step 4 seeds)
    geometry.json       all boxes in original-image pixel coords
    step2_debug.png     annotated overlay for human review (Checkpoint A)
"""

from __future__ import annotations

import json
import re
import mimetypes
import os
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field, model_validator

from .semantics import (
    encode_for_model,
    generate_json,
    DEFAULT_MODEL,
    MapSemantics,
    require_pipeline_eligible,
)

LAYOUT_RETRIES = 1  # a deadline or 5xx on this call is usually transient

# --------------------------------------------------------------------------- layout call

LAYOUT_PROMPT = """\
Locate the layout elements of this thematic map image. Return bounding boxes
as [y_min, x_min, y_max, x_max], normalized to 0-1000.

- map_areas: return ONE SEPARATE BOX for EACH geographically detached component
  of the mapped territory. Include the mainland, islands, overseas territories,
  and displaced territorial components, as well as water shown as part of the
  map picture. Keep each box tight and exclude surrounding page margins.
- Detached components that use the same thematic colors, legend, and symbology
  as the main territory belong in map_areas, NOT in other. This remains true
  when an island is moved closer to the mainland for page layout. For example,
  Corsica on a thematic map of France must be a separate map_areas entry.
- Before calling a detached colored shape an inset, compare its colors and
  symbols with the main map. If it uses the same thematic legend, classify it
  as a detached mapped-territory component. Never return it in other.
- Call something an inset map only when it is an independently framed secondary
  map, normally with its own scale, title, locator context, or different extent.
  An unframed detached island is not an inset.
- legend: tight box around the legend (color swatches + their labels + the
  legend heading). null if there is no legend.
- A legend is whatever keys the map's colors, however it is printed. It is
  still the legend when the swatches sit inside a large data table with many
  columns and hundreds of rows of supporting detail: box the whole table.
  Return it as legend, never in other, whenever it carries color swatches
  that match colors used on the map.
- title, scale_bar, north_arrow: when present, else null.
- other: inset maps, notes, logos, coordinate labels or anything else that is
  not map content, each with a short label.
"""


class LayoutBox(BaseModel):
    # The length bounds are load-bearing: with the schema supplied as text
    # (the Gemma path), Pydantic is the only validator, and box_to_px unpacks
    # exactly four values.  A wrong-length box must fail validation so the
    # model call retries instead of crashing Step 2 mid-run.
    box_2d: list[int] = Field(min_length=4, max_length=4,
                              description="[y_min, x_min, y_max, x_max] normalized to 0-1000")
    label: str


class MapLayout(BaseModel):
    map_areas: list[LayoutBox] = Field(
        min_length=1,
        description="One tight box per detached component of the mapped territory",
    )
    legend: LayoutBox | None
    title: LayoutBox | None
    scale_bar: LayoutBox | None
    north_arrow: LayoutBox | None
    other: list[LayoutBox]

    @model_validator(mode="before")
    @classmethod
    def _upgrade_cached_layout(cls, data: object) -> object:
        """Read pre-multi-component caches that stored one singular map_area."""
        if isinstance(data, dict) and "map_areas" not in data and data.get("map_area"):
            data = {**data, "map_areas": [data["map_area"]]}
            data.pop("map_area", None)
        # Gemma answers the singular box fields with a one-element list about as
        # often as with the object the schema asks for, and the whole layout
        # call then fails validation over a wrapper.  Unwrap it: with several
        # boxes keep the largest, which is the legend itself rather than one of
        # the panels a fragmented answer splits it into.
        if isinstance(data, dict):
            data = {**data}
            for field in ("legend", "title", "scale_bar", "north_arrow"):
                value = data.get(field)
                if isinstance(value, list):
                    boxes = [b for b in value if isinstance(b, dict) and b.get("box_2d")]
                    data[field] = max(
                        boxes,
                        key=lambda b: ((b["box_2d"][2] - b["box_2d"][0])
                                       * (b["box_2d"][3] - b["box_2d"][1])),
                    ) if boxes else None
        return data


def detect_layout(image_path: Path, model: str | None = None) -> MapLayout:
    from google.genai import types

    # The boxes come back normalized to 0-1000 and are mapped onto the original
    # pixels below, so showing the model a downscaled copy costs no accuracy and
    # keeps a large scan from timing out on upload and inference.
    data, mime = encode_for_model(image_path)
    # generate_json owns every model call: the shared deadline, the retry
    # classification, and the Gemma path that supplies the response schema as
    # text because Gemma's server-side response_schema option stalls into
    # DEADLINE_EXCEEDED.  Step 1 has always gone through it; Step 2 must too.
    return generate_json(
        [types.Part.from_bytes(data=data, mime_type=mime), LAYOUT_PROMPT],
        MapLayout, model=model, temperature=0.0, retries=LAYOUT_RETRIES)


# --------------------------------------------------------------------------- cv helpers

def imread(path: Path) -> np.ndarray:
    # cv2.imread cannot handle non-ASCII Windows paths (e.g. "Köppen...")
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"cannot decode image: {path}")
    return img


def imwrite(path: Path, img: np.ndarray) -> None:
    ok, buf = cv2.imencode(path.suffix, img)
    if not ok:
        raise ValueError(f"cannot encode {path}")
    buf.tofile(str(path))


def thinning(image: np.ndarray) -> np.ndarray:
    """Zhang-Suen skeletonization, with a legible error when cv2 lacks it.

    `thinning` lives in opencv-contrib-python.  Installing plain opencv-python
    alongside it overwrites the shared native module and leaves cv2.ximgproc as
    an empty stub, so the bare AttributeError names the symbol but not the
    cause.  Steps 4, 7 and 7b all depend on this, so say what to fix instead.
    """
    ximgproc = getattr(cv2, "ximgproc", None)
    thin = getattr(ximgproc, "thinning", None)
    if thin is None:
        raise RuntimeError(
            "cv2.ximgproc.thinning is unavailable, so this build of OpenCV is "
            "missing the contrib modules. This usually means opencv-python is "
            "installed alongside opencv-contrib-python and has overwritten it. "
            "Reinstall with: pip uninstall -y opencv-python opencv-python-headless "
            "opencv-contrib-python && pip install opencv-contrib-python "
            f"(current cv2 {cv2.__version__} at {cv2.__file__})"
        )
    # Resolved after the guard: on a stubbed ximgproc the constant is missing
    # too, and reading it first would raise the same opaque AttributeError.
    return thin(image, thinningType=ximgproc.THINNING_ZHANGSUEN)


def to_lab(img_bgr: np.ndarray) -> np.ndarray:
    """float32 Lab with L in 0..100 (CIE scale, usable for delta-E)."""
    return cv2.cvtColor(img_bgr.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)


def box_to_px(box: LayoutBox, w: int, h: int, pad_frac: float = 0.0) -> tuple[int, int, int, int]:
    y0, x0, y1, x1 = box.box_2d
    px, py = int(pad_frac * w), int(pad_frac * h)
    return (
        max(0, int(x0 / 1000 * w) - px), max(0, int(y0 / 1000 * h) - py),
        min(w, int(x1 / 1000 * w) + px), min(h, int(y1 / 1000 * h) + py),
    )


def border_median_lab(lab: np.ndarray, strip: int = 10) -> np.ndarray:
    parts = [lab[:strip], lab[-strip:], lab[:, :strip], lab[:, -strip:]]
    return np.median(np.concatenate([p.reshape(-1, 3) for p in parts]), axis=0)


# A legend side counts as background when it would not read as foreground
# against the best side, using the same delta-E the swatch masks use.
PAPER_SIDE_TOLERANCE = 10.0


def legend_paper_lab(lab: np.ndarray, strip: int = 10) -> np.ndarray:
    """Background Lab of a legend crop, ignoring sides that caught map content.

    A legend box cut from a map sheet often includes map content along one or
    two sides: the Africa ethnolinguistic sheet puts sea above and to the left
    of its legend table.  Pooling all four borders then returns a median
    halfway between paper and sea that matches no pixel at all -- every paper
    pixel reads as foreground, the crop collapses into a single blob, and no
    swatch survives the size filter.  A legend is mostly its own background,
    so rank the sides by agreement with the crop's dominant colour and pool
    only those that agree with the best one.  An inset legend printed straight
    onto the map has no paper, but all four of its sides then agree on the map
    colour and the result is the plain border median.
    """
    sides = [lab[:strip], lab[-strip:], lab[:, :strip], lab[:, -strip:]]
    medians = [np.median(side.reshape(-1, 3), axis=0) for side in sides]
    inner = lab[strip:-strip, strip:-strip] if min(lab.shape[:2]) > 4 * strip else lab
    dominant = np.median(inner.reshape(-1, 3), axis=0)
    best = min(medians, key=lambda m: float(np.linalg.norm(m - dominant)))
    agreeing = [side for side, median in zip(sides, medians)
                if float(np.linalg.norm(median - best)) <= PAPER_SIDE_TOLERANCE]
    return np.median(
        np.concatenate([side.reshape(-1, 3) for side in agreeing]), axis=0)


NAMED_COLORS = {  # semantic color hints used for water recovery in Step 4
    "black": (0, 0, 0), "dark gray": (80, 80, 80), "gray": (150, 150, 150),
    "white": (250, 250, 250), "cream": (238, 228, 200), "red": (205, 40, 40),
    "dark red": (130, 10, 30), "orange": (250, 140, 30), "light orange": (250, 190, 120),
    "brown": (140, 85, 30), "yellow": (245, 220, 50), "pale yellow": (243, 238, 185),
    "olive": (130, 130, 20), "light green": (155, 205, 115), "green": (70, 160, 60),
    "dark green": (10, 100, 45), "teal": (20, 140, 140), "cyan": (100, 200, 230),
    "light blue": (160, 205, 240), "blue": (60, 100, 200), "dark blue": (25, 45, 120),
    "purple": (135, 85, 165), "magenta": (200, 60, 160), "pink": (240, 170, 190),
}
_NAMED_LAB = {
    name: cv2.cvtColor(np.float32([[rgb]])[..., ::-1] / 255.0, cv2.COLOR_BGR2Lab)[0, 0]
    for name, rgb in NAMED_COLORS.items()
}
def prepare_text_input(
    map_bgr: np.ndarray,
    map_crop: tuple[int, int, int, int] | list[int],
    furniture: list,
    map_mask: np.ndarray | None = None,
) -> np.ndarray:
    """Blank Step 2 furniture on the map crop exactly as Step 3 sees it.

    Keep a context halo around the geographic mask.  Labels for coastal cities
    and small islands are commonly typeset partly outside the filled territory;
    clipping to the exact map mask used to erase those characters before the
    text detectors ran (for example, Ajaccio beside Corsica).
    """
    clean = map_bgr.copy()
    mh, mw = clean.shape[:2]
    cx0, cy0 = int(map_crop[0]), int(map_crop[1])
    lab = to_lab(map_bgr)
    # Use the lightest low-chroma pixels as the paper/background color.
    # A crop border can contain colored geography or black frame ink, so its
    # median is not a reliable fill color.
    neutral = (np.linalg.norm(lab[..., 1:], axis=2) < 6) & (lab[..., 0] > 75)
    bg_lab = (np.median(lab[neutral], axis=0) if np.any(neutral)
              else np.array([100.0, 0.0, 0.0], np.float32))
    bg = cv2.cvtColor(
        bg_lab.reshape(1, 1, 3).astype(np.float32), cv2.COLOR_Lab2BGR,
    ).reshape(3) * 255
    geography_protect = None
    if map_mask is not None:
        if map_mask.shape != (mh, mw):
            raise ValueError("map mask and map crop must have the same dimensions")
        # Scale with the image because label size does too.  Furniture is
        # blanked below with its own padding, so expanding the geographic
        # context cannot reintroduce legend/title text.
        # Five percent covers roughly half of a long horizontal place name
        # when its anchor lies on a coastline.  That is the common worst case
        # for labels overhanging a small component.
        context_px = max(16, int(round(0.05 * max(mw, mh))))
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * context_px + 1, 2 * context_px + 1),
        )
        geographic = (map_mask > 0).astype(np.uint8)
        text_context = cv2.dilate(geographic, kernel)
        # Never let an imprecise/padded furniture box cut into mapped
        # territory. One extra pixel protects the anti-aliased coastline.
        geography_protect = cv2.dilate(geographic, np.ones((3, 3), np.uint8)).astype(bool)
        clean[text_context == 0] = bg
    furniture_pad = max(8, int(0.015 * max(mw, mh)))
    for item in furniture:
        box = item["box"] if isinstance(item, dict) else item[1]
        fx0, fy0, fx1, fy1 = box
        lx0 = max(0, fx0 - cx0 - furniture_pad)
        ly0 = max(0, fy0 - cy0 - furniture_pad)
        lx1 = min(mw, fx1 - cx0 + furniture_pad)
        ly1 = min(mh, fy1 - cy0 + furniture_pad)
        if lx1 > lx0 and ly1 > ly0:
            region = clean[ly0:ly1, lx0:lx1]
            if geography_protect is None:
                region[:] = bg
            else:
                # Furniture padding may overlap a coastline even when the
                # actual legend/title does not (France below Perpignan is one
                # example). Blank only non-geographic pixels in that overlap.
                protected = geography_protect[ly0:ly1, lx0:lx1]
                region[~protected] = bg
    return clean


# --------------------------------------------------------------------------- map mask

MIN_COMPONENT_PX = 100


def _looks_like_edge_furniture(stats: np.ndarray, index: int,
                               envelope: tuple[int, int, int, int],
                               image_shape: tuple[int, int]) -> bool:
    """Identify an isolated, ruler-like component at a proposed map edge.

    This is deliberately stricter than the neatline test below.  It only
    removes an *independent* component that is extremely thin, long, and
    coincident with the outside of the AI-proposed map envelope.  Geographic
    outlines remain connected to filled regions, while detached islands are
    far less elongated, so both are retained.
    """
    h, w = image_shape
    x = int(stats[index, cv2.CC_STAT_LEFT])
    y = int(stats[index, cv2.CC_STAT_TOP])
    cw = int(stats[index, cv2.CC_STAT_WIDTH])
    ch = int(stats[index, cv2.CC_STAT_HEIGHT])
    x1, y1 = x + cw, y + ch
    ex0, ey0, ex1, ey1 = envelope
    edge_pad = max(4, int(0.02 * max(w, h)))
    vertical_ruler = (
        ch >= 0.35 * h
        and cw <= max(4, int(0.018 * w))
        and (x <= ex0 + edge_pad or x1 >= ex1 - edge_pad)
    )
    horizontal_ruler = (
        cw >= 0.35 * w
        and ch <= max(4, int(0.018 * h))
        and (y <= ey0 + edge_pad or y1 >= ey1 - edge_pad)
    )
    return vertical_ruler or horizontal_ruler


def refine_map_mask(
    img: np.ndarray,
    map_boxes: list[tuple[int, int, int, int]],
    exclude_boxes: list[tuple[int, int, int, int]],
    bg_delta: float = 8.0,
) -> tuple[np.ndarray, tuple[int, int, int, int], list[str]]:
    """Pixel-tight content mask seeded by the (padded) VLM map box.

    `bg_delta` separates the printed map from the page it sits on.  A map often
    has more than one background: the sea in its own colour, and neighbouring
    territory in a light grey that is only about 11 dE from white paper.  At the
    old 12.0 the grey fell on the paper side, so whole countries were cut out of
    the mask -- France on the Spain sheet, the surrounding states on the China
    sheet.  8.0 keeps that grey while leaving paper and its anti-aliasing out.

    A VLM box is a spatial prior, not an absolute geographic boundary.  For a
    broad, landscape map panel (notably a world map), search the full width of
    the same vertical band and retain detached continents/islands.  Narrower
    single-component layouts keep the conservative dominant-envelope cleanup
    used to reject nearby frame/tick fragments.
    """
    h, w = img.shape[:2]
    lab = to_lab(img)
    bg = border_median_lab(lab)
    content = (np.linalg.norm(lab - bg, axis=2) > bg_delta).astype(np.uint8)
    content = cv2.morphologyEx(content, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    ux0 = min(box[0] for box in map_boxes)
    uy0 = min(box[1] for box in map_boxes)
    ux1 = max(box[2] for box in map_boxes)
    uy1 = max(box[3] for box in map_boxes)
    box_w, box_h = ux1 - ux0, uy1 - uy0
    broad_map_band = (
        len(map_boxes) == 1
        and box_w >= 0.72 * w
        and box_w / max(1, box_h) >= 1.35
    )
    search_boxes = ([(0, uy0, w, uy1)] if broad_map_band else map_boxes)

    inside = np.zeros_like(content)
    for x0, y0, x1, y1 in search_boxes:
        inside[y0:y1, x0:x1] = content[y0:y1, x0:x1]
    for ex0, ey0, ex1, ey1 in exclude_boxes:
        inside[ey0:ey1, ex0:ex1] = 0

    warnings: list[str] = []

    # A VLM box says where the map is, not exactly what it covers, and the
    # box is not reproducible run to run: the same sheet can come back with
    # x1=783 on one call and x1=642 on the next, and clipping content to the
    # rectangle stamps that accident into the mask as a dead-straight edge.
    # Grow the seeded mask through connected content instead (binary
    # reconstruction): every content component that touches a seed is kept
    # whole, so the mask ends where the printed panel ends.  Furniture stays
    # carved out of the growth medium, and anything detached from the seeded
    # panel (titles, decorations, other panels) is still excluded.
    reachable = content.copy()
    for ex0, ey0, ex1, ey1 in exclude_boxes:
        reachable[ey0:ey1, ex0:ex1] = 0
    _, reach_cc = cv2.connectedComponents(reachable, connectivity=8)
    seeded_labels = np.unique(reach_cc[inside > 0])
    seeded_labels = seeded_labels[seeded_labels != 0]
    if seeded_labels.size:
        grown = np.isin(reach_cc, seeded_labels).astype(np.uint8)
        added = int(grown.sum()) - int(inside.sum())
        if added > 0.15 * max(1, int(inside.sum())):
            warnings.append(
                f"map content continued past the VLM map box; the mask grew by "
                f"{added} px to follow it")
        inside = grown

    n, cc, stats, _ = cv2.connectedComponentsWithStats(inside, connectivity=8)
    min_area = max(MIN_COMPONENT_PX, int(2e-5 * w * h))
    keep: list[int] = []
    rejected_neatlines = 0
    rejected_edge_furniture = 0
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        cw = int(stats[i, cv2.CC_STAT_WIDTH])
        ch = int(stats[i, cv2.CC_STAT_HEIGHT])
        fill = area / max(1, cw * ch)
        # A printed neatline/tick frame is a very sparse component whose
        # bounding rectangle spans most of the image. If the VLM returns the
        # whole plotting frame as map_area, retaining this component makes the
        # refinement snap to the frame rather than to the colored territory.
        # The conservative image-span checks avoid rejecting ordinary thin
        # linework or small detached islands.
        looks_like_neatline = (
            cw >= 0.75 * w
            and ch >= 0.75 * h
            and fill <= 0.08
        )
        if looks_like_neatline:
            rejected_neatlines += 1
        elif _looks_like_edge_furniture(stats, i, (ux0, uy0, ux1, uy1), (h, w)):
            rejected_edge_furniture += 1
        else:
            keep.append(i)
    # With one broad VLM box, sparse ticks and clipped frame segments can be
    # separate components rather than one recognizable neatline. Keep the
    # dominant geographic component plus components spatially contained by
    # its envelope; reject isolated components just beyond it.
    if len(map_boxes) == 1 and keep and not broad_map_band:
        main = max(keep, key=lambda i: int(stats[i, cv2.CC_STAT_AREA]))
        mx = int(stats[main, cv2.CC_STAT_LEFT])
        my = int(stats[main, cv2.CC_STAT_TOP])
        mx1 = mx + int(stats[main, cv2.CC_STAT_WIDTH])
        my1 = my + int(stats[main, cv2.CC_STAT_HEIGHT])
        pad_x, pad_y = max(4, int(0.01 * w)), max(4, int(0.01 * h))
        envelope = (mx - pad_x, my - pad_y, mx1 + pad_x, my1 + pad_y)
        spatial_keep: list[int] = []
        for i in keep:
            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            x1 = x + int(stats[i, cv2.CC_STAT_WIDTH])
            y1 = y + int(stats[i, cv2.CC_STAT_HEIGHT])
            intersects = (
                x1 > envelope[0] and x < envelope[2]
                and y1 > envelope[1] and y < envelope[3]
            )
            if i == main or intersects:
                spatial_keep.append(i)
        rejected_strays = len(keep) - len(spatial_keep)
        keep = spatial_keep
        if rejected_strays:
            warnings.append(
                f"excluded {rejected_strays} frame/tick component(s) outside "
                "the geographic content envelope"
            )
    if rejected_neatlines:
        warnings.append(
            f"excluded {rejected_neatlines} sparse outer neatline/tick frame "
            "from the map mask"
        )
    if rejected_edge_furniture:
        warnings.append(
            f"excluded {rejected_edge_furniture} isolated edge ruler/tick component(s) "
            "from the map mask"
        )
    if broad_map_band:
        original_zone = np.zeros_like(content)
        for x0, y0, x1, y1 in map_boxes:
            original_zone[y0:y1, x0:x1] = 1
        recovered = int(np.count_nonzero((inside > 0) & (original_zone == 0)))
        if recovered:
            warnings.append(
                f"broad map panel: recovered {recovered} content pixels outside "
                "the VLM box but inside its geographic band"
            )
    if not keep:
        warnings.append("map mask empty after refinement; falling back to the raw VLM boxes")
        mask = np.zeros((h, w), np.uint8)
        for x0, y0, x1, y1 in map_boxes:
            mask[y0:y1, x0:x1] = 255
        ys, xs = np.nonzero(mask)
        fallback = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1)
        return mask, fallback, warnings

    mask = (np.isin(cc, keep)).astype(np.uint8) * 255
    xs0 = int(min(stats[i, cv2.CC_STAT_LEFT] for i in keep))
    ys0 = int(min(stats[i, cv2.CC_STAT_TOP] for i in keep))
    xs1 = int(max(stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] for i in keep))
    ys1 = int(max(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in keep))
    pad = 8
    tight = (max(0, xs0 - pad), max(0, ys0 - pad), min(w, xs1 + pad), min(h, ys1 + pad))

    # content the VLM box (or exclusions) cut off -> human should check the overlay
    outside = content.copy()
    for x0, y0, x1, y1 in search_boxes:
        outside[y0:y1, x0:x1] = 0
    for ex0, ey0, ex1, ey1 in exclude_boxes:
        outside[ey0:ey1, ex0:ex1] = 0
    kept_area = int(np.count_nonzero(mask))
    if np.count_nonzero(outside) > 0.05 * kept_area:
        warnings.append("significant non-background content outside the VLM map boxes "
                        "(check step2_debug.png for clipped islands)")
    return mask, tight, warnings


# --------------------------------------------------------------------------- legend swatches

def _uniform(lab_patch: np.ndarray, tol: float = 7.0) -> bool:
    return lab_patch.size > 0 and float(lab_patch.reshape(-1, 3).std(axis=0).mean()) < tol


def _erode_rect(x: int, y: int, w: int, h: int, frac: float = 0.22) -> tuple[int, int, int, int]:
    dx, dy = max(1, int(w * frac)), max(1, int(h * frac))
    return x + dx, y + dy, max(1, w - 2 * dx), max(1, h - 2 * dy)


def _swatch_ok(lab: np.ndarray, rect: tuple[int, int, int, int],
               bg: np.ndarray | None = None) -> bool:
    ex, ey, ew, eh = _erode_rect(*rect)
    patch = lab[ey:ey + eh, ex:ex + ew]
    if not _uniform(patch):
        return False
    if bg is None:
        return True
    # Closed glyphs such as 0, 6, 8, 9, and letters expose a very uniform
    # paper-coloured interior.  Their external contour can therefore look
    # more rectangular than the actual legend swatches.  A real swatch must
    # have an interior colour that is materially different from the paper.
    median = np.median(patch.reshape(-1, 3), axis=0)
    return float(np.linalg.norm(median - bg)) >= 4


def _textured_swatch_ok(lab: np.ndarray, rect: tuple[int, int, int, int],
                        bg: np.ndarray) -> bool:
    """Accept a visibly coloured but non-uniform printed swatch.

    Historical maps often use halftone/ink texture inside otherwise ordinary
    rectangular swatches.  Text glyphs remain rejected because their eroded
    interior is mostly paper; a true textured swatch is coloured throughout.
    """
    ex, ey, ew, eh = _erode_rect(*rect)
    patch = lab[ey:ey + eh, ex:ex + ew]
    if patch.size == 0:
        return False
    distance = np.linalg.norm(patch - bg, axis=2)
    return bool(np.median(distance) >= 10 and np.mean(distance >= 10) >= 0.85)


def _split_merged(
    binmask: np.ndarray, lab: np.ndarray, rect: tuple[int, int, int, int],
    med_w: float, med_h: float, bg: np.ndarray,
) -> list[tuple[int, int, int, int]]:
    """Recover swatches from a blob that merged several of them (anti-aliasing
    halos bridge tightly stacked swatches) or a swatch fused with its label."""
    x, y, w, h = rect
    out: list[tuple[int, int, int, int]] = []
    if 0.5 * med_w <= w <= 1.8 * med_w and h > 1.6 * med_h:
        # vertical stack: adjacent swatches can touch with no mask gap at all,
        # so bisect recursively at the row with the strongest color change
        ex0 = x + max(1, int(w * 0.25))
        ex1 = x + w - max(1, int(w * 0.25))

        def rec(y0: int, hh: int, depth: int) -> None:
            if hh < 0.5 * med_h or depth > 4:
                return
            dy = max(1, int(hh * 0.15))  # skip halo rows when judging uniformity
            interior = lab[y0 + dy:y0 + hh - dy, ex0:ex1]
            if interior.size and _uniform(interior):
                med = np.median(interior.reshape(-1, 3), axis=0)
                if float(np.linalg.norm(med - bg)) >= 4:  # not bare paper
                    out.append((x, y0, w, hh))
                return
            seg = lab[y0:y0 + hh, ex0:ex1]
            rowmed = np.median(seg, axis=1)
            lo, hi = int(0.4 * med_h), hh - int(0.4 * med_h)
            if hi - lo < 2:
                return
            diffs = np.linalg.norm(rowmed[lo + 1:hi] - rowmed[lo:hi - 1], axis=1)
            cut = lo + 1 + int(np.argmax(diffs))
            rec(y0, cut, depth + 1)
            rec(y0 + cut, hh - cut, depth + 1)

        rec(y, h, 0)
    elif 0.5 * med_h <= h <= 1.8 * med_h and w > 1.8 * med_w:
        # swatch fused with the text to its right: test the leading rectangle
        sub = (x, y, int(med_w), h)
        if _swatch_ok(lab, sub, bg):
            out.append(sub)
    return out


def detect_horizontal_colorbar(
    legend_bgr: np.ndarray,
    expected: int,
    labels: list[str],
) -> tuple[list[tuple[int, int, int, int]], list[str]] | None:
    """Recover an ordered, segmented horizontal color bar.

    Generic contour filtering deliberately rejects very long rectangles, which
    is correct for ordinary legends but used to make connected color bars fall
    through to the holes inside their numeric labels.  Detect the bar as one
    long shallow component, split its ramp into equal cells, and locate a
    separate no-data patch on the same row when present.
    """
    if expected < 3 or len(labels) != expected:
        return None
    lh, lw = legend_bgr.shape[:2]
    lab = to_lab(legend_bgr)
    bg = legend_paper_lab(lab, strip=max(2, min(lh, lw) // 50))
    binmask = (np.linalg.norm(lab - bg, axis=2) > 8).astype(np.uint8)
    binmask = cv2.morphologyEx(binmask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(binmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    long_rects = []
    for contour in contours:
        x, y, rw, rh = cv2.boundingRect(contour)
        if (
            rw >= 0.45 * lw
            and 0.08 * lh <= rh <= 0.65 * lh
            and rw / max(1, rh) >= 8
        ):
            long_rects.append((x, y, rw, rh))
    if not long_rects:
        return None
    ramp = max(long_rects, key=lambda rect: rect[2] * rect[3])

    no_data_terms = ("no data", "nodata", "not available", "n/a")
    no_data_indices = [
        i for i, label in enumerate(labels)
        if any(term in label.strip().lower() for term in no_data_terms)
    ]
    if len(no_data_indices) > 1:
        return None
    ramp_count = expected - len(no_data_indices)
    if ramp_count < 2:
        return None

    rx, ry, rw, rh = ramp
    cell_w = rw / ramp_count
    if cell_w < 8:
        return None
    ramp_rects = []
    for i in range(ramp_count):
        x0 = int(round(rx + i * cell_w))
        x1 = int(round(rx + (i + 1) * cell_w))
        ramp_rects.append((x0, ry, max(1, x1 - x0), rh))

    # Confirm that this is a color ramp rather than a long rule or text
    # underline: its cell interiors must contain materially different colors.
    cell_labs = []
    for rect in ramp_rects:
        ex, ey, ew, eh = _erode_rect(*rect, frac=0.28)
        patch = lab[ey:ey + eh, ex:ex + ew]
        if not patch.size:
            return None
        cell_labs.append(np.median(patch.reshape(-1, 3), axis=0))
    max_delta = max(
        float(np.linalg.norm(cell_labs[i] - cell_labs[j]))
        for i in range(len(cell_labs))
        for j in range(i + 1, len(cell_labs))
    )
    if max_delta < 8:
        return None

    no_data_rect = None
    if no_data_indices:
        candidates = []
        for contour in contours:
            rect = cv2.boundingRect(contour)
            x, y, rw_, rh_ = rect
            if rect == ramp or x >= rx:
                continue
            overlap = max(0, min(y + rh_, ry + rh) - max(y, ry))
            if (
                overlap >= 0.5 * min(rh_, rh)
                and 0.35 * cell_w <= rw_ <= 1.5 * cell_w
                and 0.55 * rh <= rh_ <= 1.5 * rh
            ):
                candidates.append(rect)
        if not candidates:
            return None
        no_data_rect = max(candidates, key=lambda rect: rect[2] * rect[3])

    ordered: list[tuple[int, int, int, int]] = []
    ramp_i = 0
    for i in range(expected):
        if no_data_indices and i == no_data_indices[0]:
            assert no_data_rect is not None
            ordered.append(no_data_rect)
        else:
            ordered.append(ramp_rects[ramp_i])
            ramp_i += 1
    return ordered, [
        f"detected ordered horizontal color bar and split it into "
        f"{ramp_count} ramp cell(s)"
    ]


def _leading_number(label: str) -> float | None:
    """First number in a legend label, e.g. '100-200 m a.s.l.' -> 100.0."""
    match = re.search(r"-?\d+(?:[.,]\d+)?", label.replace(",", ""))
    return float(match.group()) if match else None


def _ramp_runs_upward(labels: list[str]) -> bool:
    """True when the first transcribed label sits at the BOTTOM of the bar.

    A vertical scale is drawn with its largest value at the top, so a legend
    transcribed in ascending order starts at the bottom and the cells have to be
    emitted upward to stay paired with it.  Unparseable labels keep plain
    reading order, which is what the horizontal bar assumes too.
    """
    values = [_leading_number(label) for label in labels]
    if any(value is None for value in values) or len(values) < 2:
        return False
    ascending = all(b > a for a, b in zip(values, values[1:]))
    return ascending


def _find_vertical_ramp(lab: np.ndarray) -> tuple[int, int, int, int] | None:
    """Locate a colour ramp as a column strip, without needing a background.

    An inset legend is drawn straight onto the map, so there is no paper colour
    to threshold against.  A ramp has its own signature instead: a narrow column
    that stays flat across its width while changing steadily down its height.
    """
    height, width = lab.shape[:2]
    best = None
    for x0 in range(0, max(1, width - 8)):
        for strip_w in (10, 14, 18, 24, 30):
            if x0 + strip_w > width:
                continue
            strip = lab[:, x0:x0 + strip_w]
            flat = np.linalg.norm(strip.std(axis=1), axis=1) < 3.0
            longest = run = 0
            end_row = 0
            for row, is_flat in enumerate(flat):
                run = run + 1 if is_flat else 0
                if run > longest:
                    longest, end_row = run, row
            if longest < 0.45 * height:
                continue
            top = end_row - longest + 1
            column = strip[top:end_row + 1].reshape(longest, -1, 3).mean(axis=1)
            # Plain background above and below the bar is flat too, so the run
            # can reach past both ends of the ramp.  Trim the constant stretches
            # at each end; what is left is the part that actually changes.
            moving = np.linalg.norm(np.diff(column, axis=0), axis=1) > 0.4
            if not moving.any():
                continue
            # `moving[i]` compares row i with row i+1, so the first changing
            # row is first+1 and the last one is `last`; taking the transition
            # rows themselves would keep a background row at each end and make
            # the ramp look like it returns to where it started.
            first = int(np.argmax(moving)) + 1
            last = len(moving) - int(np.argmax(moving[::-1]))
            if last - first < 0.45 * height:
                continue
            top += first
            longest = last - first
            column = column[first:last]
            span = float(np.linalg.norm(column.max(axis=0) - column.min(axis=0)))
            if span < 12:            # a plain rule or axis line, not a ramp
                continue
            # A ramp walks through colour space and keeps going.  A column that
            # crosses stacked swatches separated by paper keeps returning to the
            # same background, so its path is far longer than the distance
            # between its ends; that tells the two apart without needing to know
            # what the background is.
            steps = np.linalg.norm(np.diff(column, axis=0), axis=1).sum()
            ends = float(np.linalg.norm(column[-1] - column[0]))
            # Measured: a curved but genuine ramp runs about 5-7; a column
            # crossing stacked swatches returns to its starting colour, so
            # its ends nearly coincide and the ratio explodes.
            if steps > 20.0 * max(ends, 1e-6):
                continue
            score = longest * span
            if best is None or score > best[0]:
                best = (score, x0, strip_w, top, longest)
    if best is None:
        return None
    _, x0, strip_w, top, longest = best
    return x0, top, strip_w, longest


def detect_vertical_colorbar(
    legend_bgr: np.ndarray,
    expected: int,
    labels: list[str],
) -> tuple[list[tuple[int, int, int, int]], list[str]] | None:
    """Recover an ordered, segmented vertical colour bar.

    The mirror of detect_horizontal_colorbar, for the stacked ramps that
    hypsometric, temperature and rainfall legends use.  The bar carries no
    swatch edges for the generic contour pass to find, and an inset legend has
    no background to threshold against, so the ramp is located by its own shape.
    """
    if expected < 3 or len(labels) != expected:
        return None
    lab = to_lab(legend_bgr)
    ramp = _find_vertical_ramp(lab)
    if ramp is None:
        return None
    rx, ry, rw, rh = ramp

    cell_h = rh / expected
    if cell_h < 4:
        return None
    cells = []
    for index in range(expected):
        y0 = int(round(ry + index * cell_h))
        y1 = int(round(ry + (index + 1) * cell_h))
        cells.append((rx, y0, rw, max(1, y1 - y0)))

    # Confirm the cells really differ; a flat strip is a rule, not a ramp.
    medians = []
    for rect in cells:
        ex, ey, ew, eh = _erode_rect(*rect, frac=0.28)
        patch = lab[ey:ey + eh, ex:ex + ew]
        if not patch.size:
            return None
        medians.append(np.median(patch.reshape(-1, 3), axis=0))
    span = max(
        float(np.linalg.norm(medians[i] - medians[j]))
        for i in range(len(medians))
        for j in range(i + 1, len(medians))
    )
    if span < 8:
        return None

    upward = _ramp_runs_upward(labels)
    if upward:
        cells.reverse()
    direction = "bottom-up" if upward else "top-down"
    return cells, [
        f"detected ordered vertical colour bar and split it into {expected} "
        f"cell(s), read {direction}"
    ]


def _detect_compact_grid_swatches(
    legend_bgr: np.ndarray, expected: int,
) -> list[tuple[int, int, int, int]] | None:
    """Recover very small ordered swatches arranged in repeated columns."""
    if expected < 12:
        return None
    lh, lw = legend_bgr.shape[:2]
    hsv = cv2.cvtColor(legend_bgr, cv2.COLOR_BGR2HSV)
    mask = (hsv[:, :, 1] >= 40).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[int, int, int, int]] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if w < 4 or h < 4 or w > 0.25 * lw or h > 0.2 * lh:
            continue
        if not 0.7 <= w / h <= 3.0:
            continue
        if cv2.contourArea(contour) / (w * h) < 0.4:
            continue
        candidates.append((x, y, w, h))
    # A black separator can join several neighbouring cells into one contour.
    # We only need enough markers to establish the grid; the regular grid
    # reconstruction below will recover the cells inside joined components.
    if len(candidates) < max(4, expected // 2):
        return None

    med_w = float(np.median([r[2] for r in candidates]))
    centers = sorted(r[0] + r[2] / 2 for r in candidates)
    columns: list[list[tuple[int, int, int, int]]] = []
    for rect in sorted(candidates, key=lambda r: (r[0], r[1])):
        center = rect[0] + rect[2] / 2
        for column in columns:
            column_center = float(np.median([r[0] + r[2] / 2 for r in column]))
            if abs(center - column_center) <= 1.8 * med_w:
                column.append(rect)
                break
        else:
            columns.append([rect])
    columns.sort(key=lambda col: float(np.median([r[0] for r in col])))
    if len(columns) < 2 or len(columns) > 6:
        return None

    # Extra entries belong to the columns with the greatest vertical extent;
    # this handles the usual final black/no-data cell that has no saturation.
    counts = [expected // len(columns)] * len(columns)
    for index in sorted(range(len(columns)),
                        key=lambda i: (len(columns[i]), max(r[1] for r in columns[i])))[:expected % len(columns)]:
        counts[index] += 1

    ordered: list[tuple[int, int, int, int]] = []
    for column, target in zip(columns, counts):
        column.sort(key=lambda r: r[1])
        regular = [rect for rect in column
                   if rect[3] <= 1.5 * np.median([item[3] for item in column])]
        if not regular:
            return None
        width = int(round(np.median([r[2] for r in regular])))
        height = int(round(np.median([r[3] for r in regular])))
        x = int(round(np.median([r[0] for r in column])))
        ys = [r[1] for r in column]
        pitches = np.diff(ys)
        positive_pitches = pitches[pitches >= max(2.0, height * 0.8)]
        # The smallest valid gap is the grid pitch; larger gaps indicate the
        # missing cells inside a joined component, not a different row height.
        pitch = float(np.min(positive_pitches)) if positive_pitches.size else 0.0
        if pitch < max(5.0, height * 0.8):
            return None
        start = ys[0]
        for row in range(target):
            # Use the inferred grid for every row.  Retaining a tall observed
            # rectangle would otherwise consume two or more palette entries.
            y = int(round(start + row * pitch))
            cell_w, cell_h = width, height
            if y + cell_h > lh:
                if y - column[-1][1] > 1.5 * pitch:
                    return None
                y = lh - cell_h
            ordered.append((x, y, cell_w, cell_h))
    return ordered if len(ordered) == expected else None


def _detect_vertically_joined_swatches(
    lab: np.ndarray,
    contours: list[np.ndarray],
    expected: int,
    bg: np.ndarray,
) -> list[tuple[int, int, int, int]] | None:
    """Recover a compact vertical swatch stack whose borders touch.

    In small scanned legends, the black outlines of adjacent swatches often
    connect into one tall contour.  The generic detector rightly treats that
    contour as too large, but then text and icon fragments can become the only
    surviving candidates.  Split a tall, uniformly coloured stack into the
    known number of thematic entries before considering those fragments.
    """
    if expected < 2:
        return None
    height, width = lab.shape[:2]
    choices: list[tuple[float, list[tuple[int, int, int, int]]]] = []
    for contour in contours:
        x, y, swatch_w, stack_h = cv2.boundingRect(contour)
        if (
            swatch_w < 8
            or swatch_w > 0.35 * width
            or stack_h > 0.65 * height
            or stack_h < expected * max(5.0, 0.45 * swatch_w)
        ):
            continue
        rects = []
        for index in range(expected):
            y0 = int(round(y + index * stack_h / expected))
            y1 = int(round(y + (index + 1) * stack_h / expected))
            rects.append((x, y0, swatch_w, max(1, y1 - y0)))

        colours = []
        valid = True
        for rect in rects:
            ex, ey, ew, eh = _erode_rect(*rect)
            patch = lab[ey:ey + eh, ex:ex + ew]
            if not patch.size or not _uniform(patch, tol=11.0):
                valid = False
                break
            colour = np.median(patch.reshape(-1, 3), axis=0)
            if float(np.linalg.norm(colour - bg)) < 8.0:
                valid = False
                break
            colours.append(colour)
        if not valid:
            continue
        # A tall run of the same grey (or a text column) is not a palette.
        separation = min(
            float(np.linalg.norm(colours[i] - colours[j]))
            for i in range(len(colours))
            for j in range(i + 1, len(colours))
        )
        if separation < 8.0:
            continue
        choices.append((separation, rects))
    if not choices:
        return None
    return max(choices, key=lambda item: item[0])[1]


def detect_swatches(
    legend_bgr: np.ndarray,
    expected: int,
    labels: list[str] | None = None,
    ordered: bool = False,
) -> tuple[list[tuple[int, int, int, int]], list[str]]:
    """Find legend color swatches; returns rects in reading (column-major) order.

    A small scan's legend crop can hold swatches only a few pixels tall, which
    the erosion-based uniformity tests reject wholesale.  When detection at
    the native size does not produce the expected count on such a crop, retry
    on an upscaled copy and map the rectangles back; the upscale result is
    only trusted when it matches the expected count exactly, so behaviour on
    every legend that already worked is unchanged.
    """
    rects, warnings = _detect_swatches_at_scale(legend_bgr, expected, labels, ordered)
    if len(rects) == expected:
        return rects, warnings
    lh, lw = legend_bgr.shape[:2]
    if min(lh, lw) < 400:
        # A micro legend (TCD's colour ramp is 37 px wide) can need more than
        # 4x before the cell structure is measurable; exact-count acceptance
        # keeps the larger scales as safe as the smaller ones.
        for scale in (3, 4, 6):
            upscaled = cv2.resize(legend_bgr, None, fx=scale, fy=scale,
                                  interpolation=cv2.INTER_CUBIC)
            up_rects, up_warn = _detect_swatches_at_scale(
                upscaled, expected, labels, ordered)
            if len(up_rects) == expected:
                back = [(x // scale, y // scale,
                         max(1, w // scale), max(1, h // scale))
                        for x, y, w, h in up_rects]
                return back, warnings + up_warn + [
                    f"detected swatches on a {scale}x upscaled legend crop"]
    # Structure as the last arbiter: real swatches form one column on a regular
    # pitch, text fragments and stray blobs do not.  Still exact-count gated.
    grid = _column_grid_swatches(legend_bgr, expected, rects)
    if grid is not None:
        grid_rects, grid_warn = grid
        return grid_rects, warnings + grid_warn
    return rects, warnings


def _column_grid_swatches(
    legend_bgr: np.ndarray,
    expected: int,
    candidates: list[tuple[int, int, int, int]],
) -> tuple[list[tuple[int, int, int, int]], list[str]] | None:
    """Recover a single-column legend from its grid structure.

    Keeps the cohort of candidates that share the column's x-position and the
    (taller) modal height, infers the vertical pitch, and fills the grid holes
    whose pixels are a uniform non-paper fill -- which recovers swatches the
    contour pass lost to text fusion while rejecting text fragments (wrong
    column, wrong height) and outline-only rows (paper interior).  Only a
    reconstruction matching the expected count exactly is returned.
    """
    if len(candidates) < 4:
        return None
    heights = sorted(rect[3] for rect in candidates)
    tall = heights[len(heights) // 2:]          # text fragments skew small
    med_h = tall[len(tall) // 2]
    cohort = [rect for rect in candidates if rect[3] >= 0.75 * med_h]
    if len(cohort) < 3:
        return None
    xs = sorted(rect[0] for rect in cohort)
    med_x = xs[len(xs) // 2]
    widths = sorted(rect[2] for rect in cohort)
    med_w = widths[len(widths) // 2]
    cohort = [rect for rect in cohort if abs(rect[0] - med_x) <= 0.5 * med_w]
    if len(cohort) < 3 or len(cohort) > expected:
        return None
    cohort.sort(key=lambda rect: rect[1])
    centers = [rect[1] + rect[3] / 2 for rect in cohort]
    gaps = sorted(second - first for first, second in zip(centers, centers[1:]))
    pitches = [gap for gap in gaps if gap >= 0.8 * med_h]
    if not pitches:
        return None
    pitch = pitches[len(pitches) // 2]

    lab = to_lab(legend_bgr)
    height, width = legend_bgr.shape[:2]
    bg = legend_paper_lab(lab, strip=max(2, min(height, width) // 50))

    def looks_like_fill(y: int) -> bool:
        x0, x1 = med_x + med_w // 4, med_x + med_w - med_w // 4
        y0, y1 = y + med_h // 4, y + med_h - med_h // 4
        if y0 < 0 or y1 > height or x1 <= x0 or y1 <= y0:
            return False
        patch = lab[y0:y1, x0:x1]
        if patch.size == 0 or not _uniform(patch, tol=9.0):
            return False
        median = np.median(patch.reshape(-1, 3), axis=0)
        return float(np.linalg.norm(median - bg)) >= 4

    base = centers[0]
    result: dict[int, tuple[int, int, int, int]] = {}
    for rect, center in zip(cohort, centers):
        result[round((center - base) / pitch)] = rect
    for index in range(min(result), max(result)):
        if index not in result:
            y = int(round(base + index * pitch - med_h / 2))
            if looks_like_fill(y):
                result[index] = (med_x, y, med_w, med_h)
    step_down, step_up = max(result) + 1, min(result) - 1
    while len(result) < expected:
        grew = False
        for index in (step_down, step_up):
            if len(result) >= expected:
                break
            y = int(round(base + index * pitch - med_h / 2))
            if 0 <= y and y + med_h <= height and looks_like_fill(y):
                result[index] = (med_x, y, med_w, med_h)
                grew = True
        step_down += 1
        step_up -= 1
        if not grew:
            break
    if len(result) != expected:
        return None
    rects = [result[key] for key in sorted(result)]
    return rects, [f"reconstructed a {expected}-swatch legend column from its grid structure"]


# A legend prints its swatches at one size, so a cohort +-30% wide holds all of
# them and little else.
SWATCH_COHORT_TOL = 0.70


def _swatch_size_consensus(
    cands: list[tuple[int, int, int, int]], expected: int,
) -> tuple[float, float]:
    """The swatch size the candidates agree on, not the median candidate.

    The median is the right consensus only while swatches dominate the
    candidate set.  A legend keyed inside a dense data table inverts that: the
    Africa ethnolinguistic sheet prints 15 family swatches among a 1200-row
    ethnographic table, so ~110 of ~126 candidates are text strokes, the median
    candidate is a letter six pixels tall, and every real swatch is then
    discarded as an oversized outlier.  Score the cohort each observed size
    would admit and keep the tightest one that still covers every transcribed
    entry.  Ties go to the box with the larger short side: a swatch is a filled
    area, while the runs of body text and the bold headings it is being
    separated from are thin strips, and a wide heading can otherwise form a
    cohort of exactly the right size.
    """
    median = (float(np.median([r[2] for r in cands])),
              float(np.median([r[3] for r in cands])))
    if len(cands) <= expected:
        return median
    best: tuple[tuple[int, int, int], list[tuple[int, int, int, int]]] | None = None
    for w, h in {(r[2], r[3]) for r in cands}:
        cohort = [r for r in cands
                  if SWATCH_COHORT_TOL * w <= r[2] <= w / SWATCH_COHORT_TOL
                  and SWATCH_COHORT_TOL * h <= r[3] <= h / SWATCH_COHORT_TOL]
        if len(cohort) < expected:
            continue
        key = (len(cohort), -min(w, h), -(w * h))
        if best is None or key < best[0]:
            best = (key, cohort)
    if best is None:
        return median
    return (float(np.median([r[2] for r in best[1]])),
            float(np.median([r[3] for r in best[1]])))


def _detect_swatches_at_scale(
    legend_bgr: np.ndarray,
    expected: int,
    labels: list[str] | None = None,
    ordered: bool = False,
) -> tuple[list[tuple[int, int, int, int]], list[str]]:
    if ordered and labels:
        colorbar = detect_horizontal_colorbar(legend_bgr, expected, labels)
        if colorbar is not None:
            return colorbar
        colorbar = detect_vertical_colorbar(legend_bgr, expected, labels)
        if colorbar is not None:
            return colorbar
        compact = _detect_compact_grid_swatches(legend_bgr, expected)
        if compact is not None:
            return compact, [f"reconstructed compact {len(compact)}-swatch legend grid"]

    lh, lw = legend_bgr.shape[:2]
    lab = to_lab(legend_bgr)
    bg = legend_paper_lab(lab, strip=max(2, min(lh, lw) // 50))
    raw = (np.linalg.norm(lab - bg, axis=2) > 10).astype(np.uint8)

    # Closing bridges the anti-aliasing gap inside a swatch, but on a tight
    # legend it also bridges the gap between a swatch and the label beside it,
    # fusing the pair into one blob that then fails every uniformity test.  Read
    # the untouched mask first and only close when that finds nothing.
    def _candidates(binmask):
        contours, _ = cv2.findContours(binmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        found, rejected = [], []
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            if w < 8 or h < 6 or w > 0.6 * lw or h > 0.35 * lh:
                rejected.append((x, y, w, h))
                continue
            if not 0.5 <= w / h <= 10:
                continue
            if cv2.contourArea(c) / (w * h) < 0.55:  # text/blobs are not filled rectangles
                rejected.append((x, y, w, h))
                continue
            rect = (x, y, w, h)
            if _swatch_ok(lab, rect, bg) or _textured_swatch_ok(lab, rect, bg):
                found.append(rect)
        return contours, found, rejected

    closed = cv2.morphologyEx(raw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, cands, rejected_size = _candidates(raw)
    binmask = raw
    if not cands:
        contours, cands, rejected_size = _candidates(closed)
        binmask = closed

    joined = _detect_vertically_joined_swatches(lab, contours, expected, bg)
    if joined is not None:
        return joined, [f"split a vertically joined {expected}-swatch legend stack"]

    warnings: list[str] = []
    if not cands:
        return [], ["no legend swatches detected"]

    med_w, med_h = _swatch_size_consensus(cands, expected)

    # size consistency: letter bodies and specks are far off the consensus swatch
    cands = [r for r in cands
             if 0.55 * med_w <= r[2] <= 2.5 * med_w and 0.55 * med_h <= r[3] <= 2.5 * med_h]
    # merged blobs were size-rejected above; try to split them back into swatches.
    # What the splitter hands back is only a swatch if it matches the size the
    # rest of the legend agreed on: a run of body text is wider than a swatch
    # and about half its height, so without this it reconstructs the leading
    # word of a table row as one more swatch.
    recovered = 0
    for r in rejected_size:
        subs = [s for s in _split_merged(binmask, lab, r, med_w, med_h, bg)
                if SWATCH_COHORT_TOL * med_w <= s[2] <= med_w / SWATCH_COHORT_TOL
                and SWATCH_COHORT_TOL * med_h <= s[3] <= med_h / SWATCH_COHORT_TOL]
        recovered += len(subs)
        cands += subs
    if recovered:
        warnings.append(f"split merged legend blobs into {recovered} swatch(es)")

    # drop near-duplicates (nested contours of the same swatch)
    cands.sort(key=lambda r: r[2] * r[3], reverse=True)
    rects: list[tuple[int, int, int, int]] = []
    for r in cands:
        cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
        if not any(k[0] <= cx <= k[0] + k[2] and k[1] <= cy <= k[1] + k[3] for k in rects):
            rects.append(r)
    # column-major reading order (handles multi-column legends like the France sample)
    rects.sort(key=lambda r: r[0])
    columns: list[list[tuple[int, int, int, int]]] = []
    for r in rects:
        cx = r[0] + r[2] / 2
        for col in columns:
            if abs(cx - (col[0][0] + col[0][2] / 2)) < 1.5 * med_w:
                col.append(r)
                break
        else:
            columns.append([r])
    columns.sort(key=lambda col: col[0][0])
    for col in columns:
        col.sort(key=lambda r: r[1])

    # grid completion: recover swatches the color threshold missed (e.g. a pale
    # yellow swatch on cream paper), first in gaps, then at column ends
    def try_slot(x: float, y: float) -> tuple[int, int, int, int] | None:
        x, y, w, h = int(x), int(y), int(med_w), int(med_h)
        if x < 0 or y < 0 or x + w > lw or y + h > lh:
            return None
        ex, ey, ew, eh = _erode_rect(x, y, w, h)
        patch = lab[ey:ey + eh, ex:ex + ew]
        if not _uniform(patch, tol=5.0):
            return None
        if float(np.linalg.norm(np.median(patch.reshape(-1, 3), axis=0) - bg)) < 4:
            return None  # empty paper, not a pale swatch
        return (x, y, w, h)

    total = sum(len(c) for c in columns)
    if total < expected:
        for col in columns:
            pitches = [col[i + 1][1] - col[i][1] for i in range(len(col) - 1)]
            pitch = float(np.median(pitches)) if pitches else 0.0
            if pitch <= med_h:
                continue
            filled: list[tuple[int, int, int, int]] = []
            for i in range(len(col) - 1):
                filled.append(col[i])
                gap = col[i + 1][1] - col[i][1]
                for k in range(1, round(gap / pitch)):
                    slot = try_slot(col[i][0], col[i][1] + k * pitch)
                    if slot:
                        filled.append(slot)
            filled.append(col[-1])
            while len(filled) + sum(len(c) for c in columns if c is not col) < expected:
                slot = try_slot(filled[0][0], filled[0][1] - pitch) or try_slot(filled[-1][0], filled[-1][1] + pitch)
                if not slot:
                    break
                filled.insert(0, slot) if slot[1] < filled[0][1] else filled.append(slot)
            col[:] = filled
        total = sum(len(c) for c in columns)
        if total > len(rects):
            warnings.append(f"grid completion recovered {total - len(rects)} faint swatch(es)")

    ordered = [r for col in columns for r in col]
    if len(ordered) != expected:
        warnings.append(f"detected {len(ordered)} swatches but Step 1 transcribed {expected} legend entries")
    return ordered, warnings


def sample_swatch(legend_bgr: np.ndarray, rect: tuple[int, int, int, int]) -> tuple[list[int], list[float]]:
    x, y, w, h = _erode_rect(*rect)
    patch = legend_bgr[y:y + h, x:x + w].reshape(-1, 3)
    bgr = np.median(patch, axis=0)
    lab = cv2.cvtColor(np.float32([[bgr]]) / 255.0, cv2.COLOR_BGR2Lab)[0, 0]
    rgb = [int(bgr[2]), int(bgr[1]), int(bgr[0])]
    return rgb, [round(float(v), 2) for v in lab]


# --------------------------------------------------------------------------- runner

DELTA_E_WARN = 10.0
# Below this LAB distance two sampled classes (or a class and the paper) are
# not a printed palette but a detection artifact: every healthy run in this
# project separates its classes by >= 10, while glyph legends and mis-split
# grids sample at 0-2.2.
MIN_CLASS_SEPARATION = 3.0


def recover_legend_box(
    img: np.ndarray, layout: MapLayout, expected: int, ordered: bool,
) -> tuple[tuple[int, int, int, int] | None, list[str]]:
    """Find the legend among the boxes the layout call filed elsewhere.

    The layout prompt asks for the legend as swatches plus labels, so a legend
    printed as a data table is routinely returned under `other` instead -- the
    Africa ethnolinguistic sheet keys its 15 families inside a full-page
    ethnographic table and the call labels it `ethnographic_table`.  Step 1
    still transcribed the entries, so the palette is recoverable: re-read each
    non-map box and keep one only when it yields exactly the swatches Step 1
    counted.  An exact match is the whole test -- a notes block or a logo
    yields nothing like the transcribed count, so nothing is guessed.
    """
    if expected < 1:
        return None, []   # nothing to match against; an empty box would "match"
    h, w = img.shape[:2]
    for box in layout.other:
        x0, y0, x1, y1 = box_to_px(box, w, h)
        if x1 - x0 < 16 or y1 - y0 < 16:
            continue
        rects, _ = detect_swatches(img[y0:y1, x0:x1], expected, ordered=ordered)
        if len(rects) == expected:
            return (x0, y0, x1, y1), [
                f"recovered the legend from the '{box.label}' box the layout "
                f"call returned under other"]
    return None, []


class LegendSwatchDetectionError(ValueError):
    """Raised instead of emitting a partial or misaligned legend palette."""


def run_step2(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .semantics import interpret_map, save_semantics

    out_dir = runs_dir / image_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    # Step 2 writes its palette last, so every failure below this point used to
    # leave the previous run's classes.json and geometry.json in place. Step 4
    # reads both without a freshness check, so a stale empty palette silently
    # became a map with no thematic classes instead of a stopped pipeline.
    # Retire them up front: a Step 2 that does not finish leaves no palette.
    (out_dir / "classes.json").unlink(missing_ok=True)
    (out_dir / "geometry.json").unlink(missing_ok=True)

    sem_path = out_dir / "step1_semantics.json"
    if sem_path.exists():
        sem = MapSemantics.model_validate_json(sem_path.read_text(encoding="utf-8"))
    else:
        sem = interpret_map(image_path, model=model)
        save_semantics(sem, image_path, runs_dir)

    require_pipeline_eligible(sem, "Step 2")

    layout_path = out_dir / "step2_layout.json"
    if layout_path.exists():
        layout = MapLayout.model_validate_json(layout_path.read_text(encoding="utf-8"))
        layout_cached = True
    else:
        layout = detect_layout(image_path, model=model)
        layout_path.write_text(layout.model_dump_json(indent=2), encoding="utf-8")
        layout_cached = False

    img = imread(image_path)
    h, w = img.shape[:2]
    warnings: list[str] = []

    # Raw model result, deliberately rendered before CV refinement/exclusions.
    raw_dbg = img.copy()
    raw_items = [(f"map_area {i + 1}: {box.label} (AI)", box, (255, 180, 0))
                 for i, box in enumerate(layout.map_areas)]
    raw_items += [(name, box, (0, 128, 255)) for name, box in (
        ("legend (AI)", layout.legend), ("title (AI)", layout.title),
        ("scale_bar (AI)", layout.scale_bar), ("north_arrow (AI)", layout.north_arrow),
    ) if box is not None]
    raw_items += [(f"other:{box.label} (AI)", box, (0, 128, 255)) for box in layout.other]
    for name, box, color in raw_items:
        bx0, by0, bx1, by1 = box_to_px(box, w, h)
        cv2.rectangle(raw_dbg, (bx0, by0), (bx1, by1), color, 3)
        cv2.putText(raw_dbg, name, (bx0 + 4, max(22, by0 + 24)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
    if max(raw_dbg.shape[:2]) > 1600:
        s = 1600 / max(raw_dbg.shape[:2])
        raw_dbg = cv2.resize(raw_dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step2_layout_debug.png", raw_dbg)

    furniture = [(name, box_to_px(b, w, h)) for name, b in (
        ("legend", layout.legend), ("title", layout.title),
        ("scale_bar", layout.scale_bar), ("north_arrow", layout.north_arrow),
    ) if b is not None]
    furniture += [(f"other:{b.label}", box_to_px(b, w, h)) for b in layout.other]

    map_boxes = [box_to_px(box, w, h, pad_frac=0.02) for box in layout.map_areas]
    mask, tight, mask_warn = refine_map_mask(img, map_boxes, [f[1] for f in furniture])
    warnings += mask_warn
    tx0, ty0, tx1, ty1 = tight
    map_area = img[ty0:ty1, tx0:tx1]
    imwrite(out_dir / "map_area.png", map_area)
    cropped_mask = mask[ty0:ty1, tx0:tx1]
    # Retain an untouched automatic baseline for the Step 2 mask-review tool.
    # Re-running Step 2 intentionally starts a fresh review against the new
    # automatic result instead of replaying strokes in changed coordinates.
    imwrite(out_dir / "map_mask_auto.png", cropped_mask)
    (out_dir / "map_mask_review.json").unlink(missing_ok=True)
    imwrite(out_dir / "map_mask.png", cropped_mask)
    imwrite(out_dir / "map_text_input.png",
            prepare_text_input(map_area, tight, furniture, cropped_mask))

    # ---- legend ----
    classes: list[dict] = []
    legend_box = None
    if layout.legend is not None:
        legend_box = box_to_px(layout.legend, w, h)
    elif sem.legend_present:
        legend_box, recovery_warnings = recover_legend_box(
            img, layout, len([e for e in sem.legend_entries if e.is_thematic]),
            sem.data_ordering.value == "ordered")
        warnings += recovery_warnings
    if legend_box is not None:
        lx0, ly0, lx1, ly1 = legend_box
        legend_img = img[ly0:ly1, lx0:lx1]
        imwrite(out_dir / "legend.png", legend_img)

        # Symbols and line samples can be legitimate legend entries, but they
        # have no area-fill palette to sample. Only thematic entries must pair
        # with detected colour swatches.
        # Vision transcription can repeat a thematic label when a multi-column
        # legend wraps or OCR revisits a row. Repeated labels cannot represent
        # distinct palette classes, so retain the first occurrence.
        legend_entries = []
        seen_thematic_labels: set[str] = set()
        for entry in sem.legend_entries:
            if entry.is_thematic:
                if entry.label in seen_thematic_labels:
                    warnings.append(f"ignored duplicate thematic legend entry '{entry.label}'")
                    continue
                seen_thematic_labels.add(entry.label)
            legend_entries.append(entry)
        thematic_entries = [entry for entry in legend_entries if entry.is_thematic]
        expected = len(thematic_entries)
        if expected == 0:
            raise LegendSwatchDetectionError(
                "Step 2 cannot continue: Step 1 reported a legend but transcribed "
                "no thematic swatch entries. Rerun Step 1 or use a clearer source image."
            )
        rects, sw_warn = detect_swatches(
            legend_img,
            expected,
            labels=[entry.label for entry in thematic_entries],
            ordered=sem.data_ordering.value == "ordered",
        )
        warnings += sw_warn
        if len(rects) > expected:
            # A legend usually shows its thematic classes first and then the
            # non-thematic fills -- water, urban, no-data -- so those carry real
            # swatches too.  Dropping the surplus is only safe when Step 1 read
            # the thematic entries as one unbroken block at the start and the
            # surplus is no larger than the trailing non-thematic entries;
            # otherwise the extra swatch could sit anywhere and every label
            # after it would pair with the wrong colour.
            trailing = len(legend_entries) - len(thematic_entries)
            leading_block = all(entry.is_thematic for entry in legend_entries[:expected])
            surplus = len(rects) - expected
            if leading_block and trailing >= surplus:
                warnings.append(
                    f"kept the first {expected} legend swatches and set aside {surplus} "
                    f"trailing non-thematic swatch(es)")
                rects = rects[:expected]
        if len(rects) != expected:
            raise LegendSwatchDetectionError(
                f"Step 2 cannot continue: detected {len(rects)} legend swatches "
                f"but Step 1 identified {expected} thematic entries. The palette cannot "
                "be aligned safely; inspect the legend crop or rerun Step 1."
            )
        # For ordered maps, the displayed legend order is the authoritative
        # sequence.  A semantic model may list classes by estimated area or
        # salience (rather than numeric order), which would later let Step 5
        # merge non-adjacent bins.  Qualitative legends retain their model
        # supplied priorities.
        prio = ({entry.label: index + 1 for index, entry in enumerate(thematic_entries)}
                if sem.data_ordering.value == "ordered"
                else {c.label: c.priority for c in sem.thematic_classes})
        thematic_rects = iter(rects)
        for entry in legend_entries:
            row = {
                "label": entry.label,
                "is_thematic": entry.is_thematic,
                "priority": prio.get(entry.label),
                "rgb": None, "lab": None, "hex": None,
                "swatch_bbox_orig": None,
            }
            if entry.is_thematic:
                rect = next(thematic_rects)
                x, y, sw_, sh_ = rect
                rgb, lab_v = sample_swatch(legend_img, rect)
                row.update({
                    "rgb": rgb, "lab": lab_v,
                    "hex": "#{:02x}{:02x}{:02x}".format(*rgb),
                    "swatch_bbox_orig": [lx0 + x, ly0 + y, sw_, sh_],
                })
            classes.append(row)

        sampled = [(c["label"], np.float32(c["lab"])) for c in classes if c["lab"]]
        for i in range(len(sampled)):
            for j in range(i + 1, len(sampled)):
                de = float(np.linalg.norm(sampled[i][1] - sampled[j][1]))
                if de < DELTA_E_WARN:
                    warnings.append(
                        f"low color contrast (dE={de:.1f}) between "
                        f"'{sampled[i][0]}' and '{sampled[j][0]}' -- Step 4 may conflate them"
                    )
        # Counts can match while the colours cannot: a symbol-glyph legend
        # yields near-identical ink samples, and a mis-split grid samples the
        # same cell twice or bare paper.  No printed legend gives two classes
        # the same ink, so treat such a palette as a failed detection rather
        # than handing Step 4 an assignment it cannot make.
        paper = legend_paper_lab(
            to_lab(legend_img), strip=max(2, min(legend_img.shape[:2]) // 50))
        papery = [label for label, lab_value in sampled
                  if float(np.linalg.norm(lab_value - paper)) < MIN_CLASS_SEPARATION]
        colliding = sorted({
            label
            for i in range(len(sampled)) for j in range(i + 1, len(sampled))
            if float(np.linalg.norm(sampled[i][1] - sampled[j][1])) < MIN_CLASS_SEPARATION
            for label in (sampled[i][0], sampled[j][0])})
        if papery or colliding:
            named = ", ".join(f"'{label}'" for label in (papery or colliding)[:4])
            reason = ("sampled as bare paper" if papery
                      else "sampled with indistinguishable colors")
            raise LegendSwatchDetectionError(
                f"Step 2 cannot continue: legend entries {named} were {reason}, "
                "so regions cannot be assigned to classes by color. The legend "
                "likely uses symbols or merged swatches; inspect the legend crop "
                "or rerun Step 1.")
    elif sem.legend_present:
        # Continuing here would hand Step 4 an empty palette and fail later with
        # a message about missing seeds, far from the actual cause.  The legend
        # is what names and colours every class, so stop where it went missing.
        raise LegendSwatchDetectionError(
            "Step 2 cannot continue: Step 1 read a legend on this map but the layout "
            "call returned no legend box, so no palette could be sampled. Rerun Step 2, "
            "or correct the map area in the Step 2 review."
        )

    (out_dir / "classes.json").write_text(
        json.dumps({"classes": classes, "warnings": warnings}, indent=2), encoding="utf-8")
    (out_dir / "geometry.json").write_text(json.dumps({
        "image_size": [w, h],
        "map_boxes_vlm": [list(box) for box in map_boxes],
        "map_crop": list(tight),
        "legend_crop": list(legend_box) if legend_box else None,
        "furniture": [{"name": n, "box": list(b)} for n, b in furniture],
    }, indent=2), encoding="utf-8")

    # ---- debug overlay ----
    dbg = img.copy()
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cv2.drawContours(dbg, contours, -1, (0, 255, 255), 2)
    cv2.rectangle(dbg, (tx0, ty0), (tx1, ty1), (0, 200, 0), 3)
    for name, (bx0, by0, bx1, by1) in furniture:
        cv2.rectangle(dbg, (bx0, by0), (bx1, by1), (0, 128, 255), 3)
        cv2.putText(dbg, name, (bx0 + 4, by0 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 128, 255), 2)
    for c in classes:
        if c["swatch_bbox_orig"]:
            x, y, sw_, sh_ = c["swatch_bbox_orig"]
            cv2.rectangle(dbg, (x, y), (x + sw_, y + sh_), (255, 0, 255), 2)
    if max(dbg.shape[:2]) > 1600:
        s = 1600 / max(dbg.shape[:2])
        dbg = cv2.resize(dbg, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    imwrite(out_dir / "step2_debug.png", dbg)

    return {
        "out_dir": out_dir,
        "layout_cached": layout_cached,
        "map_crop": tight,
        "legend": legend_box is not None,
        "classes_with_color": sum(1 for c in classes if c["rgb"]),
        "classes_total": len(classes),
        "warnings": warnings,
    }
