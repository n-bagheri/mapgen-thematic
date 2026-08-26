import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
from pydantic import ValidationError

from mapgen.isolate import (
    LAYOUT_PROMPT,
    LayoutResponseError,
    MapLayout,
    _layout_furniture,
    _resolve_legend_box,
    _validate_layout_root,
    _colorbar_split_is_papery,
    drop_frame_label_boxes,
    border_median_lab,
    detect_layout,
    detect_swatches,
    legend_paper_lab,
    prepare_text_input,
    recover_legend_box,
    refine_map_mask,
    sample_swatch,
    to_lab,
)
from mapgen.semantics import MapSemantics


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

    def test_original_component_flow_does_not_apply_an_edge_ruler_heuristic(self):
        img = np.full((300, 400, 3), 255, np.uint8)
        cv2.rectangle(img, (70, 70), (260, 240), (40, 160, 80), -1)
        # The baseline keeps all qualifying connected components in a broad
        # geographic band; furniture removal is driven by explicit boxes.
        cv2.rectangle(img, (382, 35), (384, 270), (200, 40, 40), -1)

        mask, _, warnings = refine_map_mask(
            img,
            map_boxes=[(45, 25, 390, 280)],
            exclude_boxes=[],
        )

        self.assertGreater(mask[150, 150], 0)
        self.assertGreater(mask[150, 383], 0)
        self.assertFalse(any("edge ruler/tick" in warning for warning in warnings))


    def test_a_tight_vlm_box_stays_the_connected_component_seed_boundary(self):
        """The baseline refines components inside the padded model box and
        reports substantial content outside it instead of growing the mask."""
        page = np.full((400, 600, 3), 255, np.uint8)
        # One printed panel with varied content (sea and land bands).
        for x in range(60, 560, 20):
            colour = (200, 120, 40) if (x // 20) % 2 else (60, 160, 60)
            cv2.rectangle(page, (x, 40), (x + 19, 359), colour, -1)
        # A detached decoration that no box seeds: it must stay excluded.
        cv2.rectangle(page, (10, 370), (40, 395), (0, 0, 200), -1)

        # Deliberately narrow box covering only the left half of the panel.
        mask, _, warnings = refine_map_mask(page, [(80, 60, 300, 340)], [])

        self.assertGreater(mask[100, 100], 0)
        self.assertEqual(mask[100, 500], 0)
        self.assertTrue((mask[370:395, 10:40] == 0).all(),
                        "detached decorations must not be pulled in")
        self.assertTrue(any("outside the VLM map boxes" in w for w in warnings), warnings)


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

    def test_joined_vertical_swatch_stack_beats_text_fragments(self):
        legend = np.full((190, 180, 3), 245, np.uint8)
        # Adjacent black borders deliberately make this a single connected
        # component, as happens in compact scanned paper legends.
        colors_bgr = [(61, 159, 85), (137, 161, 202), (187, 237, 239)]
        for index, color in enumerate(colors_bgr):
            y = 44 + index * 16
            cv2.rectangle(legend, (36, y), (61, y + 16), color, -1)
            cv2.rectangle(legend, (36, y), (61, y + 16), (20, 20, 20), 1)
        # These text fragments used to win the generic contour filtering.
        cv2.putText(legend, "Over 70", (70, 57), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (30, 30, 30), 1, cv2.LINE_AA)
        cv2.putText(legend, "50 to 70", (70, 73), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (30, 30, 30), 1, cv2.LINE_AA)
        cv2.putText(legend, "Under 50", (70, 89), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (30, 30, 30), 1, cv2.LINE_AA)

        rects, warnings = detect_swatches(
            legend, expected=3,
            labels=["Over 70", "50 to 70", "Under 50"], ordered=True,
        )

        self.assertEqual(len(rects), 3, warnings)
        self.assertTrue(any("vertically joined" in warning for warning in warnings))
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertTrue(all(max(rgb) - min(rgb) > 20 for rgb in sampled))
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

    def test_vertical_colour_bar_is_split_into_its_cells(self):
        """Hypsometric and rainfall legends stack their ramp vertically, and an
        inset one is drawn straight onto the map with no background to
        threshold against, so the ramp has to be found by its own shape."""
        legend = np.zeros((260, 150, 3), np.uint8)
        legend[:] = (90, 140, 90)                      # map showing through
        for row in range(240):
            shade = row / 239.0
            colour = (int(40 + 60 * shade), int(200 - 120 * shade), int(60 + 150 * shade))
            cv2.line(legend, (10, 10 + row), (34, 10 + row), colour, 1)

        rects, warnings = detect_swatches(
            legend, expected=6,
            labels=["2000", "1500", "1000", "500", "200", "0"],
            ordered=True,
        )

        self.assertEqual(len(rects), 6, warnings)
        self.assertTrue(any("vertical colour bar" in w for w in warnings), warnings)
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertEqual(len({tuple(rgb) for rgb in sampled}), 6)

    def test_a_descending_scale_keeps_reading_order(self):
        """Labels transcribed 2000..0 are already top-down, so the cells must
        not be flipped; flipping would invert the whole palette."""
        from mapgen.isolate import _ramp_runs_upward
        self.assertFalse(_ramp_runs_upward(["2000", "1500", "1000", "500"]))
        self.assertTrue(_ramp_runs_upward(["0-100 m", "100-200 m", "200-300 m"]))
        self.assertFalse(_ramp_runs_upward(["forest", "water", "urban"]))

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

    def test_compact_grid_splits_joined_cells_in_each_column(self):
        legend = np.full((130, 170, 3), 245, np.uint8)
        colours = [
            (10 + index * 7, 20 + index * 11, 230 - index * 9)
            for index in range(16)
        ]
        for column in range(2):
            for row in range(8):
                x, y = 16 + column * 76, 22 + row * 12
                colour = colours[column * 8 + row]
                # Adjacent borders touch, producing joined saturation blobs.
                cv2.rectangle(legend, (x, y), (x + 10, y + 10), colour, -1)
                cv2.rectangle(legend, (x, y), (x + 10, y + 10), (20, 20, 20), 1)

        rects, warnings = detect_swatches(
            legend, expected=16, labels=[str(index) for index in range(16)], ordered=True,
        )

        self.assertEqual(len(rects), 16, warnings)
        self.assertTrue(any("compact 16-swatch" in warning for warning in warnings))
        sampled = [sample_swatch(legend, rect)[0] for rect in rects]
        self.assertEqual(len({tuple(rgb) for rgb in sampled}), 16)


class ColumnGridReconstructionTests(unittest.TestCase):
    """A single-column legend can be recovered from its grid structure when
    the contour pass loses swatches to text fusion and gains text fragments."""

    @staticmethod
    def _legend():
        """13 fill rows at pitch 19 under one outline-only row, like a real
        land-cover legend; text is simulated by thin blobs beside the column."""
        legend = np.full((300, 150, 3), 255, np.uint8)
        cv2.rectangle(legend, (3, 20), (39, 36), (0, 0, 0), 1)   # outline row
        colors = [(30 + 15 * i, 90, 220 - 12 * i) for i in range(13)]
        for row, color in enumerate(colors):
            y = 41 + row * 19
            cv2.rectangle(legend, (3, y), (39, y + 16), color, -1)
        return legend

    def test_grid_completion_recovers_lost_swatches_and_drops_fragments(self):
        from mapgen.isolate import _column_grid_swatches
        legend = self._legend()
        found = [(3, 41 + row * 19, 36, 16) for row in range(13) if row not in (5, 6, 7)]
        fragments = [(45, 46, 40, 9), (45, 103, 50, 9), (90, 103, 54, 9)]
        result = _column_grid_swatches(legend, 13, found + fragments)
        self.assertIsNotNone(result)
        rects, warnings = result
        self.assertEqual(len(rects), 13)
        self.assertEqual([rect[1] for rect in rects],
                         [41 + row * 19 for row in range(13)])
        self.assertTrue(any("grid structure" in warning for warning in warnings))

    def test_an_outline_only_row_is_never_reconstructed_as_a_swatch(self):
        """Expecting one more class than the column holds must fail, not
        promote the paper-interior outline row above the column."""
        from mapgen.isolate import _column_grid_swatches
        legend = self._legend()
        found = [(3, 41 + row * 19, 36, 16) for row in range(13) if row not in (5,)]
        self.assertIsNone(_column_grid_swatches(legend, 14, found))



class LegendPaperEstimationTests(unittest.TestCase):
    """A legend box cut from a map sheet often catches map content on one or
    two sides.  Pooling all four borders then lands between paper and map, and
    every paper pixel reads as foreground (the Africa ethnolinguistic sheet)."""

    @staticmethod
    def _legend_with_sea_on_two_sides():
        legend = np.full((400, 300, 3), 250, np.uint8)   # near-white paper
        legend[:26, :] = (240, 195, 165)                 # sea along the top
        legend[:, :26] = (240, 195, 165)                 # sea along the left
        colors_bgr = [(30, 60, 210), (40, 180, 70), (210, 120, 30), (60, 60, 60)]
        for i, color in enumerate(colors_bgr):
            y = 60 + i * 70
            cv2.rectangle(legend, (60, y), (110, y + 34), color, -1)
        return legend, colors_bgr

    def test_contaminated_sides_are_dropped_from_the_paper_estimate(self):
        legend, _ = self._legend_with_sea_on_two_sides()
        lab = to_lab(legend)
        strip = max(2, min(legend.shape[:2]) // 50)
        pooled = border_median_lab(lab, strip)
        paper = legend_paper_lab(lab, strip)
        white = to_lab(np.full((1, 1, 3), 250, np.uint8))[0, 0]

        self.assertGreater(float(np.linalg.norm(pooled - white)), 8.0)
        self.assertLess(float(np.linalg.norm(paper - white)), 2.0)

    def test_swatches_survive_map_content_bleeding_into_the_crop(self):
        legend, colors_bgr = self._legend_with_sea_on_two_sides()
        labels = ["one", "two", "three", "four"]

        rects, _ = detect_swatches(legend, len(labels), labels=labels)

        self.assertEqual(len(rects), len(colors_bgr))
        sampled = [tuple(sample_swatch(legend, rect)[0]) for rect in rects]
        self.assertEqual(
            sorted(sampled), sorted(tuple(c[::-1]) for c in colors_bgr))

    def test_an_inset_legend_with_no_paper_keeps_the_border_median(self):
        """All four sides agree on the map colour, so nothing is dropped."""
        legend = np.full((300, 240, 3), (150, 190, 120), np.uint8)
        cv2.rectangle(legend, (40, 40), (90, 74), (30, 60, 210), -1)
        cv2.rectangle(legend, (40, 120), (90, 154), (210, 120, 30), -1)
        lab = to_lab(legend)
        strip = max(2, min(legend.shape[:2]) // 50)

        self.assertLess(
            float(np.linalg.norm(
                legend_paper_lab(lab, strip) - border_median_lab(lab, strip))),
            0.5)



def _table_legend(rows=12, swatch=(24, 13), body_rows=3):
    """A legend keyed inside a dense data table (the Africa ethnolinguistic
    sheet keys 15 families inside a 1200-row ethnographic table).

    Body text is drawn as the solid blobs the detector actually sees: at scan
    resolution the letters of a word merge under the colour threshold into one
    filled, uniform, swatch-shaped component about half a swatch tall.  Those
    blobs outnumber the swatches roughly ten to one, which is what pulls the
    median candidate size down to a letter.
    """
    page = np.full((760, 560, 3), 252, np.uint8)
    palette = [(40, 60, 200), (60, 170, 80), (200, 130, 40), (150, 80, 170),
               (70, 170, 200), (110, 110, 60), (200, 90, 140), (90, 140, 60),
               (170, 100, 200), (60, 100, 150), (140, 160, 70), (200, 60, 60)]
    ink = (38, 38, 38)
    sw, sh = swatch
    for i in range(rows):
        y = 30 + i * 60
        cv2.rectangle(page, (40, y), (40 + sw, y + sh), palette[i % len(palette)], -1)
        cv2.rectangle(page, (40, y), (40 + sw, y + sh), (60, 60, 60), 1)
        # the family heading beside the swatch, then the body rows beneath it
        cv2.rectangle(page, (76, y + 2), (76 + 58, y + 2 + 7), ink, -1)
        for k in range(1, body_rows + 1):
            ty = y + 2 + k * 13
            for cx, cw in ((76, 26), (112, 17), (140, 30), (182, 22), (216, 26)):
                cv2.rectangle(page, (cx, ty), (cx + cw, ty + 6), ink, -1)
    return page, rows


class DenseTableLegendTests(unittest.TestCase):
    """The median candidate is the right swatch-size consensus only while
    swatches outnumber text.  A legend printed inside a data table inverts
    that, and every real swatch was then discarded as an oversized outlier."""

    def test_swatches_are_found_among_far_more_text_than_swatches(self):
        page, rows = _table_legend()

        rects, _ = detect_swatches(page, rows, labels=["r%d" % i for i in range(rows)])

        self.assertEqual(len(rects), rows)
        # every rect must be a swatch box, not a word: swatches share one size
        self.assertLessEqual(max(r[3] for r in rects) - min(r[3] for r in rects), 3)
        self.assertTrue(all(r[0] < 70 for r in rects), "rects drifted into the text column")

    def test_a_run_of_body_text_is_not_split_into_a_swatch(self):
        """_split_merged reconstructs a leading rectangle from a wide blob; a
        text run is wider than a swatch and about half its height."""
        page, rows = _table_legend()
        # a long unbroken rule the same height as the body text
        cv2.rectangle(page, (76, 700), (250, 707), (40, 40, 40), -1)

        rects, _ = detect_swatches(page, rows, labels=["r%d" % i for i in range(rows)])

        self.assertEqual(len(rects), rows)
        self.assertFalse(any(r[1] >= 695 for r in rects))


class LegendBoxRecoveryTests(unittest.TestCase):
    """A legend printed as a data table comes back from the layout call under
    `other`, so Step 2 saw no legend box at all and stopped."""

    @staticmethod
    def _layout(other_boxes):
        return MapLayout.model_validate({
            "map_areas": [{"box_2d": [0, 0, 500, 500], "label": "mainland"}],
            "legend": None, "title": None, "scale_bar": None, "north_arrow": None,
            "other": other_boxes,
        })

    def _page(self):
        page = np.full((1000, 1200, 3), 252, np.uint8)
        table, rows = _table_legend()
        page[120:880, 40:600] = table
        return page, rows

    def test_the_legend_is_recovered_from_an_other_box(self):
        page, rows = self._page()
        layout = self._layout([
            {"box_2d": [120, 33, 880, 500], "label": "ethnographic_table"},
            {"box_2d": [10, 10, 40, 60], "label": "logo"},
        ])

        box, warnings = recover_legend_box(page, layout, rows, False)

        self.assertIsNotNone(box)
        self.assertTrue(any("ethnographic_table" in w for w in warnings))
        x0, y0, x1, y1 = box
        rects, _ = detect_swatches(page[y0:y1, x0:x1], rows,
                                   labels=["r%d" % i for i in range(rows)])
        self.assertEqual(len(rects), rows)

    def test_a_recovery_search_region_is_not_furniture(self):
        page, rows = self._page()
        layout = self._layout([
            {"box_2d": [120, 33, 880, 500], "label": "ethnographic_table"},
            {"box_2d": [10, 10, 40, 60], "label": "logo"},
        ])
        recovered, _ = recover_legend_box(page, layout, rows, False)

        furniture = _layout_furniture(layout, 1200, 1000, recovered)

        self.assertEqual([name for name, _ in furniture], ["other:logo"])

    def test_a_box_that_is_not_a_legend_is_never_guessed(self):
        page, rows = self._page()
        layout = self._layout([{"box_2d": [10, 10, 60, 120], "label": "notes"}])

        box, warnings = recover_legend_box(page, layout, rows, False)

        self.assertIsNone(box)
        self.assertEqual(warnings, [])

    def test_nothing_is_recovered_when_step_1_transcribed_no_entries(self):
        """expected == 0 would otherwise match any empty box."""
        page, _ = self._page()
        layout = self._layout([{"box_2d": [120, 33, 880, 500], "label": "table"}])

        self.assertEqual(recover_legend_box(page, layout, 0, False), (None, []))

    @staticmethod
    def _semantics(labels):
        return MapSemantics.model_validate({
            "map_type": "area_class_chorochromatic", "in_scope": True,
            "data_ordering": "qualitative", "map_language": "English",
            "subject": "synthetic", "description": "synthetic", "title": None,
            "legend_present": True, "legend_title": None,
            "legend_entries": [{
                "label": label, "color_hint": "green", "is_thematic": True,
            } for label in labels],
            "water_present": False,
            "thematic_classes": [{
                "label": label, "priority": index + 1,
                "approx_area_share_percent": 1,
            } for index, label in enumerate(labels)],
            "non_thematic": [], "lines": [],
            "overlay_text": {
                "has_city_labels": False, "capital_city": None,
                "has_region_labels": False, "has_line_labels": False, "notes": "",
            },
        })

    def test_a_fresh_step1_reading_can_recover_a_table_the_first_read_missed(self):
        layout = self._layout([
            {"box_2d": [120, 33, 880, 500], "label": "ethnographic_table"},
        ])
        initial = self._semantics(["one", "two"])
        fresh = self._semantics([f"class {index}" for index in range(15)])
        recovered_box = (12, 1496, 895, 2751)

        with patch("mapgen.isolate.recover_legend_box", side_effect=[
                (None, []),
                (recovered_box, ["recovered the ethnographic table"]),
        ]) as recover:
            box, recovered, resolved, warnings = _resolve_legend_box(
                np.zeros((100, 100, 3), np.uint8), layout, initial,
                redraw=lambda: fresh, retries=2)

        self.assertEqual(box, recovered_box)
        self.assertTrue(recovered)
        self.assertIs(resolved, fresh)
        self.assertEqual(recover.call_args_list[0].args[2], 2)
        self.assertEqual(recover.call_args_list[1].args[2], 15)
        self.assertTrue(any("legend table recovered" in warning for warning in warnings))



class LayoutResponseShapeTests(unittest.TestCase):
    """The structured call returns one strict MapLayout at the document root."""

    BASE = {
        "map_areas": [{"box_2d": [0, 0, 961, 1000], "label": "mainland"}],
        "legend": None, "title": None, "scale_bar": None,
        "north_arrow": None, "other": [],
    }

    def test_a_one_item_root_list_is_unwrapped(self):
        layout = _validate_layout_root([{**self.BASE, "legend": {
            "box_2d": [538, 13, 975, 363], "label": "legend"}}])

        self.assertEqual(layout.legend.box_2d, [538, 13, 975, 363])

    def test_an_ambiguous_multi_item_root_list_is_rejected(self):
        with self.assertRaises(LayoutResponseError):
            _validate_layout_root([self.BASE, self.BASE])

    def test_a_non_object_root_list_item_is_rejected(self):
        with self.assertRaises(LayoutResponseError):
            _validate_layout_root(["not a layout"])

    def test_singular_furniture_fields_remain_strict_objects(self):
        with self.assertRaises(ValidationError):
            MapLayout.model_validate({
                **self.BASE,
                "legend": [{"box_2d": [538, 13, 975, 363], "label": "legend"}],
            })

    def test_the_documented_object_shape_is_untouched(self):
        layout = MapLayout.model_validate({
            **self.BASE, "legend": {"box_2d": [1, 2, 3, 4], "label": "plain"}})
        self.assertEqual(layout.legend.label, "plain")


class NativeLayoutCallTests(unittest.TestCase):
    def test_restored_prompt_is_exact(self):
        self.assertEqual(LAYOUT_PROMPT, """Locate the layout elements of this thematic map image. Return bounding boxes
as [y_min, x_min, y_max, x_max], normalized to 0-1000.

- map_areas: return ONE SEPARATE BOX for EACH geographically detached component
  of the mapped territory. Include the mainland, islands, overseas territories,
  and displaced territorial components, as well as water shown as part of the
  map picture. Keep each box tight and exclude surrounding page margins.
- Detached components that use the same thematic colors, legend, and symbology
  as the main territory belong in map_areas, NOT in other. This remains true
  when an island is moved closer to the mainland for page layout. For example,
  Corsica on a thematic map of France must be a separate map_areas entry.
- Before calling a detached colored shape an inset, compare its colors and
  symbols with the main map. If it uses the same thematic legend, classify it
  as a detached mapped-territory component. Never return it in other.
- Call something an inset map only when it is an independently framed secondary
  map, normally with its own scale, title, locator context, or different extent.
  An unframed detached island is not an inset.
- legend: tight box around the legend (color swatches + their labels + the
  legend heading). null if there is no legend.
- A legend is whatever keys the map's colors, however it is printed. It is
  still the legend when the swatches sit inside a large data table with many
  columns and hundreds of rows of supporting detail: box the whole table.
  Return it as legend, never in other, whenever it carries color swatches
  that match colors used on the map.
- title, scale_bar, north_arrow: when present, else null.
- other: inset maps, notes, logos, coordinate labels or anything else that is
  not map content, each with a short label.
""")

    def test_detect_layout_uses_native_structured_generation(self):
        native_layout = MapLayout.model_validate(LayoutResponseShapeTests.BASE)
        response = SimpleNamespace(parsed=native_layout, text=None)
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch("mapgen.isolate._ensure_api_key"), \
                patch("mapgen.isolate.encode_for_model",
                      return_value=(b"image", "image/png")), \
                patch("google.genai.Client", return_value=client) as client_type:
            result = detect_layout(Path("map.png"), model="gemini-test")

        self.assertIs(result, native_layout)
        client_type.assert_called_once()
        call = client.models.generate_content.call_args
        self.assertEqual(call.kwargs["model"], "gemini-test")
        self.assertEqual(call.kwargs["contents"][1], LAYOUT_PROMPT)
        config = call.kwargs["config"]
        self.assertEqual(config.response_mime_type, "application/json")
        self.assertIs(config.response_schema, MapLayout)
        self.assertEqual(config.temperature, 0.0)
        client.close.assert_called_once()

    def test_detect_layout_without_selection_defaults_to_gemma(self):
        native_layout = MapLayout.model_validate(LayoutResponseShapeTests.BASE)
        response = SimpleNamespace(parsed=native_layout, text=None)
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch.dict("os.environ", {"GEMINI_MODEL": "gemini-2.5-flash"}), \
                patch("mapgen.isolate._ensure_api_key"), \
                patch("mapgen.isolate.encode_for_model",
                      return_value=(b"image", "image/png")), \
                patch("google.genai.Client", return_value=client):
            detect_layout(Path("map.png"))

        self.assertEqual(
            client.models.generate_content.call_args.kwargs["model"],
            "gemma-4-26b-a4b-it",
        )
        client.close.assert_called_once()

    def test_model_legend_box_is_unpadded_furniture(self):
        layout = MapLayout.model_validate({
            **LayoutResponseShapeTests.BASE,
            "legend": {"box_2d": [100, 200, 300, 400], "label": "legend"},
        })

        furniture = _layout_furniture(layout, w=1000, h=500)

        self.assertEqual(furniture, [("legend", (200, 50, 400, 150))])

    def test_detect_layout_retries_then_rejects_ambiguous_root_lists(self):
        response = SimpleNamespace(
            parsed=[LayoutResponseShapeTests.BASE, LayoutResponseShapeTests.BASE],
            text=None,
        )
        client = MagicMock()
        client.models.generate_content.return_value = response

        with patch("mapgen.isolate._ensure_api_key"), \
                patch("mapgen.isolate.encode_for_model",
                      return_value=(b"image", "image/png")), \
                patch("google.genai.Client", return_value=client):
            with self.assertRaises(LayoutResponseError):
                detect_layout(Path("map.png"), model="gemini-test")

        self.assertEqual(client.models.generate_content.call_count, 2)
        client.close.assert_called_once()



class FrameLabelBoxTests(unittest.TestCase):
    """The layout call sometimes scatters `coordinate_label` boxes over the
    mapped picture; a frame label by definition sits in the near-paper margin,
    so a box mostly covered by content contradicts its own claim."""

    @staticmethod
    def _page():
        page = np.full((600, 800, 3), 250, np.uint8)      # paper margin
        page[60:540, 80:720] = (200, 170, 120)            # map picture
        return page

    def test_a_frame_label_box_on_map_content_is_dropped(self):
        page = self._page()
        furniture = [
            ("other:coordinate_label", (10, 100, 40, 140)),    # margin: real
            ("other:coordinate_label", (300, 250, 360, 290)),  # mid-picture
        ]

        kept, warnings = drop_frame_label_boxes(page, furniture)

        self.assertEqual([n for n, _ in kept], ["other:coordinate_label"])
        self.assertEqual(kept[0][1], (10, 100, 40, 140))
        self.assertTrue(warnings)

    def test_logos_and_dedicated_boxes_on_content_are_never_touched(self):
        page = self._page()
        furniture = [
            ("legend", (100, 100, 400, 400)),
            ("title", (500, 80, 700, 140)),
            ("other:logo", (600, 450, 700, 520)),
        ]

        kept, warnings = drop_frame_label_boxes(page, furniture)

        self.assertEqual(kept, furniture)
        self.assertEqual(warnings, [])



class Step2GateSafetyTests(unittest.TestCase):
    """A bad Step 1 redraw (an out-of-scope draw on a re-run) must stop Step 2
    at the eligibility gate WITHOUT destroying the palette a previously
    completed run left on disk."""

    def test_an_out_of_scope_redraw_leaves_the_previous_palette_alone(self):
        import json, tempfile
        from pathlib import Path
        from mapgen.isolate import run_step2
        from mapgen.semantics import OutOfScopeMapError

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "synthetic"
            run_dir.mkdir(parents=True)
            (run_dir / "step1_semantics.json").write_text(json.dumps({
                "map_type": "choropleth", "in_scope": False,
                "data_ordering": "qualitative", "map_language": "English",
                "subject": "s", "description": "d", "title": None,
                "legend_present": True, "legend_title": None,
                "legend_entries": [], "water_present": False,
                "thematic_classes": [], "non_thematic": [], "lines": [],
                "overlay_text": {"has_city_labels": False, "capital_city": None,
                                 "has_region_labels": False,
                                 "has_line_labels": False, "notes": ""},
            }), encoding="utf-8")
            (run_dir / "classes.json").write_text('{"classes": []}', encoding="utf-8")
            (run_dir / "geometry.json").write_text('{}', encoding="utf-8")

            with self.assertRaises(OutOfScopeMapError):
                run_step2(Path(tmp) / "synthetic.png", runs_dir=runs)

            self.assertTrue((run_dir / "classes.json").exists())
            self.assertTrue((run_dir / "geometry.json").exists())



class ColorbarSplitValidationTests(unittest.TestCase):
    """A multi-column grid of discrete swatches can masquerade as one vertical
    color bar (china's 4x5 climate grid); force-splitting it into equal cells
    lands half of them on the paper gaps between swatches, and each of those
    was then reported as a bare-paper class."""

    def test_a_split_landing_on_paper_gaps_is_rejected(self):
        legend = np.full((200, 120, 3), 250, np.uint8)
        colors = [(30, 60, 210), (40, 180, 70), (210, 120, 30)]
        for i, color in enumerate(colors):     # swatches with paper gaps
            cv2.rectangle(legend, (10, 12 + i * 60), (40, 36 + i * 60), color, -1)
        cells = [(10, 12 + i * 30, 30, 28) for i in range(6)]  # every other on paper

        self.assertTrue(_colorbar_split_is_papery(legend, cells))

    def test_a_real_ramp_with_one_white_end_bin_is_accepted(self):
        legend = np.full((200, 120, 3), 250, np.uint8)
        ramp = [(255, 255, 255), (180, 220, 250), (90, 160, 240),
                (40, 90, 220), (30, 40, 160), (20, 10, 90)]
        for i, color in enumerate(ramp):
            cv2.rectangle(legend, (10, 10 + i * 30), (40, 40 + i * 30), color, -1)
        cells = [(10, 10 + i * 30, 30, 30) for i in range(6)]

        self.assertFalse(_colorbar_split_is_papery(legend, cells))


class RobustSwatchSamplingTests(unittest.TestCase):
    """The fill colour must survive a glyph printed inside the swatch
    (Germany's climate regions) and a hatched fill (Russia's districts)."""

    def test_a_black_glyph_inside_the_swatch_is_ignored(self):
        legend = np.full((60, 200, 3), 250, np.uint8)
        cv2.rectangle(legend, (10, 10), (50, 50), (60, 200, 250), -1)   # amber fill
        cv2.rectangle(legend, (18, 18), (42, 42), (0, 0, 0), -1)        # big black square

        rgb, _ = sample_swatch(legend, (10, 10, 41, 41))

        self.assertEqual(rgb, [250, 200, 60])

    def test_a_hatched_fill_samples_its_ink_not_the_paper(self):
        legend = np.full((60, 200, 3), 250, np.uint8)
        for x in range(10, 50, 4):                                        # orange hatching
            cv2.line(legend, (x, 10), (x, 50), (0, 140, 250), 2)

        rgb, _ = sample_swatch(legend, (10, 10, 41, 41))

        self.assertEqual(rgb, [250, 140, 0])

    def test_a_genuinely_black_swatch_stays_black(self):
        legend = np.full((60, 200, 3), 250, np.uint8)
        cv2.rectangle(legend, (10, 10), (50, 50), (0, 0, 0), -1)

        rgb, _ = sample_swatch(legend, (10, 10, 41, 41))

        self.assertEqual(rgb, [0, 0, 0])


class JoinedStackTests(unittest.TestCase):
    def test_a_stack_filling_the_whole_crop_is_split_into_its_cells(self):
        """The Australia land-cover screenshot's legend crop is nothing but
        the joined colour column; a 65%-of-height cap used to reject it."""
        from mapgen.isolate import detect_swatches
        colors = [(200, 200, 200), (250, 80, 160), (0, 240, 250), (40, 220, 60),
                  (30, 30, 230), (90, 90, 240), (150, 150, 250), (60, 240, 120)]
        cell = 30
        legend = np.full((cell * len(colors), 260, 3), 250, np.uint8)
        for i, color in enumerate(colors):
            legend[i * cell:(i + 1) * cell, 0:26] = color
            cv2.putText(legend, "Class name", (40, i * cell + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (30, 30, 30), 1)

        rects, _ = detect_swatches(legend, len(colors), labels=["c"] * len(colors))

        self.assertEqual(len(rects), len(colors))
        sampled = [tuple(sample_swatch(legend, r)[0]) for r in rects]
        self.assertEqual(sampled, [tuple(c[::-1]) for c in colors])


class ColourDerivedPaletteTests(unittest.TestCase):
    def test_dominant_fills_become_classes_and_ink_does_not(self):
        from mapgen.isolate import derive_palette_from_map
        page = np.full((400, 600, 3), 250, np.uint8)
        page[:, :200] = (60, 200, 250)      # amber third
        page[:, 200:400] = (80, 200, 60)    # green third
        page[:, 400:] = (230, 120, 40)      # blue third
        cv2.line(page, (0, 200), (600, 200), (0, 0, 0), 6)   # a black border
        mask = np.full((400, 600), 255, np.uint8)

        rows = derive_palette_from_map(page, mask)

        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["is_thematic"] and r["source"] == "map-colour" for r in rows))
        self.assertTrue(all(r["lab"][0] > 35 for r in rows), "border ink became a class")


if __name__ == "__main__":
    unittest.main()
