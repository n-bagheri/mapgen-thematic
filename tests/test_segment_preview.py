import unittest

import cv2
import numpy as np

from mapgen.segment import (
    LINE_PREVIEW_COLORS,
    extract_coastline_cleanup_mask,
    extract_river_cleanup_mask,
    extract_cartographic_lines,
    extract_semantic_boundary_ink,
    render_lines_preview,
)
from mapgen.semantics import MapSemantics


def _semantics(line_kinds):
    return MapSemantics.model_validate({
        "map_type": "area_class_chorochromatic",
        "in_scope": True,
        "data_ordering": "qualitative",
        "map_language": "English",
        "subject": "Synthetic test map",
        "description": "Synthetic test map",
        "title": None,
        "legend_present": False,
        "legend_title": None,
        "legend_entries": [],
        "water_present": False,
        "thematic_classes": [],
        "non_thematic": [],
        "lines": [{"kind": kind, "description": kind} for kind in line_kinds],
        "overlay_text": {
            "has_city_labels": False,
            "capital_city": None,
            "has_region_labels": False,
            "has_line_labels": "river" in line_kinds,
            "notes": "",
        },
    })


class SegmentLinePreviewTests(unittest.TestCase):
    def test_coastline_cleanup_targets_dark_outline_not_saturated_coastal_colour(self):
        image = np.full((100, 140, 3), 255, np.uint8)
        mask = np.zeros((100, 140), np.uint8)
        mask[10:90, 12:128] = 255
        # Printed black outline sits partly inside the geographic mask.
        cv2.rectangle(image, (12, 10), (127, 89), (20, 20, 20), 5)
        # A saturated red thematic patch touches the coast and must survive.
        image[10:24, 50:68] = (55, 55, 235)

        cleanup, diagnostic = extract_coastline_cleanup_mask(image, mask)

        self.assertGreater(diagnostic["pixels"], 0)
        self.assertGreater(cleanup[50, 13], 0)
        self.assertEqual(cleanup[16, 58], 0)

    def test_coastal_black_thematic_region_survives_beyond_cleanup_band(self):
        image = np.full((100, 140, 3), 220, np.uint8)
        mask = np.zeros((100, 140), np.uint8)
        mask[10:90, 12:128] = 255
        cv2.rectangle(image, (12, 10), (127, 89), (15, 15, 15), 4)
        image[38:68, 12:42] = (25, 25, 25)

        cleanup, diagnostic = extract_coastline_cleanup_mask(image, mask)

        self.assertGreater(cleanup[50, 13], 0)
        self.assertEqual(cleanup[50, 30], 0)
        self.assertLess(diagnostic["band_width_px"], 18)

    def test_river_cleanup_adds_only_nearby_dark_fringe(self):
        image = np.full((80, 120, 3), 235, np.uint8)
        mask = np.full((80, 120), 255, np.uint8)
        centerline = np.zeros((80, 120), np.uint8)
        centerline[40, 15:105] = 255
        # Three-pixel black river centered on the supported path.
        image[39:42, 15:105] = (25, 25, 25)
        # Saturated thematic colour immediately beside it must not expand mask.
        image[42, 35:85] = (45, 45, 235)

        cleanup, diagnostic = extract_river_cleanup_mask(image, mask, centerline)

        self.assertGreater(cleanup[39, 50], 0)
        self.assertGreater(cleanup[41, 50], 0)
        self.assertEqual(cleanup[42, 50], 0)
        self.assertEqual(diagnostic["centerline_pixels"], 90)
        self.assertGreater(diagnostic["fringe_pixels"], 0)

    def test_river_cleanup_does_not_invent_geometry_without_supported_pixels(self):
        image = np.full((40, 60, 3), 20, np.uint8)
        mask = np.full((40, 60), 255, np.uint8)

        cleanup, diagnostic = extract_river_cleanup_mask(
            image, mask, np.zeros((40, 60), np.uint8))

        self.assertFalse(cleanup.any())
        self.assertEqual(diagnostic["pixels"], 0)

    def test_render_lines_preview_keeps_map_coordinates_and_kind_colours(self):
        mask = np.zeros((40, 60), np.uint8)
        mask[4:36, 5:55] = 255
        records = [
            {"kind": "river", "points": [[10, 12], [30, 12], [45, 20]]},
            {"kind": "border_or_coast", "points": [[15, 28], [45, 28]]},
        ]

        preview = render_lines_preview(records, mask)

        self.assertEqual(preview.shape, (40, 60, 3))
        self.assertTrue(np.array_equal(preview[0, 0], [255, 255, 255]))
        self.assertTrue(np.array_equal(preview[12, 20], LINE_PREVIEW_COLORS["river"]))
        self.assertTrue(np.array_equal(
            preview[28, 30], LINE_PREVIEW_COLORS["border_or_coast"]))

    def test_cartographic_extractor_rejects_coloured_strips(self):
        image = np.full((120, 160, 3), 245, np.uint8)
        mask = np.zeros((120, 160), np.uint8)
        mask[8:112, 10:150] = 255
        # A narrow thematic region: elongated, but saturated purple rather
        # than neutral cartographic ink.
        cv2.line(image, (25, 35), (135, 35), (145, 80, 170), 3)
        # A genuine black river with a small precise-text interruption.
        cv2.line(image, (25, 70), (135, 70), (25, 25, 25), 2)
        text_mask = np.zeros_like(mask)
        text_mask[67:74, 77:83] = 255
        labels = [{"kind": "river_label", "text": "Test River",
                   "box": [68, 60, 92, 80]}]
        _, records, diagnostic = extract_cartographic_lines(
            image, mask, text_mask, _semantics(["river", "coastline"]), labels)

        river_points = [point for record in records if record["kind"] == "river"
                        for point in record["points"]]
        self.assertTrue(river_points)
        self.assertTrue(all(abs(point[1] - 70) <= 2 for point in river_points))
        self.assertGreaterEqual(diagnostic["boundary_features"], 1)
        self.assertEqual(diagnostic["river_label_seeds"], 1)
        self.assertGreaterEqual(diagnostic["river_features"], 1)
        self.assertTrue(all(record.get("source") == "image_processing"
                            for record in records if record["kind"] == "river"))

    def test_unconfirmed_interior_lines_are_omitted_without_river_labels(self):
        image = np.full((80, 120, 3), 245, np.uint8)
        mask = np.zeros((80, 120), np.uint8)
        mask[5:75, 5:115] = 255
        cv2.line(image, (15, 40), (105, 40), (20, 20, 20), 2)

        _, records, diagnostic = extract_cartographic_lines(
            image, mask, np.zeros_like(mask), _semantics(["river"]), [])

        self.assertFalse(any(record["kind"] == "river" for record in records))
        self.assertTrue(diagnostic["omitted_unconfirmed_interior"])

    def test_dark_interior_stroke_is_not_inferred_without_river_semantics(self):
        image = np.full((80, 100, 3), 245, np.uint8)
        mask = np.zeros((80, 100), np.uint8)
        mask[5:75, 5:95] = 255
        cv2.line(image, (15, 40), (85, 40), (20, 20, 20), 2)

        _, records, diagnostic = extract_cartographic_lines(
            image, mask, np.zeros_like(mask), _semantics(["coastline"]), [])

        self.assertFalse(any(record["kind"] == "river" for record in records))
        self.assertEqual(diagnostic["river_features"], 0)

    def test_unseeded_dark_border_ink_becomes_lines_but_broad_fill_does_not(self):
        label_map = np.zeros((100, 160), np.int16)
        cv2.line(label_map, (10, 30), (145, 30), 1, 3)
        cv2.line(label_map, (80, 15), (80, 75), 1, 3)
        label_map[55:90, 110:150] = 1
        seeds = [{
            "label": "mapped class", "rgb": [220, 190, 50],
            "is_thematic": True, "source": "legend",
        }, {
            "label": "unlabelled: black", "rgb": [15, 18, 17],
            "is_thematic": False, "source": "unseeded",
        }]

        ink, records, diagnostic = extract_semantic_boundary_ink(
            label_map, seeds, _semantics(["border", "coastline"]))

        self.assertGreater(ink[30, 40], 0)
        self.assertEqual(ink[70, 130], 0)
        self.assertGreater(diagnostic["semantic_ink_pixels"], 0)
        self.assertTrue(records)
        self.assertTrue(all(record["kind"] == "border_or_coast"
                            for record in records))

    def test_unseeded_light_graticule_is_removed_from_area_classes(self):
        label_map = np.zeros((100, 160), np.int16)
        for x in (35, 80, 125):
            cv2.line(label_map, (x, 5), (x, 94), 1, 2)
        for y in (30, 65):
            cv2.line(label_map, (5, y), (154, y), 1, 2)
        seeds = [{
            "label": "mapped class", "rgb": [30, 155, 137],
            "is_thematic": True, "source": "legend",
        }, {
            "label": "unlabelled: cream", "rgb": [183, 208, 199],
            "is_thematic": False, "source": "unseeded",
        }]

        ink, records, _ = extract_semantic_boundary_ink(
            label_map, seeds, _semantics(["graticule"]))

        self.assertGreater(ink[30, 35], 0)
        self.assertTrue(records)
        self.assertTrue(all(record["kind"] == "graticule" for record in records))


if __name__ == "__main__":
    unittest.main()
