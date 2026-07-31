import unittest

from mapgen.output_spec import OutputSpec, PhysicalConstants


class OutputSpecValidationTests(unittest.TestCase):
    def test_defaults_are_valid_and_five_is_only_a_capacity(self):
        spec = OutputSpec()
        spec.validate()
        self.assertEqual(spec.texture_slots(water_present=False), 5)
        self.assertEqual(spec.texture_slots(water_present=True), 4)

    def test_rejects_texture_counts_other_than_five(self):
        for value in (1, 4, 6, 100):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "must be exactly 5"
            ):
                OutputSpec(
                    constants=PhysicalConstants(max_area_textures=value)
                ).validate()

    def test_rejects_non_integer_texture_count(self):
        for value in (5.0, True, "5"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "must be a whole number"
            ):
                OutputSpec(
                    constants=PhysicalConstants(max_area_textures=value)
                ).validate()

    def test_rejects_negative_margin(self):
        with self.assertRaisesRegex(ValueError, "must be non-negative"):
            OutputSpec(margin_mm=-1).validate()

    def test_rejects_non_positive_page_dimensions(self):
        for width, height in ((0, 297), (-1, 297), (210, 0), (210, -1)):
            with self.subTest(width=width, height=height), self.assertRaisesRegex(
                ValueError, "must be positive"
            ):
                OutputSpec(page_width_mm=width, page_height_mm=height).validate()

    def test_rejects_unknown_braille_standard(self):
        with self.assertRaisesRegex(ValueError, "braille_standard must be one of"):
            OutputSpec(braille_standard="made-up-standard").validate()


if __name__ == "__main__":
    unittest.main()
