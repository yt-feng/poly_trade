"""Controller validation only; never processes corpus data or opens SSH."""
import ast
import base64
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
