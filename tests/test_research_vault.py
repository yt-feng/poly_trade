import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import research_vault as v
from research_journal_guard import allowed_new_path, audit_paths

class VaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pin = 'SYNTHETIC-PIN-ONLY'
        cls.pub, cls.bundle, cls.recovery = v.make_identity(cls.pin)
        cls.key = v.unlock_identity(cls.bundle, cls.recovery, cls.pin)
    def seal(self, data=b'synthetic research note'):
        return v.seal(data, self.pub, self.pub['recipient_id'])
    def test_roundtrip(self):
        self.assertEqual(v.unseal(self.seal(), self.key), b'synthetic research note')
    def test_empty(self):
        self.assertEqual(v.unseal(self.seal(b''), self.key), b'')
    def test_unicode(self):
        data='Synthetic Unicode: \u6d4b\u8bd5'.encode();self.assertEqual(v.unseal(self.seal(data), self.key),data)
    def test_randomized(self):
        self.assertNotEqual(self.seal(),self.seal())
    def test_plaintext_absent(self):
        self.assertNotIn(b'synthetic research note', self.seal())
    def test_bad_pin(self):
        with self.assertRaises(InvalidTag):v.unlock_identity(self.bundle,self.recovery,'wrong')
    def test_bad_recovery(self):
        with self.assertRaises(InvalidTag):v.unlock_identity(self.bundle,bytes(32),self.pin)
    def test_short_recovery(self):
        with self.assertRaises(ValueError):v.unlock_identity(self.bundle,b'x',self.pin)
    def test_fingerprint(self):
        with self.assertRaises(ValueError):v.seal(b'x',self.pub,'0'*64)
    def test_changed_recipient(self):
        pub=copy.deepcopy(self.pub);pub['public_key']=v.b64(bytes(32))
        with self.assertRaises(ValueError):v.seal(b'x',pub,self.pub['recipient_id'])
    def test_wrong_identity(self):
        with self.assertRaises(ValueError):v.unseal(self.seal(),X25519PrivateKey.generate())
    def test_tampered_ciphertext(self):
        obj=v.load_json_bytes(self.seal());raw=bytearray(v.unb64(obj['ciphertext']));raw[0]^=1;obj['ciphertext']=v.b64(bytes(raw))
        with self.assertRaises(InvalidTag):v.unseal(v.canonical(obj),self.key)
    def test_tampered_nonce(self):
        obj=v.load_json_bytes(self.seal());obj['nonce']=v.b64(bytes(12))
        with self.assertRaises(InvalidTag):v.unseal(v.canonical(obj),self.key)
    def test_extra_fields(self):
        obj=v.load_json_bytes(self.seal());obj['plaintext']='not allowed'
        with self.assertRaises(ValueError):v.inspect_envelope(v.canonical(obj))
    def test_duplicate_fields(self):
        with self.assertRaises(ValueError):v.load_json_bytes(b'{"a":1,"a":2}')
    def test_limit(self):
        with patch.object(v,'MAX_PLAINTEXT',4):
            with self.assertRaises(ValueError):self.seal(b'12345')
    def test_write_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'x';v.write_new(p,b'first')
            with self.assertRaises(FileExistsError):v.write_new(p,b'next')
            self.assertEqual(p.read_bytes(),b'first')
    def test_private_path_in_cwd_rejected(self):
        with self.assertRaises(ValueError):v.require_private_location(Path.cwd()/'unsafe')
    def test_ci_init_forbidden(self):
        with patch.dict(os.environ,{'CI':'true'}),patch.object(sys,'argv',['vault','init','--identity-dir','/tmp/never-written']):
            with self.assertRaises(ValueError):v.main()
    def test_inspect_not_authentication(self):
        obj=v.load_json_bytes(self.seal());obj['ciphertext']=v.b64(bytes(16))
        self.assertEqual(v.inspect_envelope(v.canonical(obj))['scheme'],v.SCHEME)
        with self.assertRaises(InvalidTag):v.unseal(v.canonical(obj),self.key)
    def test_guard_private_names(self):
        for p in ['private_logs/a.json','x/recovery.key','identity.keybundle','docs/r99_findings.md','research_vault/entries/x.txt']:
            with self.subTest(p=p):self.assertFalse(allowed_new_path(p))
    def test_guard_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'research_vault/entries/a.vault';v.write_new(p,self.seal())
            self.assertTrue(audit_paths(['research_vault/entries/a.vault'],d)['passed'])
    def test_guard_invalid_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'research_vault/entries/a.vault';v.write_new(p,b'not encrypted')
            self.assertFalse(audit_paths(['research_vault/entries/a.vault'],d)['passed'])
    def test_guard_not_semantic_scan(self):
        result=audit_paths(['docs/research_vault.md']);self.assertFalse(result['semantic_secret_scan'])

if __name__=='__main__':unittest.main()
