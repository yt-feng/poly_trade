#!/usr/bin/env python3
"""Verify a small decrypted cloud delivery; never parse source PDFs or publish.

ACK confirms transport only. Semantic learning and visual review stay incomplete.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

LIMIT = 5 * 1024 * 1024
PROTOCOL = 'per_unit_evidence_kind_and_digest_v1'
SEMANTICS = 'verified_evidence_transport_only; semantic_and_visual_read_not_credited'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_bounded(path, limit=LIMIT):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or info.st_size > limit:
        raise ValueError('input_file_contract')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('input_size_limit')
    return raw


def immutable(path, raw):
    if path.exists() or path.is_symlink():
        if read_bounded(path) != raw:
            raise ValueError('immutable_readback_conflict')
        return
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as target:
        target.write(raw)
        target.flush()
        os.fsync(target.fileno())


def verified_receipt(receipt, source_run):
    run, attempt = source_run.split('-')
    identity = {'repository': 'yt-feng/poly_trade', 'run_id': run, 'run_attempt': attempt}
    expected = digest(b'quality-first-sealed-episode-v1\0' + canonical(identity))
    if receipt.get('status') != 'verified_remote' or receipt.get('episode_id') != expected:
        raise ValueError('publication_receipt_not_verified_for_run')
    if receipt.get('source_run', source_run) != source_run or receipt.get('branch', 'sealed-research-journal') != 'sealed-research-journal':
        raise ValueError('publication_receipt_run_or_branch_mismatch')
    if receipt.get('plaintext_published', False) is not False:
        raise ValueError('publication_receipt_plaintext_contract')


def check_item(item):
    unit, kind = item['unit'], item['evidence_kind']
    sid, number = unit['source_id'], unit['unit_index']
    if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{64}', sid) or type(number) is not int or not 0 <= number < 10**12 or kind not in ('unit', 'visual'):
        raise ValueError('invalid_evidence_identity')
    key = sid + ':' + str(number).zfill(12) + ':' + kind
    complete, raw, suffix, actual = item.get('unit_evidence_complete'), None, None, None
    if type(complete) is not bool or item.get('semantic_read_credited') is not False or item.get('visual_review_credited', False) is not False:
        raise ValueError('evidence_completion_contract')
    if kind == 'visual' and item.get('artifact') != unit.get('metadata', {}).get('visual_artifact'):
        raise ValueError('visual_source_binding')
    if complete and (kind == 'visual' or unit.get('artifact')):
        if item.get('artifact_complete') is not True:
            raise ValueError('incomplete_artifact_claim')
        if kind == 'visual':
            raw = base64.b64decode(item['artifact_base64'], validate=True)
            if not raw.startswith(b'\x89PNG\r\n\x1a\n') or len(raw) > 512 * 1024 or item.get('artifact_bytes') != len(raw) or item.get('artifact_mime_type') != 'image/png':
                raise ValueError('visual_bytes_contract')
            suffix = '.png'
        else:
            raw = item['artifact_utf8'].encode('utf-8')
            if len(raw) > 262144:
                raise ValueError('unit_bytes_limit')
            suffix = '.json' if unit.get('unit_kind') in ('page', 'rows', 'archive_member') else '.txt'
            if suffix == '.json':
                json.loads(raw)
        actual = digest(raw)
        if item.get('artifact_sha256') != actual or (kind == 'unit' and unit.get('content_hash') != actual):
            raise ValueError('artifact_hash_mismatch')
    elif item.get('artifact_complete') is not False or 'artifact_utf8' in item or 'artifact_base64' in item:
        raise ValueError('unexpected_artifact_payload')
    evidence = digest(canonical({'version': 1, 'unit': unit, 'evidence_kind': kind, 'artifact_hash': actual}))
    if item.get('unit_key') != key or item.get('evidence_digest') != evidence:
        raise ValueError('evidence_digest_mismatch')
    return key, evidence, complete, raw, suffix


def validate_content_hashes(values, derived, units):
    """Validate optional cross-worker content hashes against checked bytes."""
    if not isinstance(values, dict):
        raise ValueError('proposal_content_hashes_contract')
    if set(values) != set(derived):
        raise ValueError('proposal_content_hashes_coverage_mismatch')
    for key, value in values.items():
        if (not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}:[0-9]{12}:(unit|visual)', key)
                or key not in units or not isinstance(value, str)
                or not re.fullmatch('[0-9a-f]{64}', value)):
            raise ValueError('proposal_content_hash_contract')
        if value != derived[key]:
            raise ValueError('proposal_content_hash_mismatch')


def verify(report_path, output_root, receipt_path=None):
    raw_report = read_bounded(report_path)
    report = json.loads(raw_report)
    if 'extraction' in report:
        if report.get('status') != 'remote_result_received' or report.get('returncode') != 0 or report.get('stdout_truncated') is not False:
            raise ValueError('remote_result_contract')
        report = report['extraction']
    proposal = report['unit_delivery_proposal']
    source_run, worker_hash = proposal['source_run'], proposal['worker_hash']
    if not re.fullmatch('[1-9][0-9]{0,19}-[1-9][0-9]{0,19}', source_run) or not re.fullmatch('[0-9a-f]{64}', worker_hash):
        raise ValueError('proposal_identity_contract')
    if report.get('run_id') != source_run or report.get('worker_sha256') != worker_hash or report.get('unit_delivery_protocol') != PROTOCOL or report.get('delivery_omitted_requires_private_cloud_readback'):
        raise ValueError('report_proposal_binding')
    if report.get('semantic_learning_complete') is not False:
        raise ValueError('semantic_completion_not_supported')
    records, units, seen = [], {}, set()
    for item in report['unit_texts']:
        key, evidence, complete, raw, suffix = check_item(item)
        if key in seen:
            raise ValueError('duplicate_evidence_key')
        seen.add(key)
        if complete:
            units[key] = evidence
        records.append((item, raw, suffix))
    if type(proposal.get('evidence_count')) is not int or proposal['evidence_count'] != len(units):
        raise ValueError('proposal_hash_or_coverage_mismatch')
    derived_content_hashes = {item['unit_key']: digest(raw) for item, raw, suffix in records
                              if raw is not None and item.get('unit_evidence_complete') is True}
    # The checked raw bytes are already bounded and hashed by check_item.  A
    # producer summary may carry this map explicitly; current reports omit it,
    # so derive it from those exact bytes in the report.
    supplied_content_hashes = proposal.get('content_hashes')
    if supplied_content_hashes is not None:
        validate_content_hashes(supplied_content_hashes, derived_content_hashes, units)
    rebuilt = {'version': 1, 'worker_hash': worker_hash, 'source_run': source_run, 'units': units,
               'semantics': SEMANTICS}
    candidates = [rebuilt]
    if supplied_content_hashes is not None or derived_content_hashes:
        candidates.append({**rebuilt, 'content_hashes': supplied_content_hashes if supplied_content_hashes is not None else derived_content_hashes})
    if not any(digest(canonical(candidate)) == proposal.get('proposal_hash') for candidate in candidates):
        raise ValueError('proposal_hash_or_coverage_mismatch')
    receipt_raw = read_bounded(receipt_path, 65536) if receipt_path else None
    if receipt_raw:
        verified_receipt(json.loads(receipt_raw), source_run)
    destination = Path(output_root).absolute() / source_run
    for path in (destination, *destination.parents):
        if path.is_symlink():
            raise ValueError('readback_directory_symlink')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries = []
    for item, raw, suffix in records:
        entry = {key: item[key] for key in ('unit_key', 'evidence_kind', 'evidence_digest', 'unit_evidence_complete', 'unit')}
        if raw is not None:
            filename = digest(raw) + suffix
            immutable(destination / filename, raw)
            entry['artifact_file'] = filename
        metadata = canonical(entry)
        filename = digest(metadata) + '.evidence.json'
        immutable(destination / filename, metadata)
        entries.append({'evidence_file': filename, **{key: entry[key] for key in ('unit_key', 'evidence_kind', 'unit_evidence_complete')}})
    ack = {key: proposal[key] for key in ('worker_hash', 'source_run', 'proposal_hash')}
    ack['evidence_kinds'] = ['unit', 'visual']
    summary = {'version': 1, 'source_run': source_run, 'report_sha256': digest(raw_report), 'proposal_verified': True,
               'verified_evidence_count': len(units), 'entries': entries, 'semantic_learning_complete': False,
               'visual_review_complete': False, 'secret_written': False, 'workflow_dispatched': False}
    immutable(destination / 'verification.json', canonical(summary))
    immutable(destination / 'proposal.json', canonical(rebuilt))
    if receipt_raw:
        immutable(destination / ('publication-receipt-' + digest(receipt_raw) + '.json'), receipt_raw)
        immutable(destination / 'ack.json', canonical(ack))
    else:
        immutable(destination / 'ack-candidate.json', canonical({'status': 'awaiting_verified_remote_publication', 'acknowledgement': ack}))
    return {'status': 'verified_with_ack' if receipt_raw else 'verified_awaiting_publication',
            'readback_directory': str(destination), 'verified_evidence_count': len(units),
            'semantic_learning_complete': False, 'visual_review_complete': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--output-root', type=Path, default=Path(__file__).resolve().parent / 'readback')
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(verify(args.report, args.output_root, args.receipt), sort_keys=True))

