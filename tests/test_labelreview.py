import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from mapgen.labelreview import (apply_review, build_text_removal_mask,
                                occurrence_id, review_view)
from mapgen.symbols import write_overlay_labels


def label(text, box, status="gemini-only"):
    return {
        "text": text,
        "kind": "river_label",
        "priority": 3,
        "recognition_status": status,
        "box": box,
        "quad": None,
        "text_position": [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2],
        "text_position_source": "box_center",
        "feature_position": None,
        "feature_position_source": None,
    }


class LabelReviewTests(unittest.TestCase):
    def test_repeated_names_are_grouped_but_remain_separate_occurrences(self):
        source = {"labels": [
            label("Rhône", [10, 10, 30, 20]),
            label("Rhone", [80, 60, 100, 70]),
            label("Loire", [20, 40, 40, 50], "text-confirmed"),
        ]}

        result = review_view(source)

        self.assertEqual(len(result["occurrences"]), 3)
        self.assertEqual(result["occurrences"][0]["duplicate_count"], 2)
        self.assertEqual(result["occurrences"][1]["duplicate_count"], 2)
        self.assertTrue(result["occurrences"][0]["needs_review"])
        self.assertFalse(result["occurrences"][2]["needs_review"])

    def test_review_filters_and_edits_without_mutating_detector_output(self):
        source = {"coordinate_space": "map pixels", "labels": [
            label("Rhône", [10, 10, 30, 20]),
            label("Rhône", [80, 60, 100, 70]),
        ], "warnings": []}
        original = deepcopy(source)
        decisions = [
            {"id": occurrence_id(source["labels"][0], 0), "text": "Rhône", "include": True},
            {"id": occurrence_id(source["labels"][1], 1), "text": "Rhin", "include": False},
        ]

        review, approved = apply_review(source, decisions)

        self.assertEqual(source, original)
        self.assertEqual(len(approved["labels"]), 1)
        self.assertEqual(approved["labels"][0]["text"], "Rhône")
        self.assertEqual(approved["review"]["excluded"], 1)
        self.assertTrue(review_view(source, review)["saved"])

    def test_stale_review_is_not_applied_to_changed_detections(self):
        source = {"labels": [label("Rhône", [10, 10, 30, 20])]}
        review, _ = apply_review(source, [{
            "id": occurrence_id(source["labels"][0], 0),
            "text": "Rhône", "include": True,
        }])
        changed = {"labels": [label("Rhin", [10, 10, 30, 20])]}

        result = review_view(changed, review)

        self.assertFalse(result["saved"])
        self.assertFalse(result["occurrences"][0]["reviewed"])

    def test_reviewed_unresolved_label_uses_visible_box_fallback(self):
        river = label("Rhône", [10, 10, 30, 20])
        river["mask_found"] = False
        source = {"labels": [river]}
        stroke_mask = np.zeros((40, 50), np.uint8)

        automatic, automatic_meta = build_text_removal_mask(source, stroke_mask)
        review, _ = apply_review(source, [{
            "id": occurrence_id(river, 0), "text": "Rhône",
            "include": True, "remove": True,
        }])
        reviewed, reviewed_meta = build_text_removal_mask(source, stroke_mask, review)

        self.assertEqual(np.count_nonzero(automatic), 0)
        self.assertEqual(automatic_meta["kept_labels"], 1)
        self.assertGreater(np.count_nonzero(reviewed), 0)
        self.assertEqual(reviewed_meta["whole_box_labels"], 1)
        self.assertEqual(reviewed_meta["mode"], "reviewed")

    def test_overlay_export_uses_current_approved_labels(self):
        source = {"labels": [
            label("Rhône", [10, 10, 30, 20]),
            label("Rhin", [80, 60, 100, 70]),
        ]}
        review, approved = apply_review(source, [
            {"id": occurrence_id(source["labels"][0], 0), "text": "Rhône", "include": True},
            {"id": occurrence_id(source["labels"][1], 1), "text": "Rhin", "include": False},
        ])
        with TemporaryDirectory() as directory:
            out_dir = Path(directory)
            (out_dir / "labels.json").write_text(json.dumps(source), encoding="utf-8")
            (out_dir / "label_review.json").write_text(json.dumps(review), encoding="utf-8")
            (out_dir / "approved_labels.json").write_text(json.dumps(approved), encoding="utf-8")
            (out_dir / "step5_summary.json").write_text(
                json.dumps({"scale_mm_per_px": 0.5}), encoding="utf-8",
            )
            cv2.imwrite(str(out_dir / "label_map_gen.png"), np.zeros((100, 120), np.uint8))
            cv2.imwrite(str(out_dir / "step7_tactile.png"), np.zeros((200, 240), np.uint8))

            result = write_overlay_labels(out_dir)

            changed_source = deepcopy(source)
            changed_source["labels"][0]["text"] = "Seine"
            (out_dir / "labels.json").write_text(json.dumps(changed_source), encoding="utf-8")
            stale_result = write_overlay_labels(out_dir)

        self.assertEqual(result["review_source"], "approved_labels.json")
        self.assertEqual([item["text"] for item in result["labels"]], ["Rhône"])
        self.assertEqual(stale_result["review_source"], "labels.json")
        self.assertEqual([item["text"] for item in stale_result["labels"]], ["Seine", "Rhin"])


if __name__ == "__main__":
    unittest.main()
