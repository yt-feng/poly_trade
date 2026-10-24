"""Bounded read-only capacity observation on an already configured cloud host.

Only GitHub Actions may open SSH. Host, identity, observation and diagnostics
remain private; the delivery directory contains ciphertext plus generic status.
No corpus is uploaded, parsed or executed and no remote settings are changed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import research_vault
import run_quality_first_research as delivery

SSH_TIMEOUT_SECONDS = 45
OUTPUT_LIMIT = 256 * 1024
IDENTITY_CHECK_SECONDS = 5

# Fixed source, sent over stdin. Its only filesystem access is bounded reads,
# directory existence/statvfs and module/command discovery, never a data scan.
REMOTE_PROBE = r'''
import datetime, importlib.util, json, os, shutil, signal

def deadline(signum, frame):
    raise TimeoutError('REMOTE_PROBE_DEADLINE')
signal.signal(signal.SIGALRM, deadline)
signal.alarm(35)
report = {'version': 1, 'scope': 'read_only_capacity_no_data_scan',
          'observed_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'cpu_count': os.cpu_count(), 'memory': {}, 'paths': {},
          'commands': {}, 'modules': {}, 'aws': {'status': 'not_checked'}}
try:
    with open('/proc/meminfo') as f:
        for line in f.read(16384).splitlines():
            name, _, value = line.partition(':')
            if name in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
                report['memory'][name] = value.strip()
except Exception as exc:
    report['memory_error'] = type(exc).__name__
for path in ('/home/ubuntu', '/var/lib/poly-bot-btc5m', '/var/lib/poly-bot-btc5m/research'):
    item = {'exists': os.path.exists(path), 'directory': os.path.isdir(path)}
    if item['directory']:
        try:
            st = os.statvfs(path)
            item.update(total_bytes=st.f_blocks * st.f_frsize,
                        available_bytes=st.f_bavail * st.f_frsize,
                        writable=os.access(path, os.W_OK))
        except Exception as exc:
            item['error'] = type(exc).__name__
    report['paths'][path] = item
for name in ('python3', 'aws', 'ffmpeg', '7z', 'tesseract', 'rsync', 'sftp-server'):
    report['commands'][name] = shutil.which(name)
for name in ('boto3', 'pypdf', 'fitz', 'pandas', 'pyarrow', 'numpy', 'cryptography'):
    try:
        report['modules'][name] = importlib.util.find_spec(name) is not None
    except Exception:
        report['modules'][name] = False
if report['modules']['boto3']:
    try:
        import boto3
        from botocore.config import Config
        config = Config(connect_timeout=3, read_timeout=3, retries={'max_attempts': 0})
        session = boto3.Session()
        credentials = session.get_credentials()
        report['aws']['credential_provider_method'] = getattr(credentials, 'method', None)
        if credentials is None:
            report['aws']['status'] = 'no_credentials_in_remote_default_chain'
        else:
            ident = session.client('sts', config=config).get_caller_identity()
            report['aws'].update(status='identity_verified', account=ident.get('Account'), arn=ident.get('Arn'))
            try:
                response = session.client('s3', config=config).list_buckets(MaxBuckets=100)
                buckets = response.get('Buckets', [])
                report['aws']['buckets'] = [b.get('Name') for b in buckets[:100]]
                report['aws']['bucket_list_truncated'] = bool(response.get('ContinuationToken')) or len(buckets) > 100
                report['aws']['bucket_permissions_verified'] = False
            except Exception as exc:
                report['aws']['bucket_list_error_class'] = type(exc).__name__
                report['aws']['bucket_list_error'] = str(exc)[:500]
    except Exception as exc:
        report['aws'].update(status='unverified', error_class=type(exc).__name__, error=str(exc)[:500])
else:
    report['aws']['status'] = 'sdk_unavailable_credentials_unverified'
signal.alarm(0)
print(json.dumps(report, sort_keys=True))
'''


def normalize_private_key(value):
    """Repair only transport formatting, never decode or replace key material."""
    if not isinstance(value, str) or len(value) > 32768:
        raise ValueError('SSH_IDENTITY_REQUIRED')
    value = value.strip().replace('\r\n', '\n')
    # A one-line escaped secret is distinct from a mixed-format key. Leave
    # mixed literal/newline content unchanged for the parser to reject.
    if '\n' not in value and '\\n' in value:
        value = value.replace('\\n', '\n')
    return value.rstrip('\n') + '\n' if value else value


def configuration(environment):
    host = environment.get('CORPUS_SSH_HOST', '')
    user = environment.get('CORPUS_SSH_USER', '')
    trusted = environment.get('CORPUS_SSH_KNOWN_HOSTS', '')
    private_key = normalize_private_key(environment.get('AWS_ADMIN_SSH_KEY', ''))
    if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', host):
        raise ValueError('INVALID_HOST')
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', user):
        raise ValueError('INVALID_USER')
    lines = [line.strip() for line in trusted.splitlines() if line.strip()]
    if not 1 <= len(lines) <= 4:
        raise ValueError('PINNED_HOST_KEY_REQUIRED')
    for line in lines:
        fields = line.split()
        if (len(fields) != 3 or fields[0] != host
                or fields[1] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256')):
            raise ValueError('EXACT_PINNED_HOST_KEY_REQUIRED')
        try:
            key = base64.b64decode(fields[2], validate=True)
        except Exception:
            raise ValueError('INVALID_PINNED_HOST_KEY') from None
        if not 32 <= len(key) <= 4096:
            raise ValueError('INVALID_PINNED_HOST_KEY')
    if (not 64 <= len(private_key) <= 32768
            or not private_key.lstrip().startswith('-----BEGIN ')
            or 'PRIVATE KEY-----' not in private_key):
        raise ValueError('SSH_IDENTITY_REQUIRED')
    return host, user, '\n'.join(lines) + '\n', private_key


def ssh_command(host, user, identity, known_hosts):
    return ['ssh', '-F', '/dev/null', '-i', str(identity),
            '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
            '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1',
            '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(known_hosts),
            '-o', 'GlobalKnownHostsFile=/dev/null',
            '-o', 'UpdateHostKeys=no', '-o', 'ForwardAgent=no',
            '-o', 'ClearAllForwardings=yes', '-o', 'RequestTTY=no',
            user + '@' + host, 'python3 -']


def check_identity(identity, execute):
    """Cloud-only caller; derived public key is discarded, diagnostic sealed."""
    try:
        result = execute(['ssh-keygen', '-y', '-P', '', '-f', str(identity)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         timeout=IDENTITY_CHECK_SECONDS, check=False)
        error = result.stderr or b''
        return {'status': 'parseable' if result.returncode == 0 else 'unparseable',
                'returncode': result.returncode,
                'stderr_base64': base64.b64encode(error[:4096]).decode(),
                'stderr_truncated': len(error) > 4096}
    except subprocess.TimeoutExpired as exc:
        error = exc.stderr or b''
        return {'status': 'check_timeout', 'returncode': None,
                'stderr_base64': base64.b64encode(error[:4096]).decode(),
                'stderr_truncated': len(error) > 4096}
    except Exception as exc:
        return {'status': 'check_unavailable', 'returncode': None,
                'error_class': type(exc).__name__}


def observe(environment=None, execute=None):
    environment = os.environ if environment is None else environment
    if environment.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('GITHUB_ACTIONS_REQUIRED')
    host, user, trusted, private_key = configuration(environment)
    execute = execute or subprocess.run
    result = {'version': 1, 'scope': 'read_only_cloud_capacity',
              'created_utc': datetime.now(timezone.utc).isoformat(),
              'source_commit': environment.get('GITHUB_SHA'),
              'run_id': environment.get('GITHUB_RUN_ID'),
              'host': host, 'user': user, 'strategy_evaluated': False,
              'corpus_uploaded': False, 'host_key_policy': 'strict_preconfigured_pin',
              'key_format_normalized': private_key != environment.get('AWS_ADMIN_SSH_KEY', ''),
              'budgets': {'ssh_seconds': SSH_TIMEOUT_SECONDS,
                          'identity_check_seconds': IDENTITY_CHECK_SECONDS,
                          'remote_seconds': 35, 'output_bytes_per_stream': OUTPUT_LIMIT}}
    with tempfile.TemporaryDirectory(prefix='corpus-probe-identity-') as folder:
        private = Path(folder)
        identity, known = private / 'identity', private / 'known_hosts'
        research_vault.write_new(identity, private_key.encode(), 0o600)
        research_vault.write_new(known, trusted.encode(), 0o600)
        result['identity_check'] = check_identity(identity, execute)
        if result['identity_check']['status'] != 'parseable':
            result.update(status='identity_preflight_failed', returncode=None)
            return result
        try:
            completed = execute(ssh_command(host, user, identity, known),
                                input=REMOTE_PROBE.encode(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=SSH_TIMEOUT_SECONDS,
                                check=False)
            stdout, stderr = completed.stdout or b'', completed.stderr or b''
            result.update(status='observed' if completed.returncode == 0 else 'transport_failed',
                          returncode=completed.returncode)
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = exc.stdout or b'', exc.stderr or b''
            result.update(status='transport_timeout', returncode=None)
        except Exception as exc:
            stdout, stderr = b'', str(exc).encode()
            result.update(status='client_failed', error_class=type(exc).__name__, returncode=None)
        result.update(stdout_base64=base64.b64encode(stdout[:OUTPUT_LIMIT]).decode(),
                      stderr_base64=base64.b64encode(stderr[:OUTPUT_LIMIT]).decode(),
                      stdout_truncated=len(stdout) > OUTPUT_LIMIT,
                      stderr_truncated=len(stderr) > OUTPUT_LIMIT)
        if result['status'] == 'observed':
            try:
                report = json.loads(stdout)
                if report.get('scope') != 'read_only_capacity_no_data_scan':
                    raise ValueError('REMOTE_SCOPE_MISMATCH')
                result['capacity'] = report
            except Exception:
                result['status'] = 'invalid_remote_result'
    return result


def run(args):
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise ValueError('GITHUB_ACTIONS_REQUIRED')
    recipient_id = os.environ.get('QUALITY_FIRST_RECIPIENT_ID')
    recipient = delivery.load_recipient(os.environ.get('QUALITY_FIRST_RECIPIENT_JSON_B64'), recipient_id)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValueError('DELIVERY_DIRECTORY_ALREADY_EXISTS')
    output.mkdir(parents=True, mode=0o700)
    try:
        record = observe()
    except Exception as exc:
        record = {'version': 1, 'status': 'configuration_rejected',
                  'error_class': type(exc).__name__, 'error': str(exc)[:500],
                  'strategy_evaluated': False, 'corpus_uploaded': False}
    status = {'version': 1, 'status': 'complete' if record['status'] == 'observed' else 'failed',
              'stage': 'sealed_delivery', 'strategy_evaluated': False,
              'live_orders_enabled': False, 'plaintext_published': False}
    if status['status'] == 'failed':
        status['failed_stage'] = 'acquisition'
    with tempfile.TemporaryDirectory(prefix='corpus-probe-private-') as folder:
        private = Path(folder)
        research_vault.write_new(private / 'results' / 'capacity.json', research_vault.canonical(record))
        status['sealed_parts'] = delivery.deliver_bundle(delivery.make_bundle(private), output, recipient, recipient_id)
    research_vault.write_new(output / 'status.json', research_vault.canonical(status), 0o644)
    print(json.dumps(status, sort_keys=True))
    return 0 if status['status'] == 'complete' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='sealed-delivery')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        return run(args)
    except Exception:
        print('CORPUS_CAPACITY_PROBE_BLOCKED; PLAINTEXT_PUBLISHED=false')
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
