#!/usr/bin/env python3
"""Restricted SSH forced-command receiver. Private cloud storage only.

Protocol: one JSON header line, followed by exact raw bytes for upload only.
There are no shell, list, delete, path-selection or execution operations.
"""
import contextlib
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
import tempfile

ROOT = Path('/home/ubuntu/quantml-study')
CHUNK_MAX = 4 * 1024**2
FILE_MAX = 256 * 1024**2
SPOOL_MAX = 8 * 1024**3
FREE_FLOOR = 8 * 1024**3
HEADER_MAX = 8192


class Rejected(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise Rejected(code)


def integer(value, minimum=0, maximum=None):
    require(isinstance(value, int) and not isinstance(value, bool), 'invalid_integer')
    require(value >= minimum and (maximum is None or value <= maximum), 'integer_out_of_bounds')
    return value


def source_id(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None, 'invalid_source_id')
    return value


def relative_metadata(value):
    require(isinstance(value, str) and 0 < len(value) <= 4096 and '\\' not in value and '\x00' not in value,
            'invalid_relative_metadata')
    p = PurePosixPath(value)
    require(bool(p.parts) and not p.is_absolute() and '..' not in p.parts and ':' not in p.parts[0],
            'invalid_relative_metadata')
    return value


def validate_header(header):
    require(isinstance(header, dict), 'invalid_header')
    operation = header.get('op')
    allowed = {'status': {'op', 'source_id'}, 'upload': {'op', 'source_id', 'offset', 'chunk_bytes', 'size_bytes', 'relative_path'},
               'finish': {'op', 'source_id'}}
    require(operation in allowed, 'operation_not_allowed')
    require(set(header) == allowed[operation], 'unexpected_or_missing_fields')
    source_id(header['source_id'])
    if operation == 'upload':
        size = integer(header['size_bytes'], 1, FILE_MAX)
        offset = integer(header['offset'], 0, size)
        chunk = integer(header['chunk_bytes'], 1, CHUNK_MAX)
        require(offset + chunk <= size, 'chunk_exceeds_source_size')
        relative_metadata(header['relative_path'])
    return header


def regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    require(stat.S_ISREG(info.st_mode), 'nonregular_storage_entry')
    return info


def secure_directory(path, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode), 'invalid_storage_directory')
    require(info.st_uid == os.getuid(), 'storage_owner_mismatch')
    require(info.st_mode & 0o077 == 0, 'storage_permissions_too_broad')


def write_metadata(path, value):
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    require(len(raw) < HEADER_MAX, 'metadata_too_large')
    fd, name = tempfile.mkstemp(prefix='.meta-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as dest:
            dest.write(raw)
            dest.flush()
            os.fsync(dest.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    """root is injectable only for tiny unit tests, never from the SSH request."""
    def __init__(self, root, *, spool_max=SPOOL_MAX, free_floor=FREE_FLOOR):
        self.root = Path(root)
        secure_directory(self.root)
        self.partial = self.root / 'partial'
        self.objects = self.root / 'objects'
        self.metadata = self.root / 'metadata'
        for directory in (self.partial, self.objects, self.metadata):
            secure_directory(directory, create=True)
        self.spool_max, self.free_floor = spool_max, free_floor

    def load(self, sid):
        path = self.metadata / sid
        if regular(path) is None:
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(HEADER_MAX + 1)
        require(len(raw) <= HEADER_MAX, 'metadata_too_large')
        value = json.loads(raw)
        require(value.get('source_id') == sid, 'metadata_identity_mismatch')
        return value

    def bytes_used(self):
        total = 0
        for directory in (self.partial, self.objects):
            for entry in directory.iterdir():
                source_id(entry.name)
                total += regular(entry).st_size
        return total

    def status(self, sid):
        source_id(sid)
        meta = self.load(sid)
        final, part = regular(self.objects / sid), regular(self.partial / sid)
        require(not (final and part), 'conflicting_partial_and_final')
        if meta is None:
            require(final is None and part is None, 'orphaned_object_requires_review')
            return {'ok': True, 'source_id': sid, 'state': 'absent', 'offset': 0}
        expected = integer(meta.get('size_bytes'), 1, FILE_MAX)
        actual = final.st_size if final else part.st_size if part else 0
        require(actual <= expected, 'stored_size_exceeds_expected')
        state = 'complete' if final and meta.get('state') == 'complete' else 'finalization_pending' if final else 'partial'
        response = {'ok': True, 'source_id': sid, 'state': state, 'offset': actual, 'size_bytes': expected}
        if state == 'complete':
            require(actual == expected and re.fullmatch('[0-9a-f]{64}', meta.get('sha256', '')) is not None,
                    'invalid_finalization_receipt')
            response['sha256'] = meta['sha256']
        return response

    def upload(self, header, body):
        validate_header(header)
        sid, size, offset, amount = header['source_id'], header['size_bytes'], header['offset'], header['chunk_bytes']
        current = self.status(sid)
        require(current['state'] not in ('complete', 'finalization_pending'), 'final_object_immutable')
        require(current['offset'] == offset, 'offset_mismatch_read_status')
        meta = self.load(sid)
        if meta:
            require(meta['size_bytes'] == size and meta['relative_path'] == header['relative_path'], 'source_metadata_changed')
        require(self.bytes_used() + amount <= self.spool_max, 'spool_capacity_pending')
        require(shutil.disk_usage(self.root).free - amount >= self.free_floor, 'free_space_floor_pending')
        if meta is None:
            meta = {'version': 1, 'source_id': sid, 'size_bytes': size, 'relative_path': header['relative_path'],
                    'state': 'partial', 'hash_location': 'cloud_only'}
            write_metadata(self.metadata / sid, meta)
        path = self.partial / sid
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        written = 0
        with os.fdopen(fd, 'wb') as dest:
            require(os.fstat(dest.fileno()).st_size == offset, 'offset_changed_under_lock')
            try:
                while written < amount:
                    piece = body.read(min(256 * 1024, amount - written))
                    if not piece:
                        break
                    dest.write(piece)
                    written += len(piece)
            finally:
                dest.flush()
                os.fsync(dest.fileno())
        if written != amount:
            return {'ok': False, 'source_id': sid, 'error': 'interrupted_chunk_read_status', 'offset': offset + written}
        return {'ok': True, 'source_id': sid, 'state': 'partial', 'offset': offset + written, 'size_bytes': size}

    def finish(self, sid):
        current = self.status(sid)
        if current['state'] == 'complete':
            return current
        meta = self.load(sid)
        require(meta is not None and current['offset'] == meta['size_bytes'], 'source_not_fully_uploaded')
        # If rename completed before a crash, finalize that same immutable object.
        path = self.objects / sid if current['state'] == 'finalization_pending' else self.partial / sid
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        require((before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino),
                'source_changed_during_hash')
        require(after.st_size == meta['size_bytes'], 'final_size_mismatch')
        if current['state'] != 'finalization_pending':
            require(regular(self.objects / sid) is None, 'final_object_already_exists')
            os.chmod(path, 0o400)
            os.rename(path, self.objects / sid)
        meta.update(state='complete', sha256=digest.hexdigest())
        write_metadata(self.metadata / sid, meta)
        return self.status(sid)


def handle(store, header, body):
    validate_header(header)
    if header['op'] == 'status':
        return store.status(header['source_id'])
    if header['op'] == 'upload':
        return store.upload(header, body)
    return store.finish(header['source_id'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', choices=(str(ROOT),), default=str(ROOT))
    parser.parse_args()
    os.umask(0o077)
    try:
        # Validate every fixed ancestor; never accept a client-selected root.
        require(ROOT.is_absolute(), 'invalid_fixed_root')
        for directory in list(ROOT.parents)[:-1] + [ROOT]:
            require(not directory.is_symlink(), 'symlink_fixed_root')
        store = Store(ROOT)
        lockfd = os.open(ROOT / '.transport.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lockfd, 'wb'):
            try:
                fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected('transport_busy_retry_status')
            line = sys.stdin.buffer.readline(HEADER_MAX + 1)
            require(len(line) <= HEADER_MAX and line.endswith(b'\n'), 'header_limit_or_missing_newline')
            response = handle(store, json.loads(line), sys.stdin.buffer)
    except Rejected as exc:
        response = {'ok': False, 'error': exc.code}
    except Exception as exc:
        # Exception messages may contain paths or payload fragments; never emit them.
        response = {'ok': False, 'error': 'receiver_' + type(exc).__name__}
    print(json.dumps(response, sort_keys=True))


if __name__ == '__main__':
    main()
