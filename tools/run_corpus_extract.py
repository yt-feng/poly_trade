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

REPORT_LIMIT = 2 * 1024 * 1024
REMOTE_TIMEOUT_SECONDS = 360

REMOTE_EXTRACT = r'''
import base64, datetime, fcntl, hashlib, importlib.util, json, os, pathlib, re, resource, signal, stat, subprocess, sys
payload = json.loads(PAYLOAD_LITERAL)
ROOT = pathlib.Path('/home/ubuntu/quantml-study')
REPORT_LIMIT = 2 * 1024 * 1024
MEMORY_LIMIT = 512 * 1024 * 1024
MAX_METADATA = 64
report = {'version': 1, 'scope': 'bounded_private_cloud_extraction',
          'created_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'run_id': payload['run_id'], 'source_commit': payload['source_commit'],
          'semantic_learning_complete': False, 'strategy_evaluations': 0,
          'actual_orders': 0, 'trading_services_changed': False,
          'source_publication': False, 'errors': []}

def raw_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()

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

def process():
    os.umask(0o077)
    for ancestor in list(ROOT.parents)[:-1] + [ROOT]:
        if ancestor.is_symlink():
            raise ValueError('fixed_root_symlink')
    safe_directory(ROOT)
    lockpath = ROOT / '.extract-controller.lock'
    lockfd = os.open(lockpath, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock = os.fdopen(lockfd, 'wb')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        report['status'] = 'deferred_active_extraction_controller'
        return
    # Keep the descriptor open until the complete worker/readback operation ends.
    report['capacity_before'] = {'mem_available_bytes': memory_available(), 'free_bytes': available_bytes()}
    if report['capacity_before']['mem_available_bytes'] is None or report['capacity_before']['mem_available_bytes'] < 600 * 1024**2 or report['capacity_before']['free_bytes'] < 8 * 1024**3:
        report['status'] = 'deferred_capacity'
        return
    for name in ('bin', 'input', 'extract-runs', 'deps', 'extracted'):
        safe_directory(ROOT / name, create=True)
    for name in ('objects', 'metadata'):
        safe_directory(ROOT / name)
    worker_raw = base64.b64decode(payload['worker_base64'], validate=True)
    if len(worker_raw) > 200000:
        raise ValueError('worker_source_size')
    compile(worker_raw, 'corpus_worker.py', 'exec')
    worker_hash = hashlib.sha256(worker_raw).hexdigest()
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
    if (output / 'worker.lock').exists() or (output / 'worker.lock').is_symlink():
        report['status'] = 'deferred_worker_lock_requires_reconciliation'
        return
    install_marker = dependencies / 'install-complete.json'
    child_env = dict(os.environ, QUANTML_VERIFIED_CLOUD_BACKEND='aws', PYTHONPATH=str(dependencies),
                     OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', NUMEXPR_NUM_THREADS='1')
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
    report['private_dependency_distributions'] = [p.name for p in dependencies.glob('*.dist-info')][:10]
    if memory_available() < 600 * 1024**2 or available_bytes() < 8 * 1024**3:
        report['status'] = 'deferred_capacity_after_dependency'
        return
    cursor_path = ROOT / 'metadata-extract-cursor.json'
    last = ''
    if cursor_path.exists():
        last = json.loads(read_small(cursor_path, 4096)).get('last_source_id', '')
        if last and not re.fullmatch('[0-9a-f]{64}', last):
            raise ValueError('invalid_metadata_cursor')
    names = sorted(p.name for p in (ROOT / 'metadata').iterdir() if re.fullmatch('[0-9a-f]{64}', p.name))
    order = [name for name in names if name > last] + [name for name in names if name <= last]
    selected, metadata_read, deferred = [], 0, []
    for sid in order[:MAX_METADATA]:
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
    if order[:MAX_METADATA]:
        atomic(cursor_path, raw_json({'last_source_id': order[:MAX_METADATA][-1], 'updated_run_id': payload['run_id']}))
    report.update(metadata_records_read=metadata_read, metadata_names_seen=len(names),
                  selected_manifest=selected, deferred_sources=deferred,
                  manifest_scope='rotating_uploaded_objects_tranche_not_full_corpus')
    if not selected and not (output / 'state.sqlite3').exists():
        report['status'] = 'deferred_no_complete_uploaded_sources'
        return
    preflight = {'backend': 'aws', 'backend_verified': True,
                 'input_scope_and_bytes': {'current_manifest_files': len(selected), 'current_manifest_bytes': sum(e['size_bytes'] for e in selected),
                                           'existing_private_queue_may_resume': True},
                 'estimated_cpu_memory_runtime': {'cpu_parallelism': 1, 'max_address_space_mib': 512, 'worker_wall_seconds': 120},
                 'cache_identity': worker_hash, 'concurrency': 1, 'timeout_seconds': 120,
                 'cost_or_quota_bound': 'existing_host_only; no provisioning; one worker for at most120seconds',
                 'private_output_destination': str(output)}
    preflight_path = run_dir / 'preflight.json'
    atomic(preflight_path, raw_json(preflight))
    command = [sys.executable, str(worker), '--manifest', str(manifest), '--input-root', str(ROOT / 'input'),
               '--output-root', str(output), '--preflight', str(preflight_path), '--backend', 'aws',
               '--max-files', '32', '--max-units', '200', '--max-seconds', '120',
               '--max-output-bytes', str(64 * 1024**2), '--max-memory-mib', '512',
               '--max-member-bytes', str(64 * 1024**2), '--max-page-pixels', '2000000']
    with (run_dir / 'worker-private.log').open('wb') as log:
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                    env=child_env, timeout=135, preexec_fn=child_limits, check=False)
            report.update(status='batch_observed' if result.returncode == 0 else 'worker_failed', worker_returncode=result.returncode)
        except subprocess.TimeoutExpired:
            report.update(status='worker_timeout_requires_lock_reconciliation', worker_returncode=None)
    if (output / 'coverage.json').exists():
        report['coverage'] = json.loads(read_small(output / 'coverage.json', 65536))
    report['unit_texts'] = []
    report['unit_delivery_cursor'] = None
    report['unit_delivery_complete_for_cloud_snapshot'] = False
    # Deliver whole unit files only. No truncated page is called a complete unit.
    # Larger files and units beyond this2MiB transport budget stay in private cloud.
    units_path = output / 'unit-state.jsonl'
    delivery_cursor = output / 'delivery-cursor.json'
    previous_key = ''
    if delivery_cursor.exists():
        previous_key = json.loads(read_small(delivery_cursor, 4096)).get('last_key', '')
    last_key, exhausted, pending_large_units = previous_key, True, False
    if units_path.exists():
        regular(units_path)
        with units_path.open() as units:
            for line in units:
                if len(line) > 65536:
                    raise ValueError('unit_metadata_line_limit')
                unit = json.loads(line)
                key = str(unit['source_id']) + ':' + str(unit['unit_index']).zfill(12)
                if key <= previous_key:
                    continue
                item = {'unit': unit, 'unit_key': key, 'artifact_complete': False}
                artifact = unit.get('artifact')
                if artifact:
                    relative = checked_relative(artifact)
                    path = output.joinpath(*relative.parts)
                    check = output
                    for part in relative.parts:
                        check = check / part
                        if check.is_symlink():
                            raise ValueError('unit_artifact_symlink')
                    info = regular(path)
                    if info.st_size <= 262144:
                        raw = read_small(path, 262144)
                        actual_hash = hashlib.sha256(raw).hexdigest()
                        if actual_hash != unit.get('content_hash'):
                            raise ValueError('unit_artifact_hash_mismatch')
                        item.update(artifact_utf8=raw.decode('utf-8'), artifact_complete=True,
                                    artifact_sha256=actual_hash)
                    else:
                        item['delivery_state'] = 'pending_larger_private_unit_transfer'
                        item['artifact_bytes'] = info.st_size
                        pending_large_units = True
                report['unit_texts'].append(item)
                if len(raw_json(report)) > REPORT_LIMIT - 131072:
                    report['unit_texts'].pop()
                    exhausted = False
                    break
                last_key = key
    report.update(unit_delivery_cursor=last_key, unit_delivery_complete_for_cloud_snapshot=exhausted and not pending_large_units,
                  unit_delivery_semantics='whole_extracted_units_only; semantic_read_not_credited; visual_assets_remain_private_cloud')
    # This cursor is only a proposal. It is not committed before downstream
    # ciphertext delivery/readback, avoiding lost units after an SSH interruption.
    report['unit_delivery_cursor_commit_required_after_verified_readback'] = True
    report['worker_log_tail_base64'] = base64.b64encode(read_small(run_dir / 'worker-private.log', 512*1024)[-32768:]).decode()
    report['capacity_after'] = {'mem_available_bytes': memory_available(), 'free_bytes': available_bytes()}
    if len(raw_json(report)) > REPORT_LIMIT:
        raise ValueError('compact_delivery_limit')

try:
    process()
except Exception as exc:
    report.update(status='orchestration_failed', error_class=type(exc).__name__, error=str(exc)[:500])
if len(raw_json(report)) > REPORT_LIMIT:
    report.pop('unit_texts', None)
    report['delivery_omitted_requires_private_cloud_readback'] = True
print(raw_json(report).decode())
'''


def source_payload(environment, worker_source):
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
    return {'run_id': run + '-' + attempt, 'source_commit': commit,
            'worker_base64': base64.b64encode(worker_source).decode()}


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


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='sealed-delivery')
    args = parser.parse_args()
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        print('CORPUS_EXTRACT_CLOUD_ONLY; PLAINTEXT_PUBLISHED=false')
        return 2
    try:
        rid = os.environ.get('QUALITY_FIRST_RECIPIENT_ID')
        recipient = delivery.load_recipient(os.environ.get('QUALITY_FIRST_RECIPIENT_JSON_B64'), rid)
        output = Path(args.output)
        output.mkdir(mode=0o700)
        try:
            record = extract()
        except Exception as exc:
            record = {'status': 'configuration_rejected', 'error_class': type(exc).__name__}
        successful = record['status'] == 'remote_result_received' and record.get('extraction', {}).get('status') in (
            'batch_observed', 'deferred_capacity', 'deferred_capacity_after_dependency',
            'deferred_active_extraction_controller', 'deferred_worker_lock_requires_reconciliation',
            'deferred_no_complete_uploaded_sources')
        status = {'version': 1, 'status': 'complete' if successful else 'failed',
                  'stage': 'sealed_delivery', 'strategy_evaluated': False,
                  'live_orders_enabled': False, 'plaintext_published': False}
        if not successful:
            status['failed_stage'] = 'acquisition'
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder)
            vault.write_new(private / 'results' / 'extraction.json', vault.canonical(record))
            status['sealed_parts'] = delivery.deliver_bundle(delivery.make_bundle(private), output, recipient, rid)
        vault.write_new(output / 'status.json', vault.canonical(status), 0o644)
        print(json.dumps(status))
        return 0 if successful else 2
    except Exception:
        print('CORPUS_EXTRACT_DELIVERY_BLOCKED; PLAINTEXT_PUBLISHED=false')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
