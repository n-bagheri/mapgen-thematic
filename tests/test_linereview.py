import unittest

from mapgen.linereview import (
    apply_review,
    automatic_feature_id,
    materialize_review,
    review_view,
)


def _feature(kind, points):
    return {
        "type": "Feature",
        "properties": {"kind": kind, "source": "automatic"},
        "geometry": {"type": "LineString", "coordinates": points},
    }


class LineReviewTests(unittest.TestCase):
    def setUp(self):
        self.lines = {
            "type": "FeatureCollection",
            "features": [
                _feature("coastline", [[0, 0], [9, 0]]),
                _feature("river", [[1, 2], [4, 2]]),
                _feature("river", [[6, 2], [9, 2]]),
            ],
        }

    def test_review_keeps_fixed_lines_and_materializes_manual_river(self):
        second_id = automatic_feature_id(self.lines["features"][2], 2)
        review, approved = apply_review(self.lines, {
            "include_auto_ids": [second_id],
            "manual_rivers": [{
                "id": "manual-joined", "label": "Test", "edit_kind": "joined",
                "points": [[1, 2], [5, 2], [9, 2]],
            }],
        }, width=10, height=10)

        self.assertEqual(len(approved["features"]), 3)
        self.assertEqual(approved["features"][0]["properties"]["kind"], "coastline")
        self.assertEqual(approved["features"][-1]["properties"]["source"], "manual_review")
        self.assertEqual(materialize_review(self.lines, review), approved)

    def test_default_view_includes_every_automatic_river(self):
        view = review_view(self.lines, None, width=10, height=10)

        self.assertFalse(view["saved"])
        self.assertEqual(len(view["automatic_rivers"]), 2)
        self.assertTrue(all(item["include"] for item in view["automatic_rivers"]))
        self.assertEqual(len(view["fixed_features"]), 1)

    def test_invalid_manual_coordinates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside map_area"):
            apply_review(self.lines, {
                "include_auto_ids": [],
                "manual_rivers": [{
                    "id": "manual-bad", "points": [[1, 1], [12, 2]],
                }],
            }, width=10, height=10)

    def test_stale_review_is_not_applied_to_changed_automatic_lines(self):
        review, _ = apply_review(self.lines, {
            "include_auto_ids": [], "manual_rivers": [],
        }, width=10, height=10)
        changed = {**self.lines, "features": self.lines["features"] + [
            _feature("river", [[2, 5], [8, 5]]),
        ]}

        self.assertEqual(materialize_review(changed, review), changed)

    def test_drawn_branch_snaps_to_and_nodes_kept_automatic_river(self):
        target_id = automatic_feature_id(self.lines["features"][1], 1)
        review, approved = apply_review(self.lines, {
            "include_rivers": True,
            "include_auto_ids": [target_id],
            "manual_rivers": [{
                "id": "manual-branch", "edit_kind": "drawn",
                "points": [[3, 3], [3, 9]],
            }],
        }, width=10, height=10)

        manual = review["manual_rivers"][0]
        self.assertEqual(manual["points"][0], [3.0, 2.0])
        self.assertEqual(manual["connections"]["start"]["target_id"], target_id)
        automatic_points = approved["features"][1]["geometry"]["coordinates"]
        self.assertIn([3.0, 2.0], automatic_points)

    def test_master_toggle_omits_all_rivers_but_keeps_review_choices(self):
        target_id = automatic_feature_id(self.lines["features"][1], 1)
        review, approved = apply_review(self.lines, {
            "include_rivers": False,
            "include_auto_ids": [target_id],
            "manual_rivers": [{
                "id": "manual-hidden", "edit_kind": "drawn",
                "points": [[3, 3], [3, 9]],
            }],
        }, width=10, height=10)

        self.assertFalse(review["include_rivers"])
        self.assertEqual([feature["properties"]["kind"]
                          for feature in approved["features"]], ["coastline"])
        view = review_view(self.lines, review, width=10, height=10)
        self.assertFalse(view["include_rivers"])
        self.assertEqual(len(view["manual_rivers"]), 1)


if __name__ == "__main__":
    unittest.main()
