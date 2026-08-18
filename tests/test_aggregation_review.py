import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mapgen.aggregate import (_range_label, effective_aggregation,
                              load_aggregation_review, save_aggregation_review)


class CanonicalAggregationReviewTests(unittest.TestCase):
    def proposal(self):
        return {
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
        }

    def test_review_blocks_step7_until_every_merge_is_approved(self):
        proposal = self.proposal()
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            with self.assertRaises(RuntimeError):
                effective_aggregation(out_dir, proposal)

            review = save_aggregation_review(out_dir, proposal, [
                {"label": "Forest", "members": [0], "approved": True},
                {"label": "Field Crops", "members": [1, 2], "approved": False},
            ])
            self.assertEqual(review["status"], "rejected")
            with self.assertRaises(RuntimeError):
                effective_aggregation(out_dir, proposal)

            review = save_aggregation_review(out_dir, proposal, [
                {"label": "Forest", "members": [0], "approved": True},
                {"label": "Field Crops", "members": [1, 2], "approved": True},
            ])
            self.assertTrue(review["approved"])
            self.assertIsNotNone(load_aggregation_review(out_dir, proposal))
            self.assertEqual(effective_aggregation(out_dir, proposal)["groups"][1]["members"],
                             [1, 2])

    def test_every_source_class_must_be_assigned_once(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "missing"):
                save_aggregation_review(Path(directory), self.proposal(), [
                    {"label": "Forest", "members": [0], "approved": True},
                    {"label": "Cropland", "members": [1], "approved": True},
                ])

    def test_ordered_range_label_preserves_open_ended_comparator(self):
        self.assertEqual(_range_label("<= 1", "100 - 250"), "≤ 250")
        self.assertEqual(_range_label("1500 - 2000", "8000 - 10000"),
                         "1500–10000")


if __name__ == "__main__":
    unittest.main()
