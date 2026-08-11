import unittest

import numpy as np

from mapgen.alt_mapgen import (_group_area_audit, _source_to_final_transition,
                               build_group_definitions, group_raster)
from mapgen.generalize import generalize_area_raster


class AltMapGenOrderingTests(unittest.TestCase):
    def setUp(self):
        self.classes = [
            {"index": 0, "label": "Forest", "rgb": [20, 130, 60],
             "is_thematic": True, "area_px": 3},
            {"index": 1, "label": "Cropland", "rgb": [240, 155, 20],
             "is_thematic": True, "area_px": 2},
            {"index": 2, "label": "Other crops", "rgb": [240, 235, 160],
             "is_thematic": True, "area_px": 3},
        ]
        self.aggregation = {
            "water": None,
            "groups": [
                {"label": "Forest", "members": [0],
                 "member_labels": ["Forest"], "rationale": "identity"},
                {"label": "Field Crops", "members": [1, 2],
                 "member_labels": ["Cropland", "Other crops"],
                 "rationale": "reviewed crop aggregation"},
            ],
            "plain_thematic": [],
            "non_thematic_extra": [],
        }

    def test_aggregation_changes_identity_without_moving_geography(self):
        source = np.array([
            [0, 0, 1, 1],
            [0, 2, 2, 2],
        ], np.int16)
        groups = build_group_definitions(self.aggregation, self.classes)
        grouped, lookup = group_raster(source, groups)

        self.assertEqual(lookup, {0: 0, 1: 1, 2: 1})
        self.assertTrue(np.array_equal(grouped, np.array([
            [0, 0, 1, 1],
            [0, 1, 1, 1],
        ], np.int16)))
        self.assertEqual(np.count_nonzero(source >= 0), np.count_nonzero(grouped >= 0))

    def test_transition_report_starts_from_original_step4_categories(self):
        source = np.array([[0, 1, 2, 2]], np.int16)
        groups = build_group_definitions(self.aggregation, self.classes)
        before, lookup = group_raster(source, groups)
        final = before.copy()
        final[0, 1] = 0

        report = _source_to_final_transition(
            source, before, final, self.classes, groups, lookup)

        cropland = next(row for row in report["source_to_final_groups"]
                        if row["source_label"] == "Cropland")
        self.assertEqual(cropland["intended_group_label"], "Field Crops")
        self.assertEqual(cropland["geographically_reassigned_px"], 1)
        self.assertEqual(report["geographically_reassigned_pixels"], 1)

    def test_per_group_area_change_is_reported_without_a_guard(self):
        before = np.zeros((10, 10), np.int16)
        before[:, 8:] = 1
        after = before.copy()
        after[:, 6:8] = 1
        groups = build_group_definitions(self.aggregation, self.classes)

        audit = _group_area_audit(before, after, groups)

        field = next(row for row in audit["per_group"] if row["label"] == "Field Crops")
        self.assertEqual(field["gained_px"], 20)
        self.assertEqual(audit["largest_gain_or_loss_share"], 1.0)
        self.assertNotIn("budget_px", field)

    def test_aggregate_first_then_canonical_simplification_absorbs_small_group(self):
        source = np.full((20, 30), 2, np.int16)
        source[:, :12] = 0
        source[8:11, 16:19] = 1
        group_classes = [
            {"index": 0, "label": "A", "is_thematic": True,
             "area_share": 0.4},
            {"index": 1, "label": "Y", "is_thematic": True,
             "area_share": 0.015},
            {"index": 2, "label": "B", "is_thematic": True,
             "area_share": 0.585},
        ]

        simplified, _, operation = generalize_area_raster(
            source, group_classes, min_area_px=25, sigma=1.0,
            preserve_share=1.0)

        self.assertEqual(np.count_nonzero(simplified == 1), 0)
        self.assertGreaterEqual(operation["dissolved_components"], 1)


if __name__ == "__main__":
    unittest.main()
