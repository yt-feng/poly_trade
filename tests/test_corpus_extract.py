"""Controller validation only; never processes corpus data or opens SSH."""
import ast
import base64
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_corpus_extract as extract


class CorpusExtractTests(unittest.TestCase):
    def test_cloud_gate_precedes_identity_source_or_transport(self):
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'false'}), \
                patch.object(extract.probe, 'configuration') as configuration, \
                patch.object(extract.subprocess, 'run') as process:
            with self.assertRaisesRegex(ValueError, 'CLOUD_ONLY'):
                extract.extract()
            configuration.assert_not_called()
            process.assert_not_called()

    def test_payload_binds_run_attempt_and_source_without_execution(self):
        source = b'raise RuntimeError("must never execute here")\n'
        payload = extract.source_payload({'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': 'a'*40}, source)
        self.assertEqual(payload['run_id'], '123-2')
        self.assertEqual(base64.b64decode(payload['worker_base64']), source)
        for invalid in ('../../elsewhere', '', '-1'):
            with self.subTest(run=invalid), self.assertRaises(ValueError):
                extract.source_payload({'GITHUB_RUN_ID': invalid, 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_SHA': 'a'*40}, source)

    def test_embedded_program_compiles_without_running(self):
        compile(extract.REMOTE_EXTRACT, 'fixed_remote_extract', 'exec')
        tree = ast.parse(extract.REMOTE_EXTRACT)
        def constant_product(node):
            if isinstance(node, ast.Constant) and type(node.value) is int:
                return node.value
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
                return constant_product(node.left) * constant_product(node.right)
            raise AssertionError('only bounded integer constants accepted')
        assignments = {n.targets[0].id: constant_product(n.value) for n in tree.body
                       if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                       and n.targets[0].id in ('REPORT_LIMIT', 'MEMORY_LIMIT', 'MAX_METADATA')}
        self.assertEqual(assignments['REPORT_LIMIT'], 2*1024*1024)
        self.assertEqual(assignments['MEMORY_LIMIT'], 512*1024*1024)
        self.assertEqual(assignments['MAX_METADATA'], 64)

    def download_harness(self, content, headers=None):
        # Compile only the reviewed download function with fake network and
        # signal objects. Neither the remote controller nor a real URL runs.
        tree = ast.parse(extract.REMOTE_EXTRACT)
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'download_tessdata')
        url = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/' + 'a'*40 + '/eng.traineddata'
        response = io.BytesIO(content)
        response.geturl = lambda: url
        response.headers = headers or {}
        opener = Mock()
        opener.open.return_value = response
        request = SimpleNamespace(build_opener=Mock(return_value=opener))
        namespace = {'os': os, 'hashlib': hashlib, 'TESSDATA_REVISION': 'a'*40,
                     'MAX_TESSDATA_BYTES': 8, 'urllib': SimpleNamespace(request=request),
                     'NoTessdataRedirect': Mock(),
                     'signal': SimpleNamespace(signal=Mock(), alarm=Mock(), SIGALRM=14)}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'isolated_download_test', 'exec'), namespace)
        return namespace['download_tessdata'], url, request

    def test_language_download_enforces_stream_limit_without_content_length(self):
        download, url, request = self.download_harness(b'123456789')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'tessdata_download_limit'):
                download(Path(directory) / 'tiny.synthetic', url)

    def test_language_download_hashes_complete_bytes_and_rejects_unpinned_url(self):
        download, url, request = self.download_harness(b'1234', {'Content-Length': '4'})
        with tempfile.TemporaryDirectory() as directory:
            result = download(Path(directory) / 'tiny.synthetic', url)
        self.assertEqual(result, (4, hashlib.sha256(b'1234').hexdigest()))
        download, url, request = self.download_harness(b'1234')
        with self.assertRaisesRegex(ValueError, 'tessdata_source_not_pinned'):
            download(Path('never-written'), 'http://untrusted.invalid/eng.traineddata')
        request.build_opener.assert_not_called()

    def test_controller_uses_kernel_ownership_instead_of_lock_file_existence(self):
        node = next(node for node in ast.parse(extract.REMOTE_EXTRACT).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'worker_lock_busy')
        namespace = {'os': os, 'stat': stat, 'fcntl': fcntl}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'isolated_lock_test', 'exec'), namespace)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'worker.lock'
            path.write_text('metadata left by an exited worker')
            self.assertFalse(namespace['worker_lock_busy'](path))
            with path.open('rb') as active:
                fcntl.flock(active.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(namespace['worker_lock_busy'](path))
            self.assertFalse(namespace['worker_lock_busy'](path))


class DeliveryAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        # Run only small local acknowledgement/serialization functions against
        # synthetic bytes. No cloud controller, corpus parser, SSH or API runs.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.worker_hash = 'a'*64
        self.output = self.root / 'extracted' / self.worker_hash
        (self.root / 'extracted').mkdir(mode=0o700)
        self.output.mkdir(mode=0o700)
        names = {'raw_json', 'safe_directory', 'regular', 'read_small', 'atomic', 'checked_relative',
                 'unit_delivery_identity', 'validate_acknowledged_units', 'load_acknowledged',
                 'apply_delivery_ack', 'save_delivery_proposal', 'delivery_artifact',
                 'unit_delivery_items', 'build_unit_delivery'}
        nodes = [node for node in ast.parse(extract.REMOTE_EXTRACT).body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
        self.ns = {'json': json, 'hashlib': hashlib, 'os': os, 'stat': stat, 're': re,
                   'pathlib': __import__('pathlib'), 'base64': base64,
                   'REPORT_LIMIT': 2*1024*1024, 'ACK_STATE_LIMIT': 32*1024*1024}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'isolated_delivery_test', 'exec'), self.ns)

    def unit(self, source='f', text=b'native'):
        artifact = source + '.txt'
        (self.output / artifact).write_bytes(text)
        return {'source_id': source*64, 'unit_index': 0, 'kind': 'page', 'artifact': artifact,
                'content_hash': hashlib.sha256(text).hexdigest(), 'status': 'extracted', 'metadata': {}}

    def deliver(self, units, source_run):
        (self.output / 'unit-state.jsonl').write_text(''.join(json.dumps(unit) + '\n' for unit in units))
        report = {}
        self.ns['build_unit_delivery'](self.output, self.worker_hash, source_run, report)
        return report

    def ack(self, report, kinds=None):
        proposal = report['unit_delivery_proposal']
        request = {name: proposal[name] for name in ('worker_hash', 'source_run', 'proposal_hash')}
        if kinds is not None:
            request['evidence_kinds'] = kinds
        return request

    def test_without_ack_evidence_is_redelivered(self):
        unit = self.unit()
        first, second = self.deliver([unit], '1-1'), self.deliver([unit], '2-1')
        self.assertEqual(first['unit_texts'], second['unit_texts'])
        self.assertFalse((self.output / 'acknowledged-units.json').exists())

    def test_new_lower_sorted_source_is_not_skipped(self):
        old = self.unit()
        first = self.deliver([old], '1-1')
        self.ns['apply_delivery_ack'](self.root, self.ack(first))
        new = self.unit('0')
        second = self.deliver([new, old], '2-1')
        self.assertEqual([item['unit']['source_id'] for item in second['unit_texts']], ['0'*64])
        self.assertEqual(second['unit_delivery_acknowledged_unchanged_skipped'], 1)

    def test_same_unit_updated_ocr_content_and_metadata_is_redelivered(self):
        unit = self.unit()
        first = self.deliver([unit], '1-1')
        self.ns['apply_delivery_ack'](self.root, self.ack(first))
        changed = self.unit(text=b'native and new OCR evidence')
        changed['metadata']['ocr_status'] = 'performed_unverified'
        second = self.deliver([changed], '2-1')
        self.assertEqual(first['unit_texts'][0]['unit_key'], second['unit_texts'][0]['unit_key'])
        self.assertNotEqual(first['unit_texts'][0]['evidence_digest'], second['unit_texts'][0]['evidence_digest'])

    def test_metadata_only_update_is_new_evidence(self):
        unit = self.unit()
        first = self.deliver([unit], '1-1')
        self.ns['apply_delivery_ack'](self.root, self.ack(first))
        unit['metadata']['ocr_quality_verified'] = False
        second = self.deliver([unit], '2-1')
        self.assertEqual(len(second['unit_texts']), 1)
        self.assertEqual(first['unit_texts'][0]['artifact_sha256'], second['unit_texts'][0]['artifact_sha256'])

    def test_replayed_ack_is_idempotent_and_wrong_hash_does_not_mutate(self):
        report = self.deliver([self.unit()], '1-1')
        request = self.ack(report)
        self.ns['apply_delivery_ack'](self.root, request)
        path = self.output / 'acknowledged-units.json'
        before = path.read_bytes()
        replay = self.ns['apply_delivery_ack'](self.root, request)
        self.assertEqual(replay['status'], 'already_acknowledged')
        self.assertEqual(path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, 'proposal_hash_mismatch'):
            self.ns['apply_delivery_ack'](self.root, dict(request, proposal_hash='0'*64))
        self.assertEqual(path.read_bytes(), before)

    def test_text_ack_does_not_acknowledge_visual_png(self):
        unit = self.unit()
        png = b'\x89PNG\r\n\x1a\nsynthetic-encoded-pixels'
        (self.output / 'page.png').write_bytes(png)
        unit['metadata']['visual_artifact'] = 'page.png'
        first = self.deliver([unit], '1-1')
        self.assertEqual({item['evidence_kind'] for item in first['unit_texts']}, {'unit', 'visual'})
        self.ns['apply_delivery_ack'](self.root, self.ack(first))
        second = self.deliver([unit], '2-1')
        self.assertEqual([item['evidence_kind'] for item in second['unit_texts']], ['visual'])
        visual = second['unit_texts'][0]
        self.assertEqual(base64.b64decode(visual['artifact_base64']), png)
        self.assertEqual(visual['artifact_sha256'], hashlib.sha256(png).hexdigest())
        self.assertFalse(visual['visual_review_credited'])
        self.ns['apply_delivery_ack'](self.root, self.ack(first, ['visual']))
        third = self.deliver([unit], '3-1')
        self.assertEqual(third['unit_texts'], [])

    def test_missing_proposal_and_invalid_secret_are_rejected(self):
        request = {'worker_hash': self.worker_hash, 'source_run': '999-1', 'proposal_hash': '0'*64}
        with self.assertRaises(FileNotFoundError):
            self.ns['apply_delivery_ack'](self.root, request)
        self.assertFalse((self.output / 'acknowledged-units.json').exists())
        encoded = base64.b64encode(json.dumps(dict(request, source_run='../elsewhere')).encode()).decode()
        with self.assertRaisesRegex(ValueError, 'DELIVERY_ACK_SOURCE_RUN'):
            extract.delivery_ack_from_environment({'CORPUS_DELIVERY_ACK_B64': encoded})

    def test_same_key_visual_byte_update_is_redelivered_independently(self):
        unit = self.unit()
        unit['metadata']['visual_artifact'] = 'page.png'
        path = self.output / 'page.png'
        path.write_bytes(b'\x89PNG\r\n\x1a\nfirst')
        first = self.deliver([unit], '1-1')
        self.ns['apply_delivery_ack'](self.root, self.ack(first, ['unit', 'visual']))
        path.write_bytes(b'\x89PNG\r\n\x1a\nsecond')
        second = self.deliver([unit], '2-1')
        self.assertEqual([item['evidence_kind'] for item in second['unit_texts']], ['visual'])

    def test_oversized_visual_remains_unacknowledgeable(self):
        unit = self.unit()
        unit['metadata']['visual_artifact'] = 'large.png'
        (self.output / 'large.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'x'*(512*1024 - 7))
        report = self.deliver([unit], '1-1')
        visual = report['unit_texts'][1]
        self.assertFalse(visual['unit_evidence_complete'])
        self.assertNotIn('artifact_base64', visual)
        self.assertEqual(report['unit_delivery_proposal']['evidence_count'], 1)
        self.assertFalse(report['unit_delivery_complete_for_cloud_snapshot'])

    def test_visuals_share_report_budget_and_undelivered_evidence_resumes(self):
        png = b'\x89PNG\r\n\x1a\n' + b'x'*(512*1024 - 8)
        (self.output / 'page.png').write_bytes(png)
        units = [self.unit(source) for source in ('0', '1', '2')]
        for unit in units:
            unit['metadata']['visual_artifact'] = 'page.png'
        first = self.deliver(units, '1-1')
        self.assertLess(len(self.ns['raw_json'](first)), 2*1024*1024)
        self.assertFalse(first['unit_delivery_complete_for_cloud_snapshot'])
        self.ns['apply_delivery_ack'](self.root, self.ack(first, ['unit', 'visual']))
        second = self.deliver(units, '2-1')
        delivered_keys = {item['unit_key'] for item in first['unit_texts']} | {item['unit_key'] for item in second['unit_texts']}
        self.assertEqual(len(delivered_keys), 6)
        self.assertTrue(second['unit_delivery_complete_for_cloud_snapshot'])


if __name__ == '__main__':
    unittest.main()
