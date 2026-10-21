"""Place-name exclusions need independent evidence; furniture stays intact."""
import unittest
from types import SimpleNamespace

import cv2
import numpy as np

from mapgen.isolate import drop_place_name_boxes, prepare_text_input, refine_map_mask


class PlaceNameExclusionTests(unittest.TestCase):
    def setUp(self):
        self.image = np.full((400, 600, 3), 255, np.uint8)
        self.image[40:360, 40:560] = (80, 170, 210)
        self.sem = SimpleNamespace(
            overlay_text=SimpleNamespace(
                has_city_labels=True, has_region_labels=True,
                capital_city="Paris", notes="Cities: Paris, Lyon. Regions: Bretagne, Mali, Île-de-France."),
            legend_entries=[],
        )

    def test_corroborated_names_stay_available_for_text_processing(self):
        furniture = [(f"other:{name}", (80, 100+i*25, 180, 112+i*25))
                     for i, name in enumerate(["PARIS", "Lyon", "Bretagne", "Country label: Mali", "Île-de-France"])]
        kept, warnings = drop_place_name_boxes(self.image, furniture, self.sem)
        self.assertEqual(kept, [])
        self.assertEqual(len(warnings), 5)

    def test_real_furniture_retains_its_exact_boxes(self):
        names = ["legend", "title", "scale_bar", "north_arrow", "other:logo",
                 "other:Paris inset map", "other:source note", "other:legend_notes",
                 "other:coordinate labels", "other:EPSG text", "other:panel label",
                 "other:specialty crops legend"]
        furniture = [(name, (80, 100, 180, 112)) for name in names]
        kept, warnings = drop_place_name_boxes(self.image, furniture, self.sem)
        self.assertEqual(kept, furniture)
        self.assertEqual(warnings, [])

    def test_a_capital_alone_is_independent_name_evidence(self):
        self.sem.overlay_text.notes = ""
        kept, _ = drop_place_name_boxes(self.image, [("other:Paris", (80, 100, 180, 112))], self.sem)
        self.assertEqual(kept, [])

    def test_unknown_names_require_review_and_do_not_change_the_mask(self):
        furniture = [("other:Riverton", (80, 100, 180, 112))]
        kept, warnings = drop_place_name_boxes(self.image, furniture, self.sem)
        self.assertEqual(kept, furniture)
        self.assertIn("mask review", warnings[0])

    def test_names_are_whole_words_not_substrings(self):
        self.sem.overlay_text.notes = "Country: Somalia."
        furniture = [("other:Mali", (80, 100, 180, 112))]
        self.assertEqual(drop_place_name_boxes(self.image, furniture, self.sem)[0], furniture)

    def test_a_named_inset_sized_box_is_not_removed(self):
        furniture = [("other:Paris", (80, 100, 240, 230))]
        kept, warnings = drop_place_name_boxes(self.image, furniture, self.sem)
        self.assertEqual(kept, furniture)
        self.assertTrue(warnings)

    def test_missing_overlay_expectations_do_not_promote_notes_to_names(self):
        self.sem.overlay_text.has_city_labels = False
        self.sem.overlay_text.has_region_labels = False
        furniture = [("other:Lyon", (80, 100, 180, 112))]
        self.assertEqual(drop_place_name_boxes(self.image, furniture, self.sem)[0], furniture)

    def test_legend_entry_text_is_not_assumed_to_be_a_place_name(self):
        self.sem.legend_entries = [SimpleNamespace(label="Bretagne")]
        furniture = [("other:Bretagne", (80, 100, 180, 112))]
        self.assertEqual(drop_place_name_boxes(self.image, furniture, self.sem)[0], furniture)

    def test_ambiguous_generic_boxes_stay_in_review(self):
        self.sem.overlay_text.notes = "Other text and unknown labels."
        furniture = [("other:unknown", (80, 100, 180, 112))]
        kept, warnings = drop_place_name_boxes(self.image, furniture, self.sem)
        self.assertEqual(kept, furniture)
        self.assertIn("uncertain", warnings[0])

    def test_rectangle_is_kept_in_mask_and_lettering_reaches_step3_input(self):
        cv2.putText(self.image, "Paris", (85, 111), cv2.FONT_HERSHEY_SIMPLEX, .4, (0, 0, 0), 1)
        furniture = [("other:Paris", (80, 100, 180, 112))]
        boxes = [(35, 35, 565, 365)]
        before, _, _ = refine_map_mask(self.image, boxes, [furniture[0][1]])
        kept, _ = drop_place_name_boxes(self.image, furniture, self.sem)
        after, _, _ = refine_map_mask(self.image, boxes, [box for _, box in kept])
        self.assertFalse(before[100:112, 80:180].any())
        self.assertEqual(after[100:112, 80:180].min(), 255)
        self.assertFalse(np.any((before > 0) & (after == 0)))
        text_input = prepare_text_input(self.image, (0, 0, 600, 400), kept, after)
        np.testing.assert_array_equal(text_input[100:112, 80:180], self.image[100:112, 80:180])


if __name__ == "__main__":
    unittest.main()
