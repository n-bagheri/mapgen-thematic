import unittest

import numpy as np

from mapgen.segment import promote_large_unseeded
from mapgen.semantics import MapSemantics


def _semantics(water_present: bool) -> MapSemantics:
    return MapSemantics.model_validate({
        "map_type": "area_class_chorochromatic", "in_scope": True,
        "data_ordering": "qualitative", "map_language": "English",
        "subject": "s", "description": "d", "title": None,
        "legend_present": True, "legend_title": None, "legend_entries": [],
        "water_present": water_present, "thematic_classes": [],
        "non_thematic": [], "lines": [],
        "overlay_text": {"has_city_labels": False, "capital_city": None,
                         "has_region_labels": False, "has_line_labels": False,
                         "notes": ""},
    })


def _seed(label, lab, source="unseeded"):
    return {"label": label, "lab": np.float32(lab), "rgb": [0, 0, 0],
            "is_thematic": False, "priority": None, "source": source}


class PromoteLargeUnseededTests(unittest.TestCase):
    """A large coloured fill the legend never named is map content, not
    background: it must receive a texture rather than leave a hole."""

    def _map(self, shares):
        # 100x100 content; each seed index gets a horizontal band of `share`.
        mask = np.full((100, 100), 255, np.uint8)
        label_map = np.full((100, 100), -1, np.int16)
        y = 0
        for index, share in enumerate(shares):
            rows = int(round(100 * share))
            label_map[y:y + rows, :] = index
            y += rows
        return label_map, mask

    def test_a_large_coloured_fill_is_promoted(self):
        seeds = [_seed("Forest", [50, -40, 30], source="legend"),
                 _seed("unlabelled: olive", [60, -10, 40])]
        label_map, mask = self._map([0.7, 0.3])

        notes = promote_large_unseeded(seeds, label_map, mask, _semantics(False))

        self.assertTrue(seeds[1]["is_thematic"])
        self.assertTrue(any("promoted" in n for n in notes))

    def test_small_grey_and_watery_fills_stay_background(self):
        seeds = [_seed("Forest", [50, -40, 30], source="legend"),
                 _seed("unlabelled: olive", [60, -10, 40]),       # too small
                 _seed("unlabelled: white", [96, 0, 1]),          # grey/white surroundings
                 _seed("unlabelled: light blue", [80, -5, -25])]  # a lake
        label_map, mask = self._map([0.5, 0.02, 0.28, 0.2])

        promote_large_unseeded(seeds, label_map, mask, _semantics(True))

        self.assertFalse(seeds[1]["is_thematic"])
        self.assertFalse(seeds[2]["is_thematic"])
        self.assertFalse(seeds[3]["is_thematic"])

    def test_a_large_blue_fill_is_land_when_the_map_has_no_water(self):
        seeds = [_seed("unlabelled: light blue", [80, -5, -25])]
        label_map, mask = self._map([1.0])

        promote_large_unseeded(seeds, label_map, mask, _semantics(False))

        self.assertTrue(seeds[0]["is_thematic"])


if __name__ == "__main__":
    unittest.main()
