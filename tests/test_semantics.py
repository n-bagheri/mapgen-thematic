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

    def test_incomplete_semantics_does_not_count_as_run(self):
        self.write({"map_language": "unknown"})
        self.assertFalse(semantics_artifact_is_current(self.artifact))

    def test_missing_legend_still_records_a_completed_step1_interpretation(self):
        self.write(_semantics(legend_present=False))
        self.assertTrue(semantics_artifact_is_current(self.artifact))

    def test_invalid_json_does_not_count_as_run(self):
        self.artifact.write_text("not json", encoding="utf-8")
        self.assertFalse(semantics_artifact_is_current(self.artifact))


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


if __name__ == "__main__":
    unittest.main()
