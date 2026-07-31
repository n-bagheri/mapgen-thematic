import unittest

import numpy as np

from mapgen.textdetect import extract_strokes


class TextDetectionTests(unittest.TestCase):
    def test_extract_strokes_can_convert_input_to_lab(self):
        image = np.zeros((20, 20, 3), dtype=np.uint8)

        result = extract_strokes(image, [2, 2, 18, 18])

        self.assertEqual(result, {"found": False})


if __name__ == "__main__":
    unittest.main()
