"""Bounded saved-data envelopes with a key scoped to exactly one ciphertext.

This module has no network, process, identity-unlock, or trading operations.
The public descriptor commits to every accepted byte. The separate capability
adds only that envelope's AES key, never the recipient's private identity.
"""
from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import sys
import tarfile
import tempfile
import zlib

try:
    from . import research_vault
except ImportError:
    import research_vault


FORMAT = 'scoped-research-dataset-v1'
PART_FORMAT = 'quality-first-research-tar-gzip-part-v1'
MANIFEST_NAME = 'dataset-manifest.json'
MAX_FILES = 8
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_ENVELOPE_BYTES = 24 * 1024 * 1024
MAX_CAPABILITY_BYTES = 16 * 1024
DESCRIPTOR_KEYS = {'version', 'format', 'cipher_sha256', 'cipher_bytes',
                   'bundle_sha256', 'bundle_bytes', 'files'}
FILE_KEYS = {'path', 'bytes', 'sha256'}
CAPABILITY_KEYS = DESCRIPTOR_KEYS | {'content_key_b64'}
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_PATH_COMPONENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*\Z')


def _fail(code):
    raise ValueError(code)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _sha(value):
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _path(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 160
            or value != str(PurePosixPath(value)) or value.startswith('/')
            or any(not _PATH_COMPONENT.fullmatch(part) for part in value.split('/'))):
        _fail('DATASET_PATH')
    return value


def _unb64(value, size=None):
    if not isinstance(value, str):
        _fail('DATASET_ENCODING')
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        _fail('DATASET_ENCODING')
    if (base64.b64encode(decoded).decode('ascii') != value
            or (size is not None and len(decoded) != size)):
        _fail('DATASET_ENCODING')
    return decoded


def _load_json(data, cap):
    if not isinstance(data, bytes) or len(data) > cap:
        _fail('DATASET_JSON_SIZE')
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=research_vault.unique_object,
                           parse_constant=lambda unused: _fail('DATASET_JSON_CONSTANT'))
    except (UnicodeError, json.JSONDecodeError):
        _fail('DATASET_JSON')
    if not isinstance(value, dict):
        _fail('DATASET_JSON_OBJECT')
    return value


def _validate_files(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        _fail('DATASET_FILE_LIST')
    names, total = [], 0
    for entry in files:
        if (not isinstance(entry, dict) or set(entry) != FILE_KEYS
                or not _integer(entry['bytes'], 0, MAX_FILE_BYTES)
                or not _sha(entry['sha256'])):
            _fail('DATASET_FILE_ENTRY')
        names.append(_path(entry['path']))
        total += entry['bytes']
    if names != sorted(set(names)) or total > MAX_EXPANDED_BYTES or MANIFEST_NAME not in names:
        _fail('DATASET_FILE_LIST')
    # A file cannot also be another allowlisted file's parent directory.
    if any(name + '/' == other[:len(name) + 1] for name in names for other in names if name != other):
        _fail('DATASET_PATH_CONFLICT')


def descriptor_from_capability(capability):
    """Validate and copy only public descriptor fields; never return the key."""
    if not isinstance(capability, dict) or set(capability) != CAPABILITY_KEYS:
        _fail('DATASET_CAPABILITY_SCHEMA')
    _unb64(capability['content_key_b64'], 32)
    descriptor = {name: capability[name] for name in DESCRIPTOR_KEYS}
    if (type(descriptor['version']) is not int or descriptor['version'] != 1
            or descriptor['format'] != FORMAT
            or not _sha(descriptor['cipher_sha256']) or not _sha(descriptor['bundle_sha256'])
            or not _integer(descriptor['cipher_bytes'], 1, MAX_ENVELOPE_BYTES)
            or not _integer(descriptor['bundle_bytes'], 1, MAX_BUNDLE_BYTES)):
        _fail('DATASET_DESCRIPTOR_SCHEMA')
    _validate_files(descriptor['files'])
    # Return a deep independent object so callers cannot mutate the capability
    # by subsequently changing the public file descriptor.
    return _load_json(research_vault.canonical(descriptor), MAX_CAPABILITY_BYTES)


def descriptor_sha256(descriptor):
    """Hash the public descriptor using the established canonical JSON format."""
    if not isinstance(descriptor, dict) or set(descriptor) != DESCRIPTOR_KEYS:
        _fail('DATASET_DESCRIPTOR_SCHEMA')
    return _digest(research_vault.canonical(descriptor))


def validate_capability(capability, expected_descriptor_sha256):
    """Validate the separate secret before a caller acquires any ciphertext."""
    descriptor = descriptor_from_capability(capability)
    if (not _sha(expected_descriptor_sha256)
            or descriptor_sha256(descriptor) != expected_descriptor_sha256):
        _fail('DATASET_DESCRIPTOR_MISMATCH')
    return descriptor


def _read_file(path, cap):
    # Nonblocking open permits us to reject FIFOs/devices using fstat without
    # waiting for an external writer before discovering the file type.
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(Path(path), flags)
    with os.fdopen(fd, 'rb') as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > cap or metadata.st_nlink != 1:
            _fail('DATASET_INPUT_FILE')
        value = handle.read(cap + 1)
        after = os.fstat(handle.fileno())
    fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')
    if any(getattr(metadata, field) != getattr(after, field) for field in fields):
        _fail('DATASET_INPUT_CHANGED')
    if len(value) > cap or len(value) != metadata.st_size:
        _fail('DATASET_INPUT_SIZE')
    return value


def _seal_part(payload, recipient, recipient_id):
    """Identical envelope/AAD/KDF contract to research_vault.seal."""
    public = research_vault.validate_recipient(recipient, recipient_id)
    _unb64(recipient['public_key'], 32)
    ephemeral = research_vault.X25519PrivateKey.generate()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    key = research_vault.HKDF(algorithm=research_vault.hashes.SHA256(), length=32,
                             salt=salt, info=research_vault.INFO).derive(ephemeral.exchange(public))
    header = {'version': 1, 'scheme': research_vault.SCHEME, 'recipient_id': recipient_id,
              'ephemeral_public': research_vault.b64(research_vault.raw_public(ephemeral.public_key())),
              'salt': research_vault.b64(salt), 'nonce': research_vault.b64(nonce)}
    ciphertext = research_vault.AESGCM(key).encrypt(nonce, payload, research_vault.canonical(header))
    envelope = research_vault.canonical(dict(header, ciphertext=research_vault.b64(ciphertext)))
    if len(envelope) > MAX_ENVELOPE_BYTES:
        _fail('DATASET_ENVELOPE_SIZE')
    research_vault.inspect_envelope(envelope)
    return envelope, key


def pack(files, recipient, recipient_id, *, expected_files=None):
    """Package caller-allowlisted regular files and return envelope/capability/descriptor.

    ``expected_files``, when supplied, must bind every caller file to exactly
    ``{'bytes': int, 'sha256': str}`` before any encryption. A generated manifest
    commits to those files; a caller provenance manifest can be one input file.
    This function only returns bytes/objects and writes no files or secrets.
    """
    if not isinstance(files, dict) or not 1 <= len(files) < MAX_FILES:
        _fail('DATASET_INPUT_FILES')
    names = sorted(_path(name) for name in files)
    if MANIFEST_NAME in names or len(set(names)) != len(names):
        _fail('DATASET_RESERVED_PATH')
    if expected_files is not None and (not isinstance(expected_files, dict) or set(expected_files) != set(names)):
        _fail('DATASET_EXPECTED_FILES')
    members, metadata, total = {}, [], 0
    for name in names:
        value = _read_file(files[name], MAX_FILE_BYTES)
        entry = {'path': name, 'bytes': len(value), 'sha256': _digest(value)}
        if expected_files is not None:
            expected = expected_files[name]
            if (not isinstance(expected, dict) or set(expected) != {'bytes', 'sha256'}
                    or type(expected['bytes']) is not int or expected != {key: entry[key] for key in expected}):
                _fail('DATASET_EXPECTED_FILE_MISMATCH')
        total += len(value)
        if total > MAX_EXPANDED_BYTES:
            _fail('DATASET_EXPANDED_SIZE')
        members[name] = value
        metadata.append(entry)
    members[MANIFEST_NAME] = research_vault.canonical({'version': 1, 'format': FORMAT, 'files': metadata})
    manifest = members[MANIFEST_NAME]
    descriptors = sorted(metadata + [{'path': MANIFEST_NAME, 'bytes': len(manifest),
                                     'sha256': _digest(manifest)}], key=lambda entry: entry['path'])
    _validate_files(descriptors)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(members[name]), 0o600, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(members[name]))
    tar_bytes = raw.getvalue()
    if len(tar_bytes) > MAX_EXPANDED_BYTES:
        _fail('DATASET_EXPANDED_SIZE')
    bundle = gzip.compress(tar_bytes, mtime=0)
    if len(bundle) > MAX_BUNDLE_BYTES:
        _fail('DATASET_BUNDLE_SIZE')
    part = research_vault.canonical({'format': PART_FORMAT, 'index': 0, 'total': 1,
                                    'bundle_sha256': _digest(bundle),
                                    'payload_base64': research_vault.b64(bundle)})
    envelope, key = _seal_part(part, recipient, recipient_id)
    descriptor = {'version': 1, 'format': FORMAT, 'cipher_sha256': _digest(envelope),
                  'cipher_bytes': len(envelope), 'bundle_sha256': _digest(bundle),
                  'bundle_bytes': len(bundle), 'files': descriptors}
    capability = dict(descriptor, content_key_b64=research_vault.b64(key))
    descriptor = descriptor_from_capability(capability)
    return envelope, capability, descriptor


def _decrypt_bundle(envelope, capability, descriptor):
    if (not isinstance(envelope, bytes) or len(envelope) != descriptor['cipher_bytes']
            or _digest(envelope) != descriptor['cipher_sha256']):
        _fail('DATASET_CIPHER_MISMATCH')
    document = _load_json(envelope, MAX_ENVELOPE_BYTES)
    if set(document) != research_vault.HEADER_KEYS | {'ciphertext'}:
        _fail('DATASET_ENVELOPE_SCHEMA')
    research_vault.inspect_envelope(envelope)
    _unb64(document['ephemeral_public'], 32)
    _unb64(document['salt'], 16)
    nonce, ciphertext = _unb64(document['nonce'], 12), _unb64(document['ciphertext'])
    header = {name: document[name] for name in research_vault.HEADER_KEYS}
    try:
        payload = research_vault.AESGCM(_unb64(capability['content_key_b64'], 32)).decrypt(
            nonce, ciphertext, research_vault.canonical(header))
    except Exception:
        _fail('DATASET_AUTHENTICATION_FAILED')
    part = _load_json(payload, research_vault.MAX_PLAINTEXT)
    if (set(part) != {'format', 'index', 'total', 'bundle_sha256', 'payload_base64'}
            or part['format'] != PART_FORMAT or type(part['index']) is not int or part['index'] != 0
            or type(part['total']) is not int or part['total'] != 1
            or part['bundle_sha256'] != descriptor['bundle_sha256']):
        _fail('DATASET_PART_SCHEMA')
    bundle = _unb64(part['payload_base64'])
    if len(bundle) != descriptor['bundle_bytes'] or _digest(bundle) != descriptor['bundle_sha256']:
        _fail('DATASET_BUNDLE_MISMATCH')
    return bundle


def _unpack(bundle, descriptor):
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        expanded = inflater.decompress(bundle, MAX_EXPANDED_BYTES + 1)
    except zlib.error:
        _fail('DATASET_GZIP')
    if (len(expanded) > MAX_EXPANDED_BYTES or inflater.unconsumed_tail
            or not inflater.eof or inflater.unused_data):
        _fail('DATASET_GZIP_SIZE_OR_TRAILING_DATA')
    expected = {entry['path']: entry for entry in descriptor['files']}
    members = {}
    try:
        offset, ended = 0, False
        while offset < len(expanded):
            header = expanded[offset:offset + tarfile.BLOCKSIZE]
            if len(header) != tarfile.BLOCKSIZE:
                _fail('DATASET_TAR_TRUNCATED')
            if not any(header):
                # Require both EOF blocks and zero-only record padding. Parsing
                # headers ourselves also rejects extension headers that the
                # high-level tar iterator silently consumes (PAX/GNU names).
                if len(expanded) - offset < 2 * tarfile.BLOCKSIZE or any(expanded[offset:]):
                    _fail('DATASET_TAR_TRAILING_DATA')
                ended = True
                break
            item = tarfile.TarInfo.frombuf(header, encoding='utf-8', errors='strict')
            name = _path(item.name)
            if (not item.isreg() or item.type not in (tarfile.REGTYPE, tarfile.AREGTYPE)
                    or item.pax_headers or item.linkname or name in members or name not in expected
                    or item.size != expected[name]['bytes'] or item.size > MAX_FILE_BYTES):
                _fail('DATASET_TAR_MEMBER')
            start = offset + tarfile.BLOCKSIZE
            end = start + item.size
            offset = start + ((item.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
            if offset > len(expanded) or any(expanded[end:offset]):
                _fail('DATASET_TAR_PADDING_OR_SIZE')
            value = expanded[start:end]
            if len(value) != item.size or _digest(value) != expected[name]['sha256']:
                _fail('DATASET_FILE_MISMATCH')
            members[name] = value
            if len(members) > MAX_FILES:
                _fail('DATASET_FILE_LIST')
        if not ended:
            _fail('DATASET_TAR_TRUNCATED')
    except (tarfile.TarError, EOFError, UnicodeError):
        _fail('DATASET_TAR')
    if set(members) != set(expected):
        _fail('DATASET_MISSING_FILE')
    manifest = _load_json(members[MANIFEST_NAME], MAX_CAPABILITY_BYTES)
    wanted = [entry for entry in descriptor['files'] if entry['path'] != MANIFEST_NAME]
    if (set(manifest) != {'version', 'format', 'files'}
            or type(manifest['version']) is not int or manifest['version'] != 1
            or manifest['format'] != FORMAT or not isinstance(manifest['files'], list)
            or any(not isinstance(entry, dict) or set(entry) != FILE_KEYS
                   or not _integer(entry['bytes'], 0, MAX_FILE_BYTES)
                   or not _sha(entry['sha256']) for entry in manifest['files'])
            or manifest['files'] != wanted):
        _fail('DATASET_MANIFEST')
    return members


def _write_new(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_directory(members, output):
    """Reserve destination exclusively, then replace its empty contents atomically.

    The destination reservation prevents two restores from overwriting each
    other. All verification happens before this method is called. Any I/O
    failure removes our reservation and staging, leaving no partial dataset.
    """
    output = Path(output)
    if not output.parent.is_dir() or output.parent.is_symlink():
        _fail('DATASET_OUTPUT_PARENT')
    os.mkdir(output, 0o700)  # Exclusive: pre-existing files/dirs/symlinks fail.
    stage = None
    try:
        stage = Path(tempfile.mkdtemp(prefix='.dataset-stage-', dir=output.parent))
        os.chmod(stage, 0o700)
        for name in sorted(members):
            target = stage.joinpath(*name.split('/'))
            for parent in reversed(target.parent.relative_to(stage).parents):
                stage.joinpath(parent).mkdir(mode=0o700, exist_ok=True)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            _write_new(target, members[name])
        os.replace(stage, output)
        stage = None
    except BaseException:
        if stage is not None:
            shutil.rmtree(stage)
        # Only the exclusively created empty reservation is removed; an
        # already-existing user directory is never modified.
        os.rmdir(output)
        raise


def restore(envelope, capability, expected_descriptor_sha256, output):
    """Verify/decrypt exactly one registered dataset, then publish private files.

    No output is created before all ciphertext, archive, manifest and file
    checks pass. A capability for one envelope cannot decrypt a second one.
    """
    descriptor = validate_capability(capability, expected_descriptor_sha256)
    bundle = _decrypt_bundle(envelope, capability, descriptor)
    members = _unpack(bundle, descriptor)
    _publish_directory(members, output)
    return {'status': 'saved_dataset_restored', 'descriptor_sha256': expected_descriptor_sha256,
            'files_verified': len(members), 'plaintext_published': False,
            'strategy_evaluated': False, 'live_orders_enabled': False}


def write_capability(path, capability, *, working_tree=None):
    """Save a new private capability outside the caller's repository checkout."""
    descriptor_from_capability(capability)
    root = Path(working_tree or Path.cwd()).resolve()
    target = Path(path).expanduser().absolute()
    resolved = target.resolve()
    if resolved == root or root in resolved.parents or not target.parent.is_dir() or target.parent.is_symlink():
        _fail('DATASET_CAPABILITY_LOCATION')
    _write_new(target, research_vault.canonical(capability))


def main():
    parser = argparse.ArgumentParser(description='Restore one registered saved-data envelope. No network operations.')
    subcommands = parser.add_subparsers(dest='command', required=True)
    command = subcommands.add_parser('restore')
    command.add_argument('--envelope', required=True)
    command.add_argument('--capability', required=True)
    command.add_argument('--expected-descriptor-sha256', required=True)
    command.add_argument('--output', required=True)
    args = parser.parse_args()
    capability = _load_json(_read_file(args.capability, MAX_CAPABILITY_BYTES), MAX_CAPABILITY_BYTES)
    restore(_read_file(args.envelope, MAX_ENVELOPE_BYTES), capability,
            args.expected_descriptor_sha256, args.output)
    print('Registered saved dataset restored. No dataset or key printed.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('Saved dataset restoration failed; no dataset or key printed.', file=sys.stderr)
        raise SystemExit(1) from None
