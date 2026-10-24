"""Synthetic tests only: no SSH or AWS requests, no real identity/configuration."""
import argparse
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import run_corpus_capacity_probe as probe
import persist_quality_first_journal as journal
import research_vault
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


class CapacityProbeTests(unittest.TestCase):
    def setUp(self):
        self.private = X25519PrivateKey.generate()
        self.recipient = research_vault.recipient_for(self.private)
        self.env = {'GITHUB_ACTIONS': 'true', 'CORPUS_SSH_HOST': 'example.invalid',
                    'CORPUS_SSH_USER': 'worker',
                    'CORPUS_SSH_KNOWN_HOSTS': 'example.invalid ssh-ed25519 ' + base64.b64encode(b'x' * 64).decode(),
                    'AWS_ADMIN_SSH_KEY': '-----BEGIN OPENSSH PRIVATE KEY-----\n' + 'SYNTHETIC' * 12 + '\n-----END OPENSSH PRIVATE KEY-----\n',
                    'QUALITY_FIRST_RECIPIENT_JSON_B64': base64.b64encode(research_vault.canonical(self.recipient)).decode(),
                    'QUALITY_FIRST_RECIPIENT_ID': self.recipient['recipient_id']}

    def test_noncloud_execution_refused_before_ssh(self):
        with patch.object(probe.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'GITHUB_ACTIONS_REQUIRED'):
                probe.observe({**self.env, 'GITHUB_ACTIONS': 'false'})
            run.assert_not_called()

    def test_invalid_hosts_or_unpinned_key_fail_before_transport(self):
        for mutation in ({'CORPUS_SSH_HOST': '-oProxyCommand=bad'},
                         {'CORPUS_SSH_USER': 'u;command'},
                         {'CORPUS_SSH_KNOWN_HOSTS': ''},
                         {'CORPUS_SSH_KNOWN_HOSTS': self.env['CORPUS_SSH_KNOWN_HOSTS'].replace('example.invalid', '*')},
                         {'AWS_ADMIN_SSH_KEY': ''}):
            with self.subTest(mutation=mutation), patch.object(probe.subprocess, 'run') as run:
                with self.assertRaises(ValueError):
                    probe.observe({**self.env, **mutation})
                run.assert_not_called()

    def test_strict_pin_private_files_fixed_command_and_timeout(self):
        private_paths = []
        def execute(command, **kwargs):
            identity = Path(command[command.index('-i') + 1])
            known = Path(next(x.split('=', 1)[1] for x in command if x.startswith('UserKnownHostsFile=')))
            private_paths.extend((identity, known))
            self.assertEqual(identity.stat().st_mode & 0o777, 0o600)
            self.assertEqual(known.stat().st_mode & 0o777, 0o600)
            self.assertIn('StrictHostKeyChecking=yes', command)
            self.assertIn('UpdateHostKeys=no', command)
            self.assertIn('IdentitiesOnly=yes', command)
            self.assertIn('ClearAllForwardings=yes', command)
            self.assertEqual(command[-2:], ['worker@example.invalid', 'python3 -'])
            self.assertEqual(kwargs['timeout'], 45)
            self.assertEqual(kwargs['input'], probe.REMOTE_PROBE.encode())
            self.assertNotIn('shell', kwargs)
            return subprocess.CompletedProcess(command, 0, b'{"scope":"read_only_capacity_no_data_scan"}', b'')
        result = probe.observe(self.env, execute)
        self.assertEqual(result['status'], 'observed')
        self.assertTrue(all(not path.exists() for path in private_paths))

    def test_timeout_preserves_bounded_private_diagnostics(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('ssh', 45, output=b'private-output', stderr=b'private-host-error')
        result = probe.observe(self.env, timeout)
        self.assertEqual(result['status'], 'transport_timeout')
        self.assertEqual(base64.b64decode(result['stderr_base64']), b'private-host-error')

    def test_only_ciphertext_and_generic_status_are_delivered(self):
        marker = 'PRIVATE_CAPACITY_AND_HOST_DETAILS'
        report = {'version': 1, 'status': 'transport_failed', 'private_diagnostic': marker}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, self.env), \
                patch.object(probe, 'observe', return_value=report):
            output = Path(folder) / 'delivery'
            public = io.StringIO()
            with contextlib.redirect_stdout(public), contextlib.redirect_stderr(public):
                code = probe.run(argparse.Namespace(output=output))
            self.assertEqual(code, 2)
            self.assertNotIn(marker, public.getvalue())
            self.assertNotIn('example.invalid', public.getvalue())
            status = journal.inspect_delivery(output, self.recipient['recipient_id'])['status']
            self.assertEqual(status['status'], 'failed')
            self.assertEqual(status['failed_stage'], 'acquisition')
            parts = []
            for part in sorted(output.glob('part-*.vault')):
                envelope = json.loads(research_vault.unseal(part.read_bytes(), self.private))
                parts.append(base64.b64decode(envelope['payload_base64']))
            with tarfile.open(fileobj=io.BytesIO(b''.join(parts)), mode='r:gz') as archive:
                recovered = json.loads(archive.extractfile('results/capacity.json').read())
            self.assertEqual(recovered['private_diagnostic'], marker)
            self.assertEqual(set(path.name for path in output.iterdir()), {'part-0000.vault', 'status.json'})


if __name__ == '__main__':
    unittest.main()
