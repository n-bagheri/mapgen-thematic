"""Conservative recovery of geography rejected by the mainland envelope."""
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.isolate import refine_map_mask


GREEN = (60, 160, 60)
YELLOW = (40, 220, 240)
ISLAND = np.array([(190, 236), (205, 240), (227, 237), (239, 250),
                   (228, 263), (225, 279), (210, 282), (204, 270), (195, 264)])
BOX = (50, 40, 280, 300)


def fixture(polygon=ISLAND, colours=(GREEN, YELLOW)):
    image = np.full((320, 340, 3), 255, np.uint8)
    cv2.rectangle(image, (80, 60), (250, 220), GREEN, -1)
    cv2.rectangle(image, (165, 60), (250, 220), YELLOW, -1)
    island = np.zeros(image.shape[:2], np.uint8)
    cv2.fillPoly(island, [polygon], 1)
    split = (int(polygon[:, 0].min()) + int(polygon[:, 0].max())) // 2
    yy, xx = np.indices(island.shape)
    image[(island > 0) & (xx <= split)] = colours[0]
    image[(island > 0) & (xx > split)] = colours[1]
    return image, island > 0


class DetachedGeographyTests(unittest.TestCase):
    def test_nearby_irregular_island_with_two_mainland_fills_is_retained(self):
        image, island = fixture()
        mask, tight, warnings = refine_map_mask(image, [BOX], [])
        self.assertGreater(np.count_nonzero(mask[island]), 0.95 * np.count_nonzero(island))
        self.assertGreater(tight[3], 282)
        self.assertTrue(any('retained 1 detached geographic' in w for w in warnings))

    def test_retained_mainland_pixels_and_real_white_holes_are_unchanged(self):
        image, _ = fixture()
        image[100:115, 100:115] = 255
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertTrue((mask[120:200, 100:240] == 255).all())
        self.assertFalse(mask[100:115, 100:115].any())

    def test_same_evidence_works_on_an_island_to_the_side(self):
        image, island = fixture()
        image = np.transpose(image, (1, 0, 2)).copy()
        mask, _, warnings = refine_map_mask(image, [(40, 50, 300, 280)], [])
        self.assertGreater(np.count_nonzero(mask[island.T]), 0.95 * np.count_nonzero(island))
        self.assertTrue(any('retained 1 detached geographic' in w for w in warnings))

    def test_single_matching_fill_is_insufficient(self):
        image, island = fixture(colours=(GREEN, GREEN))
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertFalse(mask[island].any())

    def test_unmatched_coloured_decoration_stays_excluded(self):
        image, island = fixture(colours=((220, 30, 30), (220, 30, 160)))
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertFalse(mask[island].any())

    def test_black_ink_fragment_stays_excluded(self):
        image, island = fixture(colours=((0, 0, 0), (0, 0, 0)))
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertFalse(mask[island].any())

    def test_matching_rectangular_swatch_stays_excluded(self):
        rectangle = np.array([(190, 236), (239, 236), (239, 282), (190, 282)])
        image, island = fixture(polygon=rectangle)
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertFalse(mask[island].any())

    def test_matching_round_badge_is_still_ambiguous(self):
        image, _ = fixture()
        image[230:290] = 255
        badge = np.zeros(image.shape[:2], np.uint8)
        cv2.circle(badge, (215, 258), 21, 1, -1)
        _, xx = np.indices(badge.shape)
        image[(badge > 0) & (xx <= 215)] = GREEN
        image[(badge > 0) & (xx > 215)] = YELLOW
        mask, _, _ = refine_map_mask(image, [BOX], [])
        self.assertFalse(mask[badge > 0].any())

    def test_geographically_remote_shape_stays_excluded(self):
        image, island = fixture(polygon=ISLAND + (80, 0))
        mask, _, _ = refine_map_mask(image, [(50, 40, 330, 300)], [])
        self.assertFalse(mask[island].any())

    def test_component_adjacent_to_furniture_stays_excluded(self):
        image, island = fixture()
        mask, _, _ = refine_map_mask(image, [BOX], [(241, 235, 270, 285)])
        self.assertFalse(mask[island].any())

    def test_explicitly_excluded_component_is_not_reconsidered(self):
        image, island = fixture()
        mask, _, _ = refine_map_mask(image, [BOX], [(185, 230, 245, 288)])
        self.assertFalse(mask[island].any())

    def test_selection_edge_fragment_stays_excluded(self):
        image, island = fixture()
        mask, _, _ = refine_map_mask(image, [(50, 40, 280, 283)], [])
        self.assertFalse(mask[island].any())

    def test_multiple_selection_boxes_bypass_the_new_check(self):
        image, island = fixture()
        with patch('mapgen.isolate._supported_detached_geography') as check:
            mask, _, _ = refine_map_mask(image, [(60, 40, 270, 230), (185, 230, 245, 288)], [])
        check.assert_not_called()
        self.assertGreater(np.count_nonzero(mask[island]), 0.95 * np.count_nonzero(island))

    def test_broad_map_band_bypasses_the_new_check(self):
        image = np.full((200, 400, 3), 255, np.uint8)
        cv2.rectangle(image, (20, 60), (200, 140), GREEN, -1)
        cv2.rectangle(image, (330, 70), (380, 120), YELLOW, -1)
        with patch('mapgen.isolate._supported_detached_geography') as check:
            mask, _, _ = refine_map_mask(image, [(10, 40, 390, 160)], [])
        check.assert_not_called()
        self.assertEqual(mask[90, 350], 255)


if __name__ == '__main__':
    unittest.main()
