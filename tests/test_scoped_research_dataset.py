"""Synthetic contracts for one-envelope saved-data capabilities."""
import base64
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tools import research_vault as vault
from tools import scoped_research_dataset as dataset


class ScopedResearchDatasetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.key = vault.X25519PrivateKey.generate()
        self.recipient = vault.recipient_for(self.key)
        self.files = {'data/table.csv': b'time,value\n1,2\n', 'origin.json': b'{"source":"synthetic"}'}
        self.paths = {}
        for index, (name, value) in enumerate(self.files.items()):
            path = self.root / ('input-' + str(index))
            path.write_bytes(value)
            self.paths[name] = path
        self.envelope, self.capability, self.descriptor = dataset.pack(
            self.paths, self.recipient, self.recipient['recipient_id'])
        self.descriptor_hash = dataset.descriptor_sha256(self.descriptor)

    def _restore(self, envelope=None, capability=None, digest=None, name='output'):
        return dataset.restore(self.envelope if envelope is None else envelope,
                               self.capability if capability is None else capability,
                               self.descriptor_hash if digest is None else digest,
                               self.root / name)

    def _reject(self, envelope=None, capability=None, digest=None, name='output'):
        with self.assertRaises((ValueError, OSError)):
            self._restore(envelope, capability, digest, name)
        self.assertFalse((self.root / name).exists())
        self.assertFalse(list(self.root.glob('.dataset-stage-*')))

    def _bundle(self):
        part = vault.load_json_bytes(vault.unseal(self.envelope, self.key))
        return base64.b64decode(part['payload_base64'])

    def _reseal(self, *, bundle=None, part=None, payload=None, descriptor=None):
        descriptor = copy.deepcopy(self.descriptor if descriptor is None else descriptor)
        if bundle is not None:
            descriptor['bundle_bytes'] = len(bundle)
            descriptor['bundle_sha256'] = hashlib.sha256(bundle).hexdigest()
        if payload is None:
            if part is None:
                part = {'format': dataset.PART_FORMAT, 'index': 0, 'total': 1,
                        'bundle_sha256': descriptor['bundle_sha256'],
                        'payload_base64': vault.b64(self._bundle() if bundle is None else bundle)}
            payload = vault.canonical(part)
        envelope, key = dataset._seal_part(payload, self.recipient, self.recipient['recipient_id'])
        descriptor['cipher_bytes'], descriptor['cipher_sha256'] = len(envelope), hashlib.sha256(envelope).hexdigest()
        capability = dict(descriptor, content_key_b64=vault.b64(key))
        return envelope, capability, dataset.descriptor_sha256(descriptor)

    def _tar(self, members):
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, value, kind in members:
                info = tarfile.TarInfo(name)
                info.type, info.size = kind, len(value)
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = 'elsewhere'
                    info.size = 0
                archive.addfile(info, io.BytesIO(value) if info.size else None)
        return gzip.compress(buffer.getvalue(), mtime=0)

    def _members(self):
        with tarfile.open(fileobj=io.BytesIO(self._bundle()), mode='r:gz') as archive:
            return [(item.name, archive.extractfile(item).read(), tarfile.REGTYPE) for item in archive]

    def test_roundtrip_and_private_output_permissions(self):
        report = self._restore()
        self.assertEqual(report['status'], 'saved_dataset_restored')
        self.assertEqual(report['files_verified'], 3)
        self.assertFalse(report['strategy_evaluated'])
        self.assertFalse(report['live_orders_enabled'])
        for name, value in self.files.items():
            path = self.root / 'output' / name
            self.assertEqual(path.read_bytes(), value)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / 'output').stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.root / 'output/data').stat().st_mode), 0o700)

    def test_existing_vault_can_decrypt_exact_wrapper(self):
        part = vault.load_json_bytes(vault.unseal(self.envelope, self.key))
        self.assertEqual(set(part), {'format', 'index', 'total', 'bundle_sha256', 'payload_base64'})
        self.assertEqual((part['format'], part['index'], part['total']), (dataset.PART_FORMAT, 0, 1))
        self.assertEqual(hashlib.sha256(base64.b64decode(part['payload_base64'])).hexdigest(), self.descriptor['bundle_sha256'])

    def test_capability_contains_no_global_identity_and_descriptor_report_no_key(self):
        self.assertEqual(set(self.capability), dataset.CAPABILITY_KEYS)
        serialized = vault.canonical(self.capability)
        self.assertNotIn(vault.b64(vault.raw_private(self.key)).encode(), serialized)
        self.assertEqual(len(base64.b64decode(self.capability['content_key_b64'])), 32)
        report = self._restore()
        for value in (self.descriptor, report):
            self.assertNotIn(self.capability['content_key_b64'], json.dumps(value))
            self.assertNotIn('content_key', json.dumps(value))

    def test_second_envelope_has_independent_capability(self):
        other, other_capability, other_descriptor = dataset.pack(self.paths, self.recipient, self.recipient['recipient_id'])
        self.assertNotEqual(self.capability['content_key_b64'], other_capability['content_key_b64'])
        self.assertNotEqual(self.envelope, other)
        wrong = dict(other_capability, content_key_b64=self.capability['content_key_b64'])
        self._reject(other, wrong, dataset.descriptor_sha256(other_descriptor))

    def test_wrong_key_cannot_decrypt_other_envelope_or_identity(self):
        wrong = dict(self.capability, content_key_b64=vault.b64(b'\x01' * 32))
        self._reject(capability=wrong)
        other_identity = vault.X25519PrivateKey.generate()
        with self.assertRaises(ValueError):
            vault.unseal(self.envelope, other_identity)

    def test_descriptor_digest_and_original_objects_immutable(self):
        before = copy.deepcopy(self.capability)
        descriptor = dataset.descriptor_from_capability(self.capability)
        descriptor['files'][0]['path'] = 'changed.csv'
        self.assertEqual(self.capability, before)
        self._reject(digest='0' * 64)
        self.assertEqual(dataset.descriptor_sha256(self.descriptor), hashlib.sha256(vault.canonical(self.descriptor)).hexdigest())

    def test_allowlist_changes_require_new_registered_descriptor(self):
        for mutation in ('name', 'hash', 'bytes', 'extra'):
            with self.subTest(mutation=mutation):
                cap = copy.deepcopy(self.capability)
                if mutation == 'name':
                    cap['files'][0]['path'] = 'data/other.csv'
                elif mutation == 'hash':
                    cap['files'][0]['sha256'] = '0' * 64
                elif mutation == 'bytes':
                    cap['files'][0]['bytes'] += 1
                else:
                    cap['unregistered'] = True
                self._reject(capability=cap)

    def test_cipher_digest_checked_before_aes_decryption(self):
        with mock.patch.object(dataset.research_vault, 'AESGCM', side_effect=AssertionError('must not construct')):
            self._reject(envelope=self.envelope[:-1] + b'!')

    def test_authenticated_aad_header_tamper_rejected(self):
        for field, value in [('recipient_id', 'a' * 64), ('salt', vault.b64(b's' * 16)),
                             ('ephemeral_public', vault.b64(b'p' * 32)), ('nonce', vault.b64(b'n' * 12))]:
            with self.subTest(field=field):
                document = json.loads(self.envelope)
                document[field] = value
                envelope = vault.canonical(document)
                cap = dict(self.capability, cipher_bytes=len(envelope), cipher_sha256=hashlib.sha256(envelope).hexdigest())
                self._reject(envelope, cap, dataset.descriptor_sha256(dataset.descriptor_from_capability(cap)))

    def test_duplicate_json_at_each_layer_rejected(self):
        cases = [self.envelope[:-1] + b',"version":1}']
        for envelope in cases:
            cap = dict(self.capability, cipher_bytes=len(envelope), cipher_sha256=hashlib.sha256(envelope).hexdigest())
            self._reject(envelope, cap, dataset.descriptor_sha256(dataset.descriptor_from_capability(cap)))
        triple = self._reseal(payload=b'{"format":"x","format":"y"}')
        self._reject(*triple)
        with self.assertRaises(ValueError):
            dataset._load_json(b'{"version":1,"version":1}', 100)

    def test_strict_schema_numeric_bool_constants_and_base64(self):
        for field, value in [('version', True), ('cipher_bytes', True), ('bundle_bytes', -1),
                             ('cipher_sha256', 'A' * 64), ('content_key_b64', '!!!!')]:
            with self.subTest(field=field):
                self._reject(capability=dict(self.capability, **{field: value}))
        for encoded in (b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}'):
            with self.assertRaises(ValueError):
                dataset._load_json(encoded, 100)
        with self.assertRaises(ValueError):
            dataset._unb64('Zh==')  # Nonzero discarded bits are not canonical.

    def test_wrapper_single_part_and_exact_fields_only(self):
        original = json.loads(vault.unseal(self.envelope, self.key))
        for change in ({'index': True}, {'index': 1}, {'total': 2}, {'format': 'other'},
                       {'extra': 1}, {'bundle_sha256': '0' * 64}, {'payload_base64': '!!!!'}):
            with self.subTest(change=change):
                self._reject(*self._reseal(part=dict(original, **change)))

    def test_gzip_trailing_concatenated_and_truncated_streams_rejected(self):
        bundle = self._bundle()
        for invalid in (bundle + b'extra', bundle + gzip.compress(b'other'), bundle[:-3]):
            with self.subTest(length=len(invalid)):
                self._reject(*self._reseal(bundle=invalid))

    def test_gzip_expansion_cap_rejected_before_output(self):
        with mock.patch.object(dataset, 'MAX_EXPANDED_BYTES', 1024):
            self._reject()

    def test_tar_missing_extra_duplicate_members_rejected(self):
        members = self._members()
        for invalid in (members[:-1], members + [('extra.csv', b'1', tarfile.REGTYPE)], members + [members[0]]):
            with self.subTest(names=[item[0] for item in invalid]):
                self._reject(*self._reseal(bundle=self._tar(invalid)))

    def test_tar_traversal_absolute_and_nonregular_members_rejected(self):
        names = ('../escape', '/absolute', 'data/../../escape', './origin.json', 'data//file', 'a\\b')
        for name in names:
            with self.subTest(name=name):
                self._reject(*self._reseal(bundle=self._tar(self._members() + [(name, b'x', tarfile.REGTYPE)])))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE,
                     tarfile.CHRTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.XHDTYPE, tarfile.XGLTYPE):
            with self.subTest(kind=kind):
                self._reject(*self._reseal(bundle=self._tar([(self._members()[0][0], b'x', kind)])))
        self.assertFalse((self.root.parent / 'escape').exists())

    def test_tar_hash_size_manifest_and_trailing_archive_rejected(self):
        original = self._members()
        for index, replacement in [(0, b'bad'), (1, b'bad')]:
            members = list(original)
            members[index] = (members[index][0], replacement, tarfile.REGTYPE)
            self._reject(*self._reseal(bundle=self._tar(members)))
        expanded = gzip.decompress(self._bundle())
        for raw in (expanded + b'not-padding', expanded + gzip.decompress(self._tar(original))):
            self._reject(*self._reseal(bundle=gzip.compress(raw)))

    def test_allowlisted_content_hash_valid_but_manifest_semantics_wrong(self):
        records = [entry for entry in self.descriptor['files'] if entry['path'] != dataset.MANIFEST_NAME]
        float_records = copy.deepcopy(records)
        float_records[0]['bytes'] = float(float_records[0]['bytes'])
        cases = [{'version': 1, 'format': dataset.FORMAT, 'files': []},
                 {'version': True, 'format': dataset.FORMAT, 'files': records},
                 {'version': 1, 'format': dataset.FORMAT, 'files': float_records}]
        for document in cases:
            with self.subTest(document=document):
                members = self._members()
                descriptor = copy.deepcopy(self.descriptor)
                wrong = vault.canonical(document)
                members = [(name, wrong if name == dataset.MANIFEST_NAME else value, kind) for name, value, kind in members]
                for entry in descriptor['files']:
                    if entry['path'] == dataset.MANIFEST_NAME:
                        entry.update(bytes=len(wrong), sha256=hashlib.sha256(wrong).hexdigest())
                self._reject(*self._reseal(bundle=self._tar(members), descriptor=descriptor))

    def test_pack_requires_exact_expected_file_hashes_and_sizes(self):
        expected = {name: {'bytes': len(value), 'sha256': hashlib.sha256(value).hexdigest()}
                    for name, value in self.files.items()}
        envelope, cap, desc = dataset.pack(self.paths, self.recipient, self.recipient['recipient_id'], expected_files=expected)
        self._restore(envelope, cap, dataset.descriptor_sha256(desc))
        expected['origin.json']['bytes'] += 1
        with self.assertRaises(ValueError):
            dataset.pack(self.paths, self.recipient, self.recipient['recipient_id'], expected_files=expected)

    def test_pack_rejects_symlink_reserved_invalid_file_count_and_conflicts(self):
        link = self.root / 'link'
        link.symlink_to(next(iter(self.paths.values())))
        with self.assertRaises(OSError):
            dataset.pack({'input.csv': link}, self.recipient, self.recipient['recipient_id'])
        cases = [{}, {dataset.MANIFEST_NAME: next(iter(self.paths.values()))},
                 {'../bad': next(iter(self.paths.values()))},
                 {str(i): next(iter(self.paths.values())) for i in range(dataset.MAX_FILES)},
                 {'data': next(iter(self.paths.values())), 'data/table': next(iter(self.paths.values()))}]
        for files in cases:
            with self.subTest(files=list(files)), self.assertRaises(ValueError):
                dataset.pack(files, self.recipient, self.recipient['recipient_id'])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO requires POSIX')
    def test_fifo_open_never_waits_for_writer_before_regular_file_check(self):
        fifo = self.root / 'fifo'
        os.mkfifo(fifo, 0o600)
        script = ('from tools.scoped_research_dataset import _read_file\n'
                  'import sys\n'
                  'try:\n _read_file(sys.argv[1], 1024)\n'
                  'except ValueError:\n sys.exit(0)\n'
                  'sys.exit(2)\n')
        result = subprocess.run([sys.executable, '-c', script, str(fifo)], capture_output=True,
                                text=True, timeout=3, cwd=Path(dataset.__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hardlinked_input_is_rejected(self):
        original = next(iter(self.paths.values()))
        linked = self.root / 'hardlink'
        os.link(original, linked)
        for path in (original, linked):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                dataset._read_file(path, 1024)

    def test_input_mutation_during_read_is_rejected(self):
        path = next(iter(self.paths.values()))
        before = path.stat()
        fields = ('st_mode', 'st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
        for changed_field in ('st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink'):
            with self.subTest(changed_field=changed_field):
                modified = {field: getattr(before, field) for field in fields}
                modified[changed_field] += 1
                with mock.patch.object(dataset.os, 'fstat', side_effect=[before, SimpleNamespace(**modified)]):
                    with self.assertRaisesRegex(ValueError, 'DATASET_INPUT_CHANGED'):
                        dataset._read_file(path, 1024)

    def test_file_bundle_and_envelope_size_caps(self):
        for limit in ('MAX_FILE_BYTES', 'MAX_BUNDLE_BYTES', 'MAX_ENVELOPE_BYTES'):
            with self.subTest(limit=limit), mock.patch.object(dataset, limit, 1), self.assertRaises(ValueError):
                dataset.pack(self.paths, self.recipient, self.recipient['recipient_id'])

    def test_restore_does_not_overwrite_existing_directory_file_or_symlink(self):
        for kind in ('directory', 'file', 'symlink'):
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind == 'directory':
                    output.mkdir()
                    (output / 'existing').write_bytes(b'unchanged')
                elif kind == 'file':
                    output.write_bytes(b'unchanged')
                else:
                    output.symlink_to(self.root / 'input-0')
                with self.assertRaises(FileExistsError):
                    self._restore(name=kind)
                if kind == 'directory':
                    self.assertEqual((output / 'existing').read_bytes(), b'unchanged')
                elif kind == 'file':
                    self.assertEqual(output.read_bytes(), b'unchanged')
                else:
                    self.assertTrue(output.is_symlink())

    def test_partial_io_failure_cleans_stage_and_reservation(self):
        original = dataset._write_new
        calls = []
        def failing(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('synthetic disk failure')
            return original(path, value)
        with mock.patch.object(dataset, '_write_new', side_effect=failing):
            self._reject()
        self.assertEqual(len(calls), 2)

    def test_capability_save_new_private_file_outside_working_tree_only(self):
        checkout = self.root / 'checkout'
        checkout.mkdir()
        path = self.root / 'capability.json'
        dataset.write_capability(path, self.capability, working_tree=checkout)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_bytes()), self.capability)
        with self.assertRaises(FileExistsError):
            dataset.write_capability(path, self.capability, working_tree=checkout)
        with self.assertRaises(ValueError):
            dataset.write_capability(checkout / 'capability.json', self.capability, working_tree=checkout)
        self.assertFalse((checkout / 'capability.json').exists())

    def test_cli_roundtrip_and_failure_messages_do_not_disclose_key(self):
        envelope = self.root / 'envelope.vault'
        capability = self.root / 'capability.json'
        envelope.write_bytes(self.envelope)
        capability.write_bytes(vault.canonical(self.capability))
        script = Path(dataset.__file__)
        arguments = [sys.executable, str(script), 'restore', '--envelope', str(envelope),
                     '--capability', str(capability), '--expected-descriptor-sha256', self.descriptor_hash,
                     '--output', str(self.root / 'cli-output')]
        success = subprocess.run(arguments, capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        failure = subprocess.run(arguments, capture_output=True, text=True)
        self.assertNotEqual(failure.returncode, 0)
        for result in (success, failure):
            self.assertNotIn(self.capability['content_key_b64'], result.stdout + result.stderr)
            self.assertNotIn('synthetic', result.stdout + result.stderr)
            self.assertNotIn('Traceback', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
