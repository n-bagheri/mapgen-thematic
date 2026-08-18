"""Step 9 -- physically scaled, editable Braille legend page."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .braille import (BRAILLE_FONT_SIZE_PT, BRAILLE_LINE_SPACING_MM,
                      RENDER_PX_PER_MM, braille_font_path,
                      repair_label_text, repair_multiline_text,
                      to_grade1_font_text)
from .output_spec import OutputSpec
from .patterns import normalize_pattern_transform, render_pattern

LEGEND_VERSION = 4
SWATCH_WIDTH_MM, SWATCH_HEIGHT_MM = 40.0, 20.0
PAGE_INSET_MM, ENTRY_GAP_MM, TEXT_GAP_MM = 5.0, 6.0, 5.0
WHITE_BORDER_MM, BLACK_BORDER_MM = 2.0, 1.0
ENTRY_TEXT_DROP_MM = 2.0
TITLE_ALIGNS = ("left", "center", "right")


def _font(px_per_mm: float) -> ImageFont.FreeTypeFont:
    pixels = max(1, int(round(BRAILLE_FONT_SIZE_PT * 25.4 / 72.0 * px_per_mm)))
    return ImageFont.truetype(str(braille_font_path()), pixels)


def _point(value: object, fallback: list[float]) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return fallback.copy()
    point = [float(value[0]), float(value[1])]
    if not all(math.isfinite(component) for component in point):
        raise ValueError("position must contain finite x and y values")
    return [round(component, 3) for component in point]


def _wrapped_metrics(text: str, font: ImageFont.FreeTypeFont, width_px: float,
                     align: str = "left", px_per_mm: float = RENDER_PX_PER_MM) -> dict:
    braille = to_grade1_font_text(text, preserve_newlines=True)
    width_px = max(1.0, float(width_px))
    lines: list[str] = []
    for paragraph in (braille.split("\n") if braille else [""]):
        current = ""
        for word in (paragraph.split(" ") if paragraph else [""]):
            if font.getlength(word or " ") > width_px:
                if current:
                    lines.append(current)
                    current = ""
                chunk = ""
                for cell in word:
                    if chunk and font.getlength(chunk + cell) > width_px:
                        lines.append(chunk)
                        chunk = cell
                    else:
                        chunk += cell
                current = chunk
                continue
            candidate = word if not current else f"{current} {word}"
            if current and font.getlength(candidate) > width_px:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    bboxes = [font.getbbox(line or " ") for line in lines]
    line_height = max(1, max(box[3] - box[1] for box in bboxes))
    line_advance = BRAILLE_LINE_SPACING_MM * px_per_mm
    offsets = []
    for index, (line, bbox) in enumerate(zip(lines, bboxes)):
        line_width = font.getlength(line or " ")
        if align == "right":
            x = width_px - line_width - bbox[0]
        elif align == "center":
            x = (width_px - line_width) / 2 - bbox[0]
        else:
            x = -bbox[0]
        offsets.append([round(x, 3), round(index * line_advance - bbox[1], 3)])
    return {
        "braille_text": braille,
        "lines": lines,
        "line_offsets_px": offsets,
        "line_height_px": line_height,
        "line_spacing_px": line_advance,
        "line_spacing_mm": BRAILLE_LINE_SPACING_MM,
        "width_px": round(width_px, 3),
        "height_px": line_height + max(0, len(lines) - 1) * line_advance,
        "bbox_px": [0, int(bboxes[0][1]), int(math.ceil(width_px)),
                    int(bboxes[0][1] + line_height)],
        "align": align,
    }


def legend_swatch(layout: dict, entry: dict) -> Image.Image:
    """Render the exact swatch used in both the browser and printable page."""
    px = float(layout["render_px_per_mm"])
    swatch_w, swatch_h = (int(v) for v in layout["swatch"]["size_px"])
    pattern = render_pattern(entry["pattern"], (swatch_h, swatch_w), px,
                             entry.get("transform"))
    image = Image.fromarray(pattern).convert("L")
    draw = ImageDraw.Draw(image)
    black_px = max(1, int(round(BLACK_BORDER_MM * px)))
    draw.rectangle((0, 0, swatch_w - 1, swatch_h - 1), outline=0, width=black_px)
    if entry.get("compound_border"):
        draw.rectangle((black_px, black_px, swatch_w - 1 - black_px,
                        swatch_h - 1 - black_px), outline=255,
                       width=max(1, int(round(WHITE_BORDER_MM * px))))
    return image


def legend_swatch_hybrid(layout: dict, entry: dict) -> Image.Image:
    color = str(entry.get("color") or "")
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        return legend_swatch(layout, entry).convert("RGB")
    px = float(layout["render_px_per_mm"])
    swatch_w, swatch_h = (int(v) for v in layout["swatch"]["size_px"])
    pattern = render_pattern(entry["pattern"], (swatch_h, swatch_w), px,
                             entry.get("transform"))
    rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    result = Image.new("RGB", (swatch_w, swatch_h), rgb)
    result_pixels = result.load()
    for y in range(swatch_h):
        for x in range(swatch_w):
            if pattern[y, x] < 128:
                result_pixels[x, y] = (0, 0, 0)
    draw = ImageDraw.Draw(result)
    black_px = max(1, int(round(BLACK_BORDER_MM * px)))
    draw.rectangle((0, 0, swatch_w - 1, swatch_h - 1), outline=(0, 0, 0), width=black_px)
    if entry.get("compound_border"):
        draw.rectangle((black_px, black_px, swatch_w - 1 - black_px,
                        swatch_h - 1 - black_px), outline=(255, 255, 255),
                       width=max(1, int(round(WHITE_BORDER_MM * px))))
    return result


def build_legend_layout(out_dir: Path, previous: dict | None = None) -> dict:
    """Create legend entries while retaining user-created page positions."""
    symbols = json.loads((out_dir / "symbols.json").read_text(encoding="utf-8"))
    boundaries = json.loads((out_dir / "step8_boundaries.json").read_text(encoding="utf-8"))
    saved = previous or {}
    prior = {str(item.get("id")): item for item in saved.get("entries", [])}
    px = float(symbols.get("render_px_per_mm", RENDER_PX_PER_MM))
    spec = OutputSpec.load_or_create()
    requested_orientation = saved.get("page", {}).get("orientation")
    orientation = (requested_orientation if requested_orientation in ("portrait", "landscape")
                   else spec.orientation)
    short_side, long_side = sorted((spec.page_width_mm, spec.page_height_mm))
    page_mm = ([long_side, short_side] if orientation == "landscape"
               else [short_side, long_side])
    page = [int(round(side * px)) for side in page_mm]
    inset = int(round((spec.margin_mm + PAGE_INSET_MM) * px))
    swatch = [int(round(SWATCH_WIDTH_MM * px)), int(round(SWATCH_HEIGHT_MM * px))]
    compound = set(boundaries.get("active_priority_patterns", []))
    source_title = ""
    braille_path = out_dir / "braille_labels.json"
    if braille_path.exists():
        source_title = repair_multiline_text(
            json.loads(braille_path.read_text(encoding="utf-8")).get("title", {}).get("text"))
    if not source_title:
        semantics = out_dir / "step1_semantics.json"
        if semantics.exists():
            source_title = repair_multiline_text(
                json.loads(semantics.read_text(encoding="utf-8")).get("title"))
    default_title = f"{source_title} legend".strip() if source_title else "Map legend"
    old_title = saved.get("title", {})
    title_width = float(old_title.get("box_width_px", page[0] - 2 * inset))
    title = {
        "id": "legend-title",
        "text": repair_multiline_text(old_title.get("text", default_title)),
        "enabled": bool(old_title.get("enabled", True)),
        "align": str(old_title.get("align", "left")) if str(old_title.get("align", "left")) in TITLE_ALIGNS else "left",
        "position_page_px": _point(old_title.get("position_page_px")
                                     if int(saved.get("version", 0)) >= 2 else None,
                                     [inset, inset]),
        "box_width_px": round(min(max(title_width, 30 * px), page[0]), 3),
    }
    font = _font(px)
    title_metrics = _wrapped_metrics(title["text"], font, title["box_width_px"], title["align"], px)
    first_row_y = inset + title_metrics["height_px"] + int(round(10 * px))
    entries = []
    visible_index = 0
    for index, assignment in enumerate(symbols.get("area_assignments", [])):
        if not assignment.get("legend_visible", assignment.get("is_thematic", True)):
            continue
        ident = f"legend-{index}"
        old = prior.get(ident, {})
        default_position = [inset, first_row_y + visible_index * (swatch[1] + int(round(ENTRY_GAP_MM * px)))]
        entries.append({
            "id": ident, "group_id": index, "pattern": assignment["pattern"],
            "pattern_desc": assignment.get("pattern_desc", ""),
            "color": assignment.get("color"),
            "transform": normalize_pattern_transform(assignment.get("transform")),
            "original_text": repair_label_text(assignment.get("label")),
            "text": repair_label_text(old.get("text", assignment.get("label"))),
            "enabled": bool(old.get("enabled", True)),
            "compound_border": assignment["pattern"] in compound,
            "position_page_px": _point(old.get("position_page_px")
                                         if int(saved.get("version", 0)) >= 2 else None,
                                         default_position),
        })
        visible_index += 1
    return {
        "version": LEGEND_VERSION, "render_px_per_mm": px,
        "font": {"file": braille_font_path().name, "size_pt": BRAILLE_FONT_SIZE_PT},
        "page": {"size_mm": page_mm, "canvas_px": page,
                 "orientation": orientation,
                 "margin_mm": spec.margin_mm, "dpi": px * 25.4},
        "swatch": {"size_mm": [SWATCH_WIDTH_MM, SWATCH_HEIGHT_MM], "size_px": swatch,
                   "white_border_mm": WHITE_BORDER_MM, "black_border_mm": BLACK_BORDER_MM},
        "title": title, "entries": entries,
    }


def _draw_text_lines(draw: ImageDraw.ImageDraw, position: list[float], metrics: dict,
                     font: ImageFont.FreeTypeFont) -> None:
    for line, offset in zip(metrics["lines"], metrics["line_offsets_px"]):
        draw.text((round(position[0] + offset[0]), round(position[1] + offset[1])),
                  line, font=font, fill=0)


def render_legend_layout(out_dir: Path, layout: dict) -> dict:
    px = float(layout["render_px_per_mm"])
    page_w, page_h = (int(v) for v in layout["page"]["canvas_px"])
    swatch_w, swatch_h = (int(v) for v in layout["swatch"]["size_px"])
    gap = int(round(TEXT_GAP_MM * px))
    font = _font(px)
    base = Image.new("L", (page_w, page_h), 255)
    base.save(out_dir / "step9_legend_base.png", dpi=(layout["page"]["dpi"],) * 2)
    final = base.copy()
    hybrid = base.convert("RGB")
    title = layout["title"]
    title["text"] = repair_multiline_text(title.get("text"))
    title["braille_text"] = to_grade1_font_text(title["text"], preserve_newlines=True)
    title["box_width_px"] = min(max(float(title.get("box_width_px", page_w)), 30 * px), page_w)
    title["render_metrics"] = _wrapped_metrics(title["text"], font,
                                                title["box_width_px"], title.get("align", "left"), px)
    title_w = title["box_width_px"]
    title_h = title["render_metrics"]["height_px"]
    title["position_page_px"] = [
        round(min(max(float(title["position_page_px"][0]), 0), max(0, page_w - title_w)), 3),
        round(min(max(float(title["position_page_px"][1]), 0), max(0, page_h - title_h)), 3),
    ]
    draw = ImageDraw.Draw(final)
    hybrid_draw = ImageDraw.Draw(hybrid)
    if title.get("enabled", True):
        _draw_text_lines(draw, title["position_page_px"], title["render_metrics"], font)
        _draw_text_lines(hybrid_draw, title["position_page_px"], title["render_metrics"], font)
    enabled = 0
    for entry in layout["entries"]:
        # Keep a movable compound object instead of making every entry span the
        # entire page. Long text wraps within a 100 mm text region.
        text_width = max(1, min(100 * px, page_w - swatch_w - gap))
        metrics = _wrapped_metrics(repair_label_text(entry.get("text")), font, text_width, "left", px)
        entry["braille_text"] = metrics["braille_text"]
        entry["render_metrics"] = metrics
        group_w = swatch_w + gap + metrics["width_px"]
        text_drop = int(round(ENTRY_TEXT_DROP_MM * px))
        text_y = max(0, (swatch_h - metrics["height_px"]) / 2) + text_drop
        group_h = max(swatch_h, text_y + metrics["height_px"])
        x, y = _point(entry.get("position_page_px"), [0, 0])
        x = min(max(x, 0), max(0, page_w - group_w))
        y = min(max(y, 0), max(0, page_h - group_h))
        entry["position_page_px"] = [round(x, 3), round(y, 3)]
        entry["swatch_page_px"] = [round(x, 3), round(y, 3), swatch_w, swatch_h]
        entry["text_offset_px"] = [swatch_w + gap, round(text_y, 3)]
        entry["group_size_px"] = [round(group_w, 3), round(group_h, 3)]
        if not entry.get("enabled", True):
            continue
        enabled += 1
        final.paste(legend_swatch(layout, entry), (round(x), round(y)))
        hybrid.paste(legend_swatch_hybrid(layout, entry), (round(x), round(y)))
        text_position = [x + entry["text_offset_px"][0], y + entry["text_offset_px"][1]]
        _draw_text_lines(draw, text_position, metrics, font)
        _draw_text_lines(hybrid_draw, text_position, metrics, font)
    (out_dir / "legend_labels.json").write_text(
        json.dumps(layout, indent=2, ensure_ascii=False), encoding="utf-8")
    final.save(out_dir / "step9_legend.png", dpi=(layout["page"]["dpi"],) * 2)
    hybrid.save(out_dir / "step9_legend_hybrid.png", dpi=(layout["page"]["dpi"],) * 2)
    report = {"renderer_version": LEGEND_VERSION, "source_artifact": "symbols.json",
              "output_artifact": "step9_legend.png", "entries": len(layout["entries"]),
              "enabled_entries": enabled, "api_calls": 0}
    (out_dir / "step9_legend.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_step9(image_path: Path, runs_dir: Path = Path("runs")) -> dict:
    out_dir = runs_dir / image_path.stem
    saved_path = out_dir / "legend_labels.json"
    previous = json.loads(saved_path.read_text(encoding="utf-8")) if saved_path.exists() else None
    layout = build_legend_layout(out_dir, previous)
    report = render_legend_layout(out_dir, layout)
    return {**report, "out_dir": str(out_dir), "layout": layout}


def update_legend(out_dir: Path, target: str, patch: dict) -> tuple[dict, dict]:
    path = out_dir / "legend_labels.json"
    if not path.exists():
        raise FileNotFoundError("run Step 9 before editing its legend")
    layout = json.loads(path.read_text(encoding="utf-8"))
    item = (layout["title"] if target == "title" else
            next((entry for entry in layout["entries"] if entry["id"] == target), None))
    if item is None:
        raise KeyError(target)
    if "text" in patch:
        item["text"] = (repair_multiline_text(patch["text"]) if target == "title"
                        else repair_label_text(patch["text"]))
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            raise ValueError("enabled must be true or false")
        item["enabled"] = patch["enabled"]
    if "align" in patch:
        if target != "title" or str(patch["align"]) not in TITLE_ALIGNS:
            raise ValueError("legend title alignment must be left, center, or right")
        item["align"] = str(patch["align"])
    if "position_page_px" in patch:
        item["position_page_px"] = _point(patch["position_page_px"],
                                           item.get("position_page_px", [0, 0]))
    if "box_width_px" in patch:
        if target != "title":
            raise ValueError("only the legend title has a resizable text box")
        width = float(patch["box_width_px"])
        if not math.isfinite(width):
            raise ValueError("legend title width must be finite")
        item["box_width_px"] = width
    report = render_legend_layout(out_dir, layout)
    return item, report


def update_legend_page_orientation(out_dir: Path, orientation: object) -> tuple[dict, dict]:
    """Switch the editable legend page between the two physical A4 rotations."""
    if str(orientation).lower() not in ("portrait", "landscape"):
        raise ValueError("legend page orientation must be portrait or landscape")
    path = out_dir / "legend_labels.json"
    if not path.exists():
        raise FileNotFoundError("run Step 9 before changing its page orientation")
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.setdefault("page", {})["orientation"] = str(orientation).lower()
    layout = build_legend_layout(out_dir, saved)
    report = render_legend_layout(out_dir, layout)
    return layout, report
