"""Atomically deliver complete ciphertext or explicit compact failure evidence.

A compact package preserves small diagnostic records after a known result-size
failure. It cannot establish a complete strategy result. Plaintext remains in
its original private directory until the workflow's unconditional cleanup.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, Callable

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))
import research_vault as vault
import run_quality_first_research as bundle

MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_COMPACT_MEMBER_BYTES = 16 * 1024 * 1024
MAX_INVENTORY_BYTES = 512 * 1024 * 1024
MAX_INVENTORY_FILES = 4096
SIZE_FAILURES = frozenset({'RESULT_SIZE_LIMIT', 'BUNDLE_SIZE_LIMIT', 'BUNDLE_EXPANDED_SIZE_LIMIT'})
COMPACT_FILES = ('frozen_input.json', 'results/summary.json', 'diagnostics.txt')


def _open_regular(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    handle = os.fdopen(fd, 'rb')
    metadata = os.fstat(handle.fileno())
    if not stat.S_ISREG(metadata.st_mode):
        handle.close()
        raise ValueError('COMPACT_NOT_REGULAR')
    return handle, metadata


def _same_file(before, after):
    names = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    return all(getattr(before, name) == getattr(after, name) for name in names)


def _open_private_regular(private: Path, name: str):
    parts = Path(name).parts
    if not parts or any(part in ('', '.', '..') for part in parts) or Path(name).is_absolute():
        raise ValueError('COMPACT_PATH')
    flags = os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    directory = os.open(private, flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            | getattr(os, 'O_NONBLOCK', 0), dir_fd=directory)
    finally:
        os.close(directory)
    handle = os.fdopen(fd, 'rb')
    before = os.fstat(handle.fileno())
    if not stat.S_ISREG(before.st_mode):
        handle.close()
        raise ValueError('COMPACT_NOT_REGULAR')
    return handle, before


def _inventory(private: Path) -> list[dict[str, Any]]:
    paths = sorted(private.rglob('*'))
    if len(paths) > MAX_INVENTORY_FILES:
        raise ValueError('COMPACT_INVENTORY_FILE_LIMIT')
    result, budget = [], MAX_INVENTORY_BYTES
    for path in paths:
        name = path.relative_to(private).as_posix()
        if path.is_symlink():
            result.append({'path': name, 'status': 'symlink_not_read', 'bytes': None, 'sha256': None})
            continue
        if path.is_dir():
            continue
        handle, before = _open_private_regular(private, name)
        with handle:
            if before.st_size > budget:
                result.append({'path': name, 'status': 'hash_budget_exhausted', 'bytes': before.st_size, 'sha256': None})
                continue
            value, count = hashlib.sha256(), 0
            while True:
                chunk = handle.read(min(1024 * 1024, budget - count + 1))
                if not chunk:
                    break
                count += len(chunk)
                if count > budget:
                    raise ValueError('COMPACT_INVENTORY_CHANGED')
                value.update(chunk)
            if count != before.st_size or not _same_file(before, os.fstat(handle.fileno())):
                raise ValueError('COMPACT_INVENTORY_CHANGED')
        budget -= count
        result.append({'path': name, 'status': 'hashed', 'bytes': count, 'sha256': value.hexdigest()})
    return result


def _copy_small(private: Path, name: str, target: Path):
    handle, before = _open_private_regular(private, name)
    with handle:
        if before.st_size > MAX_COMPACT_MEMBER_BYTES:
            return False
        raw = handle.read(MAX_COMPACT_MEMBER_BYTES + 1)
        if len(raw) != before.st_size or not _same_file(before, os.fstat(handle.fileno())):
            raise ValueError('COMPACT_SOURCE_CHANGED')
    vault.write_new(target, raw)
    return True


def _compact_source(private: Path, compact: Path, metadata: dict[str, Any], code: str):
    workflow = metadata.get('workflow', {})
    fallback = {**metadata, 'strategy_evaluated': False, 'comparison_valid': False,
        'full_evidence_retained': False, 'delivery_mode': 'compact_failure_evidence',
        'analysis_step_outcome': workflow.get('EVALUATION_OUTCOME'),
        'independent_step_outcome': workflow.get('INDEPENDENT_OUTCOME'),
        'input_step_outcome': workflow.get('INPUT_OUTCOME'),
        'delivery_failure': {'stage': 'full_result_bundle', 'code': code},
        'analysis_claim_before_delivery_failure': metadata.get('strategy_evaluated')}
    inventory = _inventory(private)
    retained, omitted = ['run.json'], []
    vault.write_new(compact / 'run.json', vault.canonical(fallback))
    for name in COMPACT_FILES:
        path = private / name
        if not path.exists() or path.is_symlink():
            omitted.append({'path': name, 'reason': 'missing_or_symlink'})
        elif _copy_small(private, name, compact / name):
            retained.append(name)
        else:
            omitted.append({'path': name, 'reason': 'compact_member_limit'})
    manifest = {'format': 'compact-research-delivery-failure-v1', 'full_evidence_retained': False,
        'strategy_evaluated': False, 'comparison_valid': False,
        'delivery_failure': {'stage': 'full_result_bundle', 'code': code},
        'original_files': inventory, 'compact_retained_files': retained,
        'compact_omitted_small_records': omitted,
        'large_derived_artifacts_copied': False,
        'basis': 'small_failure_records_only_not_complete_model_or_scoring_evidence'}
    vault.write_new(compact / 'results/delivery-failure.json', vault.canonical(manifest))
    return fallback


def seal_outcomes(private_dir: Path, delivery_dir: Path, recipient: dict[str, Any], recipient_id: str, *,
                  metadata: dict[str, Any], max_member_bytes: int = 128 * 1024 * 1024,
                  make_bundle: Callable | None = None,
                  deliver_bundle: Callable | None = None) -> dict[str, Any]:
    """Publish only an entirely sealed flat delivery; remove partial staging.

    The public status keeps the existing publisher schema. Completeness flags
    live in encrypted run metadata and this function's returned workflow outputs.
    """
    private, delivery = Path(private_dir), Path(delivery_dir)
    if private.is_symlink() or not private.is_dir() or delivery.parent.is_symlink() or not delivery.parent.is_dir():
        raise ValueError('PRIVATE_DELIVERY_DIRECTORY_REQUIRED')
    if type(max_member_bytes) is not int or not 0 < max_member_bytes <= MAX_MEMBER_BYTES:
        raise ValueError('RESULT_MEMBER_BUDGET')
    vault.validate_recipient(recipient, recipient_id)
    pack = bundle.make_bundle if make_bundle is None else make_bundle
    seal = bundle.deliver_bundle if deliver_bundle is None else deliver_bundle
    os.mkdir(delivery, 0o700)  # Exclusive reservation; never overwrite an existing delivery.
    stage = compact = None
    published = False
    try:
        stage = Path(tempfile.mkdtemp(prefix='.sealed-stage-', dir=delivery.parent))
        stage.chmod(0o700)
        full_metadata = {**metadata, 'full_evidence_retained': True, 'delivery_mode': 'full'}
        vault.write_new(private / 'run.json', vault.canonical(full_metadata))
        full = True
        effective_metadata = full_metadata
        try:
            packed = pack(private, max_member_bytes=max_member_bytes)
        except ValueError as exc:
            if str(exc) not in SIZE_FAILURES:
                raise
            full = False
            compact = Path(tempfile.mkdtemp(prefix='.compact-evidence-', dir=private.parent))
            compact.chmod(0o700)
            effective_metadata = _compact_source(private, compact, metadata, str(exc))
            packed = pack(compact, max_member_bytes=max_member_bytes)
        count = seal(packed, stage, recipient, recipient_id)
        if type(count) is not int or count < 1:
            raise ValueError('SEALED_PART_COUNT_INVALID')
        expected = {f'part-{i:04d}.vault' for i in range(count)}
        if {path.name for path in stage.iterdir()} != expected:
            raise ValueError('SEALED_PART_SET_INCOMPLETE')
        for name in sorted(expected):
            handle, before = _open_regular(stage / name)
            with handle:
                if before.st_size > vault.MAX_ENVELOPE:
                    raise ValueError('SEALED_PART_SIZE_INVALID')
                raw = handle.read(vault.MAX_ENVELOPE + 1)
                if len(raw) != before.st_size or not _same_file(before, os.fstat(handle.fileno())):
                    raise ValueError('SEALED_PART_CHANGED')
            if vault.inspect_envelope(raw)['recipient_id'] != recipient_id:
                raise ValueError('SEALED_PART_RECIPIENT_MISMATCH')
        evaluated = effective_metadata.get('strategy_evaluated') is True and full
        status = {'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
            'strategy_evaluated': evaluated, 'live_orders_enabled': False,
            'plaintext_published': False, 'sealed_parts': count}
        vault.write_new(stage / 'status.json', vault.canonical(status) + b'\n', 0o644)
        os.replace(stage, delivery)
        stage = None
        published = True
        return {'delivery_complete': True, 'full_evidence_retained': full,
            'strategy_evaluated': evaluated, 'sealed_parts': count,
            'delivery_mode': 'full' if full else 'compact_failure_evidence'}
    finally:
        if stage is not None:
            shutil.rmtree(stage)
        if compact is not None:
            shutil.rmtree(compact)
        if not published:
            os.rmdir(delivery)
