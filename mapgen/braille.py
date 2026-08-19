"""Step 8 -- editable Grade 1 Braille labels over the tactile master.

This step is deliberately local and deterministic.  It consumes the reviewed
label positions exported by Step 7 and never calls an AI service.  The editable
state is saved separately from the rendered PNG so text, visibility, and manual
pin moves survive later redraws.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import unicodedata
import uuid

import numpy as np
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .output_spec import OutputSpec

# Bump this whenever the printable composition changes.  The Web UI uses this
# value to recognize an artifact rendered by an older renderer and offers a
# rerun instead of presenting an out-of-date full-size PNG.
BRAILLE_LAYOUT_VERSION = 9
RENDER_PX_PER_MM = 5.0
BRAILLE_FONT_SIZE_PT = 24.0
BRAILLE_PADDING_MM = 3.0
# This is a print-space measurement: the centres of corresponding Braille
# dots on consecutive title lines must be 10 mm apart.
BRAILLE_LINE_SPACING_MM = 10.0
# Each optional locator symbol has the requested 8 x 8 mm footprint. A 2 mm
# white surround separates its 4 mm black core from tactile map linework.
BRAILLE_PIN_TOTAL_SIZE_MM = 8.0
BRAILLE_PIN_STROKE_MM = 2.0
BRAILLE_PIN_BLACK_DIAMETER_MM = BRAILLE_PIN_TOTAL_SIZE_MM - 2 * BRAILLE_PIN_STROKE_MM
BRAILLE_FONT_NAME = "Braille SW 2024 INSEI.ttf"
MAX_LABEL_TEXT_LENGTH = 200
BOX_SIDES = ("left", "right", "top", "bottom")
PIN_SHAPES = ("circle", "triangle", "square")
TITLE_TOP_GAP_MM = 5.0
TITLE_PAGE_INSET_MM = 5.0
TITLE_ALIGNS = ("left", "center", "right")
MAP_BORDER_STROKE_MM = 3.0
NORTH_MARKER_SIZE_MM = 24.0
NORTH_MARKER_PATH = Path(__file__).resolve().parents[1] / "pattern_library" / "N.svg"


def braille_font_path() -> Path:
    """Return the user-supplied Braille font bundled with this project."""
    path = Path(__file__).resolve().parent.parent / "assets" / "fonts" / BRAILLE_FONT_NAME
    if not path.is_file():
        raise FileNotFoundError(
            f"Step 8 Braille font is missing: {path}. Put {BRAILLE_FONT_NAME!r} "
            "in assets/fonts/."
        )
    return path


def repair_label_text(value: object) -> str:
    """Clean a label and repair the common UTF-8-as-Latin-1 OCR artefact."""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if any(marker in text for marker in ("Ã", "Â", "â")):
        try:
            repaired = text.encode("latin-1").decode("utf-8")
            if repaired:
                text = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return " ".join(text.split())[:MAX_LABEL_TEXT_LENGTH]


def repair_multiline_text(value: object) -> str:
    """Clean editable page text without discarding intentional line breaks."""
    raw = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [repair_label_text(line) for line in raw.split("\n")]
    return "\n".join(lines).strip()[:MAX_LABEL_TEXT_LENGTH]


def _ascii_letter(char: str) -> str | None:
    decomposed = unicodedata.normalize("NFKD", char)
    for candidate in decomposed:
        if "a" <= candidate.lower() <= "z":
            return candidate.lower()
    return None


def to_grade1_font_text(value: object, *, preserve_newlines: bool = False) -> str:
    """Encode text for the supplied ASCII-keyed Braille font.

    The font maps lower-case letters to the six-dot letter cells.  A backtick
    is its dot-6 cell, so it supplies UEB capital indicators.  ``#`` is the
    number sign and digits are then represented by the a-j cells.
    """
    text = repair_multiline_text(value) if preserve_newlines else repair_label_text(value)
    output: list[str] = []
    number_mode = False
    i = 0
    while i < len(text):
        char = text[i]
        if char.isdigit() and char in "0123456789":
            if not number_mode:
                output.append("#")
                number_mode = True
            output.append("abcdefghij"[int(char) - 1] if char != "0" else "j")
            i += 1
            continue

        letter = _ascii_letter(char) if char.isalpha() else None
        if letter is not None:
            number_mode = False
            if char.isupper():
                # Two dot-6 cells are the UEB capitals-word indicator.
                j = i
                while j < len(text) and text[j].isalpha() and text[j].isupper():
                    j += 1
                if j - i > 1:
                    output.extend(("`", "`"))
                    for upper in text[i:j]:
                        converted = _ascii_letter(upper)
                        output.append(converted or "?")
                    i = j
                    continue
                output.append("`")
            output.append(letter)
            i += 1
            continue

        if char == "\n" and preserve_newlines:
            number_mode = False
            output.append("\n")
        elif char.isspace():
            number_mode = False
            output.append(" ")
        else:
            # The supplied font defines the common printable punctuation.
            replacement = {
                "–": "-", "—": "-", "−": "-", "’": "'", "‘": "'",
                "“": '"', "”": '"', "…": "...", "°": "?",
            }.get(char, char if 32 <= ord(char) <= 126 else "?")
            output.append(replacement)
            if char not in ".,":
                number_mode = False
        i += 1
    return "".join(output)


def _label_id(label: dict, index: int) -> str:
    existing = str(label.get("label_id") or "").strip()
    if existing:
        return existing
    stable = json.dumps({
        "text": label.get("text"),
        "kind": label.get("kind"),
        "box": label.get("box_source_px"),
        "position": label.get("text_position_source_px"),
    }, sort_keys=True, ensure_ascii=False)
    return f"label-{index}-{hashlib.sha1(stable.encode('utf-8')).hexdigest()[:10]}"


def _point(value: object, fallback: list[float]) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return fallback.copy()
    x, y = float(value[0]), float(value[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("label position must contain finite x and y values")
    return [round(x, 3), round(y, 3)]


def _rect(value: object, fallback: list[float], page_width_px: float,
          page_height_px: float) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        value = fallback
    try:
        x0, y0, x1, y1 = (float(part) for part in value)
    except (TypeError, ValueError) as exc:
        raise ValueError("border rectangle must contain four numbers") from exc
    if not all(math.isfinite(part) for part in (x0, y0, x1, y1)):
        raise ValueError("border rectangle must contain finite values")
    x0, x1 = sorted((min(max(x0, 0.0), page_width_px),
                     min(max(x1, 0.0), page_width_px)))
    y0, y1 = sorted((min(max(y0, 0.0), page_height_px),
                     min(max(y1, 0.0), page_height_px)))
    if x1 - x0 < 1 or y1 - y0 < 1:
        raise ValueError("border rectangle must have a positive width and height")
    return [round(part, 3) for part in (x0, y0, x1, y1)]


def _default_map_furniture(map_origin_px: list[float], map_size_px: list[int],
                           px_per_mm: float, page_width_px: float,
                           page_height_px: float) -> dict:
    map_x, map_y = map_origin_px
    map_width, map_height = map_size_px
    north_size_px = NORTH_MARKER_SIZE_MM * px_per_mm
    gap_px = 4.0 * px_per_mm
    north_position = [
        min(max(0.0, map_x + map_width - north_size_px - gap_px),
            max(0.0, page_width_px - north_size_px)),
        min(max(0.0, map_y + gap_px), max(0.0, page_height_px - north_size_px)),
    ]
    return {
        "border": {
            "enabled": False,
            "rect_page_px": [map_x, map_y, map_x + map_width, map_y + map_height],
            "stroke_mm": MAP_BORDER_STROKE_MM,
        },
        "north": {
            "enabled": False,
            "position_page_px": [round(part, 3) for part in north_position],
            "size_mm": NORTH_MARKER_SIZE_MM,
            "asset": "pattern_library/N.svg",
        },
        "scale": {"enabled": False, "status": "placeholder"},
    }


def normalize_map_furniture(value: object, map_origin_px: list[float],
                            map_size_px: list[int], px_per_mm: float,
                            page_width_px: float, page_height_px: float) -> dict:
    """Validate persistent Step 7 furniture without allowing the scale yet."""

    defaults = _default_map_furniture(map_origin_px, map_size_px, px_per_mm,
                                      page_width_px, page_height_px)
    raw = value if isinstance(value, dict) else {}
    raw_border = raw.get("border") if isinstance(raw.get("border"), dict) else {}
    raw_north = raw.get("north") if isinstance(raw.get("north"), dict) else {}
    north_size_px = NORTH_MARKER_SIZE_MM * px_per_mm
    north_position = _point(raw_north.get("position_page_px"),
                            defaults["north"]["position_page_px"])
    north_position = [
        round(min(max(north_position[0], 0.0), max(0.0, page_width_px - north_size_px)), 3),
        round(min(max(north_position[1], 0.0), max(0.0, page_height_px - north_size_px)), 3),
    ]
    return {
        "border": {
            "enabled": bool(raw_border.get("enabled", defaults["border"]["enabled"])),
            "rect_page_px": _rect(raw_border.get("rect_page_px"),
                                  defaults["border"]["rect_page_px"],
                                  page_width_px, page_height_px),
            # This is intentionally fixed to the requested tactile frame width.
            "stroke_mm": MAP_BORDER_STROKE_MM,
        },
        "north": {
            "enabled": bool(raw_north.get("enabled", defaults["north"]["enabled"])),
            "position_page_px": north_position,
            "size_mm": NORTH_MARKER_SIZE_MM,
            "asset": "pattern_library/N.svg",
        },
        "scale": {"enabled": False, "status": "placeholder"},
    }


def _page_size_for_orientation(spec: OutputSpec, orientation: str) -> tuple[float, float]:
    """Return the configured paper dimensions rotated to ``orientation``."""
    short_side, long_side = sorted((spec.page_width_mm, spec.page_height_mm))
    return ((long_side, short_side) if orientation == "landscape"
            else (short_side, long_side))


def _resolve_page_layout(spec: OutputSpec, map_width_px: int, map_height_px: int,
                         px_per_mm: float,
                         preferred_orientation: str | None = None) -> tuple[
                             str, float, float, list[float]]:
    """Choose a configured-paper rotation that contains the rendered map.

    Step 6 normally supplies the preferred orientation, but the final Step 7
    raster is authoritative: a stale summary or a later regeneration must not
    make Step 8 use a page that cannot contain its actual input.
    """
    preferred = (preferred_orientation if preferred_orientation in ("portrait", "landscape")
                 else spec.orientation)
    candidates = [preferred, "landscape" if preferred == "portrait" else "portrait"]
    map_size_mm = [map_width_px / px_per_mm, map_height_px / px_per_mm]
    for orientation in candidates:
        page_width_mm, page_height_mm = _page_size_for_orientation(spec, orientation)
        available_width_mm = page_width_mm - 2 * spec.margin_mm
        available_height_mm = page_height_mm - 2 * spec.margin_mm
        if (map_size_mm[0] <= available_width_mm + 1e-6
                and map_size_mm[1] <= available_height_mm + 1e-6):
            page_width_px = page_width_mm * px_per_mm
            page_height_px = page_height_mm * px_per_mm
            return (
                orientation, page_width_mm, page_height_mm,
                [round((page_width_px - map_width_px) / 2.0, 3),
                 round((page_height_px - map_height_px) / 2.0, 3)],
            )
    raise ValueError(
        "the tactile map does not fit within the configured page margins in "
        f"either orientation (map {map_size_mm[0]:.1f} x {map_size_mm[1]:.1f} mm; "
        f"paper {spec.page_width_mm:.1f} x {spec.page_height_mm:.1f} mm; "
        f"margin {spec.margin_mm:.1f} mm)"
    )


def build_step7_page_layout(out_dir: Path, previous: dict | None = None) -> dict:
    """Return the persistent A4 placement used by Steps 7 and 8."""
    map_path = out_dir / "step8a_cleanup.png"
    if not map_path.exists():
        raise FileNotFoundError("run Step 7 before positioning the tactile map")
    with Image.open(map_path) as image:
        map_width_px, map_height_px = image.size
    spec = OutputSpec.load_or_create()
    summary_path = out_dir / "step6_summary.json"
    summary = (json.loads(summary_path.read_text(encoding="utf-8"))
               if summary_path.exists() else {})
    previous = previous or {}
    # Once an editor chooses a paper rotation in Step 7, preserve that choice
    # for Steps 7 and 8 instead of silently switching back to Step 6's fit.
    preferred_orientation = previous.get("orientation") if previous.get("orientation") in (
        "portrait", "landscape") else summary.get("orientation")
    orientation, page_width_mm, page_height_mm, default_origin = _resolve_page_layout(
        spec, map_width_px, map_height_px, RENDER_PX_PER_MM, preferred_orientation,
    )
    page_width_px = int(round(page_width_mm * RENDER_PX_PER_MM))
    page_height_px = int(round(page_height_mm * RENDER_PX_PER_MM))
    map_size_mm = [map_width_px / RENDER_PX_PER_MM, map_height_px / RENDER_PX_PER_MM]
    allowed_orientations = []
    for candidate in ("portrait", "landscape"):
        candidate_w, candidate_h = _page_size_for_orientation(spec, candidate)
        if (map_size_mm[0] <= candidate_w - 2 * spec.margin_mm + 1e-6
                and map_size_mm[1] <= candidate_h - 2 * spec.margin_mm + 1e-6):
            allowed_orientations.append(candidate)
    same_geometry = (
        previous.get("canvas_px") == [page_width_px, page_height_px]
        and previous.get("map_size_px") == [map_width_px, map_height_px]
    )
    origin = _point(previous.get("map_origin_px") if same_geometry else None, default_origin)
    origin = [round(min(max(origin[0], 0.0), page_width_px - map_width_px), 3),
              round(min(max(origin[1], 0.0), page_height_px - map_height_px), 3)]
    furniture = normalize_map_furniture(
        previous.get("furniture"), origin, [map_width_px, map_height_px],
        RENDER_PX_PER_MM, page_width_px, page_height_px,
    )
    return {
        "version": 3,
        "size_mm": [page_width_mm, page_height_mm],
        "canvas_px": [page_width_px, page_height_px],
        "orientation": orientation,
        "allowed_orientations": allowed_orientations,
        "margin_mm": spec.margin_mm,
        "map_origin_px": origin,
        "map_size_px": [map_width_px, map_height_px],
        "map_size_mm": [round(map_width_px / RENDER_PX_PER_MM, 3),
                        round(map_height_px / RENDER_PX_PER_MM, 3)],
        "render_px_per_mm": RENDER_PX_PER_MM,
        "dpi": round(RENDER_PX_PER_MM * 25.4, 3),
        "furniture": furniture,
    }


def load_step7_page_layout(out_dir: Path) -> dict:
    path = out_dir / "page_layout.json"
    previous = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)
    layout = build_step7_page_layout(out_dir, previous)
    path.write_text(json.dumps(layout, indent=2), encoding="utf-8")
    return layout


def update_step7_page_layout(out_dir: Path, position: object | None = None,
                             orientation: object | None = None,
                             furniture: object | None = None) -> dict:
    layout = load_step7_page_layout(out_dir)
    if orientation is not None:
        orientation = str(orientation).lower()
        if orientation not in ("portrait", "landscape"):
            raise ValueError("page orientation must be portrait or landscape")
        if orientation != layout["orientation"]:
            # Rebuild using the requested rotation.  _resolve_page_layout
            # falls back by design, so verify the requested choice survived.
            layout = build_step7_page_layout(out_dir, {"orientation": orientation})
            if layout["orientation"] != orientation:
                raise ValueError("the tactile map does not fit on A4 " + orientation)
    point = _point(position, layout["map_origin_px"])
    page_w, page_h = layout["canvas_px"]
    map_w, map_h = layout["map_size_px"]
    layout["map_origin_px"] = [
        round(min(max(point[0], 0.0), page_w - map_w), 3),
        round(min(max(point[1], 0.0), page_h - map_h), 3),
    ]
    if furniture is not None:
        layout["furniture"] = normalize_map_furniture(
            furniture, layout["map_origin_px"], [map_w, map_h],
            float(layout.get("render_px_per_mm", RENDER_PX_PER_MM)), page_w, page_h,
        )
    (out_dir / "page_layout.json").write_text(
        json.dumps(layout, indent=2), encoding="utf-8")
    return layout


def build_braille_layout(overlay: dict, previous: dict | None = None,
                         px_per_mm: float = RENDER_PX_PER_MM,
                         spec: OutputSpec | None = None,
                         detected_title: object = "",
                         page_orientation: str | None = None,
                         page_layout: dict | None = None) -> dict:
    """Build editable Step 8 state, preserving prior human edits by label id."""
    prior = {str(label.get("id")): label for label in (previous or {}).get("labels", [])}
    deleted_label_ids = {
        str(label_id) for label_id in (previous or {}).get("deleted_label_ids", [])
        if str(label_id).strip()
    }
    labels = []
    source_ids: set[str] = set()
    for index, source in enumerate(overlay.get("labels", [])):
        label_id = _label_id(source, index)
        source_ids.add(label_id)
        if label_id in deleted_label_ids:
            continue
        saved = prior.get(label_id, {})
        original_text = repair_label_text(source.get("text"))
        text = repair_label_text(saved.get("text", original_text))
        origin = _point(source.get("text_position_tactile_px"), [0.0, 0.0])
        position = _point(saved.get("position_px"), origin)
        labels.append({
            "id": label_id,
            "source_index": index,
            "original_text": original_text,
            "text": text,
            "braille_text": to_grade1_font_text(text),
            "enabled": bool(saved.get("enabled", True)),
            "side": (str(saved.get("side", "right")).lower()
                     if str(saved.get("side", "right")).lower() in BOX_SIDES else "right"),
            "callout": bool(saved.get("callout", False)),
            "pin_shape": (str(saved.get("pin_shape", "circle")).lower()
                          if str(saved.get("pin_shape", "circle")).lower() in PIN_SHAPES
                          else "circle"),
            "position_px": position,
            "position_mm": [round(position[0] / px_per_mm, 3),
                            round(position[1] / px_per_mm, 3)],
            "original_position_px": origin,
            "kind": source.get("kind", "other"),
            "priority": source.get("priority"),
        })

    # Manual labels have no Step 7 source record, so retain them across a
    # later Step 8 regeneration.
    manual_labels = []
    for saved in prior.values():
        label_id = str(saved.get("id") or "")
        if not label_id.startswith("manual-") or label_id in source_ids:
            continue
        position = _point(saved.get("position_px"), [0.0, 0.0])
        text = repair_label_text(saved.get("text"))
        manual_labels.append({
            "id": label_id,
            "source_index": None,
            "original_text": "",
            "text": text,
            "braille_text": to_grade1_font_text(text),
            "enabled": bool(saved.get("enabled", True)),
            "side": (str(saved.get("side", "right")).lower()
                     if str(saved.get("side", "right")).lower() in BOX_SIDES else "right"),
            "callout": bool(saved.get("callout", False)),
            "pin_shape": (str(saved.get("pin_shape", "circle")).lower()
                          if str(saved.get("pin_shape", "circle")).lower() in PIN_SHAPES
                          else "circle"),
            "position_px": position,
            "position_mm": [round(position[0] / px_per_mm, 3),
                            round(position[1] / px_per_mm, 3)],
            "original_position_px": position.copy(),
            "kind": "manual",
            "priority": None,
        })
    labels = manual_labels + labels

    canvas = overlay.get("coordinate_contract", {}).get("tactile_size_px", [0, 0])
    map_width_px, map_height_px = int(canvas[0]), int(canvas[1])
    spec = spec or OutputSpec.load_or_create()
    orientation, page_width_mm, page_height_mm, map_origin = _resolve_page_layout(
        spec, map_width_px, map_height_px, px_per_mm, page_orientation,
    )
    if (page_layout and page_layout.get("map_size_px") == [map_width_px, map_height_px]
            and page_layout.get("orientation") == orientation):
        map_origin = _point(page_layout.get("map_origin_px"), map_origin)
    page_width_px = int(round(page_width_mm * px_per_mm))
    page_height_px = int(round(page_height_mm * px_per_mm))
    furniture = normalize_map_furniture(
        page_layout.get("furniture") if page_layout else None,
        map_origin, [map_width_px, map_height_px], px_per_mm,
        page_width_px, page_height_px,
    )
    saved_title = (previous or {}).get("title", {})
    original_title = repair_multiline_text(detected_title)
    title_text = repair_multiline_text(saved_title.get("text", original_title))
    title_inset_px = (spec.margin_mm + TITLE_PAGE_INSET_MM) * px_per_mm
    # Older layouts stored the centre of a text-sized title; version 4 stores the
    # top-left of the full-width title box. Start old layouts at the new page inset.
    saved_position = (saved_title.get("position_page_px")
                      if int((previous or {}).get("version", 0)) >= 4 else None)
    title_position = _point(saved_position,
                            [title_inset_px, title_inset_px])
    default_title_width = max(1.0, page_width_px - 2 * title_inset_px)
    title_width = float(saved_title.get("box_width_px", default_title_width))
    title_width = round(min(max(title_width, 30 * px_per_mm), page_width_px), 3)
    return {
        "version": BRAILLE_LAYOUT_VERSION,
        "braille_standard": "unified-english-grade1",
        "font": {"file": BRAILLE_FONT_NAME, "size_pt": BRAILLE_FONT_SIZE_PT},
        "geometry": {
            "padding_mm": BRAILLE_PADDING_MM,
            "pin_black_diameter_mm": BRAILLE_PIN_BLACK_DIAMETER_MM,
            "pin_total_diameter_mm": BRAILLE_PIN_BLACK_DIAMETER_MM + 2 * BRAILLE_PIN_STROKE_MM,
            "pin_white_stroke_mm": BRAILLE_PIN_STROKE_MM,
            "box_side": "per_label",
            "collision_policy": "manual",
        },
        "render_px_per_mm": float(px_per_mm),
        "toolbox": {
            "fix_text_to_map": bool((previous or {}).get("toolbox", {}).get(
                "fix_text_to_map", False)),
            "group_map_elements": bool((previous or {}).get("toolbox", {}).get(
                "group_map_elements", False)),
        },
        "deleted_label_ids": sorted(deleted_label_ids),
        # Positions remain in map coordinates, which keeps drag edits stable
        # when the page size changes.  The page section is the print contract.
        "canvas_px": [map_width_px, map_height_px],
        "page": {
            "size_mm": [page_width_mm, page_height_mm],
            "canvas_px": [page_width_px, page_height_px],
            "orientation": orientation,
            "margin_mm": spec.margin_mm,
            "map_origin_px": map_origin,
            "map_size_mm": [round(map_width_px / px_per_mm, 3),
                            round(map_height_px / px_per_mm, 3)],
            "dpi": round(px_per_mm * 25.4, 3),
            "furniture": furniture,
        },
        "title": {
            "id": "map-title",
            "original_text": original_title,
            "text": title_text,
            "braille_text": to_grade1_font_text(title_text, preserve_newlines=True),
            "enabled": bool(saved_title.get("enabled", True)),
            "align": (str(saved_title.get("align", "center")).lower()
                      if str(saved_title.get("align", "center")).lower() in TITLE_ALIGNS else "center"),
            "position_page_px": title_position,
            "box_width_px": title_width,
            "top_gap_from_page_margin_mm": TITLE_TOP_GAP_MM,
            "page_inset_from_margin_mm": TITLE_PAGE_INSET_MM,
        },
        "labels": labels,
    }


def _render_metrics(label: dict, font: ImageFont.FreeTypeFont,
                    px_per_mm: float) -> dict:
    """Return the one physical geometry used by both PNG and browser preview."""
    padding = int(round(BRAILLE_PADDING_MM * px_per_mm))
    pin_outer_radius = BRAILLE_PIN_TOTAL_SIZE_MM * px_per_mm / 2.0
    pin_black_radius = BRAILLE_PIN_BLACK_DIAMETER_MM * px_per_mm / 2.0
    braille = str(label.get("braille_text") or "")
    bbox = font.getbbox(braille or " ")
    text_w = max(1, int(math.ceil(font.getlength(braille or " "))))
    text_h = max(1, bbox[3] - bbox[1])
    callout = bool(label.get("callout", False))
    box_w = text_w + (2 * padding if callout else 0)
    box_h = text_h + (2 * padding if callout else 0)
    side = str(label.get("side", "right")).lower()
    if not callout:
        # A plain label is centred on its anchor. It has no opaque box and no
        # locator symbol; the box is retained solely as its editor hit area.
        box_x, box_y = -box_w / 2.0, -box_h / 2.0
    # `side` denotes the optional PIN's position relative to the text box.
    elif side == "left":
        box_x, box_y = pin_outer_radius, -box_h / 2.0
    elif side == "top":
        box_x, box_y = -box_w / 2.0, pin_outer_radius
    elif side == "bottom":
        box_x, box_y = -box_w / 2.0, -pin_outer_radius - box_h
    else:
        side, box_x, box_y = "right", -pin_outer_radius - box_w, -box_h / 2.0
    return {
        "side": side,
        "callout": callout,
        "pin_shape": (str(label.get("pin_shape", "circle")).lower()
                      if str(label.get("pin_shape", "circle")).lower() in PIN_SHAPES
                      else "circle"),
        "font_size_px": int(getattr(font, "size", 1)),
        "font_ascent_px": int(font.getmetrics()[0]),
        "text_bbox_px": [int(v) for v in bbox],
        "text_width_px": text_w,
        "text_height_px": text_h,
        "box_offset_px": [round(box_x, 3), round(box_y, 3)],
        "box_size_px": [box_w, box_h],
        "text_offset_px": [round(box_x + (padding if callout else 0) - bbox[0], 3),
                           round(box_y + (padding if callout else 0) - bbox[1], 3)],
        "pin_outer_radius_px": round(pin_outer_radius, 3),
        "pin_black_radius_px": round(pin_black_radius, 3),
    }


def _polygon_points(x: float, y: float, radius: float, shape: str) -> list[tuple[int, int]]:
    if shape == "triangle":
        return [(round(x), round(y - radius)),
                (round(x + radius), round(y + radius)),
                (round(x - radius), round(y + radius))]
    return [(round(x - radius), round(y - radius)),
            (round(x + radius), round(y - radius)),
            (round(x + radius), round(y + radius)),
            (round(x - radius), round(y + radius))]


def _draw_pin_symbol(draw: ImageDraw.ImageDraw, x: float, y: float,
                     metrics: dict) -> None:
    outer_radius = float(metrics["pin_outer_radius_px"])
    black_radius = float(metrics["pin_black_radius_px"])
    shape = str(metrics.get("pin_shape", "circle"))
    if shape == "circle":
        draw.ellipse((round(x - outer_radius), round(y - outer_radius),
                      round(x + outer_radius), round(y + outer_radius)), fill=255)
        draw.ellipse((round(x - black_radius), round(y - black_radius),
                      round(x + black_radius), round(y + black_radius)), fill=0)
        return
    draw.polygon(_polygon_points(x, y, outer_radius, shape), fill=255)
    draw.polygon(_polygon_points(x, y, black_radius, shape), fill=0)


def _draw_label(draw: ImageDraw.ImageDraw, label: dict, font: ImageFont.FreeTypeFont,
                px_per_mm: float, origin_px: tuple[float, float] = (0.0, 0.0)) -> dict:
    x = float(label["position_px"][0]) + origin_px[0]
    y = float(label["position_px"][1]) + origin_px[1]
    braille = str(label.get("braille_text") or "")
    metrics = _render_metrics(label, font, px_per_mm)
    label["render_metrics"] = metrics
    box_x, box_y = metrics["box_offset_px"]
    box_w, box_h = metrics["box_size_px"]
    box = [x + box_x, y + box_y, x + box_x + box_w, y + box_y + box_h]

    if metrics["callout"]:
        draw.rectangle(tuple(round(v) for v in box), fill=255)
    if braille:
        text_xy = (round(x + metrics["text_offset_px"][0]),
                   round(y + metrics["text_offset_px"][1]))
        draw.text(text_xy, braille, font=font, fill=0)
    if metrics["callout"]:
        _draw_pin_symbol(draw, x, y, metrics)
    return {
        "id": label["id"],
        "box_page_px": [round(v, 3) for v in box],
        "pin_page_px": ([round(x, 3), round(y, 3)] if metrics["callout"] else None),
    }


def _draw_title(draw: ImageDraw.ImageDraw, title: dict, font: ImageFont.FreeTypeFont,
                px_per_mm: float, page: dict) -> dict:
    """Draw the page title without a location pin, centered above the map."""
    padding = int(round(BRAILLE_PADDING_MM * px_per_mm))
    braille = str(title.get("braille_text") or "")
    page_width = float(page["canvas_px"][0])
    inset = (float(page.get("margin_mm", 0)) +
             float(title.get("page_inset_from_margin_mm", TITLE_PAGE_INSET_MM))) * px_per_mm
    default_position = [inset, inset]
    box_x, box_y = _point(title.get("position_page_px"), default_position)
    box_w = min(page_width, max(30 * px_per_mm,
                               float(title.get("box_width_px", page_width - 2 * inset))))
    content_width = max(1, box_w - 2 * padding)
    lines: list[str] = []
    for paragraph in (braille.split("\n") if braille else [""]):
        words = paragraph.split(" ") if paragraph else [""]
        current = ""
        for word in words:
            if font.getlength(word or " ") > content_width:
                if current:
                    lines.append(current)
                    current = ""
                chunk = ""
                for cell in word:
                    if chunk and font.getlength(chunk + cell) > content_width:
                        lines.append(chunk)
                        chunk = cell
                    else:
                        chunk += cell
                current = chunk
                continue
            candidate = word if not current else f"{current} {word}"
            if current and font.getlength(candidate) > content_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    line_bboxes = [font.getbbox(line or " ") for line in lines]
    line_height = max(1, max(box[3] - box[1] for box in line_bboxes))
    line_advance = BRAILLE_LINE_SPACING_MM * px_per_mm
    text_w = max(1, int(math.ceil(max(font.getlength(line or " ") for line in lines))))
    box_h = line_height + max(0, len(lines) - 1) * line_advance + 2 * padding
    box_x = min(max(box_x, 0.0), max(0.0, page_width - box_w))
    page_height = float(page["canvas_px"][1])
    box_y = min(max(box_y, 0.0), max(0.0, page_height - box_h))
    align = str(title.get("align", "center")).lower()
    if align not in TITLE_ALIGNS:
        align = "center"
    line_offsets = []
    for index, (line, bbox) in enumerate(zip(lines, line_bboxes)):
        line_width = font.getlength(line or " ")
        if align == "left":
            text_x = padding - bbox[0]
        elif align == "right":
            text_x = box_w - padding - line_width - bbox[0]
        else:
            text_x = (box_w - line_width) / 2.0 - bbox[0]
        line_offsets.append([round(text_x, 3), round(padding - bbox[1] + index * line_advance, 3)])
    if title.get("enabled", True) and braille:
        for line, offset in zip(lines, line_offsets):
            draw.text((round(box_x + offset[0]), round(box_y + offset[1])), line, font=font, fill=0)
    title["position_page_px"] = [round(box_x, 3), round(box_y, 3)]
    title["box_width_px"] = round(box_w, 3)
    title["render_metrics"] = {
        "font_size_px": int(getattr(font, "size", 1)),
        "font_ascent_px": int(font.getmetrics()[0]),
        "text_bbox_px": [0, int(line_bboxes[0][1]), text_w,
                         int(line_bboxes[0][1] + line_height)],
        "box_size_px": [box_w, box_h],
        "box_offset_px": [0.0, 0.0],
        "text_offset_px": line_offsets[0],
        "lines": lines,
        "line_offsets_px": line_offsets,
        "line_height_px": line_height,
        "line_spacing_px": line_advance,
        "line_spacing_mm": BRAILLE_LINE_SPACING_MM,
        "align": align,
    }
    return {"id": "map-title", "box_page_px": [round(box_x, 3), round(box_y, 3),
                                                     round(box_x + box_w, 3), round(box_y + box_h, 3)]}


def _north_marker_image(size_px: int) -> Image.Image:
    """Rasterize the supplied north-marker SVG at final print resolution."""

    if not NORTH_MARKER_PATH.is_file():
        raise FileNotFoundError(f"Step 7 north marker is missing: {NORTH_MARKER_PATH}")
    try:
        import resvg_py
    except ImportError as exc:
        raise RuntimeError("Rendering the north marker requires resvg_py") from exc
    height_px = max(1, round(size_px * 35.791565 / 35.131245))
    # The supplied Inkscape asset declares physical ``mm`` dimensions, which
    # resvg rejects for direct rasterization. Keep its viewBox and drawing
    # untouched while providing a concrete pixel viewport for final output.
    svg = NORTH_MARKER_PATH.read_text(encoding="utf-8")
    svg = re.sub(r'width="[^"]+"', f'width="{size_px}"', svg, count=1)
    svg = re.sub(r'height="[^"]+"', f'height="{height_px}"', svg, count=1)
    png = resvg_py.svg_to_bytes(
        svg_string=svg,
        width=size_px,
        height=height_px,
    )
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _draw_map_furniture(page_canvas: Image.Image, furniture: dict,
                        px_per_mm: float) -> dict:
    """Draw the optional 3 mm map frame and the supplied north marker."""

    rendered = {"border": False, "north": False, "scale": "placeholder"}
    border = furniture.get("border", {})
    if border.get("enabled", False):
        rect = tuple(round(float(value)) for value in border["rect_page_px"])
        width = max(1, round(MAP_BORDER_STROKE_MM * px_per_mm))
        ImageDraw.Draw(page_canvas).rectangle(rect, outline=0, width=width)
        rendered["border"] = True
    north = furniture.get("north", {})
    if north.get("enabled", False):
        size_px = max(1, round(NORTH_MARKER_SIZE_MM * px_per_mm))
        marker = _north_marker_image(size_px)
        position = tuple(round(float(value)) for value in north["position_page_px"])
        page_canvas.paste(marker.convert("L"), position, marker.getchannel("A"))
        rendered["north"] = True
    return rendered


def render_braille_layout(out_dir: Path, layout: dict) -> dict:
    base_path = out_dir / "step8a_cleanup.png"
    if not base_path.exists():
        raise FileNotFoundError("run Step 7 before adding Braille labels")
    map_canvas = Image.open(base_path).convert("L")
    px_per_mm = float(layout.get("render_px_per_mm", RENDER_PX_PER_MM))
    page = layout.get("page", {})
    page_size = tuple(int(v) for v in page.get("canvas_px", map_canvas.size))
    map_origin = tuple(float(v) for v in page.get("map_origin_px", [0, 0]))
    if (map_origin[0] + map_canvas.width > page_size[0]
            or map_origin[1] + map_canvas.height > page_size[1]):
        raise ValueError("Step 8 page layout cannot contain the tactile map")
    page_canvas = Image.new("L", page_size, 255)
    page_canvas.paste(map_canvas, (round(map_origin[0]), round(map_origin[1])))
    furniture = _draw_map_furniture(page_canvas, page.get("furniture", {}), px_per_mm)
    dpi = float(page.get("dpi", px_per_mm * 25.4))
    # The base is used by the live SVG preview. It intentionally has no label
    # layer, because that layer is edited in real time in the browser.
    page_canvas.save(out_dir / "step8_braille_base.png", dpi=(dpi, dpi))
    base_black = np.asarray(page_canvas).copy()
    font_px = max(1, int(round(BRAILLE_FONT_SIZE_PT * 25.4 / 72.0 * px_per_mm)))
    font = ImageFont.truetype(str(braille_font_path()), font_px)
    draw = ImageDraw.Draw(page_canvas)
    rendered = []
    title = layout.get("title", {})
    title_metrics = _draw_title(draw, title, font, px_per_mm, page)
    rendered_title = title_metrics if title.get("enabled", True) and title.get("braille_text") else None
    for label in layout.get("labels", []):
        label["render_metrics"] = _render_metrics(label, font, px_per_mm)
        if label.get("enabled", True):
            rendered.append(_draw_label(draw, label, font, px_per_mm, map_origin))

    layout_path = out_dir / "braille_labels.json"
    layout_path.write_text(json.dumps(layout, indent=2, ensure_ascii=False), encoding="utf-8")
    # The final PNG is a full A4 composition with real print-resolution metadata.
    page_canvas.save(out_dir / "step8_braille.png", dpi=(dpi, dpi))
    hybrid_path = out_dir / "step8a_hybrid.png"
    if hybrid_path.exists():
        hybrid_page = Image.new("RGB", page_size, "white")
        with Image.open(hybrid_path) as hybrid_map:
            hybrid_page.paste(hybrid_map.convert("RGB"),
                              (round(map_origin[0]), round(map_origin[1])))
        hybrid_base_pixels = np.asarray(hybrid_page).copy()
        hybrid_base_pixels[base_black < 128] = (0, 0, 0)
        Image.fromarray(hybrid_base_pixels).save(
            out_dir / "step8_hybrid_base.png", dpi=(dpi, dpi))
        # Raised content and Braille remain solid black above the printed base.
        hybrid_pixels = np.asarray(hybrid_page).copy()
        black = np.asarray(page_canvas) < 128
        hybrid_pixels[black] = (0, 0, 0)
        Image.fromarray(hybrid_pixels).save(out_dir / "step8_hybrid.png", dpi=(dpi, dpi))
    report = {
        "renderer_version": BRAILLE_LAYOUT_VERSION,
        "source_artifact": "step8a_cleanup.png",
        "layout_artifact": layout_path.name,
        "output_artifact": "step8_braille.png",
        "font": layout["font"],
        "braille_standard": layout["braille_standard"],
        "canvas_px": list(page_canvas.size),
        "page": page,
        "enabled_labels": len(rendered),
        "total_labels": len(layout.get("labels", [])),
        "title": rendered_title,
        "furniture": furniture,
        "rendered_labels": rendered,
        "api_calls": 0,
    }
    (out_dir / "step8_braille.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def run_step8(image_path: Path, runs_dir: Path = Path("runs")) -> dict:
    """Initialize or refresh Step 8 and render the final Braille-labelled map."""
    out_dir = runs_dir / image_path.stem
    overlay_path = out_dir / "overlay_labels.json"
    if not overlay_path.exists():
        raise FileNotFoundError("run Step 7 before Step 8")
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    semantics_path = out_dir / "step1_semantics.json"
    semantics = (json.loads(semantics_path.read_text(encoding="utf-8"))
                 if semantics_path.exists() else {})
    saved_path = out_dir / "braille_labels.json"
    previous = (json.loads(saved_path.read_text(encoding="utf-8"))
                if saved_path.exists() else None)
    summary_path = out_dir / "step6_summary.json"
    summary = (json.loads(summary_path.read_text(encoding="utf-8"))
               if summary_path.exists() else {})
    page_layout = load_step7_page_layout(out_dir)
    layout = build_braille_layout(
        overlay, previous, detected_title=semantics.get("title", ""),
        page_orientation=page_layout.get("orientation", summary.get("orientation")),
        page_layout=page_layout,
    )
    report = render_braille_layout(out_dir, layout)
    return {**report, "out_dir": str(out_dir), "labels": layout["labels"]}


def update_braille_label(out_dir: Path, label_id: str, patch: dict) -> tuple[dict, dict]:
    """Apply one validated UI edit and redraw the Step 8 output."""
    layout_path = out_dir / "braille_labels.json"
    if not layout_path.exists():
        raise FileNotFoundError("run Step 8 before editing Braille labels")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    label = next((item for item in layout.get("labels", [])
                  if str(item.get("id")) == label_id), None)
    if label is None:
        raise KeyError(label_id)
    if "text" in patch:
        label["text"] = repair_label_text(patch["text"])
        label["braille_text"] = to_grade1_font_text(label["text"])
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            raise ValueError("enabled must be true or false")
        label["enabled"] = patch["enabled"]
    if "side" in patch:
        side = str(patch["side"]).lower()
        if side not in BOX_SIDES:
            raise ValueError("side must be left, right, top, or bottom")
        label["side"] = side
    if "callout" in patch:
        if not isinstance(patch["callout"], bool):
            raise ValueError("callout must be true or false")
        label["callout"] = patch["callout"]
    if "pin_shape" in patch:
        pin_shape = str(patch["pin_shape"]).lower()
        if pin_shape not in PIN_SHAPES:
            raise ValueError("pin shape must be circle, triangle, or square")
        label["pin_shape"] = pin_shape
    if "position_px" in patch:
        position = _point(patch["position_px"], label["position_px"])
        page = layout.get("page", {})
        page_width, page_height = (float(v) for v in page.get(
            "canvas_px", layout.get("canvas_px", [0, 0])))
        origin_x, origin_y = _point(page.get("map_origin_px"), [0.0, 0.0])
        # Labels are map-relative when fixed, but may be detached anywhere on
        # the physical page. These bounds therefore use page space rather than
        # clipping every label to the geographic raster.
        label["position_px"] = [
            round(min(max(position[0], -origin_x), page_width - origin_x), 3),
            round(min(max(position[1], -origin_y), page_height - origin_y), 3),
        ]
        px_per_mm = float(layout.get("render_px_per_mm", RENDER_PX_PER_MM))
        label["position_mm"] = [round(label["position_px"][0] / px_per_mm, 3),
                                round(label["position_px"][1] / px_per_mm, 3)]
    report = render_braille_layout(out_dir, layout)
    return label, report


def update_braille_title(out_dir: Path, patch: dict) -> tuple[dict, dict]:
    """Update the editable page title and redraw the Step 8 output."""
    layout_path = out_dir / "braille_labels.json"
    if not layout_path.exists():
        raise FileNotFoundError("run Step 8 before editing the Braille title")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    title = layout.setdefault("title", {
        "id": "map-title", "original_text": "", "text": "", "braille_text": "",
        "enabled": True, "align": "center", "position_page_px": [0.0, 0.0],
        "top_gap_from_page_margin_mm": TITLE_TOP_GAP_MM,
        "page_inset_from_margin_mm": TITLE_PAGE_INSET_MM,
    })
    if "text" in patch:
        title["text"] = repair_multiline_text(patch["text"])
        title["braille_text"] = to_grade1_font_text(title["text"], preserve_newlines=True)
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            raise ValueError("enabled must be true or false")
        title["enabled"] = patch["enabled"]
    if "align" in patch:
        align = str(patch["align"]).lower()
        if align not in TITLE_ALIGNS:
            raise ValueError("title align must be left, center, or right")
        title["align"] = align
    if "position_page_px" in patch:
        page_width, page_height = (float(v) for v in layout.get("page", {}).get("canvas_px", [0, 0]))
        position = _point(patch["position_page_px"], title.get("position_page_px", [0.0, 0.0]))
        title["position_page_px"] = [round(min(max(position[0], 0.0), page_width), 3),
                                     round(min(max(position[1], 0.0), page_height), 3)]
    if "box_width_px" in patch:
        width = float(patch["box_width_px"])
        if not math.isfinite(width):
            raise ValueError("title width must be finite")
        page_width = float(layout.get("page", {}).get("canvas_px", [0, 0])[0])
        px_per_mm = float(layout.get("render_px_per_mm", RENDER_PX_PER_MM))
        title["box_width_px"] = round(min(max(width, 30 * px_per_mm), page_width), 3)
    report = render_braille_layout(out_dir, layout)
    return title, report


def delete_braille_label(out_dir: Path, label_id: str) -> tuple[dict, dict]:
    """Remove one label and remember deleted detections across Step 8 reruns."""
    layout_path = out_dir / "braille_labels.json"
    if not layout_path.exists():
        raise FileNotFoundError("run Step 8 before deleting Braille labels")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    labels = layout.get("labels", [])
    label = next((item for item in labels if str(item.get("id")) == label_id), None)
    if label is None:
        raise KeyError(label_id)
    layout["labels"] = [item for item in labels if str(item.get("id")) != label_id]
    if label.get("source_index") is not None:
        deleted = {str(item) for item in layout.get("deleted_label_ids", [])}
        deleted.add(label_id)
        layout["deleted_label_ids"] = sorted(deleted)
    report = render_braille_layout(out_dir, layout)
    return label, report


def update_braille_toolbox(out_dir: Path, patch: dict) -> tuple[dict, dict]:
    """Apply Step 8 page/toolbox state and redraw the editable output page."""
    layout_path = out_dir / "braille_labels.json"
    if not layout_path.exists():
        raise FileNotFoundError("run Step 8 before editing its page layout")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    toolbox = layout.setdefault("toolbox", {
        "fix_text_to_map": False,
        "group_map_elements": False,
    })
    for field in ("fix_text_to_map", "group_map_elements"):
        if field in patch:
            if not isinstance(patch[field], bool):
                raise ValueError(f"{field} must be true or false")
            toolbox[field] = patch[field]

    if "all_text_enabled" in patch:
        enabled = patch["all_text_enabled"]
        if not isinstance(enabled, bool):
            raise ValueError("all_text_enabled must be true or false")
        layout.setdefault("title", {})["enabled"] = enabled
        for label in layout.get("labels", []):
            label["enabled"] = enabled

    page = layout.setdefault("page", {})
    page_width, page_height = (float(value) for value in page.get("canvas_px", [0, 0]))
    map_width, map_height = (float(value) for value in layout.get("canvas_px", [0, 0]))
    old_origin = _point(page.get("map_origin_px"), [0.0, 0.0])
    new_origin = old_origin.copy()
    if "map_origin_px" in patch:
        requested = _point(patch["map_origin_px"], old_origin)
        new_origin = [
            round(min(max(requested[0], 0.0), max(0.0, page_width - map_width)), 3),
            round(min(max(requested[1], 0.0), max(0.0, page_height - map_height)), 3),
        ]
        delta = [new_origin[0] - old_origin[0], new_origin[1] - old_origin[1]]
        if delta != [0.0, 0.0]:
            if not toolbox.get("fix_text_to_map", False):
                # Keep detached labels at their physical page locations while
                # the geographic image moves beneath them.
                for label in layout.get("labels", []):
                    position = _point(label.get("position_px"), [0.0, 0.0])
                    label["position_px"] = [round(position[0] - delta[0], 3),
                                            round(position[1] - delta[1], 3)]
            if toolbox.get("group_map_elements", False):
                furniture = page.get("furniture", {})
                border = furniture.get("border", {})
                north = furniture.get("north", {})
                if border.get("enabled"):
                    rect = border.get("rect_page_px", [])
                    if len(rect) == 4:
                        border["rect_page_px"] = [
                            rect[0] + delta[0], rect[1] + delta[1],
                            rect[2] + delta[0], rect[3] + delta[1],
                        ]
                if north.get("enabled"):
                    point = north.get("position_page_px", [])
                    if len(point) == 2:
                        north["position_page_px"] = [point[0] + delta[0],
                                                     point[1] + delta[1]]
        page["map_origin_px"] = new_origin

    raw_furniture = patch.get("furniture", page.get("furniture"))
    page["furniture"] = normalize_map_furniture(
        raw_furniture, new_origin, [round(map_width), round(map_height)],
        float(layout.get("render_px_per_mm", RENDER_PX_PER_MM)),
        page_width, page_height,
    )
    px_per_mm = float(layout.get("render_px_per_mm", RENDER_PX_PER_MM))
    for label in layout.get("labels", []):
        position = _point(label.get("position_px"), [0.0, 0.0])
        label["position_px"] = [
            round(min(max(position[0], -new_origin[0]), page_width - new_origin[0]), 3),
            round(min(max(position[1], -new_origin[1]), page_height - new_origin[1]), 3),
        ]
        label["position_mm"] = [round(label["position_px"][0] / px_per_mm, 3),
                                round(label["position_px"][1] / px_per_mm, 3)]

    # Step 7 and Step 8 share one physical paper contract. Persist Step 8's
    # moves back to it so a later rerender does not jump to an older position.
    page_layout_path = out_dir / "page_layout.json"
    try:
        page_layout = json.loads(page_layout_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        page_layout = {}
    page_layout.update({
        "size_mm": page.get("size_mm"),
        "canvas_px": page.get("canvas_px"),
        "orientation": page.get("orientation"),
        "margin_mm": page.get("margin_mm"),
        "map_origin_px": page.get("map_origin_px"),
        "map_size_px": [round(map_width), round(map_height)],
        "map_size_mm": page.get("map_size_mm"),
        "render_px_per_mm": layout.get("render_px_per_mm", RENDER_PX_PER_MM),
        "dpi": page.get("dpi"),
        "furniture": page.get("furniture"),
    })
    page_layout_path.write_text(json.dumps(page_layout, indent=2), encoding="utf-8")
    report = render_braille_layout(out_dir, layout)
    return layout, report


def add_braille_label(out_dir: Path, text: object = "") -> tuple[dict, dict]:
    """Create a persistent user-authored label at the map centre."""
    layout_path = out_dir / "braille_labels.json"
    if not layout_path.exists():
        raise FileNotFoundError("run Step 8 before adding Braille labels")
    layout = json.loads(layout_path.read_text(encoding="utf-8"))
    width, height = (float(v) for v in layout.get("canvas_px", [0, 0]))
    label_text = repair_label_text(text)
    label = {
        "id": f"manual-{uuid.uuid4().hex[:12]}",
        "source_index": None,
        "original_text": "",
        "text": label_text,
        "braille_text": to_grade1_font_text(label_text),
        "enabled": True,
        "side": "right",
        "callout": False,
        "pin_shape": "circle",
        "position_px": [round(width / 2.0, 3), round(height / 2.0, 3)],
        "position_mm": [round(width / 2.0 / float(layout.get("render_px_per_mm", RENDER_PX_PER_MM)), 3),
                        round(height / 2.0 / float(layout.get("render_px_per_mm", RENDER_PX_PER_MM)), 3)],
        "original_position_px": [round(width / 2.0, 3), round(height / 2.0, 3)],
        "kind": "manual",
        "priority": None,
    }
    layout.setdefault("labels", []).insert(0, label)
    report = render_braille_layout(out_dir, layout)
    return label, report
