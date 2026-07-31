import unittest

import cv2
import numpy as np

from mapgen.isolate import detect_swatches, refine_map_mask, sample_swatch


class MapMaskRefinementTests(unittest.TestCase):
    def test_broad_map_band_keeps_detached_components(self):
        img = np.full((200, 400, 3), 255, np.uint8)
        cv2.rectangle(img, (10, 60), (70, 130), (40, 100, 220), -1)
        cv2.rectangle(img, (160, 55), (245, 140), (80, 180, 70), -1)
        cv2.rectangle(img, (335, 70), (390, 135), (220, 120, 40), -1)

        mask, tight, warnings = refine_map_mask(
            img,
            map_boxes=[(40, 40, 350, 160)],
            exclude_boxes=[],
        )

        self.assertGreater(mask[90, 30], 0)
        self.assertGreater(mask[90, 190], 0)
        self.assertGreater(mask[90, 360], 0)
        self.assertLessEqual(tight[0], 10)
        self.assertGreaterEqual(tight[2], 391)
        self.assertTrue(any("broad map panel" in warning for warning in warnings))

    def test_narrow_single_map_keeps_conservative_stray_filter(self):
        img = np.full((300, 300, 3), 255, np.uint8)
        cv2.rectangle(img, (100, 100), (200, 200), (40, 160, 80), -1)
        cv2.rectangle(img, (225, 50), (245, 70), (200, 80, 40), -1)

        mask, _, warnings = refine_map_mask(
            img,
            map_boxes=[(50, 35, 250, 250)],
            exclude_boxes=[],
        )

        self.assertGreater(mask[150, 150], 0)
        self.assertEqual(mask[60, 235], 0)
        self.assertTrue(any("outside the geographic content envelope" in w for w in warnings))


class LegendDetectionTests(unittest.TestCase):
    def test_ordered_horizontal_colorbar_is_split_and_sampled(self):
        legend = np.full((120, 720, 3), 255, np.uint8)
        cv2.rectangle(legend, (10, 60), (80, 100), (100, 100, 100), 1)
        for x in range(12, 78, 12):
            cv2.line(legend, (x, 98), (min(78, x + 30), 62), (190, 190, 190), 2)

        colors_bgr = [
            (180, 240, 255),
            (100, 200, 250),
            (60, 150, 245),
            (40, 80, 230),
            (45, 30, 180),
            (50, 0, 120),
        ]
        ramp_x, cell_w = 120, 90
        for i, color in enumerate(colors_bgr):
            x0 = ramp_x + i * cell_w
            cv2.rectangle(legend, (x0, 60), (x0 + cell_w, 100), color, -1)
            cv2.rectangle(legend, (x0, 60), (x0 + cell_w, 100), (80, 80, 80), 1)

        labels = ["No data", "0%", "20%", "40%", "60%", "80%", "100%"]
        rects, warnings = detect_swatches(
            legend,
            len(labels),
            labels=labels,
            ordered=True,
        )

        self.assertEqual(len(rects), len(labels))
        self.assertTrue(any("horizontal color bar" in warning for warning in warnings))
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertEqual(sampled[0], [255, 255, 255])
        self.assertEqual(len({tuple(rgb) for rgb in sampled[1:]}), len(colors_bgr))

    def test_discrete_swatch_fallback_still_works(self):
        legend = np.full((180, 220, 3), 255, np.uint8)
        colors_bgr = [(30, 60, 210), (40, 180, 70), (210, 120, 30)]
        for i, color in enumerate(colors_bgr):
            y = 20 + i * 50
            cv2.rectangle(legend, (15, y), (54, y + 24), color, -1)

        rects, _ = detect_swatches(
            legend,
            expected=3,
            labels=["10%", "20%", "30%"],
            ordered=True,
        )

        self.assertEqual(len(rects), 3)
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertEqual(len({tuple(rgb) for rgb in sampled}), 3)


if __name__ == "__main__":
    unittest.main()
