import unittest
from pathlib import Path

from tools.archive_crypto import decrypt_bytes, encrypt_bytes


class ArchiveCryptoTests(unittest.TestCase):
    def test_round_trip_and_no_plaintext_in_envelope(self):
        plaintext = b"private strategy reasoning: do not publish"
        envelope = encrypt_bytes(plaintext, "fixture-passphrase")
        self.assertNotIn(plaintext, envelope)
        self.assertEqual(decrypt_bytes(envelope, "fixture-passphrase"), plaintext)

    def test_wrong_key_and_tampering_fail(self):
        envelope = encrypt_bytes(b"secret", "fixture-passphrase")
        with self.assertRaises(ValueError):
            decrypt_bytes(envelope, "wrong")
        tampered = bytearray(envelope)
        tampered[-3] = ord("0") if tampered[-3] != ord("0") else ord("1")
        with self.assertRaises(ValueError):
            decrypt_bytes(bytes(tampered), "fixture-passphrase")

    def test_runtime_key_is_required(self):
        with self.assertRaises(ValueError):
            encrypt_bytes(b"offline-only", "")

    def test_public_docs_disclaim_weak_short_passphrases(self):
        root = Path(__file__).resolve().parents[1]
        text = "\n".join((root / path).read_text(encoding="utf-8") for path in (
            "README.md", "research/README.md", "docs/research_vault.md", "tools/archive_crypto.py"
        ))
        self.assertIn("four-digit", text)
        self.assertIn("weak", text)
        self.assertIn("not strong confidentiality", text)


if __name__ == "__main__":
    unittest.main()
