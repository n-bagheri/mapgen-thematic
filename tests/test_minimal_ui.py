"""Contract tests for the focused (minimal) view.

The focused page is a standalone application: it shares the HTTP API with the
detailed page but none of its markup or JavaScript.  These tests pin the parts
of that contract that are easy to break silently -- the nine-step numbering,
the endpoints each editor talks to, and the two controls this pipeline has no
server support for.
"""

import io
import json
import pathlib
import re
import tempfile
import unittest
from unittest.mock import patch

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

    def test_step_8_text_toggles_do_not_reload_the_unchanged_map_base(self):
        editor = (MINIMAL_DIR / "editors" / "braille.js").read_text(encoding="utf-8")
        label_patch = editor[editor.index("function patchLabel"):
                             editor.index("function patchTitle")]
        title_patch = editor[editor.index("function patchTitle"):
                             editor.index("function patchLayout")]
        layout_patch = editor[editor.index("function patchLayout"):
                              editor.index("async function removeLabel")]
        self.assertNotIn("refreshStepImages()", label_patch)
        self.assertNotIn("refreshStepImages()", title_patch)
        self.assertIn('Object.hasOwn(patch, "map_origin_px")', layout_patch)
        self.assertIn('Object.hasOwn(patch, "furniture")', layout_patch)
        self.assertIn("if (baseChanged)", layout_patch)
        all_off = editor[editor.index('$("braille-all-off")'):
                         editor.index("const titleText")]
        self.assertIn("setAllTextEnabled(false)", all_off)
        self.assertNotIn("renderControls()", all_off)

    def test_project_management_is_available_from_the_library(self):
        library = (MINIMAL_DIR / "library.js").read_text(encoding="utf-8")
        for name in ("renameMap", "reorderMaps", "deleteMap"):
            self.assertIn(name, library)

    def test_the_library_has_a_dedicated_top_position_drop_target(self):
        library = (MINIMAL_DIR / "library.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn("data-project-drop-top", library)
        self.assertIn("bindTopDropZone", library)
        self.assertIn("moveProject(dragged, first, false)", library)
        self.assertIn('classList.add("is-reordering")', library)
        self.assertNotIn("Move to top", library)
        self.assertNotIn(".project-drop-top.is-drop-target", stylesheet)

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


class ProjectOrderingTests(unittest.TestCase):
    def test_a_new_map_is_saved_at_the_top_of_the_project_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            maps = root / "maps"
            runs = root / "runs"
            maps.mkdir()
            runs.mkdir()
            (maps / "first.png").write_bytes(b"existing")
            (maps / "second.png").write_bytes(b"existing")
            order_path = runs / ".project_order.json"
            order_path.write_text('["first", "second"]', encoding="utf-8")

            with patch.object(server, "MAPS_DIR", maps), \
                    patch.object(server, "RUNS_DIR", runs), \
                    patch.object(server, "PROJECT_ORDER_PATH", order_path):
                response = server.app.test_client().post(
                    "/api/upload",
                    data={"file": (io.BytesIO(b"new map"), "new.png")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(server._project_order(), ["new", "first", "second"])
                self.assertEqual([path.stem for path in server.map_files()],
                                 ["new", "first", "second"])

    def test_upload_does_not_inherit_an_orphaned_run_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            maps = root / "maps"
            runs = root / "runs"
            maps.mkdir()
            runs.mkdir()
            (runs / "new").mkdir()
            (runs / "new" / "step1_semantics.json").write_text("{}", encoding="utf-8")
            order_path = runs / ".project_order.json"

            with patch.object(server, "MAPS_DIR", maps), \
                    patch.object(server, "RUNS_DIR", runs), \
                    patch.object(server, "PROJECT_ORDER_PATH", order_path):
                response = server.app.test_client().post(
                    "/api/upload",
                    data={"file": (io.BytesIO(b"new map"), "new.png")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["name"], "new_1.png")
                self.assertFalse((runs / "new_1").exists())


class EditorCoverageTests(unittest.TestCase):
    """Every reviewable step gets its own panel in the focused view."""

    def setUp(self):
        self.controls = (MINIMAL_DIR / "controls.js").read_text(encoding="utf-8")

    def test_each_step_has_an_editor(self):
        for name in ("readingEditorHtml", "maskDecisionHtml", "textEditorHtml", "lineEditorHtml",
                     "lineDrawingEditorHtml", "aggregationGateHtml", "simplificationDecisionHtml", "patternDecisionHtml",
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
            4: ["lineEditorHtml", "lineDrawingEditorHtml"],
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
        self.assertIn("setActiveStep(step)", self.controls)
        # One step open at a time, so the left pane is never ambiguous.
        self.assertIn("other.open = false", self.controls)

    def test_closed_steps_do_not_mount_hidden_editors_or_label_crops(self):
        """A later step must not compete with hundreds of hidden Step 3 images."""
        stack = self.controls[self.controls.index("function stepStackHtml"):
                              self.controls.index("function stepRunRowHtml")]
        text_editor = (MINIMAL_DIR / "editors" / "text.js").read_text(encoding="utf-8")
        self.assertIn("const expanded = number === open", stack)
        self.assertIn("const body = done && expanded", stack)
        self.assertIn("expanded || !done ? stepRunRowHtml", stack)
        self.assertIn("renderControls()", self.controls[
            self.controls.index('document.querySelectorAll(".step-section")'):])
        self.assertIn('loading="lazy"', text_editor)
        self.assertIn('fetchpriority="low"', text_editor)

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
        self.assertIn("setupHtml(!started, !started, !started || state.runSetupOpen)", render)
        self.assertIn("if (started) body += individualRunHtml(map)", render)
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
        self.assertIn("setupHtml(!started, !started, !started || state.runSetupOpen)", render)
        self.assertIn("if (started) body += individualRunHtml(map)", render)
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

    def test_finished_manual_job_opens_its_latest_completed_step(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        polling = workspace[workspace.index("function startPolling") :]
        self.assertIn('const latest = ["8", "7", "6", "5", "4", "3", "2", "1"]',
                      polling)
        self.assertIn("if (latest) state.activeStep = Number(latest)", polling)
        self.assertNotIn('else if (map?.steps?.["6"])', polling)

    def test_entering_individual_mode_saves_setup_and_runs_only_step_one(self):
        block = self.controls[self.controls.index("async function showIndividualSteps"):]
        block = block[:block.index("/** Resume a paused run")]
        self.assertIn("await commitRunSetup()", block)
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

    def test_fresh_map_ignores_stale_individual_mode_and_opens_setup(self):
        state_source = (MINIMAL_DIR / "state.js").read_text(encoding="utf-8")
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        self.assertIn("const hasProgress = STEP_DEFS.some", state_source)
        self.assertIn('if (!hasProgress && map?.job?.status !== "running") return false',
                      state_source)
        self.assertIn("completedCount(selectedMap()) === 0", workspace)

    def test_run_setup_is_expandable_above_progress_during_and_after_runs(self):
        render = self.controls[self.controls.index("export function renderControls()"):
                               self.controls.index("export function editorDetails")]
        setup = self.controls[self.controls.index("function setupHtml"):
                              self.controls.index("function individualStepDotsHtml")]
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('class="run-setup-disclosure"', setup)
        self.assertIn('id="run-setup-disclosure"', setup)
        self.assertLess(render.index("setupHtml("), render.index("individualRunHtml(map)"))
        self.assertIn('$("run-setup-disclosure")?.addEventListener("toggle"', self.controls)
        self.assertIn("state.runSetupOpen = event.currentTarget.open", self.controls)
        self.assertIn(".run-setup-disclosure", stylesheet)

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
        self.assertIn("constants: { ...(current.constants || {}), ...patch.constants }",
                      self.controls)
        self.assertNotIn('option("auto", orientation', self.controls)

    def test_run_setup_changes_are_drafted_until_the_apply_button_is_used(self):
        setup = self.controls[self.controls.index("function setupHtml"):
                              self.controls.index("function individualStepDotsHtml")]
        draft_flow = self.controls[self.controls.index("function markRunSetupDirty"):
                                   self.controls.index("/* ----------------------------------------------------------- step panel")]
        self.assertIn('id="apply-run-setup"', setup)
        self.assertIn("Changes are not applied yet.", setup)
        self.assertIn("state.runSetupDraft || state.spec", setup)
        self.assertIn("function stageSpec(patch, rerender = false)", draft_flow)
        self.assertIn("state.runSetupDraft = {", draft_flow)
        self.assertIn("await saveSpecText(draft)", draft_flow)
        self.assertIn("await savePreflight()", draft_flow)
        self.assertIn('$("apply-run-setup")?.addEventListener("click", applyRunSetupChanges)',
                      self.controls)
        self.assertIn("selected model starts with the next job", draft_flow)
        self.assertNotIn("function saveSpec(", self.controls)

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

    def test_step_one_more_chip_reveals_every_legend_category(self):
        self.assertIn('class="reading-extra-chip" hidden', self.controls)
        self.assertIn('class="reading-more-chip" type="button"', self.controls)
        self.assertIn('querySelectorAll(".reading-extra-chip")', self.controls)
        self.assertIn("extra.hidden = false", self.controls)
        self.assertIn("button.remove()", self.controls)

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

    def test_step_4_can_draw_undo_and_discard_manual_lines(self):
        lines = (MINIMAL_DIR / "editors" / "lines.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        for control_id in ("draw-new-line", "undo-drawn-line", "discard-drawn-lines",
                           "save-drawn-lines"):
            self.assertIn(f'id="{control_id}"', lines)
        self.assertNotIn("Draw or join paths in detailed view", lines)
        self.assertIn("manual_rivers.push", visual)
        self.assertIn("state.lineDrawing.addedIds.push", visual)
        self.assertIn('addEventListener("pointermove"', visual)
        self.assertIn('class="line-draw-cursor"', visual)
        self.assertIn(".map-overlay.is-line-drawing", stylesheet)
        self.assertIn("cursor: none", stylesheet)
        self.assertIn("undoDrawnLine", lines)
        self.assertIn("discardDrawnLines", lines)

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

    def test_step_6_boundaries_include_the_reviewed_coastline_fallback(self):
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        self.assertIn('["border", "border_or_coast", "coastline"]', visual)
        self.assertIn("generatedBoundaries.length", visual)
        self.assertIn("review?.fixed_features", visual)
        self.assertIn('class="boundary-path"', visual)

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
        self.assertIn("overflow-x: hidden", stylesheet)
        self.assertIn(".pattern-colour input[type=\"color\"]", stylesheet)
        self.assertIn("cursor: pointer", stylesheet)
        self.assertIn("item.water_only", patterns)
        self.assertIn("group.is_water", patterns)
        self.assertIn('waterConflict ? "disabled" : ""', patterns)
        detailed = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-water-only=', detailed)
        self.assertIn("syncChoiceAvailability", detailed)
        self.assertIn('choice.dataset.waterOnly === "true"', detailed)
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

    def test_step_9_text_updates_braille_on_the_page_while_typing(self):
        legend = (MINIMAL_DIR / "editors" / "legend.js").read_text(encoding="utf-8")
        braille = (MINIMAL_DIR / "editors" / "braille.js").read_text(encoding="utf-8")
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        self.assertIn('artifact: "step9_legend_base.png"', steps)
        self.assertIn("hybridOverlay: true", steps)
        self.assertIn("export function grade1Preview", braille)
        self.assertIn("entry.braille_text = grade1Preview(field.value)", legend)
        self.assertIn("title.braille_text = grade1Preview(titleText.value, true)", legend)
        self.assertGreaterEqual(legend.count("bindLegendOverlay();"), 4)
        self.assertIn('id="legend-title-preview"', legend)
        self.assertIn('class="braille-rendered-text"', legend)
        self.assertIn('dominant-baseline="hanging"', legend)
        self.assertIn("legendSwatchUrl(state.selected, box.id, state.colourView)", legend)
        self.assertIn("liveTextValues", legend)
        self.assertIn("Object.assign(layout.entries[index], item)", legend)
        self.assertGreaterEqual(legend.count('patchItem("title"'), 3)
        self.assertIn('const target = id === "title" ? "title" : id', legend)
        self.assertIn("patchItem(box.target || box.id", legend)
        self.assertNotIn('|| "legend-title", {', legend)

    def test_step_9_colour_toggle_refreshes_editable_legend_layers(self):
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        viewer = (MINIMAL_DIR / "viewer.js").read_text(encoding="utf-8")
        refresh = visual[visual.index("export async function refreshStepImages"):
                         visual.index("export function setActiveStep")]
        self.assertIn("view.hybrid || view.hybridOverlay", viewer)
        self.assertIn('if ($("legend-overlay") && state.data.legend)', refresh)
        self.assertIn("if (!view.hybridOverlay", refresh)
        self.assertIn("legendSwatchUrl(", refresh)
        self.assertIn("bindLegendOverlay()", refresh)
        self.assertIn('"step9_legend_hybrid.png" : "step9_legend.png"', refresh)
        self.assertLess(refresh.index("legendSwatchUrl("), refresh.index("bindLegendOverlay()"))

    def test_step_9_title_editor_appears_before_legend_entries(self):
        legend = (MINIMAL_DIR / "editors" / "legend.js").read_text(encoding="utf-8")
        toolbox = legend[legend.index("function legendToolboxBody"):
                         legend.index("export function legendDecisionHtml")]
        self.assertLess(toolbox.index('class="braille-title-box"'),
                        toolbox.index('class="braille-list"'))

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
        self.assertNotIn("Display lines", visual)
        self.assertNotIn('aria-label="Segmented map layers"', visual)

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

    def test_mask_eraser_is_black_and_cursor_matches_the_brush_radius(self):
        mask = (MINIMAL_DIR / "editors" / "mask.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('class="mask-mode-erase"', mask)
        self.assertIn(".mask-mode-erase input:checked", stylesheet)
        self.assertIn("color: #000", stylesheet)
        self.assertIn('className = "mask-brush-cursor"', mask)
        self.assertIn("state.maskBrush.radius * 2", mask)
        self.assertIn("canvas.clientWidth", mask)
        self.assertIn("canvas.clientHeight", mask)
        cursor_sizing = mask[mask.index("function sizeMaskBrushCursor"):
                             mask.index("function refreshMaskBrushCursorSize")]
        self.assertNotIn("getBoundingClientRect", cursor_sizing)
        self.assertIn('addEventListener("pointermove"', mask)
        self.assertIn(".mask-brush-cursor.is-visible", stylesheet)
        self.assertIn("box-sizing: border-box", stylesheet)
        self.assertIn("border-radius: 50%", stylesheet)
        self.assertNotIn("cursor: crosshair", stylesheet)

    def test_step_five_and_six_previews_are_preloaded_before_the_view_changes(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        visual = (MINIMAL_DIR / "visual.js").read_text(encoding="utf-8")
        simplification = (MINIMAL_DIR / "editors" / "simplification.js").read_text(encoding="utf-8")
        cache = (MINIMAL_DIR / "preview-cache.js").read_text(encoding="utf-8")
        self.assertIn("await preloadPreviews(stem, foreground)", workspace)
        self.assertIn("void preloadPreviews(stem, previews.filter", workspace)
        self.assertLess(workspace.index("await preloadPreviews(stem, foreground)"),
                        workspace.index("void preloadPreviews(stem, previews.filter"))
        self.assertIn('previews.push("step5_aggregation_preview.png")', workspace)
        self.assertIn("clearPreviewCache(state.selected)", workspace)
        self.assertIn("cachedPreviewUrl(map.stem, name)", visual)
        dynamic = visual[visual.index('if (view.dynamic === "simplified")'):
                         visual.index("const hybridEnabled", visual.index('if (view.dynamic === "simplified")'))]
        self.assertIn("url: cachedPreviewUrl(map.stem, name)", dynamic)
        self.assertNotIn("url: artifactUrl(map.stem, name)", dynamic)
        self.assertIn("function swapSimplificationPreview", simplification)
        self.assertIn("const next = new Image()", simplification)
        self.assertIn("image.src = source", simplification)
        self.assertIn("URL.createObjectURL", cache)
        self.assertIn("URL.revokeObjectURL", cache)

    def test_step_seven_preview_and_mode_toggles_keep_the_map_visible(self):
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        patterns = (MINIMAL_DIR / "editors" / "patterns.js").read_text(encoding="utf-8")
        self.assertIn('previews.push("step8a_cleanup.png")', workspace)
        self.assertIn('previews.push("step8a_hybrid.png")', workspace)
        mode = patterns[patterns.index("async function updateReviewMode"):
                        patterns.index("async function choosePattern")]
        self.assertIn("if (wasColourView && !state.colourView) await refreshStepImages()", mode)
        self.assertIn("syncPatternModeControls()", mode)
        self.assertIn("syncPatternViewerControls()", mode)
        self.assertNotIn("renderControls()", mode)
        self.assertNotIn("renderVisual()", mode)
        self.assertNotIn("refreshStepImages, renderVisual", patterns)

    def test_step_seven_edits_keep_pattern_rows_mounted_and_colour_picker_enabled(self):
        patterns = (MINIMAL_DIR / "editors" / "patterns.js").read_text(encoding="utf-8")
        colour_save = patterns[patterns.index("function saveColours"):
                               patterns.index("async function approveAndContinue")]
        refresh = patterns[patterns.index("async function refreshAfterRender"):]
        self.assertIn("pendingColourSave", colour_save)
        self.assertNotIn("changedPicker.disabled", colour_save)
        self.assertIn("if (!pendingColourSave && state.selected === request.stem)", colour_save)
        self.assertIn("if (!pendingColourSave) loadMaps().catch", colour_save)
        self.assertNotIn("await loadMaps()", colour_save)
        self.assertNotIn("renderControls()", refresh)
        self.assertIn("refreshPatternRows(groups)", refresh)
        self.assertIn("data-pattern-preview", patterns)

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

    def test_step_three_can_toggle_every_detected_text_entry_at_once(self):
        text_editor = (MINIMAL_DIR / "editors" / "text.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('id="toggle-all-text"', text_editor)
        self.assertIn('allOff ? "Turn all on" : "Turn all off"', text_editor)
        self.assertIn("checkboxes.every", text_editor)
        self.assertIn("item.include = enableAll", text_editor)
        self.assertIn("renderMapOverlay()", text_editor)
        self.assertIn(".label-list-toolbar", stylesheet)
        label_list = re.search(r"\.label-list \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("max-height: 260px", label_list)
        self.assertIn("overflow-y: auto", label_list)


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

    def test_display_colours_swaps_only_after_the_next_image_loads(self):
        binding = self.visual[self.visual.index('document.querySelectorAll(".page-view-toolbar")'):
                              self.visual.index("bindMaskCanvas()")]
        refresh = self.visual[self.visual.index("export async function refreshStepImages"):
                              self.visual.index("export function setActiveStep")]
        self.assertIn('if (change !== "colour")', binding)
        self.assertIn("const changed = await refreshStepImages()", binding)
        self.assertIn("const loaded = await new Promise", refresh)
        self.assertIn("if (!loaded) return", refresh)
        self.assertLess(refresh.index("if (!loaded) return"), refresh.index("image.src = next"))
        self.assertIn('onViewChange?.("colour")', self.viewer)
        self.assertIn("checkbox.disabled = true", self.viewer)

    def test_dragging_can_snap_to_the_braille_grid(self):
        self.assertIn("6 * pxPerMm", self.viewer)
        for name in ("braille.js", "legend.js", "page.js"):
            source = (MINIMAL_DIR / "editors" / name).read_text(encoding="utf-8")
            self.assertIn("snapToGrid(", source, name)

    def test_grid_guides_toggle_a_prebuilt_layer_without_reloading_the_viewer(self):
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        handler = self.viewer[self.viewer.index('toolbar.querySelector(".page-guides-toggle")'):
                              self.viewer.index('toolbar.querySelector(".page-snap-toggle")')]
        self.assertIn("state.showGuides = event.target.checked", handler)
        self.assertIn("configureGrid(canvas)", handler)
        self.assertNotIn("onViewChange", handler)
        grid = re.search(r"\.page-grid \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("opacity: 0", grid)
        self.assertIn("visibility: hidden", grid)
        self.assertIn("contain: paint", grid)
        self.assertIn("will-change: opacity", grid)
        self.assertNotIn("display: none", grid)
        self.assertIn(".page-grid.is-visible { visibility: visible; opacity: 1; }", stylesheet)

    def test_grid_uses_real_pipeline_scale_and_does_not_wait_for_an_image(self):
        configure = self.viewer[self.viewer.index("export function configureGrid"):
                                self.viewer.index("export function viewerToolbarHtml")]
        self.assertIn("const GRID_MM = 6", self.viewer)
        self.assertIn("const GUIDE_MM = 30", self.viewer)
        self.assertIn("GRID_MM / mmPerPx", configure)
        self.assertIn("GUIDE_MM / mmPerPx", configure)
        self.assertIn('sheet.dataset.mmPerPx', configure)
        self.assertIn('grid.classList.toggle("is-visible", gridAvailable && state.showGuides)',
                      configure)
        guard = self.viewer[self.viewer.index("export function bindViewer"):
                            self.viewer.index("const range")]
        self.assertNotIn("!image", guard)
        self.assertIn('state.data.pageLayout?.render_px_per_mm', self.visual)
        self.assertIn('data-grid-enabled="true"', self.visual)
        self.assertIn('data-mm-per-px=', self.visual)

    def test_grid_control_is_enabled_only_for_steps_seven_through_nine(self):
        toolbar = self.viewer[self.viewer.index("export function viewerToolbarHtml"):
                              self.viewer.index("export function snapToGrid")]
        self.assertIn("view.pageLayout || view.pageRender || view.legendPage", toolbar)
        self.assertIn('gridAvailable && state.showGuides', toolbar)
        self.assertIn('gridAvailable ? "" : "disabled"', toolbar)
        configure = self.viewer[self.viewer.index("export function configureGrid"):
                                self.viewer.index("export function viewerToolbarHtml")]
        self.assertIn('sheet.dataset.gridEnabled === "true"', configure)
        self.assertIn("gridAvailable && state.showGuides", configure)

    def test_snap_control_is_enabled_only_for_steps_seven_through_nine(self):
        toolbar = self.viewer[self.viewer.index("export function viewerToolbarHtml"):
                              self.viewer.index("export function snapToGrid")]
        self.assertIn('gridAvailable && state.snapToGrid ? "checked" : ""', toolbar)
        self.assertIn('Snap to grid is available in Steps 7, 8 and 9', toolbar)
        self.assertIn('<span aria-hidden="true"></span> Snap to grid</label>', toolbar)
        handler = self.viewer[self.viewer.index('toolbar.querySelector(".page-snap-toggle")'):
                              self.viewer.index('toolbar.querySelector("[data-colour-view]")')]
        self.assertIn("box.checked = !box.disabled && state.snapToGrid", handler)

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
        self.assertIn(".page-grid.is-visible", stylesheet)

    def test_zoom_changes_only_the_map_inside_a_fixed_viewport(self):
        stylesheet = (STATIC / "minimal.css").read_text(encoding="utf-8")
        self.assertIn('class="map-viewport-content"', self.visual)
        self.assertIn('class="map-zoom-space"', self.visual)
        viewport = re.search(r"\.map-viewport-content \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("width: max-content", viewport)
        self.assertIn("align-items: flex-start", viewport)
        self.assertIn("min-width: 100%", viewport)
        self.assertIn("min-height: 100%", viewport)
        desktop_frame = re.search(
            r"\.map-stage \.map-frame \{([^}]*)\}", stylesheet
        ).group(1)
        self.assertIn("height: 0", desktop_frame)
        self.assertIn("flex: 1 1 0", desktop_frame)
        self.assertIn("sheet.style.width", self.viewer)
        self.assertIn("sheet.style.height", self.viewer)
        self.assertIn("sheet.style.transform = `scale(${scale})`", self.viewer)
        self.assertIn("zoomSpace.style.width = `${naturalW * scale}px`", self.viewer)
        self.assertIn("zoomSpace.style.height = `${naturalH * scale}px`", self.viewer)
        zoom_space = re.search(r"\.map-zoom-space > \.map-canvas \{([^}]*)\}", stylesheet).group(1)
        self.assertIn("position: absolute", zoom_space)
        self.assertIn("transform-origin: top left", zoom_space)

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
        self.assertIn("view.hybrid || view.hybridOverlay ?", self.viewer)
        steps = (MINIMAL_DIR / "steps.js").read_text(encoding="utf-8")
        marker = "export const STEP_VIEWS = {"
        block = steps[steps.index(marker):]
        self.assertEqual(len(re.findall(r"hybrid:", block)), 2)  # raster swaps: Steps 7 and 8
        self.assertEqual(block.count("hybridOverlay: true"), 1)   # editable overlay: Step 9


class VersionSkewTests(unittest.TestCase):
    """Updating the checkout does not reload a running server.

    Every symptom of that skew used to surface as a refusal about the map --
    an unsupported mask action, a review gate that never opens, "the method is
    not allowed" -- so these pin the parts that name the real cause instead.
    """

    def setUp(self):
        self.client = server.app.test_client()

    def test_the_page_and_the_server_agree_on_one_contract_number(self):
        source = (MINIMAL_DIR / "api.js").read_text(encoding="utf-8")
        declared = re.search(r"export const UI_CONTRACT = (\d+);", source)
        self.assertIsNotNone(declared, "api.js must declare UI_CONTRACT")
        self.assertEqual(int(declared.group(1)), server.UI_CONTRACT)

    def test_the_map_list_states_the_contract_it_implements(self):
        response = self.client.get("/api/maps")
        try:
            payload = response.get_json()
            self.assertEqual(payload["contract"], server.UI_CONTRACT)
            self.assertIn("restart_required", payload)
        finally:
            response.close()

    def test_a_server_older_than_the_page_reports_no_contract(self):
        """Which is what makes an old server recognisable: the focused view
        reads a missing contract as 0 and refuses to blame the map."""
        source = (MINIMAL_DIR / "api.js").read_text(encoding="utf-8")
        self.assertIn("serverContract = Number(payload?.contract) || 0", source)
        self.assertIn("serverContract < UI_CONTRACT", source)
        self.assertIn("staleServerAdvice()", source)

    def test_the_notice_stays_up_instead_of_passing_like_a_toast(self):
        state = (MINIMAL_DIR / "state.js").read_text(encoding="utf-8")
        workspace = (MINIMAL_DIR / "workspace.js").read_text(encoding="utf-8")
        self.assertIn("export function serverNotice(message)", state)
        self.assertNotIn("setTimeout", state[state.index("export function serverNotice"):])
        self.assertEqual(workspace.count("serverNotice(staleServerAdvice())"), 2)

    def test_an_endpoint_this_build_lacks_is_a_named_404_not_a_405(self):
        """Static files are served from the root, so an unknown /api/ path also
        matches the static rule -- for GET only.  A POST to an endpoint the
        server predates must not come back as "method not allowed"."""
        for method in ("get", "post", "put", "patch", "delete"):
            response = getattr(self.client, method)("/api/no-such-endpoint/x")
            try:
                self.assertEqual(response.status_code, 404)
                self.assertIn("no /api/no-such-endpoint/x endpoint",
                              response.get_json()["error"])
            finally:
                response.close()

    def test_the_catch_all_shadows_no_real_endpoint(self):
        adapter = server.app.url_map.bind("127.0.0.1")
        for rule in server.app.url_map.iter_rules():
            if not rule.rule.startswith("/api/") or rule.endpoint == "api_unknown":
                continue
            sample = rule.rule
            for argument in rule.arguments:
                sample = sample.replace(f"<{argument}>", "x")
                sample = sample.replace(f"<int:{argument}>", "1")
                sample = sample.replace(f"<path:{argument}>", "x/y")
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                self.assertEqual(adapter.match(sample, method=method)[0], rule.endpoint,
                                 f"{method} {sample} no longer reaches {rule.endpoint}")

    def test_the_application_itself_is_never_served_from_cache(self):
        """Only the entry module carries a version query and an ES import
        inherits the importing URL without it, so a cached module would pair
        old JavaScript with a current server."""
        for path in ("/", "/minimal", "/minimal.css", "/minimal/main.js",
                     "/minimal/editors/mask.js"):
            response = self.client.get(path)
            try:
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
            finally:
                response.close()

    def test_the_running_server_notices_its_own_sources_moving(self):
        original = server._STARTED_FINGERPRINT
        server._stale_source_check = (0.0, False)
        try:
            self.assertFalse(server.restart_required())
            server._STARTED_FINGERPRINT = (("server.py", 0, 0),)
            server._stale_source_check = (0.0, False)
            self.assertTrue(server.restart_required())
        finally:
            server._STARTED_FINGERPRINT = original
            server._stale_source_check = (0.0, False)

    def test_a_transient_source_read_failure_does_not_latch_a_false_warning(self):
        original = server._STARTED_FINGERPRINT
        server._stale_source_check = (0.0, False)
        try:
            with patch.object(server, "_source_fingerprint", return_value=None):
                self.assertFalse(server.restart_required())
            server._stale_source_check = (0.0, True)
            with patch.object(server, "_source_fingerprint", return_value=original):
                self.assertFalse(server.restart_required())
        finally:
            server._stale_source_check = (0.0, False)


if __name__ == "__main__":
    unittest.main()
