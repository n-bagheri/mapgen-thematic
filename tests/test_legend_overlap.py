"""Legend overlap protection changes only proven geographic continuations."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.isolate import protect_legend_overlap, refine_map_mask


class LegendOverlapTests(unittest.TestCase):
    def fixture(self):
        image = np.full((400, 600, 3), 255, np.uint8)
        image[100:350, 250:550] = (70, 170, 210)
        image[50:65, 30:80] = (20, 180, 30)
        legend = (20, 30, 280, 210)
        furniture = [("legend", legend), ("other:inset map", (380, 200, 440, 250))]
        boxes = [(240, 90, 560, 360)]
        mask, _, _ = refine_map_mask(image, boxes, [b for _, b in furniture])
        classes = [{"swatch_bbox_orig": [10, 20, 50, 15]}]
        return image, mask, furniture, legend, boxes, classes

    def test_connected_land_restored_but_other_exclusions_preserved(self):
        image, mask, furniture, legend, boxes, classes = self.fixture()
        with patch("mapgen.isolate._sample_legend_classes", return_value=(classes, 1, 1, [])):
            checked, warnings = protect_legend_overlap(image, mask, furniture, legend, SimpleNamespace())
        self.assertTrue(warnings)
        self.assertIn(furniture[1], checked)
        after, _, _ = refine_map_mask(image, boxes, [b for _, b in checked])
        self.assertFalse(mask[100:210, 250:280].any())
        self.assertEqual(after[100:210, 250:280].min(), 255)
        self.assertFalse(after[200:250, 380:440].any())
        outside = np.ones(mask.shape, bool)
        outside[30:210, 20:280] = False
        np.testing.assert_array_equal(mask[outside], after[outside])
        self.assertFalse(after[50:65, 30:80].any())
        # Existing later-stage padding must not exclude recovered land again.
        self.assertFalse(any(a-2 <= 260 < c+2 and b-2 <= 130 < d+2 for _, (a,b,c,d) in checked))

    def test_all_four_legend_corner_orientations(self):
        image, mask, furniture, legend, _, _ = self.fixture()
        swatch = (30, 50, 80, 65)
        width, height = 600, 400
        for rotation in range(4):
            with self.subTest(rotation=rotation):
                x, y, right, bottom = swatch
                crop_classes = [{"swatch_bbox_orig": [x-legend[0], y-legend[1], right-x, bottom-y]}]
                with patch("mapgen.isolate._sample_legend_classes", return_value=(crop_classes,1,1,[])):
                    checked, _ = protect_legend_overlap(image, mask, furniture, legend, SimpleNamespace())
                self.assertNotEqual(checked, furniture)
                def rotate(box):
                    a,b,c,d=box
                    return b,width-c,d,width-a
                image=np.rot90(image).copy();mask=np.rot90(mask).copy()
                furniture=[(name,rotate(box)) for name,box in furniture]
                legend=rotate(legend);swatch=rotate(swatch)
                width,height=height,width

    def test_non_overlapping_legend_is_unchanged_without_colour_resampling(self):
        image, mask, furniture, _, _, _ = self.fixture()
        legend=(20,30,120,210);furniture=[("legend",legend),furniture[1]]
        with patch("mapgen.isolate._sample_legend_classes") as sample:
            checked,warnings=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture);self.assertEqual(warnings,[])
        sample.assert_not_called()

    def test_a_swatch_touching_the_continuation_requires_review(self):
        image,mask,furniture,legend,_,_=self.fixture()
        collision=[{"swatch_bbox_orig":[235,90,10,10]}]
        with patch("mapgen.isolate._sample_legend_classes",return_value=(collision,1,1,[])):
            checked,warnings=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture);self.assertIn("review",warnings[0])

    def test_other_legend_ink_in_the_cutout_requires_review(self):
        image,mask,furniture,legend,_,classes=self.fixture()
        image[97:100,246:249]=0
        with patch("mapgen.isolate._sample_legend_classes",return_value=(classes,1,1,[])) as sample:
            checked,warnings=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture);self.assertIn("review",warnings[0])
        sample.assert_not_called()

    def test_background_connected_through_the_legend_is_not_recovered(self):
        image,mask,furniture,legend,_,_=self.fixture()
        image[:]=255;image[:10]=150;image[-10:]=150;image[:,:10]=150;image[:,-10:]=150
        with patch("mapgen.isolate._sample_legend_classes") as sample:
            checked,_=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture);sample.assert_not_called()

    def test_a_bridge_from_geography_to_the_swatch_is_not_restored(self):
        image,mask,furniture,legend,_,_=self.fixture()
        cv2.rectangle(image,(70,60),(255,66),(0,0,0),-1)
        cv2.rectangle(image,(249,60),(255,105),(0,0,0),-1)
        checked,_=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture)

    def test_uncertain_palette_keeps_the_original_exclusion(self):
        image,mask,furniture,legend,_,_=self.fixture()
        with patch("mapgen.isolate._sample_legend_classes",return_value=([],4,3,[])):
            checked,warnings=protect_legend_overlap(image,mask,furniture,legend,SimpleNamespace())
        self.assertEqual(checked,furniture);self.assertIn("review",warnings[0])

    def test_no_legend_does_not_change_other_furniture(self):
        image,mask,furniture,_,_,_=self.fixture()
        furniture=furniture[1:]
        self.assertEqual(protect_legend_overlap(image,mask,furniture,None,SimpleNamespace()),(furniture,[]))


if __name__=="__main__":
    unittest.main()
