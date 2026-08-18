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
import mimetypes
import os
from pathlib import Path

import cv2
import numpy as np
from pydantic import BaseModel, Field, model_validator

from .semantics import (
    DEFAULT_MODEL,
    MapSemantics,
    _ensure_api_key,
    require_pipeline_eligible,
)

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
- title, scale_bar, north_arrow: when present, else null.
- other: inset maps, notes, logos, coordinate labels or anything else that is
  not map content, each with a short label.
"""


class LayoutBox(BaseModel):
    box_2d: list[int] = Field(description="[y_min, x_min, y_max, x_max] normalized to 0-1000")
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
        return data


def detect_layout(image_path: Path, model: str | None = None) -> MapLayout:
    from google import genai
    from google.genai import types

    _ensure_api_key()
    data = image_path.read_bytes()
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    client = genai.Client()
    response = client.models.generate_content(
        model=model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        contents=[types.Part.from_bytes(data=data, mime_type=mime), LAYOUT_PROMPT],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=MapLayout,
            temperature=0.0,
        ),
    )
    layout = response.parsed
    if not isinstance(layout, MapLayout):
        layout = MapLayout.model_validate_json(response.text)
    return layout


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
    bg_delta: float = 12.0,
) -> tuple[np.ndarray, tuple[int, int, int, int], list[str]]:
    """Pixel-tight content mask seeded by the (padded) VLM map box.

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

    n, cc, stats, _ = cv2.connectedComponentsWithStats(inside, connectivity=8)
    min_area = max(MIN_COMPONENT_PX, int(2e-5 * w * h))
    warnings: list[str] = []
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
    bg = border_median_lab(lab, strip=max(2, min(lh, lw) // 50))
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
    """Find legend color swatches; returns rects in reading (column-major) order."""
    if ordered and labels:
        colorbar = detect_horizontal_colorbar(legend_bgr, expected, labels)
        if colorbar is not None:
            return colorbar
        compact = _detect_compact_grid_swatches(legend_bgr, expected)
        if compact is not None:
            return compact, [f"reconstructed compact {len(compact)}-swatch legend grid"]

    lh, lw = legend_bgr.shape[:2]
    lab = to_lab(legend_bgr)
    bg = border_median_lab(lab, strip=max(2, min(lh, lw) // 50))
    binmask = (np.linalg.norm(lab - bg, axis=2) > 10).astype(np.uint8)
    binmask = cv2.morphologyEx(binmask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(binmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    joined = _detect_vertically_joined_swatches(lab, contours, expected, bg)
    if joined is not None:
        return joined, [f"split a vertically joined {expected}-swatch legend stack"]

    cands, rejected_size = [], []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < 8 or h < 6 or w > 0.6 * lw or h > 0.35 * lh:
            rejected_size.append((x, y, w, h))
            continue
        if not 0.5 <= w / h <= 10:
            continue
        if cv2.contourArea(c) / (w * h) < 0.55:  # text/blobs are not filled rectangles
            rejected_size.append((x, y, w, h))
            continue
        rect = (x, y, w, h)
        if _swatch_ok(lab, rect, bg) or _textured_swatch_ok(lab, rect, bg):
            cands.append(rect)

    warnings: list[str] = []
    if not cands:
        return [], ["no legend swatches detected"]

    med_w = float(np.median([r[2] for r in cands]))
    med_h = float(np.median([r[3] for r in cands]))

    # size consistency: letter bodies and specks are far off the median swatch
    cands = [r for r in cands
             if 0.55 * med_w <= r[2] <= 2.5 * med_w and 0.55 * med_h <= r[3] <= 2.5 * med_h]
    # merged blobs were size-rejected above; try to split them back into swatches
    recovered = 0
    for r in rejected_size:
        subs = _split_merged(binmask, lab, r, med_w, med_h, bg)
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


class LegendSwatchDetectionError(ValueError):
    """Raised instead of emitting a partial or misaligned legend palette."""


def run_step2(image_path: Path, model: str | None = None, runs_dir: Path = Path("runs")) -> dict:
    from .semantics import interpret_map, save_semantics

    out_dir = runs_dir / image_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)

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
    elif sem.legend_present:
        warnings.append("Step 1 says a legend exists but the layout call returned no legend box")

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
