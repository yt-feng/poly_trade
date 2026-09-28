"""Local-only research envelope encryption; no network or trading capability.

Recipients use X25519 / HKDF-SHA256 / AES-256-GCM. A public recipient can seal
new logs without access to the private identity. The local identity is protected
by BOTH a random 32-byte recovery file and the user's interactive PIN. A short
PIN alone is never a publication key. Neither private file belongs in GitHub.

This is application envelope code over PyCA primitives, not a claim of an
independent cryptographic audit. Ciphertext length and commit timing remain
public. Recipient authenticity must be checked out of band. Anyone knowing the
public key can encrypt; encryption does not authenticate the log's author.
"""
from __future__ import annotations
import argparse
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAX_PLAINTEXT = 16 * 1024 * 1024
MAX_ENVELOPE = 24 * 1024 * 1024
SCHEME = 'X25519-HKDF-SHA256-AES256GCM-v1'
INFO = b'poly-research-journal-envelope-v1'
WRAP_AAD = b'poly-research-local-identity-v1'
HEADER_KEYS = {'version', 'scheme', 'recipient_id', 'ephemeral_public', 'salt', 'nonce'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def b64(data):
    return base64.b64encode(data).decode('ascii')


def unb64(text, size=None):
    if not isinstance(text, str):
        raise ValueError('invalid encoding')
    result = base64.b64decode(text, validate=True)
    if size is not None and len(result) != size:
        raise ValueError('invalid field length')
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def load_json_bytes(data, cap=MAX_ENVELOPE):
    if not isinstance(data, bytes) or len(data) > cap:
        raise ValueError('invalid or oversized input')
    result = json.loads(data.decode('utf-8'), object_pairs_hook=unique_object)
    if not isinstance(result, dict):
        raise ValueError('JSON object required')
    return result


def raw_private(key):
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())


def raw_public(key):
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def recipient_for(key):
    public = raw_public(key.public_key())
    return {'version': 1, 'type': 'X25519', 'public_key': b64(public),
            'recipient_id': hashlib.sha256(public).hexdigest()}


def validate_recipient(recipient, expected_id):
    if set(recipient) != {'version', 'type', 'public_key', 'recipient_id'}:
        raise ValueError('invalid public recipient schema')
    if type(recipient['version']) is not int or recipient['version'] != 1 or recipient['type'] != 'X25519':
        raise ValueError('unsupported public recipient')
    public = unb64(recipient['public_key'], 32)
    fingerprint = hashlib.sha256(public).hexdigest()
    if not expected_id or fingerprint != expected_id or recipient['recipient_id'] != fingerprint:
        raise ValueError('recipient fingerprint mismatch')
    return X25519PublicKey.from_public_bytes(public)


def seal(data, recipient, expected_id):
    if not isinstance(data, bytes) or len(data) > MAX_PLAINTEXT:
        raise ValueError('plaintext limit exceeded')
    public = validate_recipient(recipient, expected_id)
    ephemeral = X25519PrivateKey.generate()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=INFO).derive(ephemeral.exchange(public))
    header = {'version': 1, 'scheme': SCHEME, 'recipient_id': expected_id,
              'ephemeral_public': b64(raw_public(ephemeral.public_key())), 'salt': b64(salt), 'nonce': b64(nonce)}
    return canonical(dict(header, ciphertext=b64(AESGCM(key).encrypt(nonce, data, canonical(header)))))


def inspect_envelope(data):
    obj = load_json_bytes(data)
    if set(obj) != HEADER_KEYS | {'ciphertext'}:
        raise ValueError('invalid envelope fields')
    if type(obj['version']) is not int or obj['version'] != 1 or obj['scheme'] != SCHEME:
        raise ValueError('unsupported envelope')
    rid = obj['recipient_id']
    if not isinstance(rid, str) or len(rid) != 64 or any(c not in '0123456789abcdef' for c in rid):
        raise ValueError('invalid recipient id')
    unb64(obj['ephemeral_public'], 32)
    unb64(obj['salt'], 16)
    unb64(obj['nonce'], 12)
    ciphertext = unb64(obj['ciphertext'])
    if not 16 <= len(ciphertext) <= MAX_PLAINTEXT + 16:
        raise ValueError('invalid ciphertext length')
    return obj


def unseal(data, private):
    obj = inspect_envelope(data)
    if recipient_for(private)['recipient_id'] != obj['recipient_id']:
        raise ValueError('wrong identity')
    header = {k: obj[k] for k in HEADER_KEYS}
    ephemeral = X25519PublicKey.from_public_bytes(unb64(obj['ephemeral_public'], 32))
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=unb64(obj['salt'], 16), info=INFO).derive(private.exchange(ephemeral))
    return AESGCM(key).decrypt(unb64(obj['nonce'], 12), unb64(obj['ciphertext']), canonical(header))


def wrapping_key(recovery, pin, salt):
    if not isinstance(recovery, bytes) or len(recovery) != 32:
        raise ValueError('32-byte recovery file required')
    if not isinstance(pin, str) or not pin or len(pin.encode('utf-8')) > 1024:
        raise ValueError('nonempty bounded PIN required')
    encoded = pin.encode('utf-8')
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(recovery + len(encoded).to_bytes(4, 'big') + encoded)


def make_identity(pin):
    private = X25519PrivateKey.generate()
    recovery, salt, nonce = secrets.token_bytes(32), secrets.token_bytes(16), secrets.token_bytes(12)
    wrapped = AESGCM(wrapping_key(recovery, pin, salt)).encrypt(nonce, raw_private(private), WRAP_AAD)
    bundle = canonical({'version': 1, 'type': 'local-identity', 'salt': b64(salt), 'nonce': b64(nonce), 'wrapped': b64(wrapped)})
    return recipient_for(private), bundle, recovery


def unlock_identity(bundle, recovery, pin):
    obj = load_json_bytes(bundle, 4096)
    if set(obj) != {'version', 'type', 'salt', 'nonce', 'wrapped'} or type(obj['version']) is not int or obj['version'] != 1 or obj['type'] != 'local-identity':
        raise ValueError('invalid local identity')
    key = wrapping_key(recovery, pin, unb64(obj['salt'], 16))
    raw = AESGCM(key).decrypt(unb64(obj['nonce'], 12), unb64(obj['wrapped'], 48), WRAP_AAD)
    return X25519PrivateKey.from_private_bytes(raw)


def read_bounded(path, cap):
    p = Path(path)
    if not p.is_file() or p.stat().st_size > cap:
        raise ValueError('missing or oversized file')
    with p.open('rb') as handle:
        data = handle.read(cap + 1)
    if len(data) > cap:
        raise ValueError('file grew beyond limit')
    return data


def write_new(path, data, mode=0o600):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def require_private_location(path):
    """CLI is intended to be run from the repository root."""
    p = Path(path).expanduser().resolve()
    root = Path.cwd().resolve()
    if p == root or root in p.parents:
        raise ValueError('private identity and plaintext output must be outside the repository working directory')
    return p


def main():
    ap = argparse.ArgumentParser(description='Local sealed journal. Never uploads keys or submits orders.')
    sub = ap.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--identity-dir', required=True)
    init.add_argument('--public-out', default='research_vault/recipient.json')
    enc = sub.add_parser('seal')
    enc.add_argument('--input', required=True)
    enc.add_argument('--output', required=True)
    enc.add_argument('--recipient', default='research_vault/recipient.json')
    enc.add_argument('--recipient-id', required=True, help='Previously verified public fingerprint; not a password')
    dec = sub.add_parser('open')
    dec.add_argument('--input', required=True)
    dec.add_argument('--output', required=True)
    dec.add_argument('--identity-dir', required=True)
    check = sub.add_parser('inspect')
    check.add_argument('--input', required=True)
    args = ap.parse_args()
    if args.command == 'init':
        if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
            raise ValueError('personal key initialization is local-only, never CI')
        directory = require_private_location(args.identity_dir)
        if directory.exists() or Path(args.public_out).exists():
            raise ValueError('refusing to replace any existing identity or recipient')
        pin = getpass.getpass('Local PIN (not echoed): ')
        if pin != getpass.getpass('Repeat PIN: '):
            raise ValueError('PIN mismatch')
        recipient, bundle, recovery = make_identity(pin)
        directory.mkdir(parents=True, mode=0o700)
        write_new(directory / 'identity.keybundle', bundle)
        write_new(directory / 'recovery.key', recovery)
        write_new(directory / 'recipient-id.txt', (recipient['recipient_id'] + '\n').encode())
        write_new(args.public_out, canonical(recipient), 0o644)
        print('Public recipient ID: ' + recipient['recipient_id'])
        print('Save both private files offline. Only the public recipient may be committed.')
    elif args.command == 'seal':
        recipient = load_json_bytes(read_bounded(args.recipient, 4096), 4096)
        ciphertext = seal(read_bounded(args.input, MAX_PLAINTEXT), recipient, args.recipient_id)
        if not str(args.output).endswith('.vault'):
            raise ValueError('encrypted output must use .vault extension')
        write_new(args.output, ciphertext, 0o644)
        print('Sealed successfully. No plaintext or private key printed.')
    elif args.command == 'open':
        directory = require_private_location(args.identity_dir)
        out = require_private_location(args.output)
        private = unlock_identity(read_bounded(directory / 'identity.keybundle', 4096),
                                  read_bounded(directory / 'recovery.key', 32), getpass.getpass('Local PIN: '))
        plaintext = unseal(read_bounded(args.input, MAX_ENVELOPE), private)
        write_new(out, plaintext)
        print('Decrypted to the requested local file; plaintext not printed.')
    else:
        inspect_envelope(read_bounded(args.input, MAX_ENVELOPE))
        print('Envelope syntax valid; authenticity requires successful decryption.')

if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Do not dump plaintext, paths, key material, or PIN into logs.
        print('Operation failed. No permission or integrity checks were bypassed.', file=sys.stderr)
        raise SystemExit(2)
