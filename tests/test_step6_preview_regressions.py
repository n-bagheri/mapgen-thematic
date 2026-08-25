import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mapgen.postprocess import (STEP6_METHOD_VERSION, STEP6_PRESET_ARTIFACTS,
                                run_step6_presets,
                                step6_preset_artifact_name)


ROOT = Path(__file__).resolve().parents[1]
MINIMAL = ROOT / "webui" / "static" / "minimal"


class Step6PresetLatencyTests(unittest.TestCase):
    def test_selected_level_is_generated_before_background_variants(self):
        with TemporaryDirectory() as directory:
            runs_dir = Path(directory) / "runs"
            run_dir = runs_dir / "africa"
            run_dir.mkdir(parents=True)
            image = Path(directory) / "africa.png"
            selected = 4
            (run_dir / "step6_params.json").write_text(json.dumps({
                "method_version": STEP6_METHOD_VERSION,
                "simplification_level": selected,
            }), encoding="utf-8")
            calls = []

            def fake_run(_image, model=None, runs_dir=None, params_override=None):
                level = params_override["simplification_level"]
                calls.append(level)
                summary = {"params": {
                    "method_version": STEP6_METHOD_VERSION,
                    "simplification_level": level,
                }}
                payloads = {
                    "classes_gen.json": {"classes": []},
                    "regions_gen.geojson": {"features": []},
                    "lines_gen.geojson": {"features": []},
                    "step6_summary.json": summary,
                    "step6_transitions.json": {},
                }
                for name in STEP6_PRESET_ARTIFACTS:
                    path = run_dir / name
                    if name in payloads:
                        path.write_text(json.dumps(payloads[name]), encoding="utf-8")
                    else:
                        path.write_bytes(f"level-{level}".encode())
                return {"summary": summary}

            with patch("mapgen.postprocess.run_step6", side_effect=fake_run):
                result = run_step6_presets(image, runs_dir=runs_dir)

            self.assertEqual(calls, [selected, 1, 2, 3, 5])
            self.assertEqual(result["summary"]["params"]["simplification_level"], selected)
            self.assertTrue((run_dir / step6_preset_artifact_name(
                selected, "label_map_gen_preview.png")).exists())


class Step6PreviewUiTests(unittest.TestCase):
    def test_only_foreground_preview_blocks_workspace_render(self):
        source = (MINIMAL / "workspace.js").read_text(encoding="utf-8")
        self.assertIn("await preloadPreviews(stem, foreground)", source)
        self.assertIn("void preloadPreviews(stem, previews.filter", source)
        self.assertNotIn("await preloadPreviews(stem, previews);", source)

    def test_step5_map_stays_visible_while_step6_is_preparing(self):
        source = (MINIMAL / "visual.js").read_text(encoding="utf-8")
        preparing = source[source.index("function preparingStep6"):
                           source.index("export function activeStep")]
        self.assertIn('map.steps?.["5"]', preparing)
        self.assertIn('!map.steps?.["6"]', preparing)
        self.assertIn('state.job?.status === "running"', preparing)
        dynamic = source[source.index('if (view.dynamic === "simplified")'):
                         source.index("const hybridEnabled", source.index(
                             'if (view.dynamic === "simplified")'))]
        self.assertIn('"step5_aggregation_preview.png"', dynamic)
        self.assertIn("preparing: true", dynamic)
        self.assertRegex(source, re.compile(
            r"const ready = .*\|\| preparingStep6\(map, step\)"))
        self.assertIn("`step6_preset_${level}_label_map_gen_preview.png`", source)
        watcher = source[source.index("async function watchPreparingStep6Preview"):
                         source.index("function lineDrawingMarkup")]
        self.assertIn("await preloadPreviews(stem, [name])", watcher)
        self.assertLess(watcher.index("if (url)"), watcher.index("image.src = url"))
        self.assertLess(watcher.index("delete image.dataset.overlayDisabled"),
                        watcher.index("image.src = url"))
        self.assertIn("window.setTimeout(resolve, 750)", watcher)

    def test_grid_choice_survives_middle_panel_rerenders(self):
        state = (MINIMAL / "state.js").read_text(encoding="utf-8")
        viewer = (MINIMAL / "viewer.js").read_text(encoding="utf-8")
        stylesheet = (ROOT / "webui" / "static" / "minimal.css").read_text(
            encoding="utf-8")
        self.assertIn("showGuides: false", state)
        self.assertIn('${gridAvailable && state.showGuides ? "checked" : ""}', viewer)
        self.assertIn("state.showGuides = event.target.checked", viewer)
        self.assertIn(
            'grid.classList.toggle("is-visible", gridAvailable && state.showGuides)', viewer)
        self.assertIn(".page-grid.is-visible { visibility: visible; opacity: 1; }", stylesheet)


if __name__ == "__main__":
    unittest.main()
