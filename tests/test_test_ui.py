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
            self.assertIn(b'id="test-review-resizer"', response.data)
            self.assertIn(b'id="project-nav"', response.data)
            self.assertIn(b'id="visual-pane"', response.data)
            self.assertIn(b'id="control-pane"', response.data)
            self.assertIn(b'src="/test/main.js', response.data)
            self.assertNotIn(b'src="/app.js', response.data)
        finally:
            response.close()

    def test_test_assets_are_served_without_changing_minimal_entry(self):
        for route in ("/test.css", "/test/main.js", "/test/map-library/france_env.jpg"):
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
            "Taking a closer look with a vision-language model",
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
        self.assertIn("Display colors", source)
        self.assertIn("Display original map", source)
        self.assertIn("view.originalCompare = true", source)

    def test_test_pipeline_hides_extra_step_and_export_controls(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        page = (STATIC / "test.html").read_text(encoding="utf-8")
        self.assertIn(".visual-pane .stage-heading", stylesheet)
        self.assertIn("#control-content .editor-number", stylesheet)
        self.assertIn(".review-gate > .section-kicker", stylesheet)
        self.assertIn(".visual-pane .layer-toolbar", stylesheet)
        self.assertIn("#control-content .export-input-toggle", stylesheet)
        self.assertNotIn("state.showOriginalMap = false", source)
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

    def test_review_progress_states_and_resizable_dock_are_test_only(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn('step.key === failedStep ? "failed"', source)
        self.assertIn("function bindReviewResizer", source)
        self.assertIn("--test-review-height", source)
        self.assertIn(".test-progress-step.waiting-review", stylesheet)
        self.assertIn(".test-progress-step.failed", stylesheet)
        self.assertIn("@keyframes test-progress-glow", stylesheet)
        self.assertIn("@keyframes test-review-glow", stylesheet)

    def test_step2_mask_review_copy_layout_and_full_discard(self):
        source = (STATIC / "test" / "main.js").read_text(encoding="utf-8")
        self.assertIn(
            "Use the brush to restore map areas that were mistakenly masked, and the eraser to remove unwanted areas.",
            source,
        )
        self.assertIn("function enhanceTestReviewDock", source)
        self.assertIn("function discardTestMaskEdits", source)
        self.assertIn("await resetMask(state.selected)", source)
        self.assertIn("state.data.mask?.reviewed", source)
        stylesheet = (STATIC / "test.css").read_text(encoding="utf-8")
        self.assertIn("#fff4dc", stylesheet)
        self.assertIn("> .editor-body > .action-row.end", stylesheet)
        self.assertIn("label:has(input:checked)", stylesheet)

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
