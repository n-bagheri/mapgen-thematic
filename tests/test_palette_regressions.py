"""Regression references taken from the printed legends, not detector output."""
import unittest
from pathlib import Path

import cv2
import numpy as np

from mapgen.isolate import detect_swatches, imread, sample_swatch, to_lab
from mapgen.segment import restore_lost_legend_pixels


LIBRARY = Path(__file__).resolve().parents[1] / "map_library"


class PrintedLegendRegressionTests(unittest.TestCase):
    LABELS = {
        "poland_vegetation": ["> 2000", "1500-2000", "1000-1500", "700-1000", "500-700",
                              "400-500", "300-400", "200-300", "150-200", "100-150", "50-100", "0-50"],
        "senegral_rainfall": ["< 300", "300-500", "500-700", "700-900", "900-1100", "1100-1300", "> 1300"],
        "morocco_humidity": ["High humidity", "Humid", "Subhumid", "Arid", "Semi-arid", "Saharan"],
        "morocco_population": ["0", "26", "78", "130"],
    }
    # Coordinates below were checked on enlarged copies of the source legends.
    # Each point is safely inside the actual printed colour, in crop coordinates.
    CASES = {
        "japan_landuse": ((110, 173, 382, 463), False,
                          [(15, 18), (15, 48), (15, 78), (15, 108)]),
        "morocco_humidity": ((309, 346, 423, 468), True,
                             [(16, 50), (16, 63), (16, 75), (16, 88), (16, 101), (16, 115)]),
        "morocco_population": ((866, 95, 1023, 226), True,
                               [(41, 78), (72, 78), (102, 78), (133, 78)]),
        "poland_vegetation": ((17, 361, 89, 623), True,
                             [(12, y) for y in (30, 80, 130, 170, 195, 209, 219, 229, 236, 242, 247, 251)]),
        "senegral_rainfall": ((5, 2, 152, 179), True,
                             [(14, y) for y in (64, 82, 99, 117, 134, 152, 169)]),
    }

    def test_colours_match_printed_regions_including_mixed_symbol_legends(self):
        for stem, (box, ordered, points) in self.CASES.items():
            with self.subTest(map=stem):
                path = next(LIBRARY.glob(stem + ".*"), None)
                if path is None:
                    self.skipTest("Printed map fixtures unavailable")
                source = imread(path)
                x0, y0, x1, y1 = box
                crop = source[y0:y1, x0:x1]
                rects, _ = detect_swatches(crop, len(points),
                                          labels=self.LABELS.get(stem), ordered=ordered)
                self.assertEqual(len(rects), len(points))
                for index, (rect, (x, y)) in enumerate(zip(rects, points)):
                    rx, ry, rw, rh = rect
                    self.assertTrue(rx <= x < rx+rw and ry <= y < ry+rh,
                                    f"{stem} entry {index}: swatch misses its printed interior")
                    # Direct source pixels are an independent colour reference.
                    reference = np.median(crop[y-1:y+2, x-1:x+2], axis=(0, 1))
                    ref_lab = to_lab(np.uint8([[reference]]))[0, 0]
                    _, actual_lab = sample_swatch(crop, rect)
                    self.assertLess(np.linalg.norm(ref_lab - actual_lab), 18,
                                    f"{stem} entry {index}: {rect} does not sample its printed colour")

    def test_japan_symbols_do_not_change_the_four_area_colours(self):
        image = imread(LIBRARY / "japan_landuse.jpg")
        palettes = []
        for x0, y0, x1, y1 in [(110, 173, 382, 312), (110, 173, 382, 463),
                                (97, 113, 431, 312), (97, 113, 431, 463)]:
            crop = image[y0:y1, x0:x1]
            rects, _ = detect_swatches(crop, 4)
            self.assertEqual(len(rects), 4)
            palettes.append(np.array([sample_swatch(crop, r)[1] for r in rects]))
        for palette in palettes[1:]:
            self.assertTrue(np.all(np.linalg.norm(palette - palettes[0], axis=1) < 6))

    def test_printed_references_survive_extra_margins_and_swatch_only_crops(self):
        for stem, (box, ordered, points) in self.CASES.items():
            image = imread(next(LIBRARY.glob(stem + ".*")))
            x0, y0, x1, y1 = box
            xs, ys = zip(*points)
            for bounds in [(max(0, x0-3), max(0, y0-3), min(image.shape[1], x1+3), min(image.shape[0], y1+3)),
                           (x0+min(xs)-6, y0+min(ys)-4, x0+max(xs)+7, y0+max(ys)+5)]:
                with self.subTest(map=stem, crop=bounds):
                    left, top, right, bottom = bounds
                    crop = image[top:bottom, left:right]
                    rects, _ = detect_swatches(crop, len(points), self.LABELS.get(stem), ordered)
                    self.assertEqual(len(rects), len(points))
                    for rect, (x, y) in zip(rects, points):
                        rx, ry, rw, rh = rect
                        self.assertTrue(rx <= x0+x-left < rx+rw and ry <= y0+y-top < ry+rh)
                        patch = image[y0+y-1:y0+y+2, x0+x-1:x0+x+2]
                        ref = to_lab(np.uint8([[np.median(patch, axis=(0, 1))]]))[0, 0]
                        self.assertLess(np.linalg.norm(ref - sample_swatch(crop, rect)[1]), 18)

    def test_unequal_printed_band_widths_do_not_shift_labels(self):
        image = np.full((80, 150, 3), 255, np.uint8)
        colours = [(20, 20, 200), (20, 180, 30), (200, 70, 20), (30, 180, 200)]
        edges = [15, 25, 45, 95, 115]
        for colour, start, end in zip(colours, edges, edges[1:]):
            image[30:50, start:end] = colour
        rects, _ = detect_swatches(image, 4, ["a", "b", "c", "d"], ordered=True)
        self.assertEqual(len(rects), 4)
        self.assertEqual([sample_swatch(image, r)[0] for r in rects], [list(c[::-1]) for c in colours])


class SmallLegendRegionTests(unittest.TestCase):
    def fixture(self):
        source = np.full((30, 40, 3), (0, 0, 220), np.uint8)
        seeds = [{"label": name, "source": "legend", "is_thematic": True,
                  "lab": to_lab(np.uint8([[colour]]))[0, 0]}
                 for name, colour in [("Red", (0, 0, 220)), ("Blue", (240, 0, 0))]]
        return source, seeds, np.zeros((30, 40), np.int16), np.ones((30, 40), bool)

    def test_one_source_pixel_survives_without_expanding_its_area(self):
        source, seeds, labels, valid = self.fixture()
        source[15, 20] = (240, 0, 0)
        notes = restore_lost_legend_pixels(source, labels, valid, seeds, [1])
        self.assertEqual(np.count_nonzero(labels == 1), 1)
        self.assertEqual(labels[15, 20], 1)
        self.assertTrue(notes)

    def test_an_unused_legend_entry_is_not_invented(self):
        source, seeds, labels, valid = self.fixture()
        before = labels.copy()
        self.assertEqual(restore_lost_legend_pixels(source, labels, valid, seeds, [1]), [])
        np.testing.assert_array_equal(labels, before)

    def test_removed_text_and_furniture_do_not_become_thematic_pixels(self):
        source, seeds, labels, valid = self.fixture()
        source[15, 20] = (240, 0, 0)
        valid[15, 20] = False
        self.assertEqual(restore_lost_legend_pixels(source, labels, valid, seeds, [1]), [])
        self.assertFalse(np.any(labels == 1))

    def test_present_classes_are_unchanged(self):
        source, seeds, labels, valid = self.fixture()
        source[15, 20] = (240, 0, 0)
        before = labels.copy()
        self.assertEqual(restore_lost_legend_pixels(source, labels, valid, seeds, []), [])
        np.testing.assert_array_equal(labels, before)

    def test_large_scan_blend_returns_to_the_printed_palette(self):
        source, seeds, labels, valid = self.fixture()
        source[15, 20] = (240, 0, 0)
        seeds.append({"source": "unseeded", "is_thematic": False,
                      "label": "scan blend", "lab": (seeds[0]["lab"]+seeds[1]["lab"])/2})
        labels[:, :10] = 2
        restore_lost_legend_pixels(source, labels, valid, seeds, [1])
        self.assertEqual(np.count_nonzero(labels == 1), 1)
        self.assertFalse(np.any(labels == 2))

    def test_unrelated_water_colour_survives_small_class_recovery(self):
        source, seeds, labels, valid = self.fixture()
        source[15, 20] = (240, 0, 0)
        seeds.append({"source": "unseeded", "is_thematic": False,
                      "label": "water", "lab": to_lab(np.uint8([[(255, 255, 255)]]))[0, 0]})
        labels[:, :10] = 2
        restore_lost_legend_pixels(source, labels, valid, seeds, [1])
        self.assertTrue(np.all(labels[:, :10] == 2))


if __name__ == "__main__":
    unittest.main()
