"""Contract tests for the project-opening flow in front of /minimal at /test."""

import os
import pathlib
import unittest
from unittest.mock import patch

from webui import server


STATIC = pathlib.Path(server.app.static_folder)


class TestUiRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_test_route_has_the_api_gate_home_launcher_and_focused_workspace(self):
        response = self.client.get("/test")
        try:
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'id="test-home-screen"', response.data)
            self.assertIn(b'id="test-home-title"', response.data)
            self.assertIn(b'<div class="test-brand">', response.data)
            self.assertNotIn(b'<a class="test-brand"', response.data)
            self.assertIn(b'id="test-about-link"', response.data)
            self.assertIn(b'id="test-about-screen" hidden', response.data)
            self.assertIn(b'id="test-about-home"', response.data)
            self.assertIn(b'id="test-about-title"', response.data)
            self.assertIn(b'id="test-template-strip"', response.data)
            self.assertIn(b'id="test-add-map"', response.data)
            self.assertIn(b'id="test-template-next"', response.data)
            self.assertIn(b'id="test-key-dialog"', response.data)
            self.assertIn(b'Enter your Gemini API key to continue.', response.data)
            self.assertIn(b'id="test-key-input" type="password"', response.data)
            self.assertIn(b'id="test-recent-title"', response.data)
            self.assertIn(b'Date modified', response.data)
            self.assertIn(b'id="test-project-grid"', response.data)
            self.assertIn(b'id="test-pipeline-header"', response.data)
            self.assertIn(b'id="test-pipeline-progress"', response.data)
            self.assertIn(b'id="test-source-pane"', response.data)
            self.assertIn(b'id="test-source-toggle"', response.data)
            self.assertIn(b'id="test-pipeline-start"', response.data)
            self.assertIn(b'id="test-thinking"', response.data)
            self.assertIn(b'id="test-pipeline-title-input"', response.data)
            self.assertNotIn(b'id="test-review-panel-title"', response.data)
            self.assertNotIn(b'id="test-pipeline-back"', response.data)
            self.assertNotIn(b'id="test-pipeline-rename"', response.data)
            self.assertIn(b'id="project-nav"', response.data)
            self.assertIn(b'id="visual-pane"', response.data)
            self.assertIn(b'id="control-pane"', response.data)
            self.assertIn(b'src="/test/main.js', response.data)
            self.assertNotIn(b'src="/app.js', response.data)
        finally:
            response.close()

    def test_test_assets_are_served_without_changing_minimal_entry(self):
        for route in ("/test.css", "/test/main.js", "/test/mask.js",
                      "/test/mask-surface.js", "/test/map-library/france_env.jpg"):
            response = self.client.get(route)
            try:
                self.assertEqual(response.status_code, 200, route)
            finally:
                response.close()

        minimal = self.client.get("/minimal")
        try:
            self.assertIn(b'src="/minimal/main.js', minimal.data)
            self.assertNotIn(b'id="test-entry"', minimal.data)
        finally:
            minimal.close()

    def test_project_flow_hands_opened_projects_to_the_minimal_application(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn('from "../minimal/workspace.js"', source)
        self.assertIn('FLOW_SCREENS = ["test-home-screen", "test-about-screen"]', source)
        self.assertIn('$("test-about-link").addEventListener("click", showAbout)', source)
        self.assertIn('$("test-about-home").addEventListener("click", showHome)', source)
        self.assertIn("await enterPipeline(uploaded.stem)", source)
        self.assertIn("await selectMap(stem)", source)
        self.assertIn('classList.remove("test-flow-open")', source)
        self.assertNotIn('from "../app.js"', source)

    def test_project_gallery_reuses_existing_project_endpoints(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("deleteMap(deleteStem)", source)
        self.assertIn("renameMap(renameStem, name)", source)
        self.assertIn("uploadFile(form)", source)
        self.assertIn("mapUrl(map.name)", source)

    def test_session_key_is_saved_before_the_home_launcher_is_revealed(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        save = source.index("await storeSessionKey(key)")
        home = source.index("showHome()", save)
        recent = source.index("await refreshHomeProjects()", save)
        self.assertLess(save, home)
        self.assertLess(home, recent)
        init = source.index("async function init()")
        self.assertLess(source.index("askForApiKey()", init), source.index("await Promise.all", init))
        self.assertNotIn("localStorage", source)

    def test_pipeline_shell_reuses_the_minimal_visual_and_automatic_run(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn('id="visual-content"', page)
        self.assertIn('id="control-content"', page)
        self.assertIn('$("run-all") || $("continue-run")', source)
        for message in (
            "AI is interpreting the map",
            "Finding the map and separating it from the rest of the image",
            "Reading labels, place names, titles, and other text",
            "Breaking the map into meaningful regions",
            "Bringing similar map classes together",
            "Cleaning up fine detail while keeping the map recognizable",
            "Giving each class a distinct tactile texture",
            "Placing Braille labels",
            "Building the legend for the final map",
        ):
            self.assertIn(message, source)

    def test_pipeline_uses_one_main_visual_panel_and_a_review_dock(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn(".test-source-pane { display: none !important; }", stylesheet)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", stylesheet)
        self.assertIn("test-show-controls:not(.test-job-running) .control-pane", stylesheet)
        self.assertIn("function enhanceTestViewers", source)
        self.assertIn("Gridlines", source)
        self.assertIn("Display hybrid map", source)
        self.assertIn("Display original map", source)
        self.assertIn("view.originalCompare = Number(step) >= 2", source)
        self.assertIn("test-toolbar-navigation", source)
        self.assertIn("test-toolbar-display", source)

    def test_review_panel_is_revealed_and_mounted_as_the_drawer_surface(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        ensure = source.index("function ensureTestReviewControls")
        unhide = source.index("content.hidden = false", ensure)
        lookup = source.index("const current = content.querySelector", ensure)
        self.assertLess(unhide, lookup)
        self.assertIn("content.replaceChildren(root)", source)
        self.assertIn('root.dataset.testReviewPanel = testReviewPanelKey(definition)', source)
        self.assertIn("height: 100%;", stylesheet[stylesheet.index(".test-review-shell {"):])

    def test_review_panels_use_the_requested_compact_copy_and_actions(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        for copy in (
            "This map has more than five legend categories, so some need to be grouped into broader categories, with a maximum of five in total.",
            "Use the slider to choose how much the map should be simplified.",
            "Review the assigned tactile patterns and adjust them if needed.",
            "Review the Braille labels and add, edit, or remove them as needed.",
            "Review the tactile legend, its labels, title, and page layout, and adjust them if needed.",
        ):
            self.assertIn(copy, source)
        self.assertIn('finish.textContent = "Apply legend box and continue"', source)
        self.assertIn('finish.textContent = "Approve mask and continue"', source)
        self.assertIn('undo.textContent = "Undo stroke"', source)
        self.assertIn("buildTestMaskFooter(root, body, footer)", source)
        self.assertIn("buildTestLegendCompare(root)", source)
        self.assertIn("relocateTestPatternDialog", source)
        self.assertIn("disableTestExportPanelPreview", source)

    def test_pipeline_title_is_renamed_directly_in_place(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn('id="test-pipeline-title-input"', page)
        self.assertIn("function beginPipelineTitleEdit", source)
        self.assertIn("function commitPipelineTitleEdit", source)
        self.assertIn("await renameMap(map.stem, name)", source)

    def test_transient_creation_and_start_messages_are_cleared(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("function clearPipelineStartStatus", source)
        self.assertIn("function settlePipelineStartRequest", source)
        self.assertIn("pipelineStartPending && Boolean(state.busy || state.continuing)", source)
        self.assertGreaterEqual(source.count('$("test-choice-status").textContent = "";'), 2)
        self.assertIn('$("test-project-status").textContent = "";', source)

    def test_test_toolbar_retains_the_shared_viewer_readout(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('const readout = toolbar.querySelector(".page-zoom-readout")', source)
        self.assertIn("readout.hidden = true", source)
        self.assertNotIn('toolbar.querySelector(".page-zoom-readout")?.remove()', source)
        self.assertIn("function bindTestViewerStabilizer", source)
        self.assertIn("new MutationObserver(scheduleTestViewerStabilization)", source)
        self.assertIn("queueMicrotask", source)
        self.assertIn("> [data-original-view]", stylesheet)
        self.assertIn("> [data-final-map-view]", stylesheet)

    def test_test_pipeline_hides_extra_step_and_export_controls(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn(".visual-pane .stage-heading", stylesheet)
        self.assertIn("#control-content .editor-number", stylesheet)
        self.assertIn(".review-gate > .section-kicker", stylesheet)
        self.assertIn(".visual-pane .layer-toolbar", stylesheet)
        self.assertIn("#control-content .export-input-toggle", stylesheet)
        self.assertNotIn('id="test-thinking-step"', page)

    def test_progress_and_review_copy_match_the_requested_interface(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        for label in (
            "Read image", "Locate map", "Read text", "Segment map",
            "Aggregate classes", "Simplify", "Assign textures",
            "Add Braille labels", "Generate legend",
        ):
            self.assertIn(label, source)
        for message in (
            "Map found! Adjust the selection if needed.",
            "Take a look at the classes and adjust anything that doesn’t look right.",
            "Adjust the simplification level however you like.",
            "Change the textures, swap them around, or add color if you’d like.",
            "Move the labels around, edit them, or remove any you don’t need.",
            "Give everything one final check.",
        ):
            self.assertIn(message, source)

    def test_review_progress_states_and_right_review_panel_are_test_only(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('step.key === failedStep ? "failed"', source)
        self.assertIn("TEST_REVIEW_PANELS", source)
        self.assertIn("clamp(340px, 24vw, 400px)", stylesheet)
        self.assertIn(".test-review-shell-body", stylesheet)
        self.assertIn("overflow-y: auto", stylesheet)
        self.assertIn(".test-review-shell-footer", stylesheet)
        self.assertIn("position: relative", stylesheet)
        self.assertIn("flex: 1 1 0", stylesheet)
        self.assertIn("test-review-slide-in", stylesheet)
        self.assertIn(".test-progress-step.waiting-review", stylesheet)
        self.assertIn(".test-progress-step.failed", stylesheet)
        self.assertIn("@keyframes test-progress-glow", stylesheet)

    def test_step2_mask_review_copy_layout_and_full_discard(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn(
            "Use the brush to restore map areas that were mistakenly masked, and the eraser to remove unwanted areas.",
            source,
        )
        self.assertIn("function enhanceTestReviewPanel", source)
        self.assertIn("function discardTestMaskEdits", source)
        self.assertIn("await resetMaskSurface(() => resetMask(stem))", source)
        self.assertIn("state.data.mask?.reviewed", source)
        self.assertIn('reset.textContent = "Reset to detected result"', source)
        self.assertIn("function finishTestMaskReview", source)
        self.assertIn("await saveMaskStrokes(stem, state.maskBrush.strokes)", source)
        self.assertIn("await approveMask(stem)", source)
        self.assertIn("await continuePipeline()", source)
        self.assertNotIn('$("approve-mask").click()', source)
        self.assertIn('finish.id = "test-mask-continue"', source)
        self.assertIn('finish.textContent = "Approve mask and continue"', source)
        self.assertIn(".test-review-native-actions[hidden]", stylesheet)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", stylesheet)
        self.assertIn("background: #fff", stylesheet)
        self.assertIn("> .editor-body > .action-row.end", stylesheet)
        self.assertIn("label:has(input:checked)", stylesheet)

    def test_pipeline_refinement_keeps_one_review_shell_and_compact_viewport_layout(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn("--test-status-height: 0px", stylesheet)
        self.assertIn("--test-status-height: 38px", stylesheet)
        self.assertIn("grid-template-rows: 52px 58px var(--test-status-height)", stylesheet)
        self.assertIn("height: calc(100dvh - var(--test-top-height))", stylesheet)
        self.assertIn('root.classList.add("test-review-shell")', source)
        self.assertNotIn('id="test-review-panel-title"', page)
        self.assertNotIn("Review map location", source)
        self.assertNotIn("ONE DECISION NEEDED", source.upper())
        self.assertIn('root.querySelectorAll(".advanced-box").forEach((node) => node.remove())', source)

    def test_original_toggle_compares_source_with_masked_stage_output_without_visible_labels(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('originalSheet.setAttribute("aria-label", "Original map")', source)
        self.assertIn('outputSheet.setAttribute("aria-label", "Current output")', source)
        self.assertIn('outputSheet.id = "mask-target"', source)
        self.assertIn('outputSheet.dataset.testCurrentOutput = "detected-mask"', source)
        self.assertIn('!outputSheet.querySelector(".mask-canvas")', source)
        self.assertIn("if (needsMaskBinding) maskComparison = outputSheet", source)
        self.assertIn('if (maskComparison) bindMaskCanvas()', source)
        self.assertIn('if (legendComparison) bindLegendReviewOverlay()', source)
        self.assertIn('!outputSheet.querySelector(".legend-review-box")', source)
        self.assertNotIn('content: "Original"', stylesheet)
        self.assertNotIn('content: "Current output"', stylesheet)

    def test_step_two_mask_tools_are_vertical_and_keep_full_size_icons(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: minmax(0, 1fr)", stylesheet)
        self.assertIn("grid-template-columns: 32px minmax(0, 1fr)", stylesheet)
        self.assertIn("--test-mask-action-width: 100%", stylesheet)
        self.assertIn("width: 30px", stylesheet)
        self.assertIn("height: 30px", stylesheet)
        self.assertIn("overflow: visible", stylesheet)
        self.assertIn("#test-mask-continue", stylesheet)
        self.assertIn("align-items: stretch", stylesheet)
        self.assertIn(".test-review-shell.mask-decision .test-review-shell-footer", stylesheet)
        self.assertIn("body.appendChild(secondary)", source)
        self.assertIn("footer.appendChild(finish)", source)

    def test_step_two_requires_an_explicit_mask_tool_choice(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function syncTestMaskToolSelection", source)
        self.assertIn('state.maskBrush.mode = ""', source)
        self.assertIn("radio.checked = radio.value === testMaskBrushMode", source)
        self.assertIn("function selectTestMaskTool", source)
        self.assertIn("state.maskBrush.mode = radio.value", source)
        self.assertIn("test-mask-tool-unselected", source)
        self.assertIn(".test-mask-tool-unselected #visual-content .mask-canvas", stylesheet)
        self.assertIn("pointer-events: none", stylesheet)

    def test_step_one_uses_the_requested_thinking_message(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn('"1": "AI is interpreting the map…"', source)

    def test_step_five_copy_and_reset_label_are_refined_on_test_only(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("definition === TEST_REVIEW_PANELS.aggregation", source)
        self.assertIn("The limit is a maximum, not a target", source)
        self.assertIn('reset.textContent = "Reset to suggested"', source)

    def test_running_preview_keeps_full_size_with_zoom_controls(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        copy = source.index("function copyStablePreview")
        normalize = source.index("enhanceTestViewers();", copy)
        park = source.index("stable.replaceChildren(...Array.from(visual.childNodes))", copy)
        self.assertLess(normalize, park)
        self.assertNotIn("cloneNode(true)", source[copy:source.index("function freezeCurrentPreview", copy)])
        self.assertIn(".test-toolbar-navigation", stylesheet)
        self.assertIn("flex-wrap: nowrap", stylesheet)

    def test_running_pipeline_presents_each_last_valid_stage(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function stablePreviewSelection", source)
        self.assertIn('key: extractedStep2 ? "2-extracted-map-and-legend" : step', source)
        self.assertIn("function renderExtractedStep2Preview", source)
        self.assertIn('artifactUrl(map.stem, "map_text_input.png")', source)
        self.assertIn('artifactUrl(map.stem, "legend.png")', source)
        self.assertIn("if (desired.extractedStep2) renderExtractedStep2Preview(map)", source)
        self.assertIn("viewerToolbarHtml({}, EXTRACTED_PREVIEW_VIEW)", source)
        self.assertIn("function bindExtractedStep2Viewer", source)
        self.assertIn("bindViewer(EXTRACTED_PREVIEW_VIEW, async () => true)", source)
        self.assertIn(".test-extracted-preview-grid", stylesheet)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", stylesheet)
        self.assertIn("width: 100%", stylesheet)
        self.assertIn("height: 100%", stylesheet)

    def test_step_two_mask_actions_update_without_remounting_the_review_panel(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function undoTestMaskStroke", source)
        self.assertIn("refreshTestMaskSurface()", source)
        discard = source[source.index("async function discardTestMaskEdits"):
                         source.index("function toggleSourcePane")]
        self.assertNotIn("renderWorkspace", discard)
        self.assertIn(".mask-decision-status", stylesheet)
        self.assertIn("#mask-save-status", stylesheet)
        self.assertIn("display: none !important", stylesheet)

    def test_test_view_hides_detected_lines_and_always_reviews_step_five(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("segmentedLines: false", source)
        self.assertIn("#segmented-lines-overlay", stylesheet)
        self.assertIn("!state.data.aggregation?.review?.approved", source)
        self.assertIn("function aggregationReviewIntro", source)
        self.assertIn("This map already has five tactile categories, the maximum allowed.", source)
        self.assertIn("This map has more than five tactile categories", source)
        self.assertIn("function emphasizeAggregationInstructions", source)
        self.assertIn('const backendReady = !live && Boolean(map.steps?.["5"])', source)

    def test_step_seven_uses_suggested_copy_and_hides_pattern_descriptions(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('suggested.textContent = "Use suggested patterns"', source)
        self.assertIn("function removeTestPatternDescriptions", source)
        self.assertIn(".pattern-library-choice span", stylesheet)
        self.assertIn(".page-grid-toggle:has(input:disabled)", stylesheet)
        self.assertIn("freezeCurrentPreview(true)", source)
        self.assertIn("previewPresentation.locked", source)
        self.assertIn("function toggleTestSuggestedPatterns", source)
        self.assertIn("function chooseTestOptimizedPattern", source)
        self.assertIn("assignPattern(stem, groupId, pattern, true)", source)
        self.assertIn("enableTestSuggestedPatternChanges(root)", source)
        self.assertIn("function queueTestPatternTransform", source)
        self.assertIn("window.setTimeout(flushTestPatternTransform, 450)", source)

    def test_step_five_review_redraws_are_normalized_before_they_can_flicker(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function bindTestReviewStabilizer", source)
        self.assertIn("new MutationObserver(scheduleTestReviewStabilization)", source)
        self.assertIn("queueMicrotask", source)
        self.assertIn("enhanceTestReviewPanel(reviewStep, exportReady, failed, blocked)", source)
        self.assertIn("function rememberTestReviewScroll", source)
        self.assertIn("function restoreTestReviewScroll", source)
        self.assertIn("restoreTestReviewScroll(root)", source)
        self.assertIn(
            ".test-review-active #control-content > :not(.test-review-shell)",
            stylesheet,
        )

    def test_hybrid_choice_controls_colour_defaults_and_english_text(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function applyTestHybridColourPreference", source)
        self.assertIn("state.colourView = false", source)
        self.assertIn("state.colourView = true", source)
        self.assertIn("previousHybridState !== true", source)
        self.assertIn("input.disabled = !enabled", source)
        self.assertIn('classList.toggle("test-hybrid-enabled", enabled)', source)
        self.assertIn("Display hybrid map", source)
        self.assertIn("function toggleTestHybridMode", source)
        self.assertIn("function setTestHybridView", source)
        self.assertIn("await preloadPreviews", source)
        self.assertIn("not(.test-hybrid-view-visible)", stylesheet)
        self.assertIn(".english-rendered-text", stylesheet)
        self.assertIn('root.querySelector("#pattern-review-status")?.remove()', source)
        self.assertIn("#pattern-review-status", stylesheet)

    def test_hybrid_toggle_keeps_the_tactile_preview_until_colour_is_ready(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("let hybridVisualTransition = null", source)
        self.assertIn("function activeHybridVisualTransition", source)
        self.assertIn(
            "transition ? transition.previousEnabled : hybridMapEnabled()",
            source,
        )
        self.assertIn("async function prepareTestHybridVisual", source)
        self.assertIn("urls.some((url) => !url)", source)
        self.assertIn("decoded.every(Boolean)", source)
        self.assertIn("await prepareTestHybridVisual", source)
        self.assertIn("refreshTestHybridVisual(enabled, true)", source)
        self.assertIn(
            "Never rebuild the pane as part of a colour-only change",
            source,
        )

    def test_braille_all_text_is_a_test_toggle_and_furniture_help_is_removed(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("function toggleTestBrailleText", source)
        self.assertIn("saveBrailleLayout(state.selected, { all_text_enabled: enabled })", source)
        self.assertIn("Display all text", source)
        self.assertIn('.braille-furniture-actions button[title*="Map scale"]', source)
        self.assertIn('.braille-furniture-box .field-note', source)
        self.assertIn('root.querySelectorAll(".braille-row-detail")', source)
        self.assertIn("detail.prepend(preview)", source)
        self.assertIn(".braille-callout-options label", stylesheet)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr))", stylesheet)
        self.assertIn("grid-column: auto", stylesheet)

    def test_legend_review_hides_pattern_ids_and_refits_after_orientation_changes(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('root.querySelectorAll(".pattern-chip")', source)
        self.assertIn("#legend-orientation", source)
        self.assertIn("state.data.legend?.page?.orientation === expected", source)
        self.assertIn("Test UI legend orientation refresh will retry", source)
        self.assertIn(".legend-decision .pattern-chip", stylesheet)

    def test_final_stage_defaults_to_tactile_and_switches_to_original_only(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("const FINAL_ORIGINAL_VIEW = 98", source)
        self.assertIn("function renderTestFinalOriginalVisual", source)
        self.assertIn("state.showFinalMap = !state.showOriginalMap", source)
        self.assertIn("state.showOriginalMap = false", source)
        self.assertIn("state.showFinalMap = true", source)
        self.assertIn("finalStageDefaultsApplied", source)

    def test_export_panel_explains_completed_step_navigation(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("You can now click on each step to see the details.", source)

    def test_review_panel_has_a_horizontal_resizer(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn('id="test-review-resizer"', page)
        self.assertIn('role="separator"', page)
        self.assertIn('aria-orientation="vertical"', page)
        self.assertIn("function bindReviewResizer", source)
        self.assertIn("reviewResize.startWidth + reviewResize.startX - event.clientX", source)
        self.assertIn("grid-template-columns: minmax(0, 1fr) 10px var(--test-review-width)", stylesheet)
        self.assertIn("cursor: col-resize", stylesheet)

    def test_status_and_review_continuation_are_presented_in_the_right_places(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        thinking = page.index('id="test-thinking"')
        status = page.index('id="test-start-status"')
        self.assertLess(thinking, status)
        self.assertIn('$("test-pipeline-start").textContent = "Start"', source)
        self.assertNotIn('$("test-pipeline-start").textContent = completed ? "Continue"', source)
        self.assertIn('$("test-start-panel").hidden = !map', source)
        self.assertIn('classList.toggle("test-status-visible", !thinking.hidden)', source)

    def test_export_is_normalized_into_the_shared_right_panel(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('title: "Export tactile map"', source)
        self.assertIn('ready.textContent = "Your tactile map is ready."', source)
        self.assertIn("#download-relief", stylesheet)
        self.assertIn("#download-hybrid", stylesheet)

    def test_test_pipeline_keeps_a_valid_preview_and_presents_every_stage(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn("function syncStablePreview", source)
        self.assertIn("function copyStablePreview", source)
        self.assertIn('classList.add("test-preview-frozen")', source)
        self.assertIn('id="test-stable-preview" inert hidden', page)
        self.assertIn("renderVisual()", source)
        self.assertIn("function presentedPipelineStage", source)
        self.assertIn("stagePresentation.step += 1", source)
        self.assertIn("const STAGE_DWELL_MS = 2500", source)
        self.assertIn("const ANALYSIS_SEQUENCE = [3, 4, 5]", source)
        self.assertIn("function beginAnalysisPresentation", source)
        self.assertIn("if (analysisAction) beginAnalysisPresentation()", source)
        self.assertIn("const currentForcedStepComplete", source)
        self.assertIn("currentForcedStepComplete && now >=", source)
        self.assertIn('Boolean(map.steps?.["5"])', source)
        self.assertIn("function ensureTestReviewControls", source)
        self.assertIn("renderControls()", source)
        self.assertIn("const awaitingBrailleRun", source)
        self.assertIn('Number(previewPresentation.step) === 7', source)
        cleanup = source.index('classList.remove("test-preview-frozen")', source.index("function syncStablePreview"))
        render = source.index("renderTestVisual()", cleanup)
        self.assertLess(cleanup, render)
        self.assertIn("Test UI preview transition will retry", source)

    def test_every_completed_job_reconciles_a_stale_final_map_snapshot(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn("function reconcileFinishedJobSnapshot", source)
        self.assertIn("const snapshotMissing", source)
        self.assertIn("const presentationBehind", source)
        self.assertIn("const authoritativeJob = await getJob(stem)", source)
        self.assertIn("state.job = authoritativeJob", source)
        self.assertLess(source.index("await getJob(stem)"), source.index("await loadMaps()", source.index("await getJob(stem)")))
        self.assertIn('if (state.job.status === "running") return', source)
        self.assertIn("await loadMaps()", source)
        self.assertIn("await refreshSelectedData()", source)
        self.assertIn("reconcileFinishedJobSnapshot(map, job)", source)
        self.assertNotIn("Could not refresh the pipeline status", source)

    def test_exports_replace_review_controls_only_when_ready(self):
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn(".export-panel:not(.is-ready)", stylesheet)
        self.assertIn("test-export-ready #control-content .step-stack", stylesheet)

    def test_session_key_is_kept_in_server_memory_and_not_echoed(self):
        secret = "test-session-key-not-a-real-key"
        with patch.dict(os.environ, {"GEMINI_API_KEY": "previous-key"}):
            response = self.client.post("/api/session-key", json={"key": secret})
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(os.environ["GEMINI_API_KEY"], secret)
                self.assertNotIn(secret.encode(), response.data)
                self.assertEqual(response.get_json(), {"ok": True, "configured": True})
            finally:
                response.close()

    def test_blank_session_key_is_rejected(self):
        response = self.client.post("/api/session-key", json={"key": "   "})
        try:
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], "Please enter an API key.")
        finally:
            response.close()

    def test_delete_confirmation_uses_the_requested_warning(self):
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn(
            "Are you sure you want to delete this project? All changes will be lost.",
            page,
        )

    def test_new_maps_are_a_horizontal_scrollable_library(self):
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn(".test-template-strip", stylesheet)
        self.assertIn("overflow-x: auto", stylesheet)
        self.assertIn("scrollBy", source)
        self.assertIn("TEMPLATE_MAPS", source)
        self.assertIn('data-template-file', source)

    def test_test_route_is_not_cached(self):
        response = self.client.get("/test")
        try:
            self.assertIn("no-store", response.headers.get("Cache-Control", ""))
        finally:
            response.close()


if __name__ == "__main__":
    unittest.main()
