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

    def test_entry_module_does_not_import_the_removed_pattern_picker(self):
        entry = (MINIMAL_DIR / "main.js").read_text(encoding="utf-8")
        self.assertNotIn("closePatternPickers", entry)

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

    def test_the_run_pauses_for_each_pipeline_batch(self):
        self.assertIn('INITIAL_BATCH = ["1", "2"]', self.steps)
        self.assertIn('ANALYSIS_BATCH = ["3", "4", "5"]', self.steps)
        self.assertIn('SIMPLIFICATION_BATCH = ["6"]', self.steps)
        self.assertIn('PATTERN_BATCH = ["7"]', self.steps)
        self.assertIn('LABEL_BATCH = ["8"]', self.steps)
        self.assertIn('FINAL_BATCH = ["9"]', self.steps)

    def test_every_step_the_server_knows_has_a_definition(self):
        keys = set(re.findall(r'\{ key: "([^"]+)"', self.steps))
        self.assertEqual(keys, {str(step) for step in server.STEP_ARTIFACTS})

    def test_the_gates_read_all_review_flags(self):
        source = module_source()
        self.assertIn("step5_review_ready", source)
        self.assertIn("step6_review_ready", source)
        self.assertIn("step7_review_ready", source)
        self.assertIn("step8_review_ready", source)
        self.assertIn("step9_review_ready", source)


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
                     "/api/aggregation-preview/", "/api/step7-review/",
                     "/api/north-marker.svg", "/api/braille-labels/",
                     "/api/braille-layout/", "/api/step8-review/",
                     "/api/legend/", "/api/legend-page/", "/api/legend-swatch/",
                     "/api/step9-review/",
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

    def test_the_page_overlay_draws_live_braille_over_a_label_free_base(self):
        """Text changes are immediate in SVG while the printable PNG rerenders."""
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        rule = re.search(r"\.braille-pin rect \{([^}]*)\}", stylesheet)
        self.assertIsNotNone(rule)
        self.assertIn("fill: transparent", rule.group(1))
        overlay = (MINIMAL_DIR / "editors" / "braille.js").read_text(encoding="utf-8")
        self.assertIn("<text", overlay)
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        self.assertIn('artifact: "step8_braille_base.png"', steps)

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
        for name in ("readingEditorHtml", "maskDecisionHtml", "textEditorHtml", "lineEditorHtml",
                     "aggregationGateHtml", "simplificationDecisionHtml", "patternDecisionHtml",
                     "brailleDecisionHtml", "legendDecisionHtml", "exportEditorHtml"):
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
            1: ["readingEditorHtml"],
            2: ["maskDecisionHtml"],
            3: ["textEditorHtml"],
            4: ["lineEditorHtml"],
            5: ["aggregationGateHtml"],
            6: ["simplificationDecisionHtml"],
            7: ["patternDecisionHtml"],
            8: ["brailleDecisionHtml"],
            9: ["legendDecisionHtml"],
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

    def test_fresh_setup_keeps_individual_steps_out_of_the_initial_view(self):
        self.assertIn('id="show-step-controls"', self.controls)
        self.assertIn("state.individualRun", self.controls)
        self.assertIn("individualRunHtml(map)", self.controls)
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn(".control-shell.is-preflight", stylesheet)
        self.assertIn(".map-stage .map-frame", stylesheet)

    def test_individual_mode_lists_only_completed_steps_and_no_setup_card(self):
        block = self.controls[self.controls.index("function individualRunHtml(map)"):]
        block = block[:block.index("/** Re-rendering mid-edit")]
        self.assertIn("stepStackHtml(map, true)", block)
        self.assertIn("Run step ${esc(next.number)}", block)
        self.assertNotIn("setupHtml", block)
        self.assertIn("completedOnly", self.controls)
        self.assertIn("STEP_DEFS.filter((step) => Boolean(map.steps?.[step.key]))",
                      self.controls)
        self.assertIn("STEP_DEFS.filter((item) => Boolean(stepState(map, item)))",
                      self.controls)

    def test_individual_mode_keeps_its_step_list_while_a_step_runs(self):
        render = self.controls[self.controls.index("export function renderControls()"):
                               self.controls.index("export function editorDetails")]
        self.assertIn("started ? individualRunHtml(map) : setupHtml(true, true)", render)
        self.assertNotIn("stepPanelHtml(map)", render)
        individual = self.controls[self.controls.index("function individualStepDotsHtml(map)"):
                                   self.controls.index("/** Re-rendering mid-edit")]
        self.assertIn("STEP_DEFS.map((step)", individual)
        self.assertIn('data-individual-step="${step.key}"', individual)
        self.assertIn("const nextAction = !live && next", individual)
        self.assertIn("Run step ${esc(next.number)}</button>", individual)
        self.assertNotIn("${esc(next.title)}</button>", individual)
        self.assertIn('state.individualRun ? "Individual run" : "Run all"', individual)
        self.assertIn('id="continue-run"', individual)
        self.assertIn('candidate?.key === "3" && !state.data.mask?.approved', individual)
        self.assertIn('candidate?.key === "6" && !map.step5_review_ready', individual)
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        dots = re.search(r"\.individual-step-dots \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("margin: 0", dots)
        heading = re.search(r"\.individual-run-heading \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("gap: 0", heading)

    def test_run_all_and_individual_runs_share_the_same_step_ui(self):
        render = self.controls[self.controls.index("export function renderControls()"):
                               self.controls.index("export function editorDetails")]
        self.assertIn("started ? individualRunHtml(map) : setupHtml(true, true)", render)
        self.assertNotIn("state.individualRun ? individualRunHtml", render)
        flow = self.controls[self.controls.index("function individualRunHtml(map)"):
                             self.controls.index("/** Re-rendering mid-edit")]
        self.assertIn('state.individualRun ? "Individual run" : "Run all"', flow)
        self.assertIn("stepStackHtml(map, true)", flow)
        approval = self.controls[self.controls.index("async function continueAfterApproval()"):
                                 self.controls.index("async function continueAfterLegendApproval()")]
        self.assertIn("if (!state.individualRun)", approval)
        self.assertIn("state.autorun = true", approval)
        self.assertIn("await continuePipeline()", approval)

    def test_run_all_follows_each_live_step_until_the_user_pins_one(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        self.assertIn('state.job.status === "running" && !state.viewStep', workspace)
        self.assertIn("state.activeStep = Number(current)", workspace)
        self.assertIn("currentStepKey(map) ||", workspace)
        self.assertIn("state.viewStep = dot.dataset.individualStep", self.controls)

    def test_entering_individual_mode_saves_setup_and_runs_only_step_one(self):
        block = self.controls[self.controls.index("async function showIndividualSteps"):]
        block = block[:block.index("/** Resume a paused run")]
        self.assertIn("await savePreflight()", block)
        self.assertIn("state.individualRun = true", block)
        self.assertIn("rememberIndividualMode(state.selected, true)", block)
        self.assertIn('await startJob(["1"])', block)

    def test_individual_mode_relies_on_start_over_instead_of_a_setup_shortcut(self):
        self.assertNotIn("show-run-setup", self.controls)
        self.assertNotIn("Back to run setup", self.controls)
        self.assertNotIn("Map reset. The run setup is ready again.", self.controls)
        self.assertNotIn("Deletes every result for this map and returns to the run setup.",
                         self.controls)
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn(".action-row.end.individual-next-step { justify-content: center; }",
                      stylesheet)
        reset = re.search(r"\.reset-row \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("justify-items: center", reset)

    def test_individual_mode_survives_refresh_and_map_switching(self):
        state_source = (MINIMAL_DIR / "state.js").read_text(encoding="utf-8")
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        for marker in ("rememberIndividualMode", "forgetIndividualMode", "individualModeFor",
                       "globalThis.localStorage"):
            self.assertIn(marker, state_source)
        self.assertIn("state.individualRun = individualModeFor(selectedMap())", workspace)
        self.assertIn("requested.length === 1", state_source)

    def test_run_setup_edits_the_complete_output_spec(self):
        for control_id in ("output-medium", "page-size", "page-orientation",
                           "page-margin", "braille-standard", "advanced-output-spec"):
            self.assertIn(f'id="{control_id}"', self.controls)
        for field in ("braille_cell_width_mm", "braille_cell_height_mm",
                      "min_texture_area_side_mm", "min_element_gap_mm",
                      "min_line_width_mm", "min_line_length_mm"):
            self.assertIn(f'data-spec-constant="{field}"', self.controls)
        self.assertIn('id="max-area-textures"', self.controls)
        self.assertIn('class="setup-fields"', self.controls)
        self.assertIn('readonly aria-readonly="true"', self.controls)
        self.assertIn("constants: { ...(state.spec.constants || {}), ...patch.constants }",
                      self.controls)
        self.assertNotIn('option("auto", orientation', self.controls)

    def test_every_step_uses_the_same_unnumbered_preview_card(self):
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertNotIn("stage-index", visual)
        self.assertNotIn('class="visual-header"', visual)
        self.assertNotIn("stage-badge", visual)
        self.assertNotIn("preflight-layout", stylesheet)
        self.assertIn("height: calc(100dvh - 55px)", stylesheet)

    def test_step_one_shows_a_compact_structured_reading(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('artifactJson(stem, "step1_semantics.json")', workspace)
        for field in ("sem.subject", "sem.map_type", "sem.data_ordering",
                      "sem.map_language", "sem.water_present", "sem.thematic_classes"):
            self.assertIn(field, self.controls)
        self.assertIn("What the system read", self.controls)
        self.assertIn("See full reading", self.controls)
        self.assertNotIn("Step 1 interpretation", self.controls)
        self.assertIn(".reading-summary", stylesheet)

    def test_an_out_of_scope_map_offers_only_step_1(self):
        self.assertIn("scopeBlockHtml", self.controls)
        self.assertIn("rerun-step1", self.controls)
        self.assertIn("blockingReason", self.controls)

    def test_category_grouping_is_edited_here_not_delegated(self):
        aggregation = (MINIMAL_DIR / "editors" / "aggregation.js").read_text(encoding="utf-8")
        for marker in ("tactile-group", "layer-chip", 'draggable="true"',
                       "add-group", "reset-groups"):
            self.assertIn(marker, aggregation)
        self.assertNotIn("aggregationEditorHtml", aggregation)
        self.assertIn("5: [aggregationGateHtml]", self.controls)
        self.assertIn("bindAggregationEditor(continueAfterApproval)", self.controls)

    def test_step_5_colours_come_from_step_4_classes(self):
        """classes_gen.json only exists after Step 6, so the Step 5 review has
        to colour its chips from Step 4's classes_final.json."""
        aggregation = (MINIMAL_DIR / "editors" / "aggregation.js").read_text(encoding="utf-8")
        self.assertIn("classesFinal", aggregation)
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        self.assertIn("classes_final.json", workspace)

    def test_step_5_shows_only_the_fitted_category_preview(self):
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        block = re.search(r"^  5: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertNotIn("label_map_preview.png", block)
        self.assertEqual(block.count("artifact:"), 1)
        self.assertIn("step5_aggregation_preview.png", block)

    def test_step_5_assignments_request_an_unsaved_live_preview(self):
        aggregation = (MINIMAL_DIR / "editors" / "aggregation.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        self.assertIn("previewAggregation", aggregation)
        self.assertIn("queueAggregationPreview", aggregation)
        self.assertIn('data-artifact="${esc(source.name)}"', visual)

    def test_step_6_is_a_styled_simplification_decision(self):
        simplification = (MINIMAL_DIR / "editors" / "simplification.js").read_text(
            encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("simplificationDecisionHtml", self.controls)
        self.assertIn('id="simplification-decision"', simplification)
        self.assertIn('id="preset-slider"', simplification)
        self.assertIn("Use this level &amp; continue", simplification)
        self.assertIn(".simplification-decision", stylesheet)

    def test_step_6_shows_only_the_simplified_map_with_all_layer_switches(self):
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        block = re.search(r"^  6: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertNotIn("group_map_source.png", block)
        self.assertEqual(block.count("dynamic:"), 1)
        self.assertIn('caption: "Simplified map"', block)
        self.assertIn('overlay: "layers"', block)
        for label in ("Colors", "Labels", "Lines", "Boundaries"):
            self.assertIn(f'layerButton("{label.lower() if label != "Colors" else "map"}", "{label}")',
                          visual)

    def test_step_7_is_a_pattern_decision_with_hybrid_and_distance_modes(self):
        patterns = (MINIMAL_DIR / "editors" / "patterns.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("patternDecisionHtml", self.controls)
        self.assertNotIn("patternEditorHtml", patterns)
        self.assertIn("7: [patternDecisionHtml]", self.controls)
        for control_id in ("preserve-haptic-distances", "create-hybrid-map"):
            self.assertIn(f'modeButton("{control_id}"', patterns)
        self.assertIn('id="approve-patterns"', patterns)
        for action in ("data-edit-pattern", "data-change-pattern", "data-pattern-colour"):
            self.assertIn(action, patterns)
        self.assertIn(".pattern-decision", stylesheet)
        self.assertNotIn("pageEditorHtml", self.controls)

    def test_step_7_shows_only_the_finished_master_on_the_output_page(self):
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        viewer = (MINIMAL_DIR / "viewer.js").read_text(encoding="utf-8")
        block = re.search(r"^  7: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertEqual(block.count("artifact:"), 1)
        self.assertIn('artifact: "step8a_cleanup.png"', block)
        self.assertIn("pageLayout: true", block)
        self.assertIn("originalCompare: true", block)
        self.assertIn("tactilePageCanvasHtml", visual)
        self.assertIn("Display original map", viewer)
        self.assertIn("Display colors", viewer)

    def test_step_8_is_a_full_page_layout_decision(self):
        braille = (MINIMAL_DIR / "editors" / "braille.js").read_text(encoding="utf-8")
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("brailleDecisionHtml", self.controls)
        self.assertNotIn("brailleEditorHtml", braille)
        self.assertIn("8: [brailleDecisionHtml]", self.controls)
        self.assertIn("bindBrailleEditor(continueAfterApproval)", self.controls)
        approval = self.controls[self.controls.index("async function continueAfterApproval()"):
                                 self.controls.index("async function continueAfterLegendApproval()")]
        self.assertIn("renderWorkspace(true)", approval)
        self.assertNotIn('startJob(["9"])', approval)
        decision = braille[braille.index("export function brailleDecisionHtml()"):
                           braille.index("export function bindBrailleEditor")]
        self.assertNotIn("One decision needed", decision)
        self.assertNotIn("Edit the detected text and its Braille", decision)
        row = braille[braille.index("function brailleRowHtml(label)"):
                      braille.index("function toolboxBody")]
        heading = row[row.index('class="braille-row-heading"'):
                      row.index('class="braille-row-detail"')]
        self.assertIn('class="braille-row-options"', heading)
        detail = row[row.index('class="braille-row-detail"'):]
        self.assertIn('class="braille-callout-options', detail)
        self.assertIn('class="braille-preview"', detail)
        detail_css = re.search(r"\.braille-row-detail \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("background: #f4dfb8", detail_css)
        for control_id in ("braille-add", "braille-all-off", "fix-text-to-map",
                           "group-map-elements", "toggle-north", "toggle-border",
                           "approve-braille"):
            self.assertIn(f'id="{control_id}"', braille)
        for action in ("braille-callout", "braille-shape", "braille-side",
                       "braille-delete", "data-page-map", "data-page-border",
                       "data-page-north", "data-title-resize"):
            self.assertIn(action, braille)
        block = re.search(r"^  8: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertIn("originalCompare: true", block)
        self.assertIn("pageRender: true", block)
        self.assertIn(".braille-decision", stylesheet)

    def test_step_9_is_a_final_legend_decision_with_map_comparison(self):
        legend = (MINIMAL_DIR / "editors" / "legend.js").read_text(encoding="utf-8")
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        viewer = (MINIMAL_DIR / "viewer.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("legendDecisionHtml", self.controls)
        for control_id in ("legend-compare", "approve-legend"):
            self.assertIn(f'id="{control_id}"', legend)
        embedded = legend[legend.index("export function legendEditorHtml()"):
                          legend.index("export function bindLegendEditor")]
        self.assertIn("legendToolboxBody(true)", embedded)
        self.assertIn("saveStep9Review", legend)
        block = re.search(r"^  9: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertIn("mapLegendCompare: true", block)
        self.assertIn("Display tactile map", viewer)
        self.assertIn("step9-comparison-canvas", (MINIMAL_DIR / "visual.js").read_text(
            encoding="utf-8"))
        self.assertIn(".legend-decision", stylesheet)

    def test_completed_steps_two_to_four_use_the_focused_review_views(self):
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        text = (MINIMAL_DIR / "editors" / "text.js").read_text(encoding="utf-8")
        step2 = re.search(r"^  2: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        step3 = re.search(r"^  3: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        step4 = re.search(r"^  4: \[([\s\S]*?)^  \],", steps, re.M).group(1)
        self.assertIn("intermediate: true", step2)
        self.assertIn("See all intermediate steps", visual)
        self.assertEqual(step3.count("artifact:"), 1)
        self.assertIn('caption: "Detected text on the map"', step3)
        self.assertIn("label-include", text)
        self.assertNotIn("label-remove", text)
        self.assertIn("remove: true", text)
        self.assertEqual(step4.count("artifact:"), 1)
        self.assertIn('caption: "Segmented map"', step4)
        self.assertIn('overlay: "segmented-lines"', step4)
        self.assertIn("Display lines", visual)

    def test_export_is_large_and_requires_step9_approval(self):
        export = (MINIMAL_DIR / "editors" / "exportpdf.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("map.step9_review_ready", export)
        self.assertIn("export-button", export)
        self.assertIn("data-export-preview", export)
        self.assertIn('id="export-show-input"', export)
        self.assertIn("state.showFinalMap = true", export)
        self.assertIn("state.showOriginalMap = event.target.checked", export)
        self.assertIn("step9-input-sheet", visual)
        for removed_copy in ("Final files", "The finished two-page PDF",
                             "The approved map and legend pages are ready",
                             "Two pages at"):
            self.assertNotIn(removed_copy, export)
        self.assertIn(".export-panel h3", stylesheet)
        self.assertIn(".export-button", stylesheet)
        export_css = re.search(r"\.export-panel \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("border: 1px solid var(--line)", export_css)

    def test_run_all_stops_for_an_explicit_mask_decision(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        mask = (MINIMAL_DIR / "editors" / "mask.js").read_text(encoding="utf-8")
        self.assertIn("maskDecisionHtml", self.controls)
        self.assertIn("state.data.mask?.approved", self.controls)
        self.assertIn("state.data.mask?.approved", workspace)
        for control_id in ("mask-undo", "mask-discard", "mask-save", "approve-mask"):
            self.assertIn(f'id="{control_id}"', mask)
        self.assertIn("approveMaskAndContinue", mask)

    def test_individual_step2_uses_the_full_mask_decision_panel(self):
        self.assertIn("2: [maskDecisionHtml]", self.controls)
        render = self.controls[self.controls.index("export function renderControls()"):
                               self.controls.index("export function editorDetails")]
        self.assertIn("individualRunHtml(map)", render)
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        self.assertIn("state.maskBrush.active = step === 2", visual)
        self.assertIn("bindMaskEditor(continueAfterApproval)", self.controls)
        callback = self.controls[self.controls.index("async function continueAfterApproval()"):
                                 self.controls.index("async function continueAfterLegendApproval()")]
        self.assertIn("await continuePipeline()", callback)
        self.assertNotIn('await startJob(["3"])', callback)
        mask = (MINIMAL_DIR / "editors" / "mask.js").read_text(encoding="utf-8")
        self.assertIn('class="mask-mode-icon"', mask)
        self.assertIn('src="/images/brush.png"', mask)
        self.assertNotIn("maskEditorHtml", mask)

    def test_the_right_panel_never_embeds_a_map_preview(self):
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertNotIn("stepImageSrc", self.controls)
        self.assertNotIn("step-figure", self.controls)
        self.assertNotIn(".step-figure", stylesheet)

    def test_applied_mask_changes_make_approval_glow(self):
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        mask = (MINIMAL_DIR / "editors" / "mask.js").read_text(encoding="utf-8")
        self.assertIn("needs-attention", mask)
        self.assertIn(".approve-mask.needs-attention", stylesheet)
        self.assertIn("@keyframes approval-glow", stylesheet)


class PayloadFieldTests(unittest.TestCase):
    """The Braille, legend and page-layout endpoints reject unknown fields
    outright, so the focused view must only send the documented subset."""

    def read(self, name):
        return (MINIMAL_DIR / "editors" / name).read_text(encoding="utf-8")

    def test_label_patch_fields_are_supported(self):
        sent = set(re.findall(r"patchLabel\([^,]+,\s*\{\s*(\w+)", self.read("braille.js")))
        self.assertTrue(sent)
        self.assertLessEqual(sent, {"text", "enabled", "position_px", "side",
                                    "callout", "pin_shape"})

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
        self.assertIn('artifactUrl(state.selected, "map_mask.png")', mask)
        self.assertIn("redrawMask(context, maskImage, current)", mask)
        self.assertIn("maskPixels.data[offset] >= 128", mask)
        self.assertIn("overlay.data[offset + 3] = 165", mask)


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

    def test_fit_can_go_below_the_manual_twenty_five_percent_floor(self):
        self.assertIn("const fittedZoom = Math.min", self.viewer)
        self.assertIn("Math.min(MIN_ZOOM, fittedZoom)", self.viewer)
        self.assertIn("Number(range.min) || MIN_ZOOM", self.viewer)

    def test_shared_toolbar_can_pan_a_zoomed_map(self):
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('class="page-pan-toggle"', self.viewer)
        self.assertIn("state.panMode = !state.panMode", self.viewer)
        self.assertIn("frame.setPointerCapture", self.viewer)
        self.assertIn("frame.scrollLeft", self.viewer)
        self.assertIn("frame.scrollTop", self.viewer)
        self.assertIn(".map-frame.is-pan-enabled", stylesheet)
        self.assertIn('.page-pan-toggle[aria-pressed="true"]', stylesheet)

    def test_colour_view_is_offered_only_where_a_colour_render_exists(self):
        self.assertIn("view.hybrid ?", self.viewer)
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        marker = "export const STEP_VIEWS = {"
        block = steps[steps.index(marker):]
        self.assertEqual(len(re.findall(r"hybrid:", block)), 3)  # steps 7, 8 and 9


if __name__ == "__main__":
    unittest.main()
