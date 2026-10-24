"""Tiny synthetic OCR interface checks; no real PDF, parser or corpus work."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import corpus_worker as worker


class FakePage:
    def __init__(self, native='Native text must survive.', ocr='OCR evidence.', area=100,
                 image_infos=None):
        self.native, self.ocr = native, ocr
        self.image_infos = image_infos
        self.rect = SimpleNamespace(width=area, height=1)
        self.calls = []

    def get_text(self, kind, **kwargs):
        return self.ocr if kwargs.get('textpage') is not None else self.native

    def get_images(self):
        return [object()]

    def get_image_info(self, **kwargs):
        return self.image_infos or [{'width': 100, 'height': 100}]

    def get_drawings(self):
        return []

    def get_textpage_ocr(self, **kwargs):
        self.calls.append(kwargs)
        return object()

    def get_pixmap(self, **kwargs):
        return SimpleNamespace(tobytes=lambda kind: b'synthetic-png')


class FakeDocument:
    needs_pass = False

    def __init__(self, page):
        self.page = page

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __len__(self):
        return 1

    def __getitem__(self, number):
        return self.page


class WorkerOCRTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        for language in ('eng', 'chi_sim'):
            (self.directory / (language + '.traineddata')).write_bytes(b'synthetic-data')

    def run_page(self, page, **overrides):
        instance = object.__new__(worker.Worker)
        config = dict(pdf_ocr=True, ocr_languages='eng+chi_sim', tessdata=str(self.directory),
                      max_page_pixels=4000000, retry_pending=False)
        config.update(overrides)
        instance.args = SimpleNamespace(**config)
        instance.out, instance.bytes_written = self.directory, 0
        instance.run_id = 'synthetic-run'
        instance.check, instance.state, instance.unit = Mock(), Mock(), Mock()
        instance.db = Mock()
        instance.db.execute.return_value.fetchone.return_value = (0,)
        instance.db.execute.return_value.fetchall.return_value = []
        fake = SimpleNamespace(open=lambda path: FakeDocument(page), Matrix=lambda x, y: (x, y))
        with patch.dict(sys.modules, {'pymupdf': fake}), patch.object(worker, 'atomic'), \
                patch.object(worker.shutil, 'which', side_effect=AssertionError('no external OCR binary lookup')):
            instance.pdf({'source_id': 'synthetic', 'cursor': 0}, Path('never-read.pdf'))
        call = instance.unit.call_args
        return call.args[3], call.args[4], call.kwargs, instance

    def test_embedded_ocr_preserves_native_text_and_separate_evidence(self):
        page = FakePage()
        data, metadata, kwargs, instance = self.run_page(page)
        self.assertEqual(data['text'], page.native)
        self.assertEqual(data['native_text'], page.native)
        self.assertEqual(data['ocr_text'], page.ocr)
        self.assertEqual(data['text_primary_source'], 'native')
        self.assertFalse(page.calls[0]['full'])
        self.assertEqual(page.calls[0]['tessdata'], str(self.directory))
        self.assertEqual(metadata['ocr_status'], 'performed_unverified')
        self.assertFalse(metadata['ocr_quality_verified'])
        self.assertFalse(metadata['formula_validation_complete'])

    def test_empty_native_uses_full_ocr_without_discarding_empty_evidence(self):
        page = FakePage(native='')
        data, metadata, kwargs, instance = self.run_page(page)
        self.assertTrue(page.calls[0]['full'])
        self.assertEqual(data['text'], page.ocr)
        self.assertEqual(data['native_text'], '')
        self.assertEqual(data['text_primary_source'], 'ocr')

    def test_missing_language_file_never_calls_engine(self):
        (self.directory / 'chi_sim.traineddata').unlink()
        page = FakePage()
        data, metadata, kwargs, instance = self.run_page(page)
        self.assertEqual(page.calls, [])
        self.assertTrue(metadata['ocr_status'].startswith('pending_ocr_'))
        self.assertEqual(kwargs['status'], 'pending_ocr')
        self.assertEqual(data['text'], page.native)

    def test_ocr_render_budget_is_hard_capped_at_four_million(self):
        page = FakePage(area=1000000)
        data, metadata, kwargs, instance = self.run_page(page, max_page_pixels=8000000)
        self.assertEqual(page.calls, [])
        self.assertEqual(metadata['ocr_status'], 'pending_ocr_render_capacity')
        self.assertEqual(data['text'], page.native)

    def test_explicit_language_files_reject_symlinks_and_path_languages(self):
        target = self.directory / 'eng.traineddata'
        target.unlink()
        target.symlink_to(self.directory / 'chi_sim.traineddata')
        with self.assertRaisesRegex(ValueError, 'invalid_tessdata_file'):
            worker.explicit_tessdata(str(self.directory), 'eng')
        with self.assertRaises(ValueError):
            worker.explicit_tessdata(str(self.directory), '../eng')

    def test_ocr_failure_retains_native_text(self):
        page = FakePage()
        page.get_textpage_ocr = Mock(side_effect=RuntimeError('synthetic unavailable engine'))
        data, metadata, kwargs, instance = self.run_page(page)
        self.assertEqual(data['text'], page.native)
        self.assertIsNone(data['ocr_text'])
        self.assertEqual(metadata['ocr_status'], 'pending_ocr_RuntimeError')

    def test_tiny_embedded_image_skips_ocr_engine_and_records_boundary(self):
        page = FakePage(image_infos=[{'width': 2, 'height': 36}])
        data, metadata, kwargs, instance = self.run_page(page)
        self.assertEqual(page.calls, [])
        self.assertEqual(metadata['ocr_status'], 'pending_ocr_tiny_image')
        self.assertTrue(metadata['tiny_raster_image'])
        self.assertEqual(kwargs['status'], 'pending_ocr')
        self.assertEqual(data['text'], page.native)


if __name__ == '__main__':
    unittest.main()
