import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from mapgen.patterns import (GROUPS, ORDERED_RAMPS, PATTERNS, haptic_distance,
                             haptic_embeddings, optimize_adjacent_pattern_variants,
                             pick_pattern, render_pattern)
from pattern_library import PatternLibrary, mm_to_pt, pt_to_mm


PATTERN_DIRECTORY = Path(__file__).resolve().parents[1] / "pattern_library"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"


class PatternLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.library = PatternLibrary(PATTERN_DIRECTORY)

    def test_loads_all_eight_patterns_by_filename_stem(self):
        self.assertEqual(
            self.library.names,
            (
                "01_noise_dots",
                "01_noise_splash",
                "02_grid_checkers",
                "02_grid_dots",
                "03_lines_diagonal",
                "03_lines_vertical",
                "04_waves_sine",
                "04_waves_triangle",
            ),
        )

    def test_preserves_complete_non_uniform_and_rotated_transforms(self):
        expected = {
            "03_lines_diagonal": (
                "translate(9737.9941 -8098.43113) rotate(-45) scale(1.5732)"
            ),
            "03_lines_vertical": (
                "translate(2971.5157 -19914.21654) rotate(-90) scale(1.3984)"
            ),
            "04_waves_sine": (
                "translate(4234.80779 8821.31911) scale(1.63519 2.11612)"
            ),
            "04_waves_triangle": (
                "translate(6794.91427 -217.54938) scale(2 1.05)"
            ),
        }
        for name, transform in expected.items():
            with self.subTest(name=name):
                asset = self.library.get(name)
                self.assertEqual(asset.pattern_transform, transform)
                self.assertEqual(asset.pattern_xml.get("patternTransform"), transform)

    def test_reads_tile_metadata_and_carrier_opacity(self):
        asset = self.library.get("02_grid_dots")
        self.assertEqual(asset.width, 17.0)
        self.assertEqual(asset.height, 17.0)
        self.assertEqual(asset.view_box, "0 0 17 17")
        self.assertEqual(asset.pattern_units, "userSpaceOnUse")
        self.assertEqual(asset.fill_opacity, ".9")
        self.assertIsNone(asset.opacity)

    def test_copy_to_defs_uses_deep_copy_and_collision_safe_id(self):
        defs = ET.Element(f"{{{SVG_NAMESPACE}}}defs")
        first_fill = self.library.copy_to_defs("03_lines_diagonal", defs)
        second_fill = self.library.copy_to_defs("03_lines_diagonal", defs)

        self.assertEqual(first_fill, "url(#pattern_03_lines_diagonal)")
        self.assertEqual(second_fill, "url(#pattern_03_lines_diagonal_2)")
        self.assertEqual(len(defs), 2)
        self.assertIsNot(defs[0], self.library.get("03_lines_diagonal").pattern_xml)
        self.assertEqual(
            defs[0].get("patternTransform"),
            "translate(9737.9941 -8098.43113) rotate(-45) scale(1.5732)",
        )

        defs[0].set("patternTransform", "changed")
        self.assertNotEqual(
            defs[0].get("patternTransform"),
            self.library.get("03_lines_diagonal").pattern_transform,
        )

    def test_point_millimetre_conversions(self):
        self.assertAlmostEqual(mm_to_pt(25.4), 72.0)
        self.assertAlmostEqual(pt_to_mm(72.0), 25.4)
        self.assertAlmostEqual(pt_to_mm(mm_to_pt(123.456)), 123.456)

    def test_step7_registers_svg_assets_plus_plain_and_black(self):
        self.assertEqual(
            set(PATTERNS),
            set(self.library.names) | {"plain", "solid_black"},
        )
        self.assertEqual(GROUPS["waves"], ["04_waves_sine", "04_waves_triangle"])
        self.assertEqual(ORDERED_RAMPS[1], ["solid_black"])
        self.assertEqual(ORDERED_RAMPS[5][0], "plain")

    def test_ordered_ramps_never_repeat_a_regular_pattern_family(self):
        for class_count, ramp in ORDERED_RAMPS.items():
            with self.subTest(class_count=class_count):
                regular_groups = [
                    PATTERNS[pattern]["group"]
                    for pattern in ramp
                    if PATTERNS[pattern]["group"] not in {"none", "solids"}
                ]
                self.assertEqual(len(regular_groups), len(set(regular_groups)))

    def test_haptic_distance_uses_embedding_coordinates(self):
        embeddings = haptic_embeddings()
        self.assertEqual(embeddings["04_waves_sine"], (1.3695884, 1.3522813))
        self.assertAlmostEqual(
            haptic_distance("04_waves_sine", "01_noise_dots"),
            np.hypot(1.3695884 - -1.0274456, 1.3522813 - -1.1219846),
        )

    def test_noise_choice_maximizes_distance_from_water_sine(self):
        choice = pick_pattern("dots", ["04_waves_sine"])
        distances = {
            pattern: haptic_distance(pattern, "04_waves_sine")
            for pattern in GROUPS["dots"]
        }
        self.assertEqual(choice, "01_noise_dots")
        self.assertEqual(distances[choice], max(distances.values()))

    def test_global_optimizer_scores_all_three_adjacent_regions_simultaneously(self):
        # Three pairwise-adjacent regions meeting like the A/B/C example.
        group_map = np.array([
            [0, 0, 1, 1],
            [0, 0, 1, 1],
            [0, 2, 2, 1],
            [2, 2, 2, 2],
        ], dtype=np.int32)
        candidates = {
            0: ("04_waves_sine",),
            1: ("03_lines_vertical", "03_lines_diagonal"),
            2: ("01_noise_dots", "01_noise_splash"),
        }

        assignment, audit = optimize_adjacent_pattern_variants(group_map, candidates)

        self.assertEqual(assignment, {
            0: "04_waves_sine",
            1: "03_lines_vertical",
            2: "01_noise_dots",
        })
        self.assertEqual(audit["combinations_evaluated"], 4)
        self.assertEqual(audit["eligible_pattern_adjacencies"], 3)
        chosen_distances = [edge["distance"] for edge in audit["edges"]]
        self.assertAlmostEqual(audit["minimum_distance"], min(chosen_distances))

    def test_plain_and_black_adjacencies_do_not_affect_global_score(self):
        group_map = np.array([
            [0, 0, 1, 1],
            [2, 0, 3, 1],
            [2, 3, 3, 1],
        ], dtype=np.int32)
        candidates = {
            0: ("04_waves_sine",),
            1: ("plain",),
            2: ("solid_black",),
            3: ("01_noise_dots", "01_noise_splash"),
        }

        assignment, audit = optimize_adjacent_pattern_variants(group_map, candidates)

        self.assertEqual(assignment[3], "01_noise_dots")
        self.assertEqual(audit["eligible_pattern_adjacencies"], 1)
        self.assertEqual(audit["excluded_plain_or_black_adjacencies"], 4)
        excluded = [edge for edge in audit["edges"] if not edge["eligible"]]
        self.assertTrue(all(edge["distance"] is None for edge in excluded))

    def test_step7_renders_every_illustrator_pattern(self):
        for name in self.library.names:
            with self.subTest(name=name):
                rendered = render_pattern(name, (120, 160), px_per_mm=5.0)
                self.assertEqual(rendered.shape, (120, 160))
                self.assertEqual(rendered.dtype, np.uint8)
                self.assertTrue(np.any(rendered < 128))
                self.assertTrue(np.any(rendered == 255))

    def test_special_fills_and_grid_dot_carrier_opacity(self):
        plain = render_pattern("plain", (20, 30), px_per_mm=5.0)
        black = render_pattern("solid_black", (20, 30), px_per_mm=5.0)
        grid_dots = render_pattern("02_grid_dots", (120, 160), px_per_mm=5.0)

        self.assertTrue(np.all(plain == 255))
        self.assertTrue(np.all(black == 0))
        # The SVG's carrier fill-opacity=.9 makes its fully covered pixels 10%
        # white rather than zero; Step 7 must apply that exact carrier setting.
        self.assertGreaterEqual(int(grid_dots.min()), 24)
        self.assertLessEqual(int(grid_dots.min()), 27)


if __name__ == "__main__":
    unittest.main()
