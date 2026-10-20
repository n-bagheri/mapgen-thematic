import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import cv2
import numpy as np

from mapgen.isolate import imread, imwrite, to_lab
from mapgen.labelreview import apply_review, occurrence_id
from mapgen.output_spec import OutputSpec
from mapgen.semantics import MapSemantics
from mapgen.textremoval import fill_region_label_halos
from tests.test_segment_palette_guard import SEMANTICS


def fixture():
    image = np.full((140, 280, 3), (28, 25, 215), np.uint8)
    image[:, 140:] = (97, 174, 253)
    glyphs = np.zeros(image.shape[:2], np.uint8)
    cv2.putText(glyphs, "DELTA", (65, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                255, thickness=2, lineType=cv2.LINE_AA)
    halo = cv2.dilate(glyphs, np.ones((7, 7), np.uint8))
    image[halo > 0] = 255
    image[glyphs > 0] = 0
    ys, xs = np.where(halo > 0)
    labels = {"labels": [{
        "text": "DELTA", "kind": "region_label", "box": [
            int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1],
        "localization": "craft", "recognition_status": "text-confirmed",
        "easyocr_text": "DELTA", "easyocr_conf": 0.99, "mask_found": False,
    }]}
    colours = [(28, 25, 215), (97, 174, 253)]
    classes = {"classes": [{
        "label": name, "rgb": list(bgr[::-1]), "is_thematic": True,
        "priority": i + 1, "lab": to_lab(np.uint8([[bgr]]))[0, 0].tolist(),
    } for i, (name, bgr) in enumerate(zip(["Dry", "Moist"], colours))]}
    sem = SimpleNamespace(map_type=SimpleNamespace(value="isopleth"))
    return image, np.full(image.shape[:2], 255, np.uint8), labels, classes, sem


class RegionHaloRemovalTests(unittest.TestCase):
    def test_confirmed_letters_and_halo_are_filled_with_surrounding_thematic_pixels(self):
        image, mask, labels, classes, sem = fixture()
        original = image.copy()
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertEqual(count, 1)
        self.assertTrue(np.array_equal(image, original), "source image was mutated")
        self.assertTrue(np.array_equal(cleaned[repaired == 0], image[repaired == 0]))
        colours = np.unique(cleaned[repaired > 0], axis=0)
        for colour in colours:
            self.assertIn(colour.tolist(), [[28, 25, 215], [97, 174, 253]])
        self.assertGreater(len(colours), 1, "a label crossing a boundary was given one flat colour")

    def test_even_precise_labels_receive_halo_cleanup(self):
        image, mask, labels, classes, sem = fixture()
        labels["labels"][0]["mask_found"] = True
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertEqual(count, 1)
        self.assertFalse(np.any(cleaned[repaired > 0].min(axis=1) > 230))

    def test_high_confidence_ocr_can_confirm_a_misread_model_label(self):
        image, mask, labels, classes, sem = fixture()
        labels["labels"][0].update(text="RI", recognition_status="geometry-only")
        _, _, _, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertEqual(count, 1)

    def test_unverified_model_only_detection_is_not_erased(self):
        image, mask, labels, classes, sem = fixture()
        labels["labels"][0].update(localization="gemini-unverified", recognition_status="gemini-only",
                                    easyocr_text=None, easyocr_conf=None)
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertIs(cleaned, image)
        self.assertEqual(count, 0)
        self.assertFalse(repaired.any())

    def test_explicit_keep_and_geography_outside_text_windows_are_preserved(self):
        image, mask, labels, classes, sem = fixture()
        lb = labels["labels"][0]
        review, _ = apply_review(labels, [{
            "id": occurrence_id(lb, 0), "text": "DELTA", "include": True, "remove": False,
        }])
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem, review)
        self.assertIs(cleaned, image)
        self.assertEqual(count, 0)
        mask[:, :110] = 0
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertTrue(np.array_equal(cleaned[repaired == 0], image[repaired == 0]))
        self.assertTrue(np.array_equal(geography[repaired == 0], mask[repaired == 0]))
        self.assertTrue(np.all(cleaned[(repaired > 0) & (geography == 0)] == 255))

    def test_neutral_thematic_fills_and_other_map_types_keep_the_old_path(self):
        image, mask, labels, classes, sem = fixture()
        classes["classes"][0]["lab"] = [90, 0, 0]
        cleaned, _, _, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertIs(cleaned, image)
        self.assertEqual(count, 0)
        image, mask, labels, classes, sem = fixture()
        sem.map_type.value = "area_class_chorochromatic"
        cleaned, _, _, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertIs(cleaned, image)
        self.assertEqual(count, 0)

    def test_plain_text_without_pale_halos_keeps_existing_removal(self):
        image, mask, labels, classes, sem = fixture()
        image[:] = (28, 25, 215)
        cv2.putText(image, "DELTA", (65, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), thickness=2)
        cleaned, _, _, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertIs(cleaned, image)
        self.assertEqual(count, 0)

    def test_detached_ink_fragments_are_removed_but_thematic_islands_remain(self):
        image, mask, labels, classes, sem = fixture()
        mask[:, 210:] = 0
        image[:, 210:] = 255
        image[20:25, 230:235] = 0
        mask[20:25, 230:235] = 255
        image[40:45, 230:235] = (28, 25, 215)
        mask[40:45, 230:235] = 255
        cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem)
        self.assertEqual(count, 1)
        self.assertFalse(geography[20:25, 230:235].any())
        self.assertTrue(np.all(cleaned[20:25, 230:235] == 255))
        self.assertTrue(np.all(geography[40:45, 230:235] == 255))
        self.assertTrue(np.array_equal(cleaned[40:45, 230:235], image[40:45, 230:235]))

    def test_step4_and_step5_receive_cleaned_pixels_without_new_text_classes(self):
        from mapgen.segment import run_step4
        from mapgen.postprocess import run_step5

        image, mask, labels, classes, _ = fixture()
        semantics = deepcopy(SEMANTICS)
        semantics.update(map_type="isopleth", data_ordering="ordered")
        semantics["overlay_text"]["has_region_labels"] = True
        with tempfile.TemporaryDirectory(prefix="text-halo-test-") as tmp:
            root = Path(tmp)
            run = root / "sample"
            run.mkdir()
            for name, payload in {"labels.json": labels, "classes.json": classes,
                                  "step1_semantics.json": semantics, "geometry.json": {
                                      "map_crop": [0, 0, 280, 140], "furniture": [],
                                  }}.items():
                (run / name).write_text(json.dumps(payload), encoding="utf-8")
            imwrite(run / "map_area.png", image)
            imwrite(run / "map_mask.png", mask)
            imwrite(run / "text_mask.png", np.zeros(mask.shape, np.uint8))
            run_step4(Path("sample.png"), runs_dir=root)
            with patch("mapgen.postprocess.OutputSpec.load_or_create", return_value=OutputSpec()):
                run_step5(Path("sample.png"), runs_dir=root)
            final = json.loads((run / "classes_final.json").read_text())["classes"]
            self.assertEqual({cl["label"] for cl in final if cl["area_px"]}, {"Dry", "Moist"})
            self.assertTrue(np.isin(imread(run / "label_map.png")[..., 0], [1, 2]).all())
            source = imread(run / "step4_text_removed_input.png")
            self.assertFalse(np.any(source[65:85, 70:140].min(axis=2) > 230))
            audit = json.loads((run / "step5_source_audit.json").read_text())
            self.assertEqual(audit["geographic_pixels_changed"], 0)


class SavedMapHaloRegressionTests(unittest.TestCase):
    def test_only_the_two_affected_map_inputs_change(self):
        root = Path(__file__).resolve().parents[1] / "runs"
        checked = []
        for run in root.iterdir() if root.exists() else []:
            required = [run / name for name in (
                "labels.json", "classes.json", "step1_semantics.json", "map_area.png", "map_mask.png")]
            if not all(path.is_file() for path in required):
                continue
            labels, classes = [json.loads(path.read_text(encoding="utf-8")) for path in required[:2]]
            sem = MapSemantics.model_validate_json(required[2].read_text(encoding="utf-8"))
            image, mask = imread(required[3]), imread(required[4])[..., 0]
            filled = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
            cv2.floodFill(filled, np.zeros((mask.shape[0] + 4, mask.shape[1] + 4), np.uint8), (0, 0), 255)
            mask = mask | cv2.bitwise_not(filled[1:-1, 1:-1])
            review_file = run / "label_review.json"
            review = json.loads(review_file.read_text(encoding="utf-8")) if review_file.exists() else None
            cleaned, geography, repaired, count = fill_region_label_halos(image, mask, labels, classes, sem, review)
            with self.subTest(run=run.name):
                if run.name in {"andhra_soil", "Andrha_soil", "telangana_soil"}:
                    self.assertGreaterEqual(count, 10)
                    lab = to_lab(cleaned[(repaired > 0) & (geography > 0)].reshape(-1, 1, 3))[:, 0]
                    palette = np.float32([cl["lab"] for cl in classes["classes"] if cl.get("is_thematic")])
                    distance = np.min([np.linalg.norm(lab - colour, axis=1) for colour in palette], axis=0)
                    self.assertTrue((distance < 16).all(), "text/halo colour survived reconstruction")
                else:
                    self.assertEqual(count, 0)
                    self.assertIs(cleaned, image)
                    self.assertIs(geography, mask)
                    self.assertFalse(repaired.any())
                checked.append(run.name)
        if not checked:
            self.skipTest("Saved map inputs are not available in this checkout")


if __name__ == "__main__":
    unittest.main()
