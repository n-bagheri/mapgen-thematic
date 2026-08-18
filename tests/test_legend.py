import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

from mapgen.legend import (build_legend_layout, legend_swatch_hybrid,
                           render_legend_layout, update_legend,
                           update_legend_page_orientation)


def write_inputs(out_dir: Path) -> None:
    (out_dir / "symbols.json").write_text(json.dumps({
        "render_px_per_mm": 5.0,
        "area_assignments": [{"label": "Forest", "pattern": "solid_black", "pattern_desc": "solid",
                              "is_thematic": True, "transform": {}},
                             {"label": "Grass", "pattern": "03_lines_vertical",
                              "pattern_desc": "lines", "is_thematic": True, "transform": {}},
                             {"label": "background / no fill", "pattern": "plain",
                              "pattern_desc": "smooth", "is_thematic": False,
                              "legend_visible": False, "transform": {}}],
    }), encoding="utf-8")
    (out_dir / "step8_boundaries.json").write_text(json.dumps({
        "active_priority_patterns": ["03_lines_vertical"]}), encoding="utf-8")
    (out_dir / "braille_labels.json").write_text(json.dumps({"title": {"text": "Land use"}}), encoding="utf-8")


class LegendTests(unittest.TestCase):
    def test_hybrid_swatch_preserves_white_compound_boundary_over_color(self):
        layout = {"render_px_per_mm": 5.0, "swatch": {"size_px": [200, 100]}}
        swatch = legend_swatch_hybrid(layout, {
            "pattern": "plain", "transform": {}, "color": "#59F7FF",
            "compound_border": True,
        })
        self.assertEqual(swatch.getpixel((0, 50)), (0, 0, 0))
        self.assertEqual(swatch.getpixel((6, 50)), (255, 255, 255))
        self.assertEqual(swatch.getpixel((100, 50)), (89, 247, 255))

    def test_legend_uses_physical_samples_and_preserves_edits(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_inputs(out_dir)
            layout = build_legend_layout(out_dir)
            self.assertEqual(len(layout["entries"]), 2)
            self.assertEqual(layout["swatch"]["size_mm"], [40.0, 20.0])
            self.assertFalse(layout["entries"][0]["compound_border"])
            self.assertTrue(layout["entries"][1]["compound_border"])
            render_legend_layout(out_dir, layout)
            item, _ = update_legend(out_dir, "legend-0", {"text": "Woodland"})
            self.assertEqual(item["braille_text"], "`woodland")
            saved = json.loads((out_dir / "legend_labels.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["entries"][0]["text"], "Woodland")
            self.assertTrue((out_dir / "step9_legend_base.png").exists())
            self.assertTrue((out_dir / "step9_legend.png").exists())

    def test_disabling_an_entry_removes_its_swatch_and_text(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_inputs(out_dir)
            layout = build_legend_layout(out_dir)
            render_legend_layout(out_dir, layout)
            x, y = (round(value) for value in layout["entries"][0]["position_page_px"])
            with Image.open(out_dir / "step9_legend.png") as enabled:
                self.assertEqual(enabled.getpixel((x + 20, y + 20)), 0)
            item, _ = update_legend(out_dir, "legend-0", {
                "enabled": False, "position_page_px": [100, 120],
            })
            self.assertFalse(item["enabled"])
            with Image.open(out_dir / "step9_legend.png") as disabled:
                self.assertEqual(disabled.getpixel((120, 140)), 255)

    def test_legend_title_supports_lines_alignment_position_and_width(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_inputs(out_dir)
            render_legend_layout(out_dir, build_legend_layout(out_dir))
            item, _ = update_legend(out_dir, "title", {
                "text": "Land use\nlegend", "align": "center",
                "position_page_px": [25, 30], "box_width_px": 300,
            })
            self.assertEqual(item["text"], "Land use\nlegend")
            self.assertEqual(item["align"], "center")
            self.assertEqual(item["position_page_px"], [25.0, 30.0])
            self.assertEqual(item["box_width_px"], 300.0)

    def test_legend_title_uses_ten_mm_line_spacing(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_inputs(out_dir)
            layout = build_legend_layout(out_dir)
            layout["title"]["text"] = "Land use\nlegend"
            render_legend_layout(out_dir, layout)
            metrics = layout["title"]["render_metrics"]
            self.assertEqual(metrics["line_spacing_mm"], 10.0)
            self.assertEqual(metrics["line_spacing_px"], 50.0)
            self.assertEqual(metrics["line_offsets_px"][1][1] -
                             metrics["line_offsets_px"][0][1], 50.0)

    def test_legend_page_orientation_can_be_changed_and_persists(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_inputs(out_dir)
            render_legend_layout(out_dir, build_legend_layout(out_dir))
            layout, _ = update_legend_page_orientation(out_dir, "landscape")
            self.assertEqual(layout["page"]["orientation"], "landscape")
            self.assertEqual(layout["page"]["size_mm"], [297.0, 210.0])
            saved = json.loads((out_dir / "legend_labels.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["page"]["orientation"], "landscape")
