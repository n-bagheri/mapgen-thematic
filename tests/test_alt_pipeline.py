import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from mapgen.alt_aggregate import (aggregate_reviewed, effective_aggregation,
                                  propose_complete_aggregation,
                                  save_aggregation_review)
from mapgen.alt_generalize import (merge_small_components,
                                   SAFE_BOUNDARY_PRESETS,
                                   simplify_boundaries_safely, transition_audit)
from mapgen.alt_mapgen import run_alt_step6
from mapgen.alt_symbols import (generalize_group_geometry, run_alt_step7,
                                tactile_minimum_for_pattern)


def classes():
    return [
        {"index": 0, "label": "Grassland", "is_thematic": True,
         "source": "legend", "priority": 1, "area_px": 48, "area_share": 0.96},
        {"index": 1, "label": "Cropland", "is_thematic": True,
         "source": "legend", "priority": 2, "area_px": 1, "area_share": 0.02},
        {"index": 2, "label": "Other crops", "is_thematic": True,
         "source": "legend", "priority": 3, "area_px": 1, "area_share": 0.02},
    ]


def relationships(allow_1_to_0=False, aggregate_0_1=False):
    return {"reviewed": True, "pairs": [
        {"a_index": 0, "b_index": 1,
         "allow_a_to_b": False, "allow_b_to_a": allow_1_to_0,
         "aggregation_compatible": aggregate_0_1, "rationale": "reviewed"},
        {"a_index": 0, "b_index": 2,
         "allow_a_to_b": False, "allow_b_to_a": False,
         "aggregation_compatible": False, "rationale": "forbidden"},
        {"a_index": 1, "b_index": 2,
         "allow_a_to_b": False, "allow_b_to_a": False,
         "aggregation_compatible": False, "rationale": "forbidden"},
    ]}


class WholeComponentMergeTests(unittest.TestCase):
    def test_alternate_detail_presets_have_visible_physical_progression(self):
        widths = [SAFE_BOUNDARY_PRESETS[level]["min_feature_mm"] for level in range(1, 6)]
        smoothing = [SAFE_BOUNDARY_PRESETS[level]["smooth_mm"] for level in range(1, 6)]

        self.assertEqual(widths, [5.0, 7.0, 10.0, 13.0, 16.0])
        self.assertEqual(smoothing, [0.5, 0.8, 1.2, 1.5, 2.0])

    def test_allowed_patch_merges_as_one_component(self):
        label_map = np.zeros((7, 7), np.int16)
        label_map[3, 3] = 1
        mask = np.full((7, 7), 255, np.uint8)

        merges, unresolved = merge_small_components(
            label_map, mask, classes(), min_area_px=4,
            relationships=relationships(allow_1_to_0=True), preserve_share=0.5,
        )

        self.assertEqual(label_map[3, 3], 0)
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["source_label"], "Cropland")
        self.assertEqual(merges[0]["target_label"], "Grassland")
        self.assertEqual(unresolved, [])

    def test_incompatible_patch_is_retained_and_flagged(self):
        label_map = np.zeros((7, 7), np.int16)
        label_map[3, 3] = 1
        mask = np.full((7, 7), 255, np.uint8)

        merges, unresolved = merge_small_components(
            label_map, mask, classes(), min_area_px=4,
            relationships=relationships(allow_1_to_0=False), preserve_share=0.5,
        )

        self.assertEqual(label_map[3, 3], 1)
        self.assertEqual(merges, [])
        self.assertEqual(unresolved[0]["reason"], "no_semantically_allowed_neighbour")

    def test_direction_is_respected(self):
        label_map = np.ones((7, 7), np.int16)
        label_map[3, 3] = 0
        mask = np.full((7, 7), 255, np.uint8)

        merges, _ = merge_small_components(
            label_map, mask, classes(), min_area_px=4,
            relationships=relationships(allow_1_to_0=True), preserve_share=2.0,
        )

        self.assertEqual(label_map[3, 3], 0)
        self.assertEqual(merges, [])

    def test_model_proposal_cannot_change_pixels_before_human_review(self):
        label_map = np.zeros((7, 7), np.int16)
        label_map[3, 3] = 1
        mask = np.full((7, 7), 255, np.uint8)
        draft = relationships(allow_1_to_0=True)
        draft["reviewed"] = False

        merges, unresolved = merge_small_components(
            label_map, mask, classes(), min_area_px=4,
            relationships=draft, preserve_share=0.5,
        )

        self.assertEqual(label_map[3, 3], 1)
        self.assertEqual(merges, [])
        self.assertEqual(unresolved[0]["reason"], "no_semantically_allowed_neighbour")

    def test_safe_boundary_smoothing_obeys_each_class_change_budget(self):
        original = np.zeros((30, 30), np.int16)
        original[:, 15:] = 1
        original[5:25:3, 14] = 1
        original[6:25:3, 15] = 0
        simplified = original.copy()
        mask = np.full(original.shape, 255, np.uint8)
        info = simplify_boundaries_safely(
            simplified, mask, classes()[:2], mm_per_px=0.25, level=5)
        audit = transition_audit(original, simplified, classes()[:2])

        for row in audit["per_class"]:
            budget = info["per_class_budget_px"][row["index"]]
            self.assertLessEqual(row["gained_px"], budget)
            self.assertLessEqual(row["lost_px"], budget)


class AlternateAggregationTests(unittest.TestCase):
    def test_alternate_steps_do_not_auto_run_missing_predecessors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "sample.png"
            with self.assertRaisesRegex(FileNotFoundError, "Alt Step 5 manually"):
                run_alt_step6(image, runs_dir=root / "runs")
            with self.assertRaisesRegex(FileNotFoundError, "Alt Step 6 manually"):
                run_alt_step7(image, runs_dir=root / "runs")

    def test_only_reviewed_compatible_groups_merge(self):
        thematic = classes()
        groups, plain, log = aggregate_reviewed(
            thematic, slots=2, relationships=relationships(aggregate_0_1=True),
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual(plain, [])
        self.assertEqual(set(log[0]["members"]), {"Grassland", "Cropland"})

    def test_unmergeable_group_remains_plain(self):
        groups, plain, log = aggregate_reviewed(
            classes(), slots=2, relationships=relationships(aggregate_0_1=False),
        )

        self.assertEqual(len(groups), 2)
        self.assertEqual(len(plain), 1)
        self.assertEqual(log, [])

    def test_complete_proposal_reaches_texture_ceiling_without_changing_pixels(self):
        thematic = [
            {"index": 0, "label": "Forest", "is_thematic": True,
             "priority": 1, "area_px": 100},
            {"index": 1, "label": "Cropland", "is_thematic": True,
             "priority": 2, "area_px": 50},
            {"index": 2, "label": "Other crops (primarily cereals)",
             "is_thematic": True, "priority": 3, "area_px": 60},
        ]
        groups, log = propose_complete_aggregation(
            thematic, slots=2, relationships={"reviewed": False, "pairs": []})

        self.assertEqual(len(groups), 2)
        merged = next(group for group in groups if len(group["members"]) == 2)
        self.assertEqual(merged["label"], "Field Crops")
        self.assertEqual(set(merged["members"]), {1, 2})
        self.assertEqual(len(log), 1)

    def test_semantic_proposal_may_use_fewer_than_the_maximum_slots(self):
        labels = [
            "Forest", "Grassland", "Grassland and crops", "Cropland",
            "Other crops (primarily cereals)", "Olives", "Market gardening",
            "Vineyards",
        ]
        thematic = [{"index": index, "label": label, "is_thematic": True,
                     "priority": index + 1, "area_px": 100 + index}
                    for index, label in enumerate(labels)]
        groups, _ = propose_complete_aggregation(
            thematic, slots=5, relationships={"reviewed": False, "pairs": []})

        self.assertEqual(len(groups), 4)
        self.assertLess(len(groups), 5)
        self.assertIn("Field Crops", {group["label"] for group in groups})

    def test_post_aggregation_geometry_removes_small_whole_patch(self):
        group_map = np.zeros((24, 24), np.int16)
        group_map[2:4, 2:4] = 1
        group_map[12:19, 12:19] = 1

        simplified, audit = generalize_group_geometry(
            group_map, min_area_px=20, mm_per_px=0.25, level=1)

        self.assertEqual(simplified[2, 2], 0)
        self.assertEqual(simplified[15, 15], 1)
        self.assertGreaterEqual(audit["whole_components_merged"], 1)

    def test_texture_minimum_depends_on_pattern_pitch(self):
        sparse_dots = tactile_minimum_for_pattern("dots_sparse", 13.0)
        grid = tactile_minimum_for_pattern("grid_cross", 13.0)
        solid = tactile_minimum_for_pattern("solid_black", 13.0)

        self.assertGreater(sparse_dots["min_width_mm"], grid["min_width_mm"])
        self.assertGreater(grid["min_width_mm"], solid["min_width_mm"])
        self.assertEqual(solid["min_width_mm"], 10.0)

    def test_long_but_too_narrow_region_fails_width_test(self):
        group_map = np.zeros((220, 1200), np.int16)
        group_map[10:12, 10:910] = 1  # enough area, only 0.5 mm wide
        group_map[40:190, 1000:1150] = 1  # protected usable occurrence

        simplified, audit = generalize_group_geometry(
            group_map, min_area_px=10, mm_per_px=0.25, level=1,
            group_patterns={0: "solid_black", 1: "plain"}, base_min_mm=1.0)

        self.assertEqual(simplified[10, 100], 0)
        self.assertEqual(simplified[100, 1080], 1)
        record = next(item for item in audit["merge_records"]
                      if item["source_group"] == 1)
        self.assertTrue(record["failed_width"])

    def test_tactile_cleanup_does_not_expand_forest_over_agriculture(self):
        group_map = np.zeros((100, 100), np.int16)  # forest
        group_map[5:55, 5:55] = 1  # protected field-crop occurrence
        group_map[70:95, 70:95] = 2  # protected grassland occurrence
        group_map[68:70, 70:72] = 1  # tiny field patch touches both

        simplified, audit = generalize_group_geometry(
            group_map, min_area_px=10, mm_per_px=0.25, level=1,
            group_patterns={0: "solid_black", 1: "grid_cross", 2: "lines_horizontal"},
            base_min_mm=1.0,
            group_labels={0: "Forest", 1: "Field Crops", 2: "Grasslands"})

        self.assertNotEqual(simplified[68, 70], 0)
        self.assertFalse(any(item["target_group"] == 0 and item["source_group"] == 1
                             for item in audit["merge_records"]))

    def test_concrete_review_gates_effective_aggregation(self):
        aggregation = {
            "slots": 2,
            "review_required": True,
            "source_classes": [
                {"index": 0, "label": "Forest"},
                {"index": 1, "label": "Cropland"},
                {"index": 2, "label": "Other crops"},
            ],
            "groups": [
                {"label": "Forest", "members": [0], "member_labels": ["Forest"]},
                {"label": "Field Crops", "members": [1, 2],
                 "member_labels": ["Cropland", "Other crops"]},
            ],
            "plain_thematic": [],
        }
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            rejected = save_aggregation_review(out_dir, aggregation, [
                {"label": "Forest", "members": [0], "approved": True},
                {"label": "Field Crops", "members": [1, 2], "approved": False},
            ])
            self.assertEqual(rejected["status"], "rejected")
            with self.assertRaises(RuntimeError):
                effective_aggregation(out_dir, aggregation)

            approved = save_aggregation_review(out_dir, aggregation, [
                {"label": "Forest", "members": [0], "approved": True},
                {"label": "Field Crops", "members": [1, 2], "approved": True,
                 "rationale": "reviewed cereal grouping"},
            ])
            self.assertEqual(approved["status"], "approved")
            effective = effective_aggregation(out_dir, aggregation)
            self.assertEqual(effective["review_status"], "approved")
            self.assertEqual(effective["groups"][1]["members"], [1, 2])

    def test_review_can_split_a_four_group_proposal_into_five_slots(self):
        aggregation = {
            "slots": 5,
            "review_required": True,
            "source_classes": [
                {"index": index, "label": f"Class {index}"}
                for index in range(8)
            ],
            "groups": [
                {"label": "Group 1", "members": [0, 1],
                 "member_labels": ["Class 0", "Class 1"]},
                {"label": "Group 2", "members": [2, 3],
                 "member_labels": ["Class 2", "Class 3"]},
                {"label": "Group 3", "members": [4, 5],
                 "member_labels": ["Class 4", "Class 5"]},
                {"label": "Group 4", "members": [6, 7],
                 "member_labels": ["Class 6", "Class 7"]},
            ],
            "plain_thematic": [],
        }
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            review = save_aggregation_review(out_dir, aggregation, [
                {"label": "Final 1", "members": [0, 1], "approved": True},
                {"label": "Final 2", "members": [2, 3], "approved": True},
                {"label": "Final 3", "members": [4, 5], "approved": True},
                {"label": "Final 4", "members": [6], "approved": True},
                {"label": "Final 5", "members": [7], "approved": True},
            ])

            self.assertTrue(review["approved"])
            self.assertEqual(len(review["groups"]), 5)
            self.assertEqual(effective_aggregation(out_dir, aggregation)["groups"][4]["members"], [7])

    def test_transition_audit_exposes_local_contamination(self):
        original = np.array([[0, 1], [0, 1]], np.int16)
        generalized = np.array([[0, 0], [0, 1]], np.int16)
        audit = transition_audit(original, generalized, classes()[:2])

        grass = audit["per_class"][0]
        self.assertEqual(audit["changed_pixels"], 1)
        self.assertEqual(grass["gained_px"], 1)
        self.assertEqual(grass["contamination_share"], 0.3333)


if __name__ == "__main__":
    unittest.main()
