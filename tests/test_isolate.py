import unittest

import cv2
import numpy as np

from mapgen.isolate import (
    detect_swatches,
    prepare_text_input,
    refine_map_mask,
    sample_swatch,
)


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

    def test_removes_isolated_long_ruler_at_ai_map_edge(self):
        img = np.full((300, 400, 3), 255, np.uint8)
        cv2.rectangle(img, (70, 70), (260, 240), (40, 160, 80), -1)
        # A tall, independent coordinate ruler mistakenly included by a broad
        # model-proposed map box.
        cv2.rectangle(img, (382, 35), (384, 270), (200, 40, 40), -1)

        mask, _, warnings = refine_map_mask(
            img,
            map_boxes=[(45, 25, 390, 280)],
            exclude_boxes=[],
        )

        self.assertGreater(mask[150, 150], 0)
        self.assertEqual(mask[150, 383], 0)
        self.assertTrue(any("edge ruler/tick" in warning for warning in warnings))


class TextInputPreparationTests(unittest.TestCase):
    def test_keeps_label_that_overhangs_small_island(self):
        img = np.full((200, 300, 3), 255, np.uint8)
        mask = np.zeros((200, 300), np.uint8)
        cv2.rectangle(img, (220, 80), (260, 130), (40, 160, 80), -1)
        cv2.rectangle(mask, (220, 80), (260, 130), 255, -1)

        # Simulate a coastal label whose left-hand characters lie on paper.
        cv2.putText(img, "CITY", (205, 108), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (0, 0, 0), 1, cv2.LINE_AA)
        clean = prepare_text_input(img, [0, 0, 300, 200], [], mask)

        original_ink = np.all(img[94:111, 205:220] < 80, axis=2)
        retained_ink = np.all(clean[94:111, 205:220] < 80, axis=2)
        self.assertEqual(np.count_nonzero(retained_ink), np.count_nonzero(original_ink))

    def test_still_blanks_content_far_from_map(self):
        img = np.full((200, 300, 3), 255, np.uint8)
        mask = np.zeros((200, 300), np.uint8)
        cv2.rectangle(img, (220, 80), (260, 130), (40, 160, 80), -1)
        cv2.rectangle(mask, (220, 80), (260, 130), 255, -1)
        cv2.putText(img, "NOTE", (10, 30), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (0, 0, 0), 1, cv2.LINE_AA)

        clean = prepare_text_input(img, [0, 0, 300, 200], [], mask)

        self.assertTrue(np.all(clean[10:35, 5:60] > 240))

    def test_furniture_is_blank_even_when_inside_context_halo(self):
        img = np.full((200, 300, 3), 255, np.uint8)
        mask = np.zeros((200, 300), np.uint8)
        cv2.rectangle(img, (220, 80), (260, 130), (40, 160, 80), -1)
        cv2.rectangle(mask, (220, 80), (260, 130), 255, -1)
        cv2.putText(img, "KEY", (185, 108), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (0, 0, 0), 1, cv2.LINE_AA)

        furniture = [{"name": "legend", "box": [180, 85, 220, 115]}]
        clean = prepare_text_input(img, [0, 0, 300, 200], furniture, mask)

        self.assertTrue(np.all(clean[85:115, 180:220] > 240))

    def test_furniture_padding_does_not_cut_into_map(self):
        img = np.full((200, 300, 3), 255, np.uint8)
        mask = np.zeros((200, 300), np.uint8)
        cv2.rectangle(img, (220, 80), (260, 130), (40, 160, 80), -1)
        cv2.rectangle(mask, (220, 80), (260, 130), 255, -1)

        # The detected furniture box and its padding overlap the map edge.
        furniture = [{"name": "legend", "box": [180, 85, 240, 115]}]
        clean = prepare_text_input(img, [0, 0, 300, 200], furniture, mask)

        self.assertTrue(np.all(clean[90:110, 185:215] > 240))
        np.testing.assert_array_equal(clean[90:110, 225:235], img[90:110, 225:235])


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

    def test_closed_label_glyphs_are_not_detected_as_white_swatches(self):
        legend = np.full((250, 620, 3), 255, np.uint8)
        colors_bgr = [
            (80, 220, 245), (80, 190, 110), (170, 80, 55),
            (110, 45, 145),
        ]
        for i, color in enumerate(colors_bgr):
            column = i // 2
            row = i % 2
            x = 12 + column * 300
            y = 70 + row * 70
            cv2.rectangle(legend, (x, y), (x + 64, y + 38), color, -1)
            cv2.putText(legend, f"{1000 + i * 1000} - {2000 + i * 1000}",
                        (x + 82, y + 31), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 0), 2, cv2.LINE_AA)

        rects, warnings = detect_swatches(
            legend,
            expected=4,
            labels=["1000 - 2000", "2000 - 3000", "3000 - 4000", "4000 - 5000"],
            ordered=True,
        )

        self.assertEqual(len(rects), 4, warnings)
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertTrue(all(max(rgb) - min(rgb) > 20 for rgb in sampled))
        self.assertEqual(len({tuple(rgb) for rgb in sampled}), 4)

    def test_compact_three_column_grid_recovers_tiny_swatches(self):
        legend = np.full((105, 132, 3), 245, np.uint8)
        colours = [(20 + i * 7, 80 + i * 5, 180 + i * 2) for i in range(24)]
        index = 0
        for column, count in enumerate((8, 8, 9)):
            for row in range(count):
                x, y = column * 45, row * 12
                colour = colours[index] if index < 24 else (20, 20, 20)
                cv2.rectangle(legend, (x, y), (x + 13, y + 7), colour, -1)
                index += 1

        rects, warnings = detect_swatches(
            legend, expected=25, labels=[str(i) for i in range(25)], ordered=True,
        )

        self.assertEqual(len(rects), 25, warnings)


if __name__ == "__main__":
    unittest.main()
