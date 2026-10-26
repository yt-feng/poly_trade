"""Run a bounded private corpus extraction batch on the verified existing host.

No source document, extracted text, host detail, or diagnostic is published in
plaintext. The administrative identity stays on Actions and is never returned.
This step extracts content; it does not claim semantic learning or evaluation.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import research_vault as vault
import run_quality_first_research as delivery
import run_corpus_capacity_probe as probe

# Keep both transport ends aligned. This allows more already-exported page
# evidence per readback without changing the worker or its cache identity.
REPORT_LIMIT = 4 * 1024 * 1024
# The remote worker is bounded at 480s, but the controller also has to
# serialize its checkpoint/export and sealed report before SSH returns.  The
# old 660s deadline expired during that handoff, producing an unknown outcome
# even when the remote cursor had advanced.  Keep a finite bound with enough
# handoff grace for the cloud-only path.
REMOTE_TIMEOUT_SECONDS = 780
WORKER_MAX_SECONDS = 480
WORKER_PROCESS_TIMEOUT_SECONDS = WORKER_MAX_SECONDS + 15

# Public status is intentionally limited to a small operational taxonomy.  It
# helps the next scheduler distinguish transport/configuration failures from a
# remote report that ran and failed, without exposing host details or sealed
# research diagnostics.
FAILURE_CODES = {
    'identity_preflight_failed': 'identity_preflight_failed',
    'remote_failed': 'remote_transport_failed',
    'remote_outcome_unknown_requires_readback': 'remote_timeout_unknown',
    'remote_result_too_large': 'remote_result_too_large',
    'configuration_rejected': 'configuration_rejected',
}
EVIDENCE_CODES = {
    'worker_completed', 'readback_observed', 'deferred', 'worker_failed',
    'worker_timeout', 'remote_transport_failed', 'remote_timeout_unknown',
    'remote_result_too_large', 'remote_report_unclassified',
    'configuration_rejected', 'unknown',
}
CURSOR_CODES = {'committed', 'not_committed', 'readback_no_advance', 'unknown'}
COVERAGE_CODES = {'present', 'absent', 'unknown'}


def public_evidence_codes(record):
    """Return fixed operational categories without exposing report content."""
    if not isinstance(record, dict):
        return {'evidence_code': 'unknown', 'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    outer = record.get('status')
    if outer == 'remote_failed':
        return {'evidence_code': 'remote_transport_failed', 'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    if outer == 'remote_outcome_unknown_requires_readback':
        return {'evidence_code': 'remote_timeout_unknown', 'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    if outer == 'remote_result_too_large':
        return {'evidence_code': 'remote_result_too_large', 'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    if outer == 'identity_preflight_failed':
        return {'evidence_code': 'configuration_rejected', 'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    if outer != 'remote_result_received' or not isinstance(record.get('extraction'), dict):
        return {'evidence_code': 'configuration_rejected' if outer == 'configuration_rejected' else 'unknown',
                'cursor_code': 'unknown', 'coverage_code': 'unknown'}
    extraction = record['extraction']
    state = extraction.get('status')
    evidence = {
        'batch_observed': 'worker_completed',
        'readback_observed': 'readback_observed',
        'worker_failed': 'worker_failed',
        'worker_timeout_checkpoint_recovery_pending': 'worker_timeout',
        'deferred_capacity': 'deferred',
        'deferred_capacity_after_dependency': 'deferred',
        'deferred_active_extraction_controller': 'deferred',
        'deferred_active_worker': 'deferred',
        'deferred_no_complete_uploaded_sources': 'deferred',
    }.get(state, 'remote_report_unclassified')
    if state == 'readback_observed':
        cursor = 'readback_no_advance'
    elif type(extraction.get('metadata_cursor_committed')) is bool:
        cursor = 'committed' if extraction['metadata_cursor_committed'] else 'not_committed'
    else:
        cursor = 'unknown'
    coverage = 'present' if isinstance(extraction.get('coverage'), dict) else 'unknown'
    return {'evidence_code': evidence, 'cursor_code': cursor, 'coverage_code': coverage}

REMOTE_EXTRACT = r'''
import base64, datetime, fcntl, hashlib, importlib.util, json, os, pathlib, re, resource, signal, stat, subprocess, sys, urllib.request
payload = json.loads(PAYLOAD_LITERAL)
ROOT = pathlib.Path('/home/ubuntu/quantml-study')
REPORT_LIMIT = 4 * 1024 * 1024
MEMORY_LIMIT = 512 * 1024 * 1024
MAX_METADATA = 64
def bounded_option(name, default, minimum, maximum):
    value = payload.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('invalid_' + name)
    return value
MAX_METADATA_LIMIT = bounded_option('max_metadata', MAX_METADATA, 1, MAX_METADATA)
MAX_FILES_LIMIT = bounded_option('max_files', 32, 1, 32)
MAX_UNITS_LIMIT = bounded_option('max_units', 200, 1, 200)
PDF_OCR_ENABLED = payload.get('pdf_ocr', True)
if type(PDF_OCR_ENABLED) is not bool:
    raise ValueError('invalid_pdf_ocr')
WORKER_MAX_SECONDS = 480
WORKER_PROCESS_TIMEOUT_SECONDS = WORKER_MAX_SECONDS + 15
SOURCE_RECEIPT_ROWS = 128
SOURCE_RECEIPT_BYTES = 256 * 1024
SOURCE_INVENTORY_ROWS = 4096
SOURCE_INVENTORY_BYTES = 1024 * 1024
SOURCE_INVENTORY_INPUT_BYTES = 8 * 1024 * 1024
UNIT_INVENTORY_INPUT_BYTES = 16 * 1024 * 1024
UNIT_INVENTORY_ROWS = 200000
ACK_STATE_LIMIT = 32 * 1024 * 1024
TESSDATA_REVISION = '87416418657359cb625c412a48b6e1d6d41c29bd'
MAX_TESSDATA_BYTES = 32 * 1024 * 1024
report = {'version': 1, 'scope': 'bounded_private_cloud_extraction',
          'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'run_id': payload['run_id'], 'source_commit': payload['source_commit'], 'mode': payload['mode'],
          # Every invocation is a bounded tranche over already-uploaded
          # objects.  Keep this explicit in the sealed report so a successful
          # worker run cannot be mistaken for full-corpus learning.
          'manifest_scope': 'rotating_uploaded_objects_tranche_not_full_corpus',
          'full_corpus_complete': False,
          'semantic_learning_complete': False, 'strategy_evaluations': 0,
          'actual_orders': 0, 'trading_services_changed': False,
          'source_publication': False, 'errors': []}

def raw_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

def delivery_report_size(value):
    # The journal seals ASCII-canonical JSON. Count that representation here:
    # CJK and non-BMP text can expand after the UTF-8 SSH response is received.
    # Keep raw_json unchanged because it also defines stored evidence digests.
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii'))

def safe_directory(path, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or path.is_symlink() or s.st_uid != os.getuid():
        raise ValueError('private_directory_contract')
    if s.st_mode & 0o077:
        raise ValueError('private_directory_permissions')

def regular(path, cap=None):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or path.is_symlink() or (cap is not None and s.st_size > cap):
        raise ValueError('private_file_contract')
    return s

def read_small(path, cap):
    regular(path, cap)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as f:
        raw = f.read(cap + 1)
    if len(raw) > cap:
        raise ValueError('bounded_read_exceeded')
    return raw

def atomic(path, raw):
    temp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    if path.is_symlink():
        raise ValueError('target_symlink')
    temp.replace(path)

def checked_relative(value):
    if not isinstance(value, str) or not 0 < len(value) <= 4096 or '\\' in value or '\x00' in value:
        raise ValueError('invalid_relative_path')
    p = pathlib.PurePosixPath(value)
    if p.is_absolute() or '..' in p.parts or not p.parts or ':' in p.parts[0]:
        raise ValueError('unsafe_relative_path')
    return p

def child_limits():
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT, MEMORY_LIMIT))

def memory_available():
    with open('/proc/meminfo') as f:
        for line in f.read(16384).splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    return None

def available_bytes():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize

def worker_lock_busy(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('worker_lock_not_regular')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False
    finally:
        os.close(fd)

def tessdata_digest(path):
    info = regular(path, MAX_TESSDATA_BYTES)
    if info.st_size <= 0:
        raise ValueError('empty_tessdata')
    digest = hashlib.sha256()
    read_bytes = 0
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        for block in iter(lambda: source.read(262144), b''):
            read_bytes += len(block)
            if read_bytes > MAX_TESSDATA_BYTES:
                raise ValueError('tessdata_cached_file_limit')
            digest.update(block)
    if read_bytes != info.st_size:
        raise ValueError('tessdata_cached_file_changed')
    return info.st_size, digest.hexdigest()

class NoTessdataRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('tessdata_redirect_rejected')

def download_tessdata(path, url):
    # Only immutable HTTPS URLs constructed below reach this function. A signal
    # caps the entire transfer, not just individual socket reads.
    allowed = {'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/' + TESSDATA_REVISION + '/' + language + '.traineddata' for language in ('eng', 'chi_sim')}
    if url not in allowed:
        raise ValueError('tessdata_source_not_pinned')
    def expired(signum, frame):
        raise TimeoutError('tessdata_download_deadline')
    old = signal.signal(signal.SIGALRM, expired)
    signal.alarm(20)
    size, digest = 0, hashlib.sha256()
    try:
        opener = urllib.request.build_opener(NoTessdataRedirect())
        with opener.open(url, timeout=15) as response:
            if response.geturl() != url or not url.startswith('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/' + TESSDATA_REVISION + '/'):
                raise ValueError('tessdata_source_changed')
            length = response.headers.get('Content-Length')
            if length is not None and not 0 < int(length) <= MAX_TESSDATA_BYTES:
                raise ValueError('tessdata_content_length')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as target:
                while True:
                    block = response.read(min(262144, MAX_TESSDATA_BYTES - size + 1))
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_TESSDATA_BYTES:
                        raise ValueError('tessdata_download_limit')
                    target.write(block); digest.update(block)
                if size == 0:
                    raise ValueError('empty_tessdata_download')
                if length is not None and size != int(length):
                    raise ValueError('incomplete_tessdata_download')
                target.flush(); os.fsync(target.fileno())
        path.chmod(0o400)
        return size, digest.hexdigest()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)

def prepare_tessdata(run_dir):
    directory = ROOT / 'deps' / ('tessdata-fast-' + TESSDATA_REVISION)
    safe_directory(directory, create=True)
    provenance = []
    for language in ('eng', 'chi_sim'):
        url = 'https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/' + TESSDATA_REVISION + '/' + language + '.traineddata'
        target = directory / (language + '.traineddata')
        marker = directory / (language + '.source.json')
        if target.exists() or target.is_symlink() or marker.exists() or marker.is_symlink():
            if not target.exists() or not marker.exists():
                raise ValueError('tessdata_unconfirmed_file_requires_review')
            record = json.loads(read_small(marker, 4096))
            size, digest = tessdata_digest(target)
            if record.get('source_url') != url or record.get('source_commit') != TESSDATA_REVISION or record.get('language') != language or record.get('sha256') != digest or record.get('bytes') != size:
                raise ValueError('tessdata_cache_identity_mismatch')
        else:
            staging = run_dir / (language + '.traineddata.download')
            size, digest = download_tessdata(staging, url)
            record = {'language': language, 'source_url': url, 'source_commit': TESSDATA_REVISION,
                      'bytes': size, 'sha256': digest, 'downloaded_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            staging.rename(target)
            atomic(marker, raw_json(record))
        provenance.append(record)
    return directory, provenance

def unit_delivery_identity(unit, evidence_kind='unit', artifact_hash=None):
    sid, index = unit.get('source_id'), unit.get('unit_index')
    if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{64}', sid) or type(index) is not int or not 0 <= index < 10**12:
        raise ValueError('invalid_unit_delivery_identity')
    if evidence_kind not in ('unit', 'visual'):
        raise ValueError('invalid_delivery_evidence_kind')
    key = sid + ':' + str(index).zfill(12) + ':' + evidence_kind
    # The unit includes the artifact SHA and all extraction metadata/status.
    # Actual artifact bytes are verified before this identity can be acknowledged.
    digest = hashlib.sha256(raw_json({'version': 1, 'unit': unit,
                                    'evidence_kind': evidence_kind, 'artifact_hash': artifact_hash})).hexdigest()
    return key, digest

def validate_acknowledged_units(units):
    if not isinstance(units, dict):
        raise ValueError('invalid_acknowledged_units')
    for key, digest in units.items():
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}:[0-9]{12}:(unit|visual)', key) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid_acknowledged_unit_digest')

def validate_cross_worker_content_hashes(values):
    if not isinstance(values, dict):
        raise ValueError('invalid_cross_worker_content_hashes')
    for key, digest in values.items():
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}:[0-9]{12}:(unit|visual)', key) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid_cross_worker_content_hash')

def validate_cross_worker_content_workers(values):
    if not isinstance(values, dict):
        raise ValueError('invalid_cross_worker_content_workers')
    for key, worker_hash in values.items():
        if not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}:[0-9]{12}:(unit|visual)', key) or not isinstance(worker_hash, str) or not re.fullmatch('[0-9a-f]{64}', worker_hash):
            raise ValueError('invalid_cross_worker_content_worker')

def load_acknowledged(output, worker_hash):
    path = output / 'acknowledged-units.json'
    if not path.exists() and not path.is_symlink():
        return {'version': 1, 'worker_hash': worker_hash, 'units': {}, 'proposals': {}}
    state = json.loads(read_small(path, ACK_STATE_LIMIT))
    if state.get('version') != 1 or state.get('worker_hash') != worker_hash or not isinstance(state.get('proposals'), dict):
        raise ValueError('acknowledged_state_identity_mismatch')
    validate_acknowledged_units(state.get('units'))
    for source_run, receipt in state['proposals'].items():
        if not re.fullmatch('[0-9]{1,25}-[0-9]{1,5}', source_run) or not isinstance(receipt, dict) or not isinstance(receipt.get('proposal_hash'), str) or not re.fullmatch('[0-9a-f]{64}', receipt['proposal_hash']) or not isinstance(receipt.get('evidence_kinds'), list) or not receipt['evidence_kinds'] or not set(receipt['evidence_kinds']) <= {'unit', 'visual'}:
            raise ValueError('invalid_acknowledged_proposal')
    return state

def load_cross_worker_acknowledged(output):
    """Load the root acknowledgement index shared by all worker hashes.

    Per-worker acknowledgement files remain the audit record for a specific
    worker.  This index is only a conservative transport dedupe: a unit is
    skipped only when its exact delivery digest matches an acknowledgement.
    Artifact hashes alone do not acknowledge changed metadata. Any malformed index is
    rejected so a bad state can never cause evidence to be silently omitted.
    """
    path = output.parent.parent / 'extracted' / 'acknowledged-units.json'
    if not path.exists() and not path.is_symlink():
        return {'version': 1, 'units': {}, 'content_hashes': {}, 'content_workers': {}}
    state = json.loads(read_small(path, ACK_STATE_LIMIT))
    if state.get('version') != 1 or not isinstance(state.get('units'), dict):
        raise ValueError('cross_worker_acknowledged_state_identity_mismatch')
    validate_acknowledged_units(state['units'])
    content_hashes = state.get('content_hashes', {})
    content_workers = state.get('content_workers', {})
    validate_cross_worker_content_hashes(content_hashes)
    validate_cross_worker_content_workers(content_workers)
    if not set(content_workers) <= set(content_hashes):
        raise ValueError('cross_worker_content_worker_without_hash')
    if not set(content_hashes) <= set(state['units']):
        raise ValueError('cross_worker_content_hash_without_unit')
    state['content_hashes'] = content_hashes
    state['content_workers'] = content_workers
    return state

def load_versioned_acknowledged(root):
    """Independent registry; the legacy singleton files remain read-only."""
    path = root / 'extracted' / 'acknowledged-evidence-v2.json'
    if not path.exists() and not path.is_symlink():
        return {'version': 2, 'units': {}, 'proposals': {}, 'legacy_migrations': {}}
    state = json.loads(read_small(path, ACK_STATE_LIMIT))
    if state.get('version') != 2 or any(not isinstance(state.get(key), dict)
                                      for key in ('units', 'proposals', 'legacy_migrations')):
        raise ValueError('versioned_acknowledged_state_identity_mismatch')
    for reference, receipt in state['proposals'].items():
        if (not re.fullmatch('[0-9a-f]{64}:[0-9]{1,25}-[0-9]{1,5}', reference)
                or not isinstance(receipt, dict)
                or not isinstance(receipt.get('proposal_hash'), str)
                or not re.fullmatch('[0-9a-f]{64}', receipt['proposal_hash'])
                or not isinstance(receipt.get('evidence_kinds'), list)
                or not receipt['evidence_kinds'] or not set(receipt['evidence_kinds']) <= {'unit', 'visual'}):
            raise ValueError('invalid_versioned_acknowledged_proposal')
    for origin, digest in state['legacy_migrations'].items():
        if (not re.fullmatch('shared_v1|worker_v1:[0-9a-f]{64}', origin)
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise ValueError('invalid_legacy_ack_migration')
    for key, versions in state['units'].items():
        if not isinstance(versions, dict) or not versions:
            raise ValueError('invalid_acknowledged_versions')
        for digest, evidence in versions.items():
            validate_acknowledged_units({key: digest})
            if (not isinstance(evidence, dict) or not isinstance(evidence.get('proposal_refs'), dict)
                    or not isinstance(evidence.get('legacy_sources'), dict)
                    or not (evidence['proposal_refs'] or evidence['legacy_sources'])):
                raise ValueError('invalid_acknowledged_version_provenance')
            artifact_hash = evidence.get('artifact_sha256')
            if artifact_hash is not None:
                validate_cross_worker_content_hashes({key: artifact_hash})
            for reference, proposal_hash in evidence['proposal_refs'].items():
                receipt = state['proposals'].get(reference, {})
                if receipt.get('proposal_hash') != proposal_hash or key.rsplit(':', 1)[1] not in receipt.get('evidence_kinds', []):
                    raise ValueError('invalid_acknowledged_version_proposal')
            for origin, legacy_hash in evidence['legacy_sources'].items():
                if origin not in state['legacy_migrations'] or state['legacy_migrations'][origin] != legacy_hash:
                    raise ValueError('invalid_acknowledged_version_legacy_origin')
    return state

def acknowledged_version(state, key, digest, artifact_hash=None):
    versions = state['units'].setdefault(key, {})
    evidence = versions.setdefault(digest, {'artifact_sha256': None, 'proposal_refs': {}, 'legacy_sources': {}})
    previous = evidence['artifact_sha256']
    if previous is not None and artifact_hash is not None and previous != artifact_hash:
        raise ValueError('acknowledged_version_artifact_conflict')
    if artifact_hash is not None:
        evidence['artifact_sha256'] = artifact_hash
    return evidence

def migrate_legacy_acknowledged(output, worker_hash, state):
    """Explicitly import exact v1 versions without rewriting either v1 file."""
    legacy_worker = load_acknowledged(output, worker_hash)
    legacy_shared = load_cross_worker_acknowledged(output)
    for origin, path, legacy in (
            ('shared_v1', output.parent / 'acknowledged-units.json', legacy_shared),
            ('worker_v1:' + worker_hash, output / 'acknowledged-units.json', legacy_worker)):
        if not path.exists():
            continue
        # Hash canonical validated metadata only; never hash source documents.
        legacy_hash = hashlib.sha256(raw_json(legacy)).hexdigest()
        previous = state['legacy_migrations'].get(origin)
        if previous is not None and previous != legacy_hash:
            raise ValueError('legacy_ack_changed_after_migration')
        for key, digest in legacy['units'].items():
            evidence = acknowledged_version(state, key, digest, legacy.get('content_hashes', {}).get(key))
            evidence['legacy_sources'][origin] = legacy_hash
        state['legacy_migrations'][origin] = legacy_hash
    return legacy_worker

def derive_delivery_content_hashes(output, units):
    """Recover content hashes from a legacy proposal that omitted them."""
    path = output / 'unit-state.jsonl'
    if not path.exists() or path.is_symlink():
        return {}
    regular(path)
    found, consumed = {}, 0
    with path.open() as source:
        for line in source:
            if len(line) > 65536:
                raise ValueError('unit_metadata_line_limit')
            consumed += len(line)
            if consumed > REPORT_LIMIT:
                break
            unit = json.loads(line)
            for item in unit_delivery_items(output, unit):
                key = item['unit_key']
                if key in units and units[key] == item['evidence_digest'] and item.get('artifact_sha256'):
                    found[key] = item['artifact_sha256']
            if len(found) == len(units):
                break
    return found

def apply_delivery_ack(root, ack):
    if ack is None:
        return {'status': 'not_supplied', 'semantic_read_credited': False}
    required = {'worker_hash', 'source_run', 'proposal_hash'}
    if not isinstance(ack, dict) or not required <= set(ack) or not set(ack) <= required | {'evidence_kinds'}:
        raise ValueError('invalid_delivery_ack_fields')
    kinds = ack.get('evidence_kinds', ['unit'])
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= 2 or not all(isinstance(kind, str) for kind in kinds) or len(set(kinds)) != len(kinds) or not set(kinds) <= {'unit', 'visual'}:
        raise ValueError('invalid_delivery_ack_evidence_kinds')
    worker_hash, source_run, proposal_hash = ack['worker_hash'], ack['source_run'], ack['proposal_hash']
    if not isinstance(worker_hash, str) or not re.fullmatch('[0-9a-f]{64}', worker_hash) or not isinstance(source_run, str) or not re.fullmatch('[0-9]{1,25}-[0-9]{1,5}', source_run) or not isinstance(proposal_hash, str) or not re.fullmatch('[0-9a-f]{64}', proposal_hash):
        raise ValueError('invalid_delivery_ack_identity')
    output = root / 'extracted' / worker_hash
    for directory in (root / 'extracted', output, output / 'delivery-proposals'):
        safe_directory(directory)
    raw = read_small(output / 'delivery-proposals' / (source_run + '.json'), REPORT_LIMIT)
    if hashlib.sha256(raw).hexdigest() != proposal_hash:
        raise ValueError('delivery_ack_proposal_hash_mismatch')
    proposal = json.loads(raw)
    if proposal.get('version') != 1 or proposal.get('worker_hash') != worker_hash or proposal.get('source_run') != source_run:
        raise ValueError('delivery_ack_proposal_identity_mismatch')
    validate_acknowledged_units(proposal.get('units'))
    content_hashes = proposal.get('content_hashes', {})
    if 'content_hashes' not in proposal:
        content_hashes = derive_delivery_content_hashes(output, proposal['units'])
    validate_cross_worker_content_hashes(content_hashes)
    if not set(content_hashes) <= set(proposal['units']):
        raise ValueError('cross_worker_content_hash_without_unit')
    selected = {key: digest for key, digest in proposal['units'].items() if key.rsplit(':', 1)[1] in kinds}
    selected_content_hashes = {key: digest for key, digest in content_hashes.items() if key.rsplit(':', 1)[1] in kinds}
    state = load_versioned_acknowledged(root)
    before = raw_json(state)
    versions_before = sum(len(versions) for versions in state['units'].values())
    legacy = migrate_legacy_acknowledged(output, worker_hash, state)
    versions_after_migration = sum(len(versions) for versions in state['units'].values())
    prior_other_versions = sum(any(old != digest for old in state['units'].get(key, {}))
                               for key, digest in selected.items())
    reference = worker_hash + ':' + source_run
    previous = state['proposals'].get(reference)
    legacy_previous = legacy['proposals'].get(source_run)
    for receipt in (previous, legacy_previous):
        if receipt is not None and receipt['proposal_hash'] != proposal_hash:
            raise ValueError('delivery_ack_replay_conflict')
    already = previous is not None and set(kinds) <= set(previous['evidence_kinds'])
    for key, digest in selected.items():
        evidence = acknowledged_version(state, key, digest, selected_content_hashes.get(key))
        evidence['proposal_refs'][reference] = proposal_hash
    state['proposals'][reference] = {'proposal_hash': proposal_hash,
                                   'evidence_kinds': sorted(set(kinds) | set(previous['evidence_kinds'] if previous else []))}
    raw_state = raw_json(state)
    if len(raw_state) > ACK_STATE_LIMIT:
        raise ValueError('acknowledged_state_capacity')
    # The existing controller flock covers validation, migration and this
    # single atomic commit. Legacy ACKs and immutable proposals are untouched.
    if raw_state != before:
        atomic(root / 'extracted' / 'acknowledged-evidence-v2.json', raw_state)
    versions_after = sum(len(versions) for versions in state['units'].values())
    return {'status': 'already_acknowledged' if already else 'acknowledged',
            'ack_registry_version': 2, 'legacy_states_preserved': True,
            'ack_registry_sha256': hashlib.sha256(raw_state).hexdigest(),
            'registered_versions_before': versions_before,
            'registered_versions_after_migration': versions_after_migration,
            'registered_versions_after': versions_after,
            'newly_acknowledged_versions': versions_after - versions_after_migration,
            'selected_keys_with_prior_other_version': prior_other_versions,
            'legacy_migration_count': len(state['legacy_migrations']),
            'legacy_migrations_canonical_sha256': state['legacy_migrations'],
            'source_run': source_run, 'worker_hash': worker_hash,
            'proposal_hash': proposal_hash, 'acknowledged_evidence_count': len(selected),
            'evidence_kinds_acknowledged': kinds, 'semantic_read_credited': False, 'visual_review_credited': False}

def save_delivery_proposal(output, worker_hash, source_run, units, content_hashes=None):
    validate_acknowledged_units(units)
    content_hashes = content_hashes or {}
    validate_cross_worker_content_hashes(content_hashes)
    if not set(content_hashes) <= set(units):
        raise ValueError('cross_worker_content_hash_without_unit')
    proposal = {'version': 1, 'worker_hash': worker_hash, 'source_run': source_run, 'units': units,
                'content_hashes': content_hashes,
                'semantics': 'verified_evidence_transport_only; semantic_and_visual_read_not_credited'}
    raw = raw_json(proposal)
    if len(raw) > REPORT_LIMIT:
        raise ValueError('delivery_proposal_capacity')
    directory = output / 'delivery-proposals'
    safe_directory(directory, create=True)
    path = directory / (source_run + '.json')
    if path.exists() or path.is_symlink():
        if read_small(path, REPORT_LIMIT) != raw:
            raise ValueError('delivery_proposal_immutable_conflict')
    else:
        atomic(path, raw)
    return {'worker_hash': worker_hash, 'source_run': source_run,
            'proposal_hash': hashlib.sha256(raw).hexdigest(), 'evidence_count': len(units),
            'ack_required_after_verified_ciphertext_readback': True,
            'ack_evidence_kinds_default': ['unit'], 'visual_ack_requires_explicit_evidence_kinds': True,
            'semantic_read_credited': False}

def delivery_artifact(output, artifact, cap):
    relative = checked_relative(artifact)
    path = output.joinpath(*relative.parts)
    check = output
    for part in relative.parts:
        check = check / part
        if check.is_symlink():
            raise ValueError('unit_artifact_symlink')
    info = regular(path)
    return (read_small(path, cap) if info.st_size <= cap else None), info.st_size

def unit_delivery_items(output, unit):
    artifact = unit.get('artifact')
    item = {'unit': unit, 'evidence_kind': 'unit', 'artifact_complete': False,
            'unit_evidence_complete': not artifact, 'semantic_read_credited': False}
    actual_hash = None
    if artifact:
        raw, size = delivery_artifact(output, artifact, 262144)
        if raw is not None:
            actual_hash = hashlib.sha256(raw).hexdigest()
            if actual_hash != unit.get('content_hash'):
                raise ValueError('unit_artifact_hash_mismatch')
            item.update(artifact_utf8=raw.decode('utf-8'), artifact_complete=True,
                        artifact_sha256=actual_hash, unit_evidence_complete=True)
        else:
            item.update(delivery_state='pending_larger_private_unit_transfer', artifact_bytes=size)
    key, digest = unit_delivery_identity(unit, 'unit', actual_hash)
    item.update(unit_key=key, evidence_digest=digest)
    yield item
    visual = unit.get('metadata', {}).get('visual_artifact')
    if visual:
        item = {'unit': unit, 'evidence_kind': 'visual', 'artifact': visual,
                'artifact_complete': False, 'unit_evidence_complete': False,
                'semantic_read_credited': False, 'visual_review_credited': False}
        raw, size = delivery_artifact(output, visual, 512 * 1024)
        actual_hash = None
        if raw is not None:
            if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                raise ValueError('visual_png_signature')
            actual_hash = hashlib.sha256(raw).hexdigest()
            item.update(artifact_base64=base64.b64encode(raw).decode(), artifact_complete=True,
                        artifact_sha256=actual_hash, artifact_bytes=size,
                        artifact_mime_type='image/png', unit_evidence_complete=True)
        else:
            item.update(delivery_state='pending_bounded_cloud_visual_or_private_transfer', artifact_bytes=size)
        key, digest = unit_delivery_identity(unit, 'visual', actual_hash)
        item.update(unit_key=key, evidence_digest=digest)
        yield item

def build_unit_delivery(output, worker_hash, source_run, report):
    report['unit_texts'] = []
    report['unit_delivery_complete_for_cloud_snapshot'] = False
    units_path = output / 'unit-state.jsonl'
    acknowledged = load_acknowledged(output, worker_hash)['units']
    cross_worker_state = load_cross_worker_acknowledged(output)
    cross_worker_acknowledged = cross_worker_state['units']
    versioned_acknowledged = load_versioned_acknowledged(output.parent.parent)['units']
    proposed, proposed_content_hashes = {}, {}
    # Keep the retired content-only skip counter at zero for old report readers.
    exhausted, pending_large_units, skipped, cross_worker_skipped = True, False, 0, 0
    report['unit_delivery_protocol'] = 'per_unit_evidence_kind_and_digest_v1'
    report['unit_acknowledgement_protocol'] = 'exact_evidence_versions_v2_with_readonly_v1'
    report['legacy_delivery_cursor_ignored'] = (output / 'delivery-cursor.json').exists()
    if units_path.exists():
        regular(units_path)
        # The unit-state export is a durable queue, not a FIFO for the current
        # selected manifest.  Prior to this ordering pass, an old unacknowledged
        # page could consume the report budget before units produced by the
        # current cursor tranche, making every readback look like a duplicate.
        # Prefer the current manifest's source IDs (and their discovered child
        # sources) while retaining the original order for all other evidence.
        priority_sources = {entry.get('source_id') for entry in (report.get('selected_manifest') or [])
                            if isinstance(entry, dict) and isinstance(entry.get('source_id'), str)}
        if priority_sources and (output / 'manifest-state.jsonl').exists():
            # Close the parent->child relation without loading the complete
            # manifest into memory.  A bounded number of passes is sufficient
            # for archive discovery and leaves unlisted deep descendants in the
            # ordinary fallback pass.
            for _ in range(8):
                expanded = False
                with (output / 'manifest-state.jsonl').open() as manifest:
                    for line in manifest:
                        if len(line) > 65536:
                            raise ValueError('manifest_metadata_line_limit')
                        row = json.loads(line)
                        if row.get('parent_id') in priority_sources and row.get('source_id') not in priority_sources:
                            priority_sources.add(row.get('source_id'))
                            expanded = True
                if not expanded:
                    break
        report['unit_delivery_priority_source_count'] = len(priority_sources)

        def deliver_pass(priority_only):
            nonlocal exhausted, pending_large_units, skipped
            with units_path.open() as units:
                for line in units:
                    if len(line) > 65536:
                        raise ValueError('unit_metadata_line_limit')
                    unit = json.loads(line)
                    is_priority = unit.get('source_id') in priority_sources
                    if priority_only is not None and is_priority != priority_only:
                        continue
                    for item in unit_delivery_items(output, unit):
                        key, digest = item['unit_key'], item['evidence_digest']
                        complete = item['unit_evidence_complete']
                        content_hash = item.get('artifact_sha256')
                        exact_ack = (acknowledged.get(key) == digest or cross_worker_acknowledged.get(key) == digest
                                     or digest in versioned_acknowledged.get(key, {}))
                        if complete and exact_ack:
                            skipped += 1
                            continue
                        report['unit_texts'].append(item)
                        if delivery_report_size(report) > REPORT_LIMIT - 131072:
                            report['unit_texts'].pop()
                            exhausted = False
                            break
                        if complete:
                            proposed[key] = digest
                            if isinstance(content_hash, str):
                                proposed_content_hashes[key] = content_hash
                        else:
                            pending_large_units = True
                    if not exhausted:
                        break

        if priority_sources:
            deliver_pass(True)
            if exhausted:
                deliver_pass(False)
        else:
            deliver_pass(None)
    report.update(unit_delivery_complete_for_cloud_snapshot=exhausted and not pending_large_units,
                  unit_delivery_acknowledged_unchanged_skipped=skipped,
                  unit_delivery_cross_worker_skipped=cross_worker_skipped,
                  unit_delivery_count_semantics='unit_and_visual_evidence_are_separate; not_page_or_learning_counts',
                  unit_delivery_semantics='whole_unit_artifacts_and_separate_small_PNGs; no_semantic_or_visual_review_credit')
    report['unit_delivery_proposal'] = save_delivery_proposal(output, worker_hash, source_run, proposed, proposed_content_hashes)

def source_receipts(output, report):
    path = output / 'manifest-state.jsonl'
    receipts, consumed, rows = [], 0, 0
    summary = {'available': path.exists(), 'max_rows': SOURCE_RECEIPT_ROWS, 'max_bytes': SOURCE_RECEIPT_BYTES,
               'snapshot_kind': 'existing_manifest_export', 'truncated': False,
               'semantic_learning_complete': False}
    if path.exists():
        info = regular(path)
        fields = ('source_id', 'relative_path', 'expected_bytes', 'expected_hash', 'content_hash',
                  'total_units', 'cursor', 'status', 'unit_kind', 'notes', 'parser_protocol',
                  'parent_id', 'archive_depth', 'duplicate_of', 'updated_utc')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
            while rows < SOURCE_RECEIPT_ROWS and consumed < SOURCE_RECEIPT_BYTES:
                raw = source.readline(min(65536, SOURCE_RECEIPT_BYTES - consumed))
                if not raw:
                    break
                consumed += len(raw)
                if not raw.endswith(b'\n') and consumed < info.st_size:
                    summary.update(truncated=True, truncation_reason='source_line_or_byte_bound')
                    break
                row = json.loads(raw)
                rows += 1
                receipt = {key: row.get(key) for key in fields}
                receipt.update(sha256=row.get('content_hash') or row.get('expected_hash'),
                               sha256_verified=bool(row.get('content_hash')),
                               semantic_learning_complete=False, visual_review_complete=False)
                if len(raw_json(receipts + [receipt])) > SOURCE_RECEIPT_BYTES:
                    summary.update(truncated=True, truncation_reason='serialized_receipt_byte_bound')
                    break
                receipts.append(receipt)
        if consumed < info.st_size:
            summary['truncated'] = True
            summary.setdefault('truncation_reason', 'row_or_byte_bound')
        summary.update(snapshot_bytes=info.st_size, input_bytes_read=consumed, input_rows_read=rows,
                       last_source_id=receipts[-1]['source_id'] if receipts else None)
    summary.update(receipts_returned=len(receipts), serialized_receipt_bytes=len(raw_json(receipts)))
    report.update(source_receipts=receipts, source_receipts_bounds=summary)

def inventory_export_rows(path, max_bytes, max_rows, bounds):
    """Read only a bounded existing metadata export, never its artifact paths."""
    bounds.update(available=path.exists(), max_input_bytes=max_bytes, max_rows=max_rows,
                  input_bytes_read=0, rows_read=0, truncated=False, complete=False)
    if not bounds['available']:
        return
    info = regular(path)
    bounds['snapshot_bytes'] = info.st_size
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as source:
        while bounds['input_bytes_read'] < info.st_size:
            remaining = max_bytes - bounds['input_bytes_read']
            if remaining <= 0 or bounds['rows_read'] >= max_rows:
                bounds.update(truncated=True, truncation_reason='input_byte_or_row_bound')
                break
            raw = source.readline(min(65536, remaining))
            bounds['input_bytes_read'] += len(raw)
            if not raw or (not raw.endswith(b'\n') and bounds['input_bytes_read'] < info.st_size):
                bounds.update(truncated=True, truncation_reason='line_or_input_byte_bound')
                break
            row = json.loads(raw)
            if not isinstance(row, dict):
                raise ValueError('inventory_export_row_not_object')
            bounds['rows_read'] += 1
            yield row
    after = regular(path)
    if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        bounds.update(truncated=True, truncation_reason='export_changed_during_read')
    bounds['complete'] = not bounds['truncated'] and bounds['input_bytes_read'] == info.st_size

def compact_source_inventory(output, report):
    """Queue visibility only: persisted metadata is neither delivery nor learning."""
    source_bounds, unit_bounds, items = {}, {}, {}
    fields = ('source_id', 'parent_id', 'archive_depth', 'duplicate_of', 'status',
              'expected_bytes', 'expected_hash', 'content_hash', 'unit_kind', 'cursor',
              'total_units', 'updated_utc')
    for row in inventory_export_rows(output / 'manifest-state.jsonl', SOURCE_INVENTORY_INPUT_BYTES,
                                     SOURCE_INVENTORY_ROWS, source_bounds):
        sid = row.get('source_id')
        if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{64}', sid) or sid in items:
            raise ValueError('inventory_source_identity')
        item = {field: row.get(field) for field in fields}
        # Pure suffix classification performs no source-file stat/read/hash.
        suffix = pathlib.PurePosixPath(row.get('relative_path') or '').suffix.lower()
        item['extension'] = suffix
        item['kind'] = ('pdf' if suffix == '.pdf' else
                        'archive' if suffix in ('.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar') else
                        'video' if suffix in ('.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v') else
                        row.get('unit_kind') or 'unclassified')
        item['total_pages'] = row.get('total_units') if row.get('unit_kind') == 'pages' else None
        notes = row.get('notes') or {}
        item['pages_with_extraction_gap'] = notes.get('pages_with_extraction_gap') if isinstance(notes, dict) else None
        item.update(exported_unit_count=0, available_unit_count=0, fully_extracted_unit_count=0)
        items[sid] = item
    for unit in inventory_export_rows(output / 'unit-state.jsonl', UNIT_INVENTORY_INPUT_BYTES,
                                      UNIT_INVENTORY_ROWS, unit_bounds):
        item = items.get(unit.get('source_id'))
        if item is None:
            continue
        item['exported_unit_count'] += 1
        if unit.get('artifact') and unit.get('content_hash'):
            item['available_unit_count'] += 1
            if unit.get('status') == 'extracted':
                item['fully_extracted_unit_count'] += 1
    inventory, serialized_bytes = [], 2
    for item in items.values():
        added = delivery_report_size(item) + bool(inventory)
        if serialized_bytes + added > SOURCE_INVENTORY_BYTES:
            break
        inventory.append(item)
        serialized_bytes += added
    output_truncated = len(inventory) != len(items)
    report.update(source_inventory=inventory, source_inventory_bounds={
        'version': 1, 'scope': 'existing_worker_queue_exports_only_not_full_corpus',
        'max_rows': SOURCE_INVENTORY_ROWS, 'max_serialized_bytes': SOURCE_INVENTORY_BYTES,
        'rows_returned': len(inventory), 'serialized_bytes': serialized_bytes,
        'source_export': source_bounds, 'unit_export': unit_bounds,
        'truncated': source_bounds.get('truncated', False) or unit_bounds.get('truncated', False) or output_truncated,
        'output_truncated': output_truncated,
        'complete_for_source_export': source_bounds.get('complete', False) and not output_truncated,
        'unit_counts_complete_for_export': unit_bounds.get('complete', False),
        'unit_count_semantics': 'export_records_with_artifact_and_hash; lower_bounds_unless_unit_export_complete; artifacts_not_reverified',
        'content_hash_provenance': 'persisted_worker_content_hash_only_no_expected_hash_fallback',
        'expected_hash_provenance': 'source_metadata_claim_not_reverified',
        'source_content_read': False, 'source_hashes_recomputed': False,
        'semantic_learning_credit': 0, 'visual_review_credit': 0, 'full_corpus_complete': False})

def source_availability_snapshot(root, report):
    """Inspect small upload metadata and object stat only; never hash corpus bytes."""
    cursor = root / 'metadata-extract-cursor.json'
    snapshot = {'metadata_cursor_advanced': False, 'source_content_read': False,
                'source_hashes_recomputed': False, 'max_sources': SOURCE_RECEIPT_ROWS,
                'source_scope': 'bounded_source_receipts_only', 'sources': []}
    if cursor.exists() or cursor.is_symlink():
        raw = read_small(cursor, 4096)
        value = json.loads(raw)
        last = value.get('last_source_id', '')
        if last and not re.fullmatch('[0-9a-f]{64}', last):
            raise ValueError('readback_metadata_cursor_identity')
        snapshot['metadata_cursor'] = {'last_source_id': last, 'updated_run_id': value.get('updated_run_id'),
                                       'snapshot_sha256': hashlib.sha256(raw).hexdigest()}
    else:
        snapshot['metadata_cursor'] = None
    metadata, objects = root / 'metadata', root / 'objects'
    try:
        safe_directory(metadata)
        safe_directory(objects)
    except (FileNotFoundError, ValueError) as exc:
        snapshot.update(status='source_directories_unavailable', error_class=type(exc).__name__)
        report['source_availability'] = snapshot
        return
    for source in report.get('source_receipts', [])[:SOURCE_RECEIPT_ROWS]:
        sid = source.get('source_id')
        if not isinstance(sid, str) or not re.fullmatch('[0-9a-f]{64}', sid):
            raise ValueError('readback_source_identity')
        item = {'source_id': sid, 'content_hash_recomputed': False}
        if source.get('parent_id'):
            item['status'] = 'archive_child_not_top_level_uploaded_object'
        else:
            try:
                meta = json.loads(read_small(metadata / sid, 8192))
                info = regular(objects / sid)
                size = meta.get('size_bytes')
                valid = (meta.get('state') == 'complete' and meta.get('source_id') == sid
                         and type(size) is int and size == info.st_size
                         and isinstance(meta.get('sha256'), str) and re.fullmatch('[0-9a-f]{64}', meta['sha256'])
                         and source.get('expected_bytes') in (None, size)
                         and meta.get('relative_path') == source.get('relative_path')
                         and meta.get('sha256') == source.get('sha256'))
                item.update(status='available_metadata_and_size_match' if valid else 'metadata_or_size_mismatch',
                            upload_state=meta.get('state'), object_bytes=info.st_size,
                            object_mtime_ns=info.st_mtime_ns, metadata_sha256=meta.get('sha256'))
            except (FileNotFoundError, ValueError, OSError) as exc:
                item.update(status='unavailable_or_rejected', error_class=type(exc).__name__)
        snapshot['sources'].append(item)
    snapshot.update(status='observed', sources_checked=len(snapshot['sources']),
                    source_receipts_truncated=report.get('source_receipts_bounds', {}).get('truncated', False))
    report['source_availability'] = snapshot

def load_uncommitted_manifest(root, cursor):
    """Recover a manifest whose cursor was written before its worker finished.

    Older controller versions advanced the metadata cursor before launching the
    worker.  A transport drop could therefore leave a manifest on disk while
    making the next batch skip it.  Recovery is intentionally narrow: it only
    follows the run id stamped in the cursor, requires the immutable manifest,
    and refuses a run that already has a completion marker.
    """
    if not isinstance(cursor, dict):
        return None
    run_id = cursor.get('updated_run_id')
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9]{1,25}-[0-9]{1,5}', run_id):
        return None
    run_dir = root / 'extract-runs' / run_id
    manifest = run_dir / 'manifest.jsonl'
    complete = run_dir / 'worker-complete.json'
    if not manifest.exists() or manifest.is_symlink() or complete.exists() or complete.is_symlink():
        return None
    regular(manifest)
    raw = read_small(manifest, 2 * 1024 * 1024)
    entries = []
    for line in raw.splitlines():
        if not line:
            continue
        entry = json.loads(line)
        source_id = entry.get('source_id')
        relative_path = entry.get('relative_path')
        size_bytes = entry.get('size_bytes')
        digest = entry.get('sha256')
        if (not isinstance(source_id, str) or not re.fullmatch(r'[0-9a-f]{64}', source_id)
                or not isinstance(relative_path, str) or not isinstance(size_bytes, int)
                or isinstance(size_bytes, bool) or not 0 < size_bytes <= 256 * 1024**2
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise ValueError('uncommitted_manifest_entry_identity')
        entries.append({'source_id': source_id, 'relative_path': relative_path,
                        'size_bytes': size_bytes, 'sha256': digest})
    if not entries:
        raise ValueError('uncommitted_manifest_empty')
    return {'run_id': run_id, 'run_dir': run_dir, 'manifest': manifest, 'selected': entries}

def attach_output_snapshot(output, worker_hash, source_run, report):
    if (output / 'coverage.json').exists():
        report['coverage'] = json.loads(read_small(output / 'coverage.json', 65536))
    source_receipts(output, report)
    compact_source_inventory(output, report)
    if payload['mode'] == 'readback':
        source_availability_snapshot(ROOT, report)
    build_unit_delivery(output, worker_hash, source_run, report)

def readback_existing_output(worker_raw, worker_hash):
    output = ROOT / 'extracted' / worker_hash
    for directory in (ROOT / 'bin', ROOT / 'extracted', output):
        safe_directory(directory)
    if read_small(ROOT / 'bin' / ('corpus_worker-' + worker_hash + '.py'), 200000) != worker_raw:
        raise ValueError('readback_worker_source_mismatch')
    report.update(worker_sha256=worker_hash, private_output_root=str(output),
                  worker_executed=False, extraction_dependencies_installed=False,
                  snapshot_scope='existing_exports_only; no_source_parsing_or_queue_advance')
    if worker_lock_busy(output / 'worker.lock'):
        report['status'] = 'deferred_active_worker'
        return
    attach_output_snapshot(output, worker_hash, payload['run_id'], report)
    report['status'] = 'readback_observed'

def process():
    if payload.get('mode') not in ('extract', 'readback'):
        raise ValueError('invalid_controller_mode')
    os.umask(0o077)
    for ancestor in list(ROOT.parents)[:-1] + [ROOT]:
        if ancestor.is_symlink():
            raise ValueError('fixed_root_symlink')
    safe_directory(ROOT)
    lockpath = ROOT / '.extract-controller.lock'
    lockfd = os.open(lockpath, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            report['status'] = 'deferred_active_extraction_controller'
            return
        process_locked()
    finally:
        os.close(lockfd)

def process_locked():
    # The caller owns the controller flock for extraction and readback alike.
    report['delivery_ack_result'] = apply_delivery_ack(ROOT, payload.get('delivery_ack'))
    worker_raw = base64.b64decode(payload['worker_base64'], validate=True)
    if not 0 < len(worker_raw) <= 200000:
        raise ValueError('worker_source_size')
    compile(worker_raw, 'corpus_worker.py', 'exec')
    worker_hash = hashlib.sha256(worker_raw).hexdigest()
    if payload['mode'] == 'readback':
        readback_existing_output(worker_raw, worker_hash)
        return
    report['worker_executed'] = False
    report['capacity_before'] = {'mem_available_bytes': memory_available(), 'free_bytes': available_bytes()}
    if report['capacity_before']['mem_available_bytes'] is None or report['capacity_before']['mem_available_bytes'] < 600 * 1024**2 or report['capacity_before']['free_bytes'] < 8 * 1024**3:
        report['status'] = 'deferred_capacity'
        return
    for name in ('bin', 'input', 'extract-runs', 'deps', 'extracted'):
        safe_directory(ROOT / name, create=True)
    for name in ('objects', 'metadata'):
        safe_directory(ROOT / name)
    worker = ROOT / 'bin' / ('corpus_worker-' + worker_hash + '.py')
    if worker.exists():
        if read_small(worker, 200000) != worker_raw:
            raise ValueError('worker_hash_path_conflict')
    else:
        atomic(worker, worker_raw)
    run_dir = ROOT / 'extract-runs' / payload['run_id']
    safe_directory(run_dir, create=True)
    output = ROOT / 'extracted' / worker_hash
    safe_directory(output, create=True)
    dependencies = ROOT / 'deps' / 'pymupdf-v1'
    report.update(worker_sha256=worker_hash, private_output_root=str(output))
    if worker_lock_busy(output / 'worker.lock'):
        report['status'] = 'deferred_active_worker'
        return
    install_marker = dependencies / 'install-complete.json'
    child_env = dict(os.environ, QUANTML_VERIFIED_CLOUD_BACKEND='aws', PYTHONPATH=str(dependencies),
                     OMP_THREAD_LIMIT='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
    if not install_marker.exists():
        if dependencies.exists() or dependencies.is_symlink():
            report['status'] = 'dependency_directory_unconfirmed_requires_review'
            return
        staging_dependencies = ROOT / 'deps' / ('pymupdf-v1-install-' + payload['run_id'])
        if staging_dependencies.exists() or staging_dependencies.is_symlink():
            report['status'] = 'dependency_install_outcome_requires_review'
            return
        safe_directory(staging_dependencies, create=True)
        install_cmd = [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
                       '--no-cache-dir', '--only-binary=:all:', '--no-deps', '--target', str(staging_dependencies), 'PyMuPDF']
        with (run_dir / 'pip-private.log').open('wb') as log:
            try:
                result = subprocess.run(install_cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                        env=child_env, timeout=180, preexec_fn=child_limits, check=False)
                report['dependency_install_returncode'] = result.returncode
            except subprocess.TimeoutExpired:
                report['status'] = 'dependency_install_timeout'
                return
        if result.returncode != 0:
            report['status'] = 'dependency_install_failed'
            report['dependency_log_tail_base64'] = base64.b64encode(read_small(run_dir / 'pip-private.log', 512*1024)[-65536:]).decode()
            return
        atomic(staging_dependencies / 'install-complete.json', raw_json({'package': 'PyMuPDF', 'installation_scope': 'dedicated_task_directory',
                                                                        'installed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}))
        staging_dependencies.rename(dependencies)
    else:
        safe_directory(dependencies)
        regular(install_marker, 16384)
    report['private_dependency_distributions'] = sorted(p.name for p in dependencies.glob('*.dist-info'))[:10]
    if PDF_OCR_ENABLED:
        tessdata, tessdata_provenance = prepare_tessdata(run_dir)
        report['ocr_language_data'] = tessdata_provenance
        report['ocr_engine'] = 'PyMuPDF_embedded_Tesseract; no_external_executable'
    else:
        tessdata, tessdata_provenance = None, []
        report['ocr_language_data'] = []
        report['ocr_engine'] = 'disabled_native_text_only'
    capability_id = hashlib.sha256(raw_json({'distributions': report['private_dependency_distributions'],
                                            'pdf_ocr': PDF_OCR_ENABLED,
                                            'language_data': [{key: item[key] for key in ('language', 'source_commit', 'sha256', 'bytes')}
                                                              for item in tessdata_provenance]})).hexdigest()
    report['extraction_capability_id'] = capability_id
    if memory_available() < 600 * 1024**2 or available_bytes() < 8 * 1024**3:
        report['status'] = 'deferred_capacity_after_dependency'
        return
    cursor_path = ROOT / 'metadata-extract-cursor.json'
    cursor_value = {}
    last = ''
    if cursor_path.exists():
        cursor_value = json.loads(read_small(cursor_path, 4096))
        if not isinstance(cursor_value, dict):
            raise ValueError('invalid_metadata_cursor')
        last = cursor_value.get('last_source_id', '')
        if last and not re.fullmatch('[0-9a-f]{64}', last):
            raise ValueError('invalid_metadata_cursor')
    recovery = load_uncommitted_manifest(ROOT, cursor_value)
    names = sorted(p.name for p in (ROOT / 'metadata').iterdir() if re.fullmatch('[0-9a-f]{64}', p.name))
    recovery_source_ids = payload.get('recovery_source_ids', [])
    if not isinstance(recovery_source_ids, list) or len(recovery_source_ids) > 8 or any(
            not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value)
            for value in recovery_source_ids):
        raise ValueError('invalid_recovery_source_ids')
    recovery_source_ids = list(dict.fromkeys(recovery_source_ids))
    targeted_recovery = bool(recovery_source_ids)
    targeted = [name for name in recovery_source_ids if name in names]
    order = targeted + [name for name in names if name not in set(targeted) and name > last] + [name for name in names if name not in set(targeted) and name <= last]
    selected, metadata_read, deferred = [], 0, []
    if recovery is not None:
        selected = recovery['selected']
        manifest = recovery['manifest']
        run_dir = recovery['run_dir']
        report['recovered_uncommitted_run_id'] = recovery['run_id']
        report['recovery_scope'] = 'cursor_stamped_manifest_before_worker_completion'
    else:
        for sid in order[:MAX_METADATA_LIMIT]:
            metadata_read += 1
            try:
                meta = json.loads(read_small(ROOT / 'metadata' / sid, 8192))
                if meta.get('state') != 'complete':
                    deferred.append({'source_id': sid, 'state': 'upload_not_complete'})
                    continue
                if meta.get('source_id') != sid or not re.fullmatch('[0-9a-f]{64}', meta.get('sha256', '')):
                    raise ValueError('object_identity_contract')
                size = meta.get('size_bytes')
                if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= 256 * 1024**2:
                    raise ValueError('object_size_contract')
                origin = ROOT / 'objects' / sid
                original_stat = regular(origin)
                if original_stat.st_size != size:
                    raise ValueError('object_size_mismatch')
                relative = checked_relative(meta['relative_path'])
                target = ROOT / 'input'
                for part in relative.parts[:-1]:
                    target = target / part
                    safe_directory(target, create=True)
                target = target / relative.name
                if target.exists() or target.is_symlink():
                    current = regular(target)
                    if (current.st_dev, current.st_ino) != (original_stat.st_dev, original_stat.st_ino):
                        raise ValueError('existing_input_identity_conflict')
                else:
                    os.link(origin, target, follow_symlinks=False)
                selected.append({'source_id': sid, 'relative_path': meta['relative_path'],
                                 'size_bytes': size, 'sha256': meta['sha256']})
            except Exception as exc:
                deferred.append({'source_id': sid, 'state': 'metadata_or_object_rejected', 'error_class': type(exc).__name__, 'error': str(exc)[:160]})
        manifest = run_dir / 'manifest.jsonl'
        atomic(manifest, b''.join(raw_json(entry) + b'\n' for entry in selected))
        if targeted_recovery:
            report['recovery_scope'] = 'explicit_source_id_recovery'
            report['recovery_source_ids'] = targeted
            report['recovery_source_ids_missing'] = [value for value in recovery_source_ids if value not in names]
    report.update(metadata_records_read=metadata_read, metadata_names_seen=len(names),
                  selected_manifest=selected, deferred_sources=deferred,
                  manifest_scope='rotating_uploaded_objects_tranche_not_full_corpus',
                  full_corpus_complete=False)
    if not selected and not (output / 'state.sqlite3').exists():
        report['status'] = 'deferred_no_complete_uploaded_sources'
        return
    preflight = {'backend': 'aws', 'backend_verified': True,
                 'input_scope_and_bytes': {'current_manifest_files': len(selected), 'current_manifest_bytes': sum(e['size_bytes'] for e in selected),
                                           'existing_private_queue_may_resume': True},
                 'estimated_cpu_memory_runtime': {'cpu_parallelism': 1, 'max_address_space_mib': 512, 'worker_wall_seconds': WORKER_MAX_SECONDS},
                 'cache_identity': worker_hash, 'concurrency': 1, 'timeout_seconds': WORKER_MAX_SECONDS,
                 'cost_or_quota_bound': 'existing_host_only; no provisioning; one worker for at most480seconds',
                 'batch_profile': {'max_metadata': MAX_METADATA_LIMIT, 'max_files': MAX_FILES_LIMIT,
                                   'max_units': MAX_UNITS_LIMIT, 'pdf_ocr': PDF_OCR_ENABLED},
                 'private_output_destination': str(output)}
    preflight_path = run_dir / 'preflight.json'
    atomic(preflight_path, raw_json(preflight))
    command = [sys.executable, str(worker), '--manifest', str(manifest), '--input-root', str(ROOT / 'input'),
               '--output-root', str(output), '--preflight', str(preflight_path), '--backend', 'aws',
               '--max-files', str(MAX_FILES_LIMIT), '--max-units', str(MAX_UNITS_LIMIT), '--max-seconds', str(WORKER_MAX_SECONDS),
               '--max-output-bytes', str(64 * 1024**2), '--max-memory-mib', '512',
               '--max-member-bytes', str(64 * 1024**2), '--max-page-pixels', '4000000',
               '--capability-id', capability_id]
    if PDF_OCR_ENABLED:
        command.extend(('--pdf-ocr', '--tessdata', str(tessdata), '--ocr-languages', 'eng+chi_sim'))
    with (run_dir / 'worker-private.log').open('wb') as log:
        try:
            report['worker_executed'] = True
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    env=child_env, timeout=WORKER_PROCESS_TIMEOUT_SECONDS, preexec_fn=child_limits, check=False)
            report.update(status='batch_observed' if result.returncode == 0 else 'worker_failed', worker_returncode=result.returncode)
            if result.returncode == 0:
                atomic(run_dir / 'worker-complete.json', raw_json({'run_id': run_dir.name,
                                                                   'controller_run_id': payload['run_id'],
                                                                   'completed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}))
                if recovery is None and not targeted_recovery and order[:MAX_METADATA_LIMIT]:
                    atomic(cursor_path, raw_json({'last_source_id': order[:MAX_METADATA_LIMIT][-1],
                                                  'updated_run_id': payload['run_id']}))
                    report['metadata_cursor_committed'] = True
                else:
                    report['metadata_cursor_committed'] = False
        except subprocess.TimeoutExpired:
            report.update(status='worker_timeout_checkpoint_recovery_pending', worker_returncode=None,
                          recovery='next_worker_reconciles_unfinished_page_attempts_under_kernel_lock')
    report['coverage_snapshot_may_precede_failed_worker'] = report.get('worker_returncode') != 0
    attach_output_snapshot(output, worker_hash, payload['run_id'], report)
    report['worker_log_tail_base64'] = base64.b64encode(read_small(run_dir / 'worker-private.log', 512*1024)[-32768:]).decode()
    report['capacity_after'] = {'mem_available_bytes': memory_available(), 'free_bytes': available_bytes()}
    if delivery_report_size(report) > REPORT_LIMIT:
        raise ValueError('compact_delivery_limit')

try:
    process()
except Exception as exc:
    report.update(status='orchestration_failed', error_class=type(exc).__name__, error=str(exc)[:500])
if delivery_report_size(report) > REPORT_LIMIT:
    report.pop('unit_texts', None)
    report.pop('unit_delivery_proposal', None)
    report['delivery_omitted_requires_private_cloud_readback'] = True
print(raw_json(report).decode())
'''


def delivery_ack_from_environment(environment):
    encoded = environment.get('CORPUS_DELIVERY_ACK_B64', '')
    if not encoded:
        return None
    if not isinstance(encoded, str) or len(encoded) > 4096:
        raise ValueError('DELIVERY_ACK_SIZE')
    ack = json.loads(base64.b64decode(encoded, validate=True))
    required = {'worker_hash', 'source_run', 'proposal_hash'}
    if not isinstance(ack, dict) or not required <= set(ack) or not set(ack) <= required | {'evidence_kinds'}:
        raise ValueError('DELIVERY_ACK_FIELDS')
    kinds = ack.get('evidence_kinds', ['unit'])
    if not isinstance(kinds, list) or not 1 <= len(kinds) <= 2 or not all(isinstance(kind, str) for kind in kinds) or len(set(kinds)) != len(kinds) or not set(kinds) <= {'unit', 'visual'}:
        raise ValueError('DELIVERY_ACK_EVIDENCE_KINDS')
    for name in ('worker_hash', 'proposal_hash'):
        if not isinstance(ack[name], str) or not re.fullmatch(r'[0-9a-f]{64}', ack[name]):
            raise ValueError('DELIVERY_ACK_HASH')
    if not isinstance(ack['source_run'], str) or not re.fullmatch(r'[0-9]{1,25}-[0-9]{1,5}', ack['source_run']):
        raise ValueError('DELIVERY_ACK_SOURCE_RUN')
    return ack


def source_payload(environment, worker_source):
    mode = environment.get('CORPUS_EXTRACT_MODE') or 'extract'
    if mode not in ('extract', 'readback'):
        raise ValueError('INVALID_CONTROLLER_MODE')
    run = str(environment.get('GITHUB_RUN_ID', ''))
    attempt = str(environment.get('GITHUB_RUN_ATTEMPT', ''))
    commit = environment.get('GITHUB_SHA', '')
    if not re.fullmatch(r'[0-9]{1,25}', run) or not re.fullmatch(r'[0-9]{1,5}', attempt):
        raise ValueError('CLOUD_RUN_ID_REQUIRED')
    if not re.fullmatch(r'[0-9a-f]{40,64}', commit):
        raise ValueError('SOURCE_COMMIT_REQUIRED')
    if not isinstance(worker_source, bytes) or not 0 < len(worker_source) <= 200000:
        raise ValueError('WORKER_SOURCE_SIZE')
    compile(worker_source, 'corpus_worker.py', 'exec')
    raw_ocr = environment.get('CORPUS_PDF_OCR', 'false').strip().lower()
    if raw_ocr not in ('true', 'false'):
        raise ValueError('INVALID_PDF_OCR')
    def option(name, default, minimum, maximum):
        raw = environment.get(name, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ValueError('INVALID_' + name.upper())
        if not minimum <= value <= maximum:
            raise ValueError('INVALID_' + name.upper())
        return value
    raw_recovery = environment.get('CORPUS_RECOVERY_SOURCE_IDS', '').strip()
    recovery_source_ids = [] if not raw_recovery else raw_recovery.split(',')
    if len(recovery_source_ids) > 8 or any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in recovery_source_ids):
        raise ValueError('INVALID_CORPUS_RECOVERY_SOURCE_IDS')
    return {'run_id': run + '-' + attempt, 'source_commit': commit, 'mode': mode,
            'worker_base64': base64.b64encode(worker_source).decode(),
            'delivery_ack': delivery_ack_from_environment(environment),
            'pdf_ocr': raw_ocr == 'true',
            'max_metadata': option('CORPUS_MAX_METADATA', 16, 1, 64),
            'max_files': option('CORPUS_MAX_FILES', 8, 1, 32),
            'max_units': option('CORPUS_MAX_UNITS', 80, 1, 200),
            'recovery_source_ids': recovery_source_ids}


def extract():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('CLOUD_ONLY')
    host, user, known, key = probe.configuration(os.environ)
    payload = source_payload(os.environ, Path(__file__).with_name('corpus_worker.py').read_bytes())
    program = 'PAYLOAD_LITERAL=' + repr(json.dumps(payload)) + '\n' + REMOTE_EXTRACT
    with tempfile.TemporaryDirectory(prefix='corpus-extract-identity-') as folder:
        path = Path(folder)
        identity, trust = path / 'identity', path / 'known_hosts'
        vault.write_new(identity, key.encode(), 0o600)
        vault.write_new(trust, known.encode(), 0o600)
        check = probe.check_identity(identity, subprocess.run)
        if check['status'] != 'parseable':
            return {'status': 'identity_preflight_failed', 'identity_check': check}
        try:
            result = subprocess.run(probe.ssh_command(host, user, identity, trust), input=program.encode(),
                                    capture_output=True, timeout=REMOTE_TIMEOUT_SECONDS, check=False)
            record = {'status': 'remote_result_received' if result.returncode == 0 else 'remote_failed',
                      'returncode': result.returncode,
                      'stdout_truncated': len(result.stdout) > REPORT_LIMIT,
                      'stderr_base64': base64.b64encode(result.stderr[:65536]).decode()}
            if result.returncode == 0 and len(result.stdout) <= REPORT_LIMIT:
                record['extraction'] = json.loads(result.stdout)
            else:
                record['stdout_base64'] = base64.b64encode(result.stdout[:REPORT_LIMIT]).decode()
                if record['stdout_truncated']:
                    record['status'] = 'remote_result_too_large'
            return record
        except subprocess.TimeoutExpired as exc:
            return {'status': 'remote_outcome_unknown_requires_readback',
                    'stderr_base64': base64.b64encode((exc.stderr or b'')[:65536]).decode()}


def deliver_direct_json(raw, status, output, recipient, recipient_id):
    """Seal the same private JSON report without requiring archive extraction."""
    vault.load_json_bytes(raw, vault.MAX_PLAINTEXT)
    envelope = vault.seal(raw, recipient, recipient_id)
    output = Path(output)
    output.mkdir(mode=0o700)
    vault.write_new(output / 'part-0000.vault', envelope)
    # Write the completion marker last. A partial directory must not publish.
    vault.write_new(output / 'status.json', vault.canonical(dict(status, sealed_parts=1)), 0o644)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='sealed-delivery')
    parser.add_argument('--direct-json-output', help='Additional sealed JSON delivery directory')
    args = parser.parse_args(argv)
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print('CORPUS_EXTRACT_CLOUD_ONLY; PLAINTEXT_PUBLISHED=false')
        return 2
    try:
        rid = os.environ.get('QUALITY_FIRST_RECIPIENT_ID')
        recipient = delivery.load_recipient(os.environ.get('QUALITY_FIRST_RECIPIENT_JSON_B64'), rid)
        output = Path(args.output)
        output_ready = False
        try:
            if args.direct_json_output and Path(args.direct_json_output).resolve() == output.resolve():
                raise ValueError('DELIVERY_PATHS_MUST_DIFFER')
            if args.direct_json_output and (Path(args.direct_json_output).exists()
                                            or Path(args.direct_json_output).is_symlink()):
                raise ValueError('DIRECT_OUTPUT_EXISTS')
            output.mkdir(mode=0o700)
            output_ready = True
            record = extract()
        except Exception as exc:
            record = {'status': 'configuration_rejected', 'error_class': type(exc).__name__}
        successful = record['status'] == 'remote_result_received' and record.get('extraction', {}).get('status') in (
            'batch_observed', 'readback_observed', 'deferred_capacity', 'deferred_capacity_after_dependency',
            'deferred_active_extraction_controller', 'deferred_active_worker',
            'deferred_no_complete_uploaded_sources')
        status = {'version': 1, 'status': 'complete' if successful else 'failed',
                  'stage': 'sealed_delivery', 'strategy_evaluated': False,
                  'live_orders_enabled': False, 'plaintext_published': False}
        status.update(public_evidence_codes(record))
        if not successful:
            status['failed_stage'] = 'acquisition'
            record_status = record.get('status')
            if record_status == 'remote_result_received':
                # The SSH transport succeeded, but the sealed remote report
                # did not reach an accepted terminal state.
                status['failure_code'] = 'remote_report_not_successful'
            else:
                status['failure_code'] = FAILURE_CODES.get(record_status, 'acquisition_unclassified')
        raw = vault.canonical(record)
        delivery_failed = False
        try:
            if not output_ready:
                raise ValueError('OUTPUT_UNAVAILABLE')
            with tempfile.TemporaryDirectory() as folder:
                private = Path(folder)
                vault.write_new(private / 'results' / 'extraction.json', raw)
                status['sealed_parts'] = delivery.deliver_bundle(delivery.make_bundle(private), output, recipient, rid)
            vault.write_new(output / 'status.json', vault.canonical(status), 0o644)
        except Exception:
            delivery_failed = True
        if args.direct_json_output:
            direct_status = dict(status)
            if delivery_failed and successful:
                direct_status.update(status='failed', failed_stage='acquisition',
                                     failure_code='acquisition_unclassified')
            try:
                deliver_direct_json(raw, direct_status, args.direct_json_output, recipient, rid)
            except Exception:
                delivery_failed = True
        if delivery_failed:
            print('CORPUS_EXTRACT_DELIVERY_BLOCKED; PLAINTEXT_PUBLISHED=false')
            return 2
        print(json.dumps(status))
        return 0 if successful else 2
    except Exception:
        print('CORPUS_EXTRACT_DELIVERY_BLOCKED; PLAINTEXT_PUBLISHED=false')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
