import json
import tempfile
import unittest
from pathlib import Path

from mapgen.semantics import (
    MapSemantics,
    OutOfScopeMapError,
    postprocess,
    require_pipeline_eligible,
    semantics_artifact_is_current,
)


def _semantics(language: str = "unknown", map_type: str = "area_class_chorochromatic",
               legend_present: bool = True) -> dict:
    return {
        "map_type": map_type,
        "in_scope": True,
        "data_ordering": "qualitative",
        "map_language": language,
        "subject": "Climate regions of Iran",
        "description": "Iran is divided into qualitative climate regions.",
        "title": None,
        "legend_present": legend_present,
        "legend_title": "Climate regions" if legend_present else None,
        "legend_entries": ([{
            "label": "dry climate",
            "color_hint": "yellow",
            "is_thematic": True,
            "kind": "area_fill",
        }] if legend_present else []),
        "water_present": True,
        "thematic_classes": [{
            "label": "dry climate",
            "priority": 1,
            "approx_area_share_percent": 100.0,
        }],
        "non_thematic": [],
        "lines": [],
        "overlay_text": {
            "has_city_labels": False,
            "capital_city": None,
            "has_region_labels": False,
            "has_line_labels": False,
            "notes": "No readable text is present.",
        },
    }


class SemanticsArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.artifact = Path(self.temp_dir.name) / "step1_semantics.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def write(self, payload) -> None:
        self.artifact.write_text(json.dumps(payload), encoding="utf-8")

    def test_unknown_language_is_a_completed_semantic_interpretation(self):
        self.write(_semantics("unknown"))
        self.assertTrue(semantics_artifact_is_current(self.artifact))

    def test_named_language_is_a_completed_semantic_interpretation(self):
        self.write(_semantics("Persian"))
        self.assertTrue(semantics_artifact_is_current(self.artifact))

    def test_legacy_artifact_without_language_still_requires_rerun(self):
        payload = _semantics()
        del payload["map_language"]
        self.write(payload)
        self.assertFalse(semantics_artifact_is_current(self.artifact))

    def test_legacy_artifact_without_legend_entry_kind_requires_rerun(self):
        payload = _semantics()
        del payload["legend_entries"][0]["kind"]
        self.write(payload)
        self.assertFalse(semantics_artifact_is_current(self.artifact))

    def test_incomplete_semantics_does_not_count_as_run(self):
        self.write({"map_language": "unknown"})
        self.assertFalse(semantics_artifact_is_current(self.artifact))

    def test_missing_legend_still_records_a_completed_step1_interpretation(self):
        self.write(_semantics(legend_present=False))
        self.assertTrue(semantics_artifact_is_current(self.artifact))

    def test_invalid_json_does_not_count_as_run(self):
        self.artifact.write_text("not json", encoding="utf-8")
        self.assertFalse(semantics_artifact_is_current(self.artifact))


class LegendEncodingTests(unittest.TestCase):
    def test_all_fill_legend_entries_are_preserved_as_area_fills(self):
        payload = _semantics()
        payload["legend_entries"] = [{
            "label": "dry climate", "color_hint": "yellow",
            "is_thematic": True, "kind": "area_fill",
        }, {
            "label": "humid climate", "color_hint": "green",
            "is_thematic": True, "kind": "area_fill",
        }]

        semantics = MapSemantics.model_validate(payload)

        self.assertEqual([entry.kind.value for entry in semantics.legend_entries],
                         ["area_fill", "area_fill"])

    def test_mixed_legend_entries_keep_their_independent_encoding_kinds(self):
        payload = _semantics()
        payload["legend_entries"] = [{
            "label": "forest", "color_hint": "green",
            "is_thematic": True, "kind": "area_fill",
        }, {
            "label": "migration route", "color_hint": "black",
            "is_thematic": True, "kind": "line",
        }, {
            "label": "capital", "color_hint": "black",
            "is_thematic": True, "kind": "point_symbol",
        }, {
            "label": "map frame", "color_hint": "black",
            "is_thematic": False, "kind": "other",
        }]

        semantics = MapSemantics.model_validate(payload)

        self.assertEqual(
            [(entry.is_thematic, entry.kind.value) for entry in semantics.legend_entries],
            [(True, "area_fill"), (True, "line"), (True, "point_symbol"),
             (False, "other")],
        )


class ScopePolicyTests(unittest.TestCase):
    def semantics(self, map_type: str, legend_present: bool = True) -> MapSemantics:
        return MapSemantics.model_validate(
            _semantics(map_type=map_type, legend_present=legend_present))

    def test_chorochromatic_is_in_scope(self):
        self.assertTrue(postprocess(self.semantics("area_class_chorochromatic")).in_scope)

    def test_isopleth_is_in_scope(self):
        result = postprocess(self.semantics("isopleth"))
        self.assertTrue(result.in_scope)
        self.assertIs(require_pipeline_eligible(result, "Step 2"), result)

    def test_chorochromatic_is_eligible_for_downstream_steps(self):
        result = postprocess(self.semantics("area_class_chorochromatic"))
        self.assertIs(require_pipeline_eligible(result, "Step 2"), result)

    def test_legacy_classed_sequential_is_upgraded_to_eligible_isopleth(self):
        result = self.semantics("classed_sequential")
        self.assertEqual(result.map_type.value, "isopleth")
        self.assertTrue(result.in_scope)
        self.assertIs(require_pipeline_eligible(result, "Step 2"), result)

    def test_choropleth_is_out_of_scope(self):
        result = postprocess(self.semantics("choropleth"))
        self.assertFalse(result.in_scope)
        with self.assertRaisesRegex(OutOfScopeMapError, "choropleth.*out of scope"):
            require_pipeline_eligible(result, "Step 2")

    def test_other_is_out_of_scope(self):
        self.assertFalse(postprocess(self.semantics("other")).in_scope)

    def test_missing_legend_is_preserved_for_the_pipeline_gate(self):
        result = postprocess(
            self.semantics("area_class_chorochromatic", legend_present=False))
        self.assertTrue(result.in_scope)
        self.assertFalse(result.legend_present)

    def test_thematically_covered_ocean_is_not_kept_as_non_thematic_water(self):
        payload = _semantics(map_type="isopleth")
        payload["water_present"] = False
        payload["non_thematic"] = [{
            "name": "ocean/sea",
            "color_hint": "teal",
            "priority": 1,
            "reason": "Water covers much of the map.",
        }, {
            "name": "country boundaries",
            "color_hint": "black",
            "priority": 2,
            "reason": "Political context.",
        }]

        result = postprocess(MapSemantics.model_validate(payload))

        self.assertFalse(result.water_present)
        self.assertEqual([item.name for item in result.non_thematic],
                         ["country boundaries"])


class GenerateJsonSalvageTests(unittest.TestCase):
    """The Gemma text path must survive the malformations Gemma actually emits:
    trailing commas, a bare list instead of the single-field wrapper object,
    and a thought-only response with no text at all."""

    @staticmethod
    def _response(text):
        from types import SimpleNamespace
        finish = SimpleNamespace(finish_reason="MAX_TOKENS")
        return SimpleNamespace(parsed=None, text=text,
                               candidates=[finish] if text is None else [])

    def _run(self, responses, retries=1):
        from unittest.mock import MagicMock, patch
        from mapgen import semantics
        from mapgen.textdetect import TextDetections
        client = MagicMock()
        client.models.generate_content.side_effect = responses
        calls = client.models.generate_content
        with patch.object(semantics, "os") as fake_os:
            fake_os.environ.get.return_value = "gemma-test"
            with patch("google.genai.Client", return_value=client):
                result = semantics.generate_json(
                    ["prompt"], TextDetections, temperature=0.0, retries=retries)
        return result, calls

    def test_trailing_commas_are_repaired_locally(self):
        from mapgen.semantics import _strip_trailing_commas
        repaired = _strip_trailing_commas('{"a": [1, 2,], }')
        self.assertEqual(json.loads(repaired), {"a": [1, 2]})
        untouched = '{"a": "text, ] inside", "b": [1]}'
        self.assertEqual(_strip_trailing_commas(untouched), untouched)

    def test_bare_list_is_wrapped_into_the_single_field_schema(self):
        item = '{"text": "WOLOF", "kind": "region_label", "box_2d": [5, 6, 7, 8]}'
        result, calls = self._run([self._response(f"[{item}]")])
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].text, "WOLOF")
        self.assertEqual(calls.call_count, 1)

    def test_thought_only_response_retries_warm_then_succeeds(self):
        good = '{"items": [{"text": "A", "kind": "city", "box_2d": [1, 2, 3, 4]}]}'
        result, calls = self._run([self._response(None), self._response(good)])
        self.assertEqual(len(result.items), 1)
        self.assertEqual(calls.call_count, 2)
        second_config = calls.call_args_list[1].kwargs["config"]
        self.assertEqual(second_config.temperature, 0.6)

    def test_persistent_thought_only_response_raises_the_typed_error(self):
        from mapgen.semantics import EmptyModelResponse
        with self.assertRaises(EmptyModelResponse):
            self._run([self._response(None), self._response(None)], retries=1)

    def test_missing_required_sections_are_named_on_retry(self):
        from unittest.mock import MagicMock, patch
        from mapgen import semantics

        incomplete = _semantics()
        for field in ("non_thematic", "lines", "overlay_text"):
            incomplete.pop(field)
        complete = _semantics()
        client = MagicMock()
        client.models.generate_content.side_effect = [
            self._response(json.dumps(incomplete)),
            self._response(json.dumps(complete)),
        ]

        with patch.object(semantics, "_ensure_api_key"), \
                patch("google.genai.Client", return_value=client):
            result = semantics.generate_json(
                ["prompt"], MapSemantics, model="gemma-test", retries=1)

        self.assertEqual(result.subject, complete["subject"])
        retry_contents = client.models.generate_content.call_args_list[1].kwargs["contents"]
        reminder = retry_contents[-1]
        self.assertIn("non_thematic", reminder)
        self.assertIn("lines", reminder)
        self.assertIn("overlay_text", reminder)
        self.assertIn("every required schema field", reminder)

    def test_step1_prompt_explicitly_requires_trailing_semantic_sections(self):
        from mapgen.semantics import PROMPT

        self.assertIn("Always return non_thematic, lines, and overlay_text", PROMPT)
        self.assertIn("Never end the response after thematic_classes", PROMPT)

    def test_cap_truncated_list_is_closed_after_its_last_complete_element(self):
        item = '{"text": "KEPT", "kind": "city", "box_2d": [1, 2, 3, 4]}'
        cut = '{"items": [' + item + ', {"text": "LOST", "ki'
        result, calls = self._run([self._response(cut)])
        self.assertEqual([it.text for it in result.items], ["KEPT"])
        self.assertEqual(calls.call_count, 1)

    def test_a_complete_but_wrong_document_is_not_rescued_as_truncation(self):
        from mapgen.semantics import _close_truncated_json
        self.assertIsNone(_close_truncated_json('{"items": []}'))
        self.assertIsNone(_close_truncated_json('not json at all'))

    def test_brackets_inside_strings_do_not_confuse_the_closer(self):
        cut = ('{"items": [{"text": "a } ] b", "kind": "city",'
               ' "box_2d": [1, 2, 3, 4]}, {"text": "C')
        result, _ = self._run([self._response(cut)])
        self.assertEqual([it.text for it in result.items], ["a } ] b"])


if __name__ == "__main__":
    unittest.main()
