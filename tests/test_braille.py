import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw, ImageFont

from mapgen.braille import (build_braille_layout, render_braille_layout,
                            to_grade1_font_text, update_braille_label,
                            add_braille_label, update_braille_title,
                            _draw_title, braille_font_path,
                            load_step7_page_layout, update_step7_page_layout)
from webui import server


def overlay_fixture() -> dict:
    return {
        "coordinate_contract": {"tactile_size_px": [240, 160]},
        "labels": [{
            "text": "PARIS 30",
            "kind": "capital",
            "priority": 1,
            "text_position_tactile_px": [40, 50],
            "text_position_source_px": [20, 25],
            "box_source_px": [10, 20, 30, 30],
        }],
    }


class BrailleConversionTests(unittest.TestCase):
    def test_grade1_adds_capitals_word_and_number_indicators(self):
        self.assertEqual(to_grade1_font_text("PARIS 30"), "``paris #cj")
        self.assertEqual(to_grade1_font_text("Paris"), "`paris")

    def test_accents_are_transliterated_for_the_configured_english_standard(self):
        self.assertEqual(to_grade1_font_text("Saône"), "`saone")

    def test_title_keeps_manual_lines_and_wraps_inside_a_resizable_box(self):
        layout = build_braille_layout(overlay_fixture(), detected_title="Iran\nclimate")
        self.assertEqual(layout["title"]["text"], "Iran\nclimate")
        self.assertIn("\n", layout["title"]["braille_text"])
        layout["title"]["box_width_px"] = 170
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            render_braille_layout(out_dir, layout)
        self.assertGreaterEqual(len(layout["title"]["render_metrics"]["lines"]), 2)

    def test_title_lines_are_spaced_ten_mm_center_to_center(self):
        layout = build_braille_layout(overlay_fixture(), detected_title="Iran\nclimate")
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            render_braille_layout(out_dir, layout)
        metrics = layout["title"]["render_metrics"]
        self.assertEqual(metrics["line_spacing_mm"], 10.0)
        self.assertEqual(metrics["line_spacing_px"], 50.0)
        self.assertEqual(metrics["line_offsets_px"][1][1] -
                         metrics["line_offsets_px"][0][1], 50.0)

    def test_step7_page_position_persists_for_step8(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            first = load_step7_page_layout(out_dir)
            moved = update_step7_page_layout(out_dir, [12, 34])
            reloaded = load_step7_page_layout(out_dir)
            self.assertNotEqual(first["map_origin_px"], moved["map_origin_px"])
            self.assertEqual(reloaded["map_origin_px"], [12.0, 34.0])

    def test_user_selected_step7_orientation_persists(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            changed = update_step7_page_layout(out_dir, orientation="landscape")
            reloaded = load_step7_page_layout(out_dir)
            self.assertEqual(changed["orientation"], "landscape")
            self.assertEqual(reloaded["orientation"], "landscape")
            self.assertEqual(reloaded["canvas_px"], [1485, 1050])
            self.assertEqual(reloaded["allowed_orientations"], ["portrait", "landscape"])

    def test_step7_map_furniture_persists_and_renders_into_the_page_base(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            layout = load_step7_page_layout(out_dir)
            furniture = {
                "border": {"enabled": True,
                           "rect_page_px": [30, 40, 270, 200]},
                "north": {"enabled": True, "position_page_px": [80, 45]},
            }
            saved_page = update_step7_page_layout(out_dir, furniture=furniture)
            self.assertTrue(saved_page["furniture"]["border"]["enabled"])
            self.assertEqual(saved_page["furniture"]["border"]["stroke_mm"], 3.0)
            self.assertTrue(saved_page["furniture"]["north"]["enabled"])

            rendered_layout = build_braille_layout(
                overlay_fixture(), page_layout=saved_page,
            )
            report = render_braille_layout(out_dir, rendered_layout)
            with Image.open(out_dir / "step8_braille_base.png") as base:
                self.assertEqual(base.getpixel((30, 40)), 0)
                self.assertIsNotNone(ImageChops.difference(
                    base, Image.new("L", base.size, 255)).getbbox())
            self.assertTrue(report["furniture"]["border"])
            self.assertTrue(report["furniture"]["north"])
            self.assertEqual(report["furniture"]["scale"], "placeholder")

    def test_moving_map_leaves_independent_furniture_in_place(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            with_furniture = update_step7_page_layout(out_dir, furniture={
                "border": {"enabled": True, "rect_page_px": [30, 40, 270, 200]},
                "north": {"enabled": True, "position_page_px": [80, 45]},
            })
            origin = with_furniture["map_origin_px"]
            moved = update_step7_page_layout(out_dir,
                                             [origin[0] + 10, origin[1] + 15])
            self.assertEqual(moved["map_origin_px"], [origin[0] + 10, origin[1] + 15])
            self.assertEqual(moved["furniture"]["border"]["rect_page_px"],
                             [30.0, 40.0, 270.0, 200.0])
            self.assertEqual(moved["furniture"]["north"]["position_page_px"],
                             [80.0, 45.0])

    def test_page_layout_api_saves_map_furniture(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            Image.new("L", (240, 160), 255).save(run_dir / "step8a_cleanup.png")
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                response = server.app.test_client().post("/api/page-layout/sample", json={
                    "furniture": {
                        "border": {"enabled": True,
                                   "rect_page_px": [10, 20, 210, 140]},
                        "north": {"enabled": True,
                                  "position_page_px": [35, 40]},
                    },
                })
            self.assertEqual(response.status_code, 200)
            furniture = response.get_json()["layout"]["furniture"]
            self.assertTrue(furniture["border"]["enabled"])
            self.assertEqual(furniture["border"]["stroke_mm"], 3.0)
            self.assertTrue(furniture["north"]["enabled"])

    def test_previous_user_edits_survive_a_layout_refresh(self):
        first = build_braille_layout(overlay_fixture())
        first["labels"][0].update({"text": "Edited", "enabled": False,
                                   "position_px": [90, 70]})
        refreshed = build_braille_layout(overlay_fixture(), first)
        label = refreshed["labels"][0]
        self.assertEqual(label["text"], "Edited")
        self.assertFalse(label["enabled"])
        self.assertEqual(label["position_px"], [90.0, 70.0])

    def test_renderer_writes_a_map_sized_png_and_audit(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 0).save(out_dir / "step8a_cleanup.png")
            layout = build_braille_layout(overlay_fixture())
            report = render_braille_layout(out_dir, layout)
            with Image.open(out_dir / "step8_braille.png") as rendered:
                self.assertEqual(rendered.size, (1050, 1485))
                self.assertAlmostEqual(rendered.info["dpi"][0], 127.0, places=1)
            with Image.open(out_dir / "step8_braille_base.png") as base:
                self.assertEqual(base.size, (1050, 1485))
                origin = tuple(round(v) for v in layout["page"]["map_origin_px"])
                self.assertEqual(base.getpixel(origin), 0)
                self.assertEqual(base.getpixel((origin[0] + 239, origin[1] + 159)), 0)
                self.assertEqual(base.getpixel((0, 0)), 255)
            self.assertEqual(report["enabled_labels"], 1)
            self.assertEqual(report["api_calls"], 0)
            self.assertTrue((out_dir / "braille_labels.json").exists())
            saved = json.loads((out_dir / "braille_labels.json").read_text(encoding="utf-8"))
            metrics = saved["labels"][0]["render_metrics"]
            self.assertEqual(metrics["pin_outer_radius_px"], 25.0)  # 6 mm black + 2 mm ring
            self.assertEqual(metrics["pin_black_radius_px"], 15.0)
            self.assertLess(metrics["box_offset_px"][0], -25.0)  # pin is right of its box
            self.assertEqual(saved["geometry"]["padding_mm"], 3.0)

    def test_landscape_step6_orientation_uses_landscape_a4_page(self):
        layout = build_braille_layout(
            overlay_fixture(), page_orientation="landscape",
        )

        self.assertEqual(layout["page"]["orientation"], "landscape")
        self.assertEqual(layout["page"]["size_mm"], [297.0, 210.0])
        self.assertEqual(layout["page"]["canvas_px"], [1485, 1050])

    def test_layout_falls_back_to_the_other_paper_rotation_when_needed(self):
        overlay = overlay_fixture()
        # 260 x 120 mm: portrait cannot fit the width between 15 mm margins,
        # but the same configured A4 sheet fits after rotation.
        overlay["coordinate_contract"]["tactile_size_px"] = [1300, 600]

        layout = build_braille_layout(overlay, page_orientation="portrait")

        self.assertEqual(layout["page"]["orientation"], "landscape")
        self.assertEqual(layout["page"]["size_mm"], [297.0, 210.0])

    def test_layout_reports_when_neither_paper_rotation_can_fit_the_map(self):
        overlay = overlay_fixture()
        overlay["coordinate_contract"]["tactile_size_px"] = [1400, 600]

        with self.assertRaisesRegex(ValueError, "either orientation"):
            build_braille_layout(overlay)

    def test_each_side_places_the_box_outside_the_pin(self):
        layout = build_braille_layout(overlay_fixture())
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            for side in ("left", "right", "top", "bottom"):
                layout["labels"][0]["side"] = side
                render_braille_layout(out_dir, layout)
                saved = json.loads((out_dir / "braille_labels.json").read_text(encoding="utf-8"))
                metrics = saved["labels"][0]["render_metrics"]
                self.assertEqual(metrics["side"], side)
                if side == "left":
                    self.assertEqual(metrics["box_offset_px"][0], 25)
                elif side == "right":
                    self.assertLess(metrics["box_offset_px"][0], -25)
                elif side == "top":
                    self.assertEqual(metrics["box_offset_px"][1], 25)
                else:
                    self.assertLess(metrics["box_offset_px"][1], -25)

    def test_title_is_centered_five_mm_below_the_page_margin(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            layout = build_braille_layout(overlay_fixture(), detected_title="Land use")
            render_braille_layout(out_dir, layout)
            saved = json.loads((out_dir / "braille_labels.json").read_text(encoding="utf-8"))
            title = saved["title"]
            self.assertEqual(title["text"], "Land use")
            self.assertEqual(title["position_page_px"], [100.0, 100.0])
            self.assertEqual(title["render_metrics"]["box_offset_px"], [0.0, 0.0])
            self.assertEqual(title["render_metrics"]["box_size_px"][0], 850.0)
            self.assertEqual(title["align"], "center")
            self.assertEqual(title["top_gap_from_page_margin_mm"], 5.0)

    def test_title_does_not_paint_an_opaque_background_box(self):
        canvas = Image.new("L", (400, 220), 0)
        title = {
            "braille_text": "`iran", "enabled": True, "align": "center",
            "position_page_px": [25, 25], "page_inset_from_margin_mm": 5.0,
        }
        font = ImageFont.truetype(str(braille_font_path()), 42)

        _draw_title(ImageDraw.Draw(canvas), title, font, 5.0,
                    {"canvas_px": [400, 220], "margin_mm": 15.0})

        # The title dots are black; an opaque title box would introduce white.
        self.assertEqual(canvas.getextrema(), (0, 0))

    def test_rendered_title_never_erases_the_map_beneath_it(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            # Deliberately put dark map content behind the title position.
            Image.new("L", (240, 160), 0).save(out_dir / "step8a_cleanup.png")
            overlay = overlay_fixture()
            overlay["labels"] = []
            render_braille_layout(
                out_dir, build_braille_layout(overlay, detected_title="Iran"),
            )
            with Image.open(out_dir / "step8_braille_base.png") as base, \
                    Image.open(out_dir / "step8_braille.png") as rendered:
                # A transparent title may add black dots, but must never turn
                # an existing dark map pixel into a lighter/white pixel.
                self.assertIsNone(ImageChops.subtract(rendered, base).getbbox())

    def test_manual_labels_and_title_edits_persist(self):
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            Image.new("L", (240, 160), 255).save(out_dir / "step8a_cleanup.png")
            render_braille_layout(out_dir, build_braille_layout(overlay_fixture()))
            manual, _ = add_braille_label(out_dir, "New place")
            title, _ = update_braille_title(out_dir, {"text": "My map"})
            self.assertEqual(manual["kind"], "manual")
            self.assertEqual(title["braille_text"], "`my map")
            saved = json.loads((out_dir / "braille_labels.json").read_text(encoding="utf-8"))
            refreshed = build_braille_layout(overlay_fixture(), saved)
            self.assertEqual(len(refreshed["labels"]), 2)
            self.assertEqual(refreshed["labels"][0]["id"], manual["id"])
            self.assertEqual(refreshed["title"]["text"], "My map")


class BrailleApiTests(unittest.TestCase):
    def test_download_combines_map_and_legend_into_a_pdf(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir(); run_dir.mkdir(parents=True)
            Image.new("RGB", (4, 4), "white").save(maps_dir / "sample.png")
            Image.new("L", (100, 140), 255).save(run_dir / "step8_braille.png", dpi=(127, 127))
            Image.new("L", (100, 140), 255).save(run_dir / "step9_legend.png", dpi=(127, 127))
            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                response = server.app.test_client().get("/api/download/sample")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "application/pdf")
            self.assertTrue(response.data.startswith(b"%PDF"))

    def test_api_edits_text_visibility_and_pin_position(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir()
            run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            Image.new("L", (240, 160), 255).save(run_dir / "step8a_cleanup.png")
            render_braille_layout(run_dir, build_braille_layout(overlay_fixture()))
            label_id = json.loads((run_dir / "braille_labels.json").read_text(
                encoding="utf-8"))["labels"][0]["id"]

            with patch.object(server, "MAPS_DIR", maps_dir), \
                    patch.object(server, "RUNS_DIR", root / "runs"):
                client = server.app.test_client()
                current = client.get("/api/braille-labels/sample")
                self.assertEqual(current.status_code, 200)
                changed = client.post(f"/api/braille-labels/sample/{label_id}", json={
                    "text": "Lyon 2", "enabled": False, "side": "top", "position_px": [110, 80],
                })
            self.assertEqual(changed.status_code, 200)
            label = changed.get_json()["label"]
            self.assertEqual(label["text"], "Lyon 2")
            self.assertEqual(label["braille_text"], "`lyon #b")
            self.assertFalse(label["enabled"])
            self.assertEqual(label["side"], "top")
            self.assertEqual(label["position_px"], [110.0, 80.0])
            self.assertTrue((run_dir / "step8_braille.png").exists())

    def test_api_adds_manual_label_and_edits_title(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            maps_dir = root / "maps"
            run_dir = root / "runs" / "sample"
            maps_dir.mkdir(); run_dir.mkdir(parents=True)
            (maps_dir / "sample.png").write_bytes(b"map")
            Image.new("L", (240, 160), 255).save(run_dir / "step8a_cleanup.png")
            render_braille_layout(run_dir, build_braille_layout(overlay_fixture()))
            with patch.object(server, "MAPS_DIR", maps_dir), patch.object(server, "RUNS_DIR", root / "runs"):
                client = server.app.test_client()
                added = client.post("/api/braille-labels/sample", json={"text": "Manual"})
                titled = client.post("/api/braille-labels/sample/title", json={"text": "My map"})
            self.assertEqual(added.status_code, 200)
            self.assertTrue(added.get_json()["label"]["id"].startswith("manual-"))
            self.assertEqual(titled.status_code, 200)
            self.assertEqual(titled.get_json()["title"]["text"], "My map")


if __name__ == "__main__":
    unittest.main()
