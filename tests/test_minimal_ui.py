"""Contract tests for the focused (minimal) view.

The focused page is a standalone application: it shares the HTTP API with the
detailed page but none of its markup or JavaScript.  These tests pin the parts
of that contract that are easy to break silently -- the nine-step numbering,
the endpoints each editor talks to, and the two controls this pipeline has no
server support for.
"""

import json
import pathlib
import re
import tempfile
import unittest

from webui import server

STATIC = pathlib.Path(server.app.static_folder)
MINIMAL_DIR = STATIC / "minimal"


def module_source() -> str:
    """Every focused-view module concatenated, for whole-application checks."""
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted(MINIMAL_DIR.rglob("*.js")))


class MinimalUiRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_detailed_and_focused_pages_stay_separate(self):
        detailed = self.client.get("/")
        focused = self.client.get("/minimal")
        try:
            self.assertEqual(detailed.status_code, 200)
            self.assertEqual(focused.status_code, 200)
            self.assertIn(b'id="steps"', detailed.data)
            self.assertNotIn(b"minimal", detailed.data)
            self.assertIn(b'id="project-nav"', focused.data)
            self.assertIn(b'id="visual-pane"', focused.data)
            self.assertIn(b'id="control-pane"', focused.data)
            self.assertIn(b'src="/minimal/main.js', focused.data)
        finally:
            detailed.close()
            focused.close()

    def test_focused_page_does_not_load_the_detailed_application(self):
        """The focused view is standalone; pulling in app.js would run two
        applications against the same DOM."""
        response = self.client.get("/minimal")
        try:
            self.assertNotIn(b"app.js", response.data)
            self.assertNotIn(b'id="job-log"', response.data)
        finally:
            response.close()

    def test_focused_assets_are_served(self):
        stylesheet = self.client.get("/minimal.css")
        entry = self.client.get("/minimal/main.js")
        try:
            self.assertEqual(stylesheet.status_code, 200)
            self.assertEqual(entry.status_code, 200)
            # A standalone sheet, not a re-skin layered over the detailed page.
            self.assertNotIn(b"@import", stylesheet.data)
        finally:
            stylesheet.close()
            entry.close()

    def test_every_module_is_reachable_over_http(self):
        for path in sorted(MINIMAL_DIR.rglob("*.js")):
            route = "/" + path.relative_to(STATIC).as_posix()
            response = self.client.get(route)
            try:
                self.assertEqual(response.status_code, 200, route)
            finally:
                response.close()

    def test_palette_follows_the_detailed_page(self):
        minimal = (STATIC / "minimal.css").read_text(encoding="utf-8")
        detailed = (STATIC / "style.css").read_text(encoding="utf-8")
        for token in ("#2456d6", "#f5f6f8", "#e2e5ea", "#1c2430", "#6b7686"):
            self.assertIn(token, minimal)
            self.assertIn(token, detailed)

    def test_no_job_log_anywhere_in_the_focused_view(self):
        source = module_source() + (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertNotIn("job-log", source)


class StepNumberingTests(unittest.TestCase):
    """This pipeline aggregates at Step 5, simplifies at Step 6, merges symbols
    and boundaries into Step 7, and adds Braille Steps 8 and 9."""

    def setUp(self):
        self.steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")

    def test_nine_steps_with_no_eight_a(self):
        keys = re.findall(r'\{ key: "([^"]+)"', self.steps)
        self.assertEqual(keys, ["1", "2", "3", "4", "5", "6", "7", "8", "9"])
        self.assertNotIn('"8a"', self.steps)

    def test_the_run_pauses_for_the_step_5_category_review(self):
        self.assertIn('FIRST_BATCH = ["1", "2", "3", "4", "5"]', self.steps)
        self.assertIn('FINAL_BATCH = ["6", "7", "8", "9"]', self.steps)

    def test_every_step_the_server_knows_has_a_definition(self):
        keys = set(re.findall(r'\{ key: "([^"]+)"', self.steps))
        self.assertEqual(keys, {str(step) for step in server.STEP_ARTIFACTS})

    def test_the_gate_reads_the_step_5_review_flag(self):
        source = module_source()
        self.assertIn("step5_review_ready", source)
        self.assertNotIn("step6_review_ready", source)


class EndpointContractTests(unittest.TestCase):
    """The focused view may only call endpoints this server actually exposes."""

    def setUp(self):
        self.source = module_source()
        self.rules = {str(rule) for rule in server.app.url_map.iter_rules()}

    def test_renamed_simplification_endpoints_are_used(self):
        for path in ("/api/step6params/", "/api/step6presets/", "/api/step6preset/"):
            self.assertIn(path, self.source)
        for path in ("/api/step5params", "/api/step5presets", "/api/step5preset"):
            self.assertNotIn(path, self.source)

    def test_pattern_editing_uses_the_per_group_endpoints(self):
        for path in ("/api/pattern-assignments/", "/api/pattern-transforms/",
                     "/api/pattern-library-preview/", "/api/pattern-preview/"):
            self.assertIn(path, self.source)
        # The batch approve/reset flow does not exist on this server.
        self.assertNotIn("/api/pattern-review", self.source)

    def test_controls_without_server_support_are_absent(self):
        """This server has no cancel and no boundary-stroke endpoint, so the
        focused view must not offer either control."""
        self.assertNotIn("/api/cancel", self.source)
        self.assertNotIn("/api/boundary-stroke", self.source)
        self.assertNotIn("boundary-width-slider", self.source)
        self.assertNotIn("/api/cancel", self.rules)
        self.assertNotIn("/api/boundary-stroke/<stem>", self.rules)

    def test_every_new_feature_is_reachable(self):
        for path in ("/api/maskreview/", "/api/category-colors/", "/api/page-layout/",
                     "/api/north-marker.svg", "/api/braille-labels/",
                     "/api/legend/", "/api/legend-page/", "/api/legend-swatch/",
                     "/api/download/", "/api/maps/order"):
            self.assertIn(path, self.source)

    def test_the_braille_typeface_is_loaded_by_the_stylesheet(self):
        """Braille glyphs are drawn as text in the SVG overlay, so the page has
        to fetch the same font the server renders the PNG with."""
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("@font-face", stylesheet)
        self.assertIn("/api/braille-font", stylesheet)
        self.assertIn("MapGenBraille", stylesheet)
        # The rendered PNG already carries the braille on the page, so the face
        # is needed for the previews beside each editable name.
        self.assertIn(".braille-preview", stylesheet)

    def test_the_page_overlay_never_paints_over_the_render(self):
        """The PNG under the overlay is the real page. Filling the overlay
        shapes would hide the artwork it is supposed to let you grab."""
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        rule = re.search(r"\.braille-pin rect \{([^}]*)\}", stylesheet)
        self.assertIsNotNone(rule)
        self.assertIn("fill: transparent", rule.group(1))
        overlay = (MINIMAL_DIR / "editors" / "braille.js").read_text(encoding="utf-8")
        self.assertNotIn("<text", overlay)

    def test_project_management_is_available_from_the_library(self):
        library = (MINIMAL_DIR / "library.js").read_text(encoding="utf-8")
        for name in ("renameMap", "reorderMaps", "deleteMap"):
            self.assertIn(name, library)

    def test_every_api_path_in_the_source_matches_a_server_route(self):
        """A typo in a path would otherwise only surface as a 404 at runtime."""
        used = {match.rstrip("/")
                for match in re.findall(r'["\x60](/api/[A-Za-z0-9._/-]*)', self.source)}
        prefixes = {re.sub(r"<[^>]+>", "", rule).rstrip("/") for rule in self.rules}
        prefixes.discard("")
        for path in sorted(used):
            self.assertTrue(
                any(path == prefix or path.startswith(prefix) for prefix in prefixes),
                f"{path} does not match any route on this server",
            )


class EditorCoverageTests(unittest.TestCase):
    """Every reviewable step gets its own panel in the focused view."""

    def setUp(self):
        self.controls = (MINIMAL_DIR / "controls.js").read_text(encoding="utf-8")

    def test_each_step_has_an_editor(self):
        for name in ("maskEditorHtml", "textEditorHtml", "lineEditorHtml",
                     "simplificationEditorHtml", "patternEditorHtml", "pageEditorHtml",
                     "brailleEditorHtml", "legendEditorHtml", "exportEditorHtml"):
            self.assertIn(name, self.controls)

    def test_every_editor_belongs_to_a_step(self):
        """The right pane is the step list, so each editor is reached by opening
        the step that owns it rather than by a name of its own."""
        marker = "const STEP_EDITORS = {"
        block = self.controls[self.controls.index(marker) + len(marker):]
        block = block[:block.index("};")]
        owned = {int(step): [n.strip() for n in body.split(",") if n.strip()]
                 for step, body in re.findall(r"(\d+): \[([^]]*)\]", block)}
        self.assertEqual(owned, {
            2: ["maskEditorHtml"],
            3: ["textEditorHtml"],
            4: ["lineEditorHtml"],
            5: ["aggregationEditorHtml"],
            6: ["simplificationEditorHtml"],
            7: ["patternEditorHtml", "pageEditorHtml"],
            8: ["brailleEditorHtml"],
            9: ["legendEditorHtml"],
        })

    def test_an_open_step_insets_its_contents(self):
        """The shared .editor-body rule assumes a card that already has side
        padding. A step row carries its padding on the summary, so without its
        own rule the contents sit flush against the border."""
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        body = re.search(r"\.step-section > \.editor-body \{([^}]*)\}", stylesheet)
        self.assertIsNotNone(body, "step bodies have no padding rule of their own")
        sides = re.search(r"padding:\s*([^;]+);", body.group(1)).group(1).split()
        self.assertGreaterEqual(len(sides), 2, "padding must set a horizontal value")
        self.assertNotEqual(sides[1].strip(), "0", "step contents would sit flush left")

    def test_opening_a_step_drives_the_left_pane(self):
        self.assertIn("setActiveStep(details.dataset.step)", self.controls)
        # One step open at a time, so the left pane is never ambiguous.
        self.assertIn("other.open = false", self.controls)

    def test_each_step_can_be_run_on_its_own(self):
        self.assertIn("data-run-step=", self.controls)
        self.assertIn("runSingleStep", self.controls)

    def test_an_out_of_scope_map_offers_only_step_1(self):
        self.assertIn("scopeBlockHtml", self.controls)
        self.assertIn("rerun-step1", self.controls)
        self.assertIn("blockingReason", self.controls)

    def test_category_grouping_is_edited_here_not_delegated(self):
        aggregation = (MINIMAL_DIR / "editors" / "aggregation.js").read_text(encoding="utf-8")
        for marker in ("tactile-group", "layer-chip", 'draggable="true"',
                       "add-group", "reset-groups"):
            self.assertIn(marker, aggregation)

    def test_step_5_colours_come_from_step_4_classes(self):
        """classes_gen.json only exists after Step 6, so the Step 5 review has
        to colour its chips from Step 4's classes_final.json."""
        aggregation = (MINIMAL_DIR / "editors" / "aggregation.js").read_text(encoding="utf-8")
        self.assertIn("classesFinal", aggregation)
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        self.assertIn("classes_final.json", workspace)


class PayloadFieldTests(unittest.TestCase):
    """The Braille, legend and page-layout endpoints reject unknown fields
    outright, so the focused view must only send the documented subset."""

    def read(self, name):
        return (MINIMAL_DIR / "editors" / name).read_text(encoding="utf-8")

    def test_label_patch_fields_are_supported(self):
        sent = set(re.findall(r"patchLabel\([^,]+,\s*\{\s*(\w+)", self.read("braille.js")))
        self.assertTrue(sent)
        self.assertLessEqual(sent, {"text", "enabled", "position_px", "side"})

    def test_title_patch_fields_are_supported(self):
        sent = set(re.findall(r"patchTitle\(\{\s*(\w+)", self.read("braille.js")))
        self.assertTrue(sent)
        self.assertLessEqual(sent, {"text", "enabled", "align",
                                    "position_page_px", "box_width_px"})

    def test_legend_patch_fields_are_supported(self):
        sent = set(re.findall(r"patchItem\([^,]+,\s*\{\s*(\w+)", self.read("legend.js")))
        self.assertTrue(sent)
        self.assertLessEqual(sent, {"text", "enabled", "align",
                                    "position_page_px", "box_width_px"})

    def test_page_layout_patch_fields_are_supported(self):
        sent = set(re.findall(r"commitLayout\(\{\s*(\w+)", self.read("page.js")))
        self.assertTrue(sent)
        self.assertLessEqual(sent, {"map_origin_px", "orientation", "furniture"})

    def test_mask_strokes_stay_inside_the_server_limits(self):
        source = self.read("mask.js")
        self.assertIn("length >= 300", source)   # at most 300 strokes per call
        self.assertIn("length < 4000", source)   # well inside the 5000-point cap


class AggregationPayloadTests(unittest.TestCase):
    """Round-trip the exact payload the focused view builds through the real
    review writer, so a shape change here fails loudly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self._tmp.name)
        self.aggregation = {
            "slots": 3,
            "source_classes": [{"index": 0, "label": "ice"},
                               {"index": 1, "label": "tundra"},
                               {"index": 2, "label": "forest"}],
            "groups": [{"label": "cold", "members": [0, 1]},
                       {"label": "forest", "members": [2]}],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_reviewed_grouping_is_accepted(self):
        from mapgen.aggregate import save_aggregation_review
        groups = [
            {"label": "ice", "members": [0], "approved": True,
             "rationale": "reviewed in the focused view"},
            {"label": "plants", "members": [1, 2], "approved": True,
             "rationale": "reviewed in the focused view"},
        ]
        review = save_aggregation_review(self.run_dir, self.aggregation, groups)
        self.assertTrue(review["approved"])
        saved = json.loads(
            (self.run_dir / "aggregation_review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(saved["groups"]), 2)

    def test_dropping_a_source_class_is_rejected(self):
        from mapgen.aggregate import save_aggregation_review
        groups = [{"label": "cold", "members": [0, 1], "approved": True, "rationale": ""}]
        with self.assertRaises(ValueError):
            save_aggregation_review(self.run_dir, self.aggregation, groups)


class StepViewTests(unittest.TestCase):
    """The left pane is driven by STEP_VIEWS, so a typo in an artifact name
    would only show up as a silently broken image."""

    def setUp(self):
        self.steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")

    def known_artifacts(self):
        names = set()
        for table in (server.STEP_ARTIFACTS, server.STEP_EXTRA):
            for group in table.values():
                names.update(group)
        return names

    def test_every_step_has_pictures_to_show(self):
        views = set(int(n) for n in re.findall(r"^  (\d+): \[", self.steps, re.M))
        self.assertEqual(views, {int(s) for s in server.STEP_ARTIFACTS})

    def test_every_named_artifact_is_one_the_pipeline_writes(self):
        known = self.known_artifacts()
        marker = "export const STEP_VIEWS = {"
        block = self.steps[self.steps.index(marker):]
        used = set(re.findall(r'artifact: "([^"]+)"', block))
        used |= set(re.findall(r'hybrid: "([^"]+)"', block))
        unknown = sorted(used - known)
        self.assertEqual(unknown, [], f"not produced by any step: {unknown}")

    def test_the_left_pane_follows_the_opened_step(self):
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        self.assertIn("STEP_VIEWS[step]", visual)
        self.assertIn("export function setActiveStep", visual)

    def test_the_mask_brush_sits_on_the_image_its_coordinates_use(self):
        """maskreview reports map_area.png pixels, so the brush must be drawn
        on that picture and not on the uploaded source."""
        self.assertIn('artifact: "map_area.png"', self.steps)
        self.assertIn('overlay: "mask"', self.steps)
        mask = (MINIMAL_DIR / "editors" / "mask.js").read_text(encoding="utf-8")
        self.assertIn('$("mask-target")', mask)


class ViewerToolTests(unittest.TestCase):
    """The picture tools the detailed page offers are the same on every step,
    so the controls do not move as the reader walks the pipeline."""

    def setUp(self):
        self.viewer = (MINIMAL_DIR / "viewer.js").read_text(encoding="utf-8")
        self.visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")

    def test_the_toolbar_offers_what_the_detailed_page_offers(self):
        detailed = (STATIC / "app.js").read_text(encoding="utf-8")
        for control in ("page-zoom-out", "page-zoom-in", "page-zoom-100", "page-zoom-fit",
                        "page-zoom-range", "page-zoom-readout", "page-guides-toggle",
                        "page-snap-toggle"):
            self.assertIn(control, self.viewer, control)
            self.assertIn(control, detailed, f"{control} is not a detailed-page control")

    def test_every_picture_on_every_step_gets_the_toolbar(self):
        # One toolbar per rendered view, emitted by the same loop.
        self.assertIn("viewerToolbarHtml(view, index)", self.visual)
        self.assertIn("bindViewer(Number(bar.dataset.viewer)", self.visual)

    def test_dragging_can_snap_to_the_braille_grid(self):
        self.assertIn("6 * pxPerMm", self.viewer)
        for name in ("braille.js", "legend.js", "page.js"):
            source = (MINIMAL_DIR / "editors" / name).read_text(encoding="utf-8")
            self.assertIn("snapToGrid(", source, name)

    def test_a_page_is_framed_the_way_the_detailed_page_frames_one(self):
        """A recessed surround with the sheet centred inside it, so the page
        edges and their margins stay visible instead of bleeding to the border."""
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        surround = re.search(r"\.map-frame \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("overflow: auto", surround)
        self.assertIn("padding:", surround)
        sheet = re.search(r"\.map-canvas \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("margin: 0 auto", sheet)
        self.assertIn("box-shadow", sheet)
        self.assertIn("background: #fff", sheet)
        # The grid rides on the sheet, so zoom scales it with the page.
        self.assertIn(".map-canvas.show-grid .page-grid", stylesheet)

    def test_fit_shows_the_whole_sheet_not_just_its_width(self):
        viewer = (MINIMAL_DIR / "viewer.js").read_text(encoding="utf-8")
        self.assertIn("availableH", viewer)
        self.assertIn("naturalH", viewer)

    def test_colour_view_is_offered_only_where_a_colour_render_exists(self):
        self.assertIn("view.hybrid ?", self.viewer)
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        marker = "export const STEP_VIEWS = {"
        block = steps[steps.index(marker):]
        self.assertEqual(len(re.findall(r"hybrid:", block)), 3)  # steps 7, 8 and 9


if __name__ == "__main__":
    unittest.main()
