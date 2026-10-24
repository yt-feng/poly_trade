"""Synthetic small result checks; no source PDFs, cloud access or publication."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import verify_delivery as verify


class DeliveryVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.report_path = self.root / 'report.json'
        self.receipt_path = self.root / 'receipt.json'
        self.text = verify.canonical({'native_text': '原生文本', 'ocr_text': 'OCR evidence'})
        self.png = b'\x89PNG\r\n\x1a\nsynthetic-picture'
        unit = {'source_id': 'a'*64, 'unit_index': 0, 'unit_kind': 'page', 'artifact': 'unit.json',
                'content_hash': verify.digest(self.text), 'status': 'extracted',
                'metadata': {'visual_artifact': 'page.png', 'ocr_quality_verified': False}}
        self.items = []
        for kind, raw in [('unit', self.text), ('visual', self.png)]:
            key = 'a'*64 + ':000000000000:' + kind
            item = {'unit': unit, 'unit_key': key, 'evidence_kind': kind, 'artifact_complete': True,
                    'unit_evidence_complete': True, 'semantic_read_credited': False,
                    'artifact_sha256': verify.digest(raw)}
            if kind == 'unit':
                item['artifact_utf8'] = raw.decode()
            else:
                item.update(artifact='page.png', artifact_base64=base64.b64encode(raw).decode(),
                            artifact_bytes=len(raw), artifact_mime_type='image/png', visual_review_credited=False)
            item['evidence_digest'] = verify.digest(verify.canonical({'version': 1, 'unit': unit,
                                                                     'evidence_kind': kind, 'artifact_hash': verify.digest(raw)}))
            self.items.append(item)
        self.proposal = {'version': 1, 'worker_hash': 'b'*64, 'source_run': '123-1',
                         'units': {item['unit_key']: item['evidence_digest'] for item in self.items},
                         'content_hashes': {item['unit_key']: item['artifact_sha256'] for item in self.items},
                         'semantics': verify.SEMANTICS}
        self.report = {'run_id': '123-1', 'worker_sha256': 'b'*64, 'semantic_learning_complete': False,
                       'unit_delivery_protocol': verify.PROTOCOL, 'unit_texts': self.items,
                       'unit_delivery_proposal': {'worker_hash': 'b'*64, 'source_run': '123-1',
                                                  'proposal_hash': verify.digest(verify.canonical(self.proposal)), 'evidence_count': 2,
                                                  'content_hashes': self.proposal['content_hashes']}}
        identity = {'repository': 'yt-feng/poly_trade', 'run_id': '123', 'run_attempt': '1'}
        self.receipt = {'status': 'verified_remote', 'source_run': '123-1',
                        'episode_id': verify.digest(b'quality-first-sealed-episode-v1\0' + verify.canonical(identity)),
                        'recipient_id': 'c'*64, 'source_artifact_directory': '/private/synthetic',
                        'verification': 'existing publisher remote readback'}

    def run_verifier(self, report=None, receipt=None):
        self.report_path.write_bytes(verify.canonical(self.report if report is None else report))
        if receipt is not None:
            self.receipt_path.write_bytes(verify.canonical(receipt))
        return verify.verify(self.report_path, self.root / 'readback', self.receipt_path if receipt is not None else None)

    def test_no_receipt_only_creates_candidate_then_verified_receipt_allows_ack(self):
        pending = self.run_verifier()
        output = Path(pending['readback_directory'])
        self.assertFalse((output / 'ack.json').exists())
        self.assertTrue((output / 'ack-candidate.json').exists())
        self.run_verifier(receipt=self.receipt)
        ack = json.loads((output / 'ack.json').read_bytes())
        self.assertEqual(ack['evidence_kinds'], ['unit', 'visual'])
        self.assertEqual((output / (verify.digest(self.png) + '.png')).read_bytes(), self.png)
        self.assertEqual((output / (verify.digest(self.text) + '.json')).read_bytes(), self.text)

    def test_corrupt_content_rejected_before_any_ack(self):
        broken = copy.deepcopy(self.report)
        broken['unit_texts'][0]['artifact_utf8'] = '{"tampered":true}'
        with self.assertRaisesRegex(ValueError, 'artifact_hash_mismatch'):
            self.run_verifier(broken, self.receipt)
        self.assertFalse((self.root / 'readback').exists())

    def test_omitted_complete_item_breaks_proposal_verification(self):
        broken = copy.deepcopy(self.report)
        broken['unit_texts'].pop()
        with self.assertRaisesRegex(ValueError, 'proposal_hash_or_coverage_mismatch'):
            self.run_verifier(broken, self.receipt)

    def test_legacy_proposal_shape_remains_accepted(self):
        legacy = copy.deepcopy(self.report)
        legacy_proposal = copy.deepcopy(self.proposal)
        legacy_proposal.pop('content_hashes')
        legacy['unit_delivery_proposal'].pop('content_hashes')
        legacy['unit_delivery_proposal']['proposal_hash'] = verify.digest(verify.canonical(legacy_proposal))
        result = self.run_verifier(legacy)
        self.assertEqual(result['status'], 'verified_awaiting_publication')

    def test_empty_new_proposal_with_content_hashes_is_accepted(self):
        empty = copy.deepcopy(self.report)
        empty['unit_texts'] = []
        proposal = {'version': 1, 'worker_hash': 'b'*64, 'source_run': '123-1',
                    'units': {}, 'content_hashes': {}, 'semantics': verify.SEMANTICS}
        empty['unit_delivery_proposal'].update(
            proposal_hash=verify.digest(verify.canonical(proposal)), evidence_count=0, content_hashes={})
        result = self.run_verifier(empty)
        self.assertEqual(result['verified_evidence_count'], 0)

    def test_content_hashes_must_match_checked_artifacts(self):
        broken = copy.deepcopy(self.report)
        key = next(iter(broken['unit_delivery_proposal']['content_hashes']))
        broken['unit_delivery_proposal']['content_hashes'][key] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'proposal_content_hash_mismatch'):
            self.run_verifier(broken)

    def test_replay_is_idempotent_and_wrong_run_receipt_is_rejected(self):
        result = self.run_verifier(receipt=self.receipt)
        output = Path(result['readback_directory'])
        before = {path.name: path.read_bytes() for path in output.iterdir()}
        self.run_verifier(receipt=self.receipt)
        self.assertEqual(before, {path.name: path.read_bytes() for path in output.iterdir()})
        with self.assertRaisesRegex(ValueError, 'run_or_branch_mismatch'):
            self.run_verifier(receipt=dict(self.receipt, source_run='124-1'))

    def test_tampered_evidence_digest_and_unverified_publication_are_rejected(self):
        broken = copy.deepcopy(self.report)
        broken['unit_texts'][1]['evidence_digest'] = '0'*64
        with self.assertRaisesRegex(ValueError, 'evidence_digest_mismatch'):
            self.run_verifier(broken)
        with self.assertRaisesRegex(ValueError, 'publication_receipt_not_verified_for_run'):
            self.run_verifier(receipt=dict(self.receipt, status='persisted'))


if __name__ == '__main__':
    unittest.main()

