"""Stage validated sealed research deliveries for permanent GitHub journals.

This local file tool does not decrypt, access keys, call GitHub, or commit files.
Run identity comes from trusted caller parameters, never empirical status text.
Envelope syntax and the expected public fingerprint can be checked without a
private identity; these checks do not authenticate authors or prove recovery.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

import research_vault

MAX_PARTS = 16
MAX_STATUS_BYTES = 4096
PART_NAME = re.compile(r'part-([0-9]{4})\.vault\Z')
FINGERPRINT = re.compile(r'[0-9a-f]{64}\Z')
STATUS_KEYS = {'version', 'status', 'stage', 'strategy_evaluated',
               'live_orders_enabled', 'plaintext_published', 'sealed_parts'}
FAILURE_CODES = {
    'identity_preflight_failed', 'remote_transport_failed',
    'remote_timeout_unknown', 'remote_result_too_large',
    'configuration_rejected', 'remote_report_not_successful',
    'acquisition_unclassified',
}
OPERATIONAL_KEYS = {'evidence_code', 'cursor_code', 'coverage_code'}
EVIDENCE_CODES = {
    'worker_completed', 'readback_observed', 'deferred', 'worker_failed',
    'worker_timeout', 'remote_transport_failed', 'remote_timeout_unknown',
    'remote_result_too_large', 'remote_report_unclassified',
    'configuration_rejected', 'unknown',
}
CURSOR_CODES = {'committed', 'not_committed', 'readback_no_advance', 'unknown'}
COVERAGE_CODES = {'present', 'absent', 'unknown'}


def validate_status(value):
    if not isinstance(value, dict):
        raise ValueError('INVALID_STATUS_SCHEMA')
    outcome = value.get('status')
    expected = STATUS_KEYS | ({'failed_stage'} if outcome == 'failed' else set())
    allowed = [expected, expected | OPERATIONAL_KEYS]
    # Keep old failed envelopes valid while accepting the new bounded category.
    if outcome == 'failed':
        allowed.append(expected | {'failure_code'})
        allowed.append(expected | OPERATIONAL_KEYS | {'failure_code'})
    if set(value) not in allowed or outcome not in {'complete', 'failed'}:
        raise ValueError('INVALID_STATUS_SCHEMA')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('INVALID_STATUS_VERSION')
    if value['stage'] != 'sealed_delivery':
        raise ValueError('UNSEALED_DELIVERY_STATUS')
    if value['plaintext_published'] is not False or value['live_orders_enabled'] is not False:
        raise ValueError('PUBLICATION_CONTRACT_VIOLATION')
    # A manual work-summary bundle can be complete without running a strategy.
    # The explicit flag keeps completion of delivery separate from evaluation.
    if type(value['strategy_evaluated']) is not bool or (outcome == 'failed' and value['strategy_evaluated'] is not False):
        raise ValueError('INCONSISTENT_RESEARCH_STATUS')
    count = value['sealed_parts']
    if type(count) is not int or not 1 <= count <= MAX_PARTS:
        raise ValueError('INVALID_SEALED_PART_COUNT')
    if outcome == 'failed' and value['failed_stage'] not in {'acquisition', 'research'}:
        raise ValueError('INVALID_FAILURE_STAGE')
    if outcome == 'failed' and 'failure_code' in value and value['failure_code'] not in FAILURE_CODES:
        raise ValueError('INVALID_FAILURE_CODE')
    if 'evidence_code' in value and value['evidence_code'] not in EVIDENCE_CODES:
        raise ValueError('INVALID_EVIDENCE_CODE')
    if 'cursor_code' in value and value['cursor_code'] not in CURSOR_CODES:
        raise ValueError('INVALID_CURSOR_CODE')
    if 'coverage_code' in value and value['coverage_code'] not in COVERAGE_CODES:
        raise ValueError('INVALID_COVERAGE_CODE')
    # Return a fresh allowlisted record. No free text, paths, identity or metrics.
    keys = set(expected)
    if outcome == 'failed' and 'failure_code' in value:
        keys.add('failure_code')
    keys.update(OPERATIONAL_KEYS & set(value))
    return {key: value[key] for key in sorted(keys)}


def episode_identity(repository, run_id, run_attempt, *, cell_id=None):
    if not isinstance(repository, str) or not re.fullmatch(
            r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', repository):
        raise ValueError('INVALID_REPOSITORY_IDENTITY')
    if repository.rsplit('/', 1)[1] in {'.', '..'}:
        raise ValueError('INVALID_REPOSITORY_IDENTITY')
    identity = {'repository': repository.lower()}
    for name, value in [('run_id', run_id), ('run_attempt', run_attempt)]:
        if type(value) is int:
            value = str(value)
        if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', value):
            raise ValueError('INVALID_RUN_IDENTITY')
        identity[name] = value
    # Omission preserves every legacy episode name. A caller may opt into a
    # stable cell label; never derive it from ciphertext or empirical status.
    if cell_id is not None:
        if not isinstance(cell_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', cell_id):
            raise ValueError('INVALID_CELL_IDENTITY')
        identity['cell_id'] = cell_id
    return hashlib.sha256(b'quality-first-sealed-episode-v1\0' + research_vault.canonical(identity)).hexdigest()


def read_regular(path, limit):
    """Reject links/devices and boundedly read one stable regular file."""
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit or path.is_symlink():
            raise ValueError('INVALID_DELIVERY_FILE')
        data = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
        if len(data) > limit or len(data) != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise ValueError('DELIVERY_FILE_CHANGED')
    return data


def inspect_delivery(source, expected_recipient):
    """Validate a flat delivery; plaintext payload integrity is not asserted."""
    source = Path(source)
    if not isinstance(expected_recipient, str) or not FINGERPRINT.fullmatch(expected_recipient):
        raise ValueError('EXPECTED_RECIPIENT_REQUIRED')
    if source.is_symlink() or not source.is_dir():
        raise ValueError('INVALID_DELIVERY_DIRECTORY')
    entries = list(source.iterdir())
    if not 2 <= len(entries) <= MAX_PARTS + 1:
        raise ValueError('INVALID_DELIVERY_FILE_COUNT')
    names = {entry.name for entry in entries}
    for entry in entries:
        if entry.is_symlink() or not entry.is_file() or (entry.name != 'status.json' and not PART_NAME.fullmatch(entry.name)):
            raise ValueError('UNEXPECTED_DELIVERY_FILE')
    if 'status.json' not in names:
        raise ValueError('DELIVERY_STATUS_MISSING')
    status_raw = read_regular(source / 'status.json', MAX_STATUS_BYTES)
    status_value = validate_status(research_vault.load_json_bytes(status_raw, MAX_STATUS_BYTES))
    expected_names = {'status.json'} | {f'part-{i:04d}.vault' for i in range(status_value['sealed_parts'])}
    if names != expected_names:
        raise ValueError('DELIVERY_PART_SEQUENCE_MISMATCH')
    hashes = {}
    for name in sorted(names - {'status.json'}):
        body = read_regular(source / name, research_vault.MAX_ENVELOPE)
        envelope = research_vault.inspect_envelope(body)
        if envelope['recipient_id'] != expected_recipient:
            raise ValueError('DELIVERY_RECIPIENT_MISMATCH')
        hashes[name] = hashlib.sha256(body).hexdigest()
    return {'status': status_value, 'part_sha256': hashes,
            'status_sha256': hashlib.sha256(status_raw).hexdigest()}


def validate_delivery(source, expected_recipient):
    """Return upload-ready allowlisted bytes, with exact ciphertext preserved.

    This convenience API is bounded by MAX_PARTS * MAX_ENVELOPE. The streaming
    persist_delivery path avoids holding all envelope bytes simultaneously.
    """
    source = Path(source)
    inspected = inspect_delivery(source, expected_recipient)
    result = {'status.json': research_vault.canonical(inspected['status']) + b'\n'}
    for name, expected_digest in inspected['part_sha256'].items():
        body = read_regular(source / name, research_vault.MAX_ENVELOPE)
        if hashlib.sha256(body).hexdigest() != expected_digest:
            raise ValueError('DELIVERY_CHANGED_BEFORE_COPY')
        result[name] = body
    if inspect_delivery(source, expected_recipient) != inspected:
        raise ValueError('DELIVERY_CHANGED_BEFORE_PUBLICATION')
    return result


def persist_delivery(source, episodes_dir, repository, run_id, run_attempt, expected_recipient, *, cell_id=None):
    opaque_id = episode_identity(repository, run_id, run_attempt, cell_id=cell_id)
    source, episodes_dir = Path(source), Path(episodes_dir)
    delivery = inspect_delivery(source, expected_recipient)
    status_bytes = research_vault.canonical(delivery['status']) + b'\n'
    target = episodes_dir / opaque_id
    if episodes_dir.is_symlink() or (episodes_dir.exists() and not episodes_dir.is_dir()):
        raise ValueError('INVALID_EPISODE_ROOT')
    if target.is_symlink():
        raise ValueError('INVALID_EPISODE_DIRECTORY')

    def existing_matches():
        existing = inspect_delivery(target, expected_recipient)
        if existing['status'] != delivery['status'] or existing['part_sha256'] != delivery['part_sha256']:
            raise ValueError('RUN_IDENTITY_ALREADY_HAS_DIFFERENT_DELIVERY')

    if target.exists():
        existing_matches()
        return {'version': 1, 'status': 'already_present', 'episode_id': opaque_id,
                'sealed_parts': delivery['status']['sealed_parts'], 'plaintext_published': False,
                'validation_scope': 'envelope_schema_and_recipient_only'}
    episodes_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=episodes_dir))
    try:
        for name, expected_digest in delivery['part_sha256'].items():
            body = read_regular(source / name, research_vault.MAX_ENVELOPE)
            if hashlib.sha256(body).hexdigest() != expected_digest:
                raise ValueError('DELIVERY_CHANGED_BEFORE_COPY')
            # Preserve ciphertext bytes, including the original envelope encoding.
            research_vault.write_new(staging / name, body, 0o644)
        research_vault.write_new(staging / 'status.json', status_bytes, 0o644)
        # Detect changes/additions while copying instead of publishing a mixture.
        final_source = inspect_delivery(source, expected_recipient)
        if final_source != delivery:
            raise ValueError('DELIVERY_CHANGED_BEFORE_PUBLICATION')
        os.chmod(staging, 0o755)
        try:
            # An existing nonempty episode cannot be overwritten by rename.
            staging.rename(target)
        except FileExistsError:
            existing_matches()
        except OSError:
            if not target.exists():
                raise
            existing_matches()
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'version': 1, 'status': 'persisted', 'episode_id': opaque_id,
            'sealed_parts': delivery['status']['sealed_parts'], 'plaintext_published': False,
            'validation_scope': 'envelope_schema_and_recipient_only'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--episodes-dir', type=Path, default=Path('research_vault/episodes'))
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--recipient-id', required=True)
    parser.add_argument('--cell-id', help='Optional stable matrix-cell label; omission keeps legacy episode names.')
    args = parser.parse_args()
    try:
        result = persist_delivery(args.input, args.episodes_dir, args.repository,
                                  args.run_id, args.run_attempt, args.recipient_id, cell_id=args.cell_id)
    except Exception:
        # No malformed source content, filenames, or exception text in public CI.
        print('{"version":1,"status":"rejected","plaintext_published":false}')
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
