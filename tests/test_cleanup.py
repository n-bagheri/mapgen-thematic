import unittest

import numpy as np

from mapgen.cleanup import compose_component_layers


class ComponentLayerCleanupTests(unittest.TestCase):
    def test_non_owner_fill_is_repainted_above_centered_owner_stroke(self):
        height, width = 80, 120
        group_map = np.zeros((height, width), dtype=np.int16)
        group_map[:, 60:] = 1
        base = np.full((height, width), 255, np.uint8)
        # A patterned owner on the left; solid-black C on the right.
        base[10::10, :60] = 0
        base[:, 60:] = 0

        result, audit = compose_component_layers(
            base, group_map,
            {0: "dots_sparse", 1: "solid_black"},
            px_per_mm=5.0,
        )

        self.assertTrue(np.array_equal(result[:, 60:], base[:, 60:]))
        self.assertTrue(np.any(result[:, 47:60] != base[:, 47:60]))
        self.assertEqual(audit["repainted_components"], 1)
        self.assertGreater(audit["restored_pixels"], 0)
        self.assertEqual(audit["open_owner_endpoints"], 0)

    def test_two_owner_components_keep_the_shared_centered_boundary(self):
        group_map = np.zeros((80, 120), dtype=np.int16)
        group_map[:, 60:] = 1
        base = np.full(group_map.shape, 255, np.uint8)
        base[::8, :60] = 0
        base[::10, 60:] = 0

        result, audit = compose_component_layers(
            base, group_map,
            {0: "dots_sparse", 1: "lines_horizontal"},
            px_per_mm=5.0,
        )

        self.assertEqual(audit["repainted_components"], 0)
        self.assertTrue(np.all(result[20, 57:62] == 0))
        self.assertTrue(np.all(result[20, 48:57] == 255))
        self.assertTrue(np.all(result[20, 62:72] == 255))

    def test_plain_group_stays_below_owner_strokes(self):
        group_map = np.zeros((80, 120), dtype=np.int16)
        group_map[:, 60:] = 1
        base = np.full(group_map.shape, 255, np.uint8)
        base[::8, :60] = 0

        result, audit = compose_component_layers(
            base, group_map,
            {0: "dots_sparse", 1: "plain"},
            px_per_mm=5.0,
        )

        # The white clearance and black centerline stay visible on both sides
        # of the patterned/plain interface; plain has not repainted them and
        # owns a closed contour of its own.
        self.assertTrue(np.all(result[20, 57:62] == 0))
        self.assertTrue(np.all(result[20, 62:72] == 255))
        self.assertEqual(audit["repainted_components"], 0)
        self.assertEqual(audit["owner_group_ids"], [0, 1])
        self.assertTrue(np.any(result[:, -3:] == 0))

    def test_wave_pattern_gets_complete_outside_boundary(self):
        group_map = np.ones((80, 120), dtype=np.int16)
        group_map[15:65, 20:85] = 0
        base = np.full(group_map.shape, 255, np.uint8)
        base[20:65:8, 20:85] = 0

        result, audit = compose_component_layers(
            base, group_map,
            {0: "04_waves_sine", 1: "plain"},
            px_per_mm=5.0,
        )

        self.assertEqual(audit["owner_group_ids"], [0, 1])
        self.assertGreater(audit["owner_contours"], 0)
        self.assertEqual(audit["open_owner_endpoints"], 0)
        self.assertTrue(np.any(result[13:18, 25:80] == 0))


if __name__ == "__main__":
    unittest.main()
