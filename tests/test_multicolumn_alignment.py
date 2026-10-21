"""Reading-order validation uses printed positions, not climate-code rules."""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from mapgen import isolate


class MulticolumnAlignmentTests(unittest.TestCase):
    def fixture(self):
        image = np.full((160, 430, 3), 255, np.uint8)
        rects = [(x, y, 30, 20) for x in (10, 220) for y in (15, 60, 105)]
        labels = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot']
        entries = [SimpleNamespace(label=s) for s in labels]
        text = [([[x+40, y], [x+130, y], [x+130, y+20], [x+40, y+20]], label, .99)
                for (x, y, _, _), label in zip(rects, labels)]
        return image, rects, entries, text

    def test_supported_existing_alignment_is_preserved(self):
        image, rects, entries, text = self.fixture()
        with patch.object(isolate, '_legend_alignment_text', return_value=text):
            actual, warnings = isolate._align_multicolumn_swatches(image, entries, rects)
        self.assertIs(actual, rects)
        self.assertEqual(warnings, [])

    def test_row_major_transcription_is_paired_with_physical_cells(self):
        image, rects, entries, text = self.fixture()
        order = [0, 3, 1, 4, 2, 5]
        with patch.object(isolate, '_legend_alignment_text', return_value=text):
            actual, warnings = isolate._align_multicolumn_swatches(
                image, [entries[i] for i in order], rects)
        self.assertEqual(actual, [rects[i] for i in order])
        self.assertTrue(warnings)

    def test_fully_observed_nonstandard_order_does_not_require_code_sorting(self):
        image, rects, entries, text = self.fixture()
        order = [4, 0, 5, 2, 1, 3]
        with patch.object(isolate, '_legend_alignment_text', return_value=text):
            actual, _ = isolate._align_multicolumn_swatches(image, [entries[i] for i in order], rects)
        self.assertEqual(actual, [rects[i] for i in order])

    def test_unique_partial_evidence_can_verify_reading_order(self):
        image, rects, entries, text = self.fixture()
        order = [0, 3, 1, 4, 2, 5]
        with patch.object(isolate, '_legend_alignment_text', return_value=[text[i] for i in (1, 2, 3)]):
            actual, _ = isolate._align_multicolumn_swatches(image, [entries[i] for i in order], rects)
        self.assertEqual(actual, [rects[i] for i in order])

    def test_anchors_shared_by_two_orders_require_review(self):
        image, rects, entries, text = self.fixture()
        indices = [0, 1, 3, 4]
        rects = [rects[i] for i in indices]
        entries = [entries[i] for i in indices]
        # First/last are identical in row and column traversal of a 2x2 grid.
        with patch.object(isolate, '_legend_alignment_text', return_value=[text[0], text[4]]):
            actual, warnings = isolate._align_multicolumn_swatches(image, entries, rects)
        self.assertIsNone(actual)
        self.assertIn('review', warnings[0])

    def test_missing_low_confidence_or_misplaced_evidence_requires_review(self):
        image, rects, entries, text = self.fixture()
        misplaced = [(np.asarray(quad)-[100, 0], label, confidence) for quad, label, confidence in text]
        for evidence in ([], [(q, s, .3) for q, s, _ in text], misplaced):
            with self.subTest(evidence=evidence), patch.object(isolate, '_legend_alignment_text', return_value=evidence):
                actual, warnings = isolate._align_multicolumn_swatches(image, entries, rects)
            self.assertIsNone(actual)
            self.assertIn('review', warnings[0])

    def test_conflicting_anchors_require_review(self):
        image, rects, entries, text = self.fixture()
        conflicting = text + [(text[0][0], 'Bravo', .99)]
        with patch.object(isolate, '_legend_alignment_text', return_value=conflicting):
            actual, warnings = isolate._align_multicolumn_swatches(image, entries, rects)
        self.assertIsNone(actual)
        self.assertIn('review', warnings[0])

    def test_duplicate_normalized_labels_require_review(self):
        image, rects, entries, _ = self.fixture()
        entries[1] = SimpleNamespace(label='ALPHA!')
        with patch.object(isolate, '_legend_alignment_text') as ocr:
            actual, _ = isolate._align_multicolumn_swatches(image, entries, rects)
        self.assertIsNone(actual)
        ocr.assert_not_called()

    def test_multiline_unique_prefixes_anchor_complete_labels(self):
        image, rects, entries, text = self.fixture()
        entries[0] = SimpleNamespace(label='Alpha forest (managed woodland)')
        text[0] = (text[0][0], 'Alpha forest', .99)
        with patch.object(isolate, '_legend_alignment_text', return_value=text):
            actual, warnings = isolate._align_multicolumn_swatches(image, entries, rects)
        self.assertIs(actual, rects)
        self.assertFalse(warnings)

    def test_single_column_and_single_row_do_not_invoke_ocr(self):
        image, rects, entries, _ = self.fixture()
        for indices in ([0, 1, 2], [0, 3]):
            subset = [rects[i] for i in indices]
            with self.subTest(indices=indices), patch.object(isolate, '_legend_alignment_text') as ocr:
                actual, warnings = isolate._align_multicolumn_swatches(image, [entries[i] for i in indices], subset)
            self.assertIs(actual, subset)
            self.assertEqual(warnings, [])
            ocr.assert_not_called()

    def test_an_offset_fragment_is_not_a_second_legend_column(self):
        rects = [(10, y, 20, 10) for y in (10, 30, 50, 70)] + [(32, 50, 17, 10)]
        with patch.object(isolate, '_legend_alignment_text') as ocr:
            result, warnings = isolate._align_multicolumn_swatches(
                np.zeros((100, 200, 3), np.uint8), [SimpleNamespace(label=str(i)) for i in range(5)], rects)
        self.assertIs(result, rects)
        self.assertEqual(warnings, [])
        ocr.assert_not_called()

    def test_ambiguous_alignment_does_not_emit_a_ready_palette(self):
        image, rects, entries, _ = self.fixture()
        sem = SimpleNamespace(data_ordering=SimpleNamespace(value='qualitative'), legend_entries=[])
        with patch.object(isolate, '_dedupe_thematic', return_value=([], [])), \
             patch.object(isolate, '_detect_for_entries', return_value=(entries, rects, [])), \
             patch.object(isolate, '_legend_alignment_text', return_value=[]):
            classes, expected, detected, warnings = isolate._sample_legend_classes(image, sem)
        self.assertEqual(classes, [])
        self.assertEqual((expected, detected), (6, 6))
        self.assertTrue(any('review' in s for s in warnings))

    def test_identical_crops_reuse_bounded_memory_ocr_cache(self):
        image, _, _, text = self.fixture()
        reader = SimpleNamespace(readtext=lambda *a, **kw: text)
        isolate._legend_alignment_text.cache_clear()
        try:
            with patch.object(isolate, '_legend_alignment_reader', return_value=reader), \
                 patch.object(reader, 'readtext', wraps=reader.readtext) as read:
                for _ in range(2):
                    isolate._legend_alignment_text(*image.shape[:2], image.tobytes())
                self.assertEqual(read.call_count, 1)
        finally:
            isolate._legend_alignment_text.cache_clear()


class PrintedMulticolumnTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def test_real_world_and_france_row_and_column_orders_match_printed_reference(self):
        # Physical text order checked against the source legend images.
        reference_columns = {
            'world_climate': [
                ['Af', 'Am', 'Aw'], ['BWh', 'BWk', 'BSh'], ['BSk', 'Csa', 'Csb'],
                ['Csc', 'Cwa', 'Cwb'], ['Cwc', 'Cfa', 'Cfb'], ['Cfc', 'Dsa', 'Dsb'],
                ['Dsc', 'Dsd', 'Dwa'], ['Dwb', 'Dwc', 'Dwd'], ['Dfa', 'Dfb', 'Dfc'],
                ['Dfd', 'ET', 'EF'],
            ],
            'france_env': [
                ['Forest', 'Grassland', 'Grassland and crops', 'Cropland (primarily wheat, sugar beets, corn)'],
                ['Other crops (primarily cereals)', 'Olives (often with vines and wheat)',
                 'Market gardening (fruits, vegetables, flowers)', 'Vineyards'],
            ],
        }
        for name, labels_by_column in reference_columns.items():
            run = self.ROOT/'runs'/name
            sem = isolate.MapSemantics.model_validate_json((run/'step1_semantics.json').read_text(encoding='utf-8'))
            image = isolate.imread(run/'legend.png')
            labels = [s for column in labels_by_column for s in column]
            entries = {e.label: e for e in sem.legend_entries}
            rects, _ = isolate.detect_swatches(image, len(labels), labels, False)
            self.assertEqual(len(rects), len(labels))
            # Scanned swatches vary at individual pixels. Use their printed
            # interiors as independent references, without the inked outline.
            pixels = {label: np.median(image[y+2:y+h-2, x+2:x+w-2], axis=(0, 1))[::-1]
                      for label, (x, y, w, h) in zip(labels, rects)}
            row_order = [column[row] for row in range(max(map(len, labels_by_column)))
                         for column in labels_by_column if row < len(column)]
            for order in (labels, row_order):
                with self.subTest(map=name, row_order=order==row_order):
                    candidate = sem.model_copy(update={'legend_entries': [entries[label] for label in order]})
                    classes, expected, detected, warnings = isolate._sample_legend_classes(image, candidate)
                    self.assertEqual((len(classes), expected, detected), (len(labels),)*3, warnings)
                    for item in classes:
                        self.assertLess(np.linalg.norm(np.array(item['rgb'])-pixels[item['label']]), 12, item)


if __name__ == '__main__':
    unittest.main()
