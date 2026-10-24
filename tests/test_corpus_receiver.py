"""Tiny synthetic storage/transport boundaries; never contact remote hosts."""
import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from corpus_receiver import CHUNK_MAX, FILE_MAX, Rejected, Store, handle, validate_header

SID = 'a' * 64


def header(offset=0, amount=5, size=5, sid=SID, relative='book/chapter.txt'):
    return {'op': 'upload', 'source_id': sid, 'offset': offset, 'chunk_bytes': amount,
            'size_bytes': size, 'relative_path': relative}


class ReceiverBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.store = Store(self.root, spool_max=32, free_floor=0)

    def tearDown(self):
        self.temp.cleanup()

    def test_interrupted_append_resumes_exact_offset_and_final_is_immutable(self):
        partial = handle(self.store, header(), io.BytesIO(b'hel'))
        self.assertFalse(partial['ok'])
        self.assertEqual(self.store.status(SID)['offset'], 3)
        with self.assertRaises(Rejected):
            handle(self.store, header(), io.BytesIO(b'hello'))
        handle(self.store, header(offset=3, amount=2), io.BytesIO(b'lo'))
        result = self.store.finish(SID)
        self.assertEqual(result['sha256'], hashlib.sha256(b'hello').hexdigest())
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(self.store.finish(SID), result)
        with self.assertRaises(Rejected):
            handle(self.store, header(), io.BytesIO(b'other'))

    def test_no_path_or_command_escape(self):
        for bad in ('../secret', '/etc/passwd', 'C:/secret', 'a\\b'):
            with self.subTest(bad=bad), self.assertRaises(Rejected):
                validate_header(header(relative=bad))
        for op in ('delete', 'exec', 'list'):
            with self.assertRaises(Rejected):
                validate_header({'op': op, 'source_id': SID})
        with self.assertRaises(Rejected):
            validate_header({'op': 'status', 'source_id': SID, 'root': '/etc'})

    def test_file_and_chunk_limits_before_body_read(self):
        for args in ({'size': FILE_MAX + 1}, {'amount': CHUNK_MAX + 1, 'size': FILE_MAX}):
            with self.assertRaises(Rejected):
                validate_header(header(**args))

    def test_symlink_storage_rejected(self):
        target = self.root / 'target'
        target.write_bytes(b'fixture')
        (self.store.partial / SID).symlink_to(target)
        with self.assertRaises(Rejected):
            self.store.status(SID)

    def test_spool_limit_and_metadata_identity(self):
        self.store.spool_max = 4
        handle(self.store, header(amount=3, size=3), io.BytesIO(b'abc'))
        with self.assertRaises(Rejected):
            handle(self.store, header(amount=2, size=2, sid='b' * 64), io.BytesIO(b'de'))
        with self.assertRaises(Rejected):
            handle(self.store, header(offset=3, amount=1, size=4), io.BytesIO(b'd'))

    def test_finish_recovers_rename_before_metadata_checkpoint(self):
        handle(self.store, header(), io.BytesIO(b'hello'))
        os.rename(self.store.partial / SID, self.store.objects / SID)
        self.assertEqual(self.store.status(SID)['state'], 'finalization_pending')
        self.assertEqual(self.store.finish(SID)['state'], 'complete')


if __name__ == '__main__':
    unittest.main()
