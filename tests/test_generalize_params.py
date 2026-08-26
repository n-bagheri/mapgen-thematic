import json
import tempfile
import unittest
from pathlib import Path

from mapgen.generalize import LINE_POLICY_VERSION, load_params


def _semantics(lines):
    return {
        "map_type": "isopleth",
        "in_scope": True,
        "data_ordering": "ordered",
        "map_language": "English",
        "subject": "Synthetic precipitation map",
        "description": "Synthetic precipitation map",
        "title": None,
        "legend_present": True,
        "legend_title": "Precipitation",
        "legend_entries": [{
            "label": "low", "color_hint": "yellow", "is_thematic": True,
            "kind": "area_fill",
        }],
        "water_present": False,
        "thematic_classes": [{
            "label": "low", "priority": 1, "approx_area_share_percent": 100,
        }],
        "non_thematic": [],
        "lines": [{"kind": kind, "description": kind} for kind in lines],
        "overlay_text": {
            "has_city_labels": False,
            "capital_city": None,
            "has_region_labels": False,
            "has_line_labels": False,
            "notes": "",
        },
    }


class GeneralizeLineDefaultsTests(unittest.TestCase):
    def test_visible_country_borders_are_kept_by_default(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "step1_semantics.json").write_text(
                json.dumps(_semantics(["border", "coastline"])), encoding="utf-8")

            params = load_params(out_dir)

            self.assertEqual(params["keep_line_kinds"], ["border_or_coast"])
            self.assertEqual(params["line_policy_version"], LINE_POLICY_VERSION)

    def test_old_empty_line_default_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "step1_semantics.json").write_text(
                json.dumps(_semantics(["border"])), encoding="utf-8")
            (out_dir / "step5_params.json").write_text(json.dumps({
                "keep_line_kinds": [],
                "simplification_level": 3,
            }), encoding="utf-8")

            params = load_params(out_dir)

            self.assertEqual(params["keep_line_kinds"], ["border"])

    def test_versioned_empty_selection_remains_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "step1_semantics.json").write_text(
                json.dumps(_semantics(["border"])), encoding="utf-8")
            (out_dir / "step5_params.json").write_text(json.dumps({
                "keep_line_kinds": [],
                "line_policy_version": LINE_POLICY_VERSION,
                "simplification_level": 3,
            }), encoding="utf-8")

            params = load_params(out_dir)

            self.assertEqual(params["keep_line_kinds"], [])


if __name__ == "__main__":
    unittest.main()
