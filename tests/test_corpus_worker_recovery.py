"""Tiny queue/lock/recovery checks using synthetic rows and fake PDF pages."""
import json
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
sys.path.insert(0, str(TOOLS))
import corpus_worker as worker


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        manifest = self.directory / 'empty.jsonl'
        manifest.write_text('')
        args = SimpleNamespace(backend='aws', input_root=str(self.directory / 'input'),
                               output_root=str(self.directory / 'output'), manifest=str(manifest),
                               max_seconds=120, max_memory_mib=512, max_files=32, max_units=2,
                               max_output_bytes=64*1024**2, pdf_ocr=True, ocr_languages='eng',
                               tessdata=None, max_page_pixels=4000000, capability_id='dependency-v1')
        database = sqlite3.connect(':memory:')
        self.addCleanup(database.close)
        with patch.object(worker, 'cloud_guard'), patch.object(worker.sqlite3, 'connect', return_value=database):
            self.worker = worker.Worker(args, {'private_output_destination': args.output_root, 'timeout_seconds': 120})
        self.worker.export = Mock(side_effect=lambda reason: {'stop_reason': reason})

    def add_source(self, sid, status='inventoried', cursor=0):
        self.worker.register({'source_id': sid, 'relative_path': sid + '.pdf', 'size_bytes': 1})
        self.worker.state(sid, status=status, cursor=cursor)

    def row(self, sid):
        return self.worker.db.execute('SELECT * FROM sources WHERE source_id=?', (sid,)).fetchone()

    def insert_unit(self, sid, index, status, metadata):
        self.worker.db.execute('INSERT INTO units VALUES (?,?,?,?,?,?,?)',
                               (sid, index, 'page', None, None, status, json.dumps(metadata)))
        self.worker.db.commit()

    def attempt(self, sid, index, attempt_id='interrupted-attempt'):
        self.worker.db.execute('INSERT INTO page_attempts VALUES (?,?,?,?,?)',
                               (sid, index, self.worker.pdf_capability(), attempt_id, 'started'))
        self.worker.db.commit()

    def test_round_robin_advances_new_sources_and_never_requeues_completed(self):
        self.add_source('a', 'extracting', 2)
        self.add_source('b')
        first = self.worker.next_source(set())
        self.assertEqual(first['source_id'], 'a')
        self.worker.mark_scheduled(first)
        second = self.worker.next_source(set())
        self.assertEqual(second['source_id'], 'b')
        self.worker.mark_scheduled(second)
        self.worker.state('b', status='extracted')
        self.add_source('0')
        self.assertEqual(self.worker.next_source(set())['source_id'], '0')
        self.assertIsNone(self.worker.next_source({'0', 'a'}))

    def test_pending_ocr_requires_changed_capability_and_capacity_gap_stays_pending(self):
        self.add_source('a', 'partial_pending_page_capability', 2)
        self.insert_unit('a', 0, 'pending_ocr', {'ocr_status': 'pending_ocr_RuntimeError',
                                              'ocr_capability_id': self.worker.pdf_capability()})
        self.insert_unit('a', 1, 'pending_ocr', {'ocr_status': 'pending_ocr_render_capacity',
                                              'ocr_required_pixels': 5000000})
        self.assertIsNone(self.worker.next_source(set()))
        self.worker.args.capability_id = 'dependency-v2'
        self.assertEqual(self.worker.next_source(set())['source_id'], 'a')
        self.assertEqual(self.worker.retryable_pages('a'), [0])

    def test_interrupted_page_is_retained_as_gap_and_later_pages_can_resume(self):
        self.add_source('a', 'extracting', 1)
        self.attempt('a', 1)
        self.worker.recover_interrupted_pages()
        self.assertEqual(self.row('a')['cursor'], 2)
        unit = self.worker.db.execute('SELECT * FROM units WHERE source_id=?', ('a',)).fetchone()
        self.assertEqual(unit['status'], 'pending_page_interrupted')
        self.assertFalse(self.worker.pending_page_retryable(unit))
        self.worker.args.capability_id = 'dependency-v2'
        self.assertTrue(self.worker.pending_page_retryable(unit))

    def test_committed_page_before_interrupt_is_not_replaced_or_recomputed(self):
        self.add_source('a', 'extracting', 0)
        self.attempt('a', 0)
        self.insert_unit('a', 0, 'extracted', {'page_attempt_id': 'interrupted-attempt'})
        self.worker.recover_interrupted_pages()
        self.assertEqual(self.row('a')['cursor'], 1)
        status = self.worker.db.execute('SELECT status FROM units').fetchone()[0]
        self.assertEqual(status, 'extracted')

    def test_revised_unit_keeps_previous_committed_artifact_intact(self):
        self.worker.unit('a', 0, 'page', {'native_text': 'previous'})
        previous = self.worker.db.execute('SELECT artifact FROM units').fetchone()[0]
        before = (self.worker.out / previous).read_bytes()
        self.worker.unit('a', 0, 'page', {'native_text': 'updated'})
        current = self.worker.db.execute('SELECT artifact FROM units').fetchone()[0]
        self.assertNotEqual(previous, current)
        self.assertEqual((self.worker.out / previous).read_bytes(), before)

    def test_cooperative_budget_exit_keeps_same_unfinished_page(self):
        self.add_source('a')
        def bounded_process(row):
            self.attempt('a', 0)
            raise worker.BudgetStop('output_budget_requires_larger_cloud_batch')
        self.worker.process = bounded_process
        with patch.object(worker, 'contained_file', return_value=SimpleNamespace(is_file=lambda: True)):
            self.worker.run()
        self.worker.recover_interrupted_pages()
        self.assertEqual(self.row('a')['cursor'], 0)
        self.assertEqual(self.worker.db.execute('SELECT count(*) FROM units').fetchone()[0], 0)
        self.assertEqual(self.worker.db.execute('SELECT status FROM page_attempts').fetchone()[0], 'deferred_budget')

    def test_page_budget_resumes_all_remaining_pages_without_duplicate_extraction(self):
        self.add_source('a')
        seen = []
        class Page:
            rect = SimpleNamespace(width=10, height=10)
            def get_text(self, *args, **kwargs):
                return 'native text with enough characters to avoid any OCR path'
            def get_images(self):
                return []
            def get_drawings(self):
                return []
        class Document:
            needs_pass = False
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __len__(self):
                return 6
            def __getitem__(self, index):
                seen.append(index)
                return Page()
        self.worker.process = lambda row: self.worker.pdf(row, Path('never-read.pdf'))
        with patch.dict(sys.modules, {'pymupdf': SimpleNamespace(open=lambda path: Document())}), \
                patch.object(worker, 'atomic'), \
                patch.object(worker, 'contained_file', return_value=SimpleNamespace(is_file=lambda: True)):
            for run in range(4):
                self.worker.run_id = str(run)
                self.worker.started = time.monotonic()
                self.worker.units_written = self.worker.bytes_written = 0
                self.worker.run()
        self.assertEqual(seen, list(range(6)))
        self.assertEqual(self.row('a')['cursor'], 6)
        self.assertEqual(self.row('a')['status'], 'extracted')


class KernelLockTests(unittest.TestCase):
    def test_sigkill_releases_lock_while_persistent_file_remains(self):
        # This tiny child owns only a synthetic lock and waits on stdin. It does
        # not parse documents, open network connections or run research work.
        source = '''import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from corpus_worker import worker_lock
with worker_lock(Path(sys.argv[2])):
    print("locked", flush=True)
    sys.stdin.read()
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.lock'
            child = subprocess.Popen([sys.executable, '-c', source, str(TOOLS), str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                with selectors.DefaultSelector() as ready:
                    ready.register(child.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(3), 'tiny lock child did not become ready')
                self.assertEqual(child.stdout.readline().strip(), 'locked')
                with self.assertRaises(BlockingIOError), worker.worker_lock(path):
                    pass
                child.kill()
                child.wait(timeout=3)
                self.assertTrue(path.exists())
                with worker.worker_lock(path):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
                child.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
