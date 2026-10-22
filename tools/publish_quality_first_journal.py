"""Publish validated ciphertext to a dedicated GitHub branch, never main.

GitHub authentication stays with gh. API responses and errors are captured;
public stdout contains only generic publication status. No decryption occurs.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

from persist_quality_first_journal import persist_delivery

BRANCH = 'sealed-research-journal'
REPOSITORY = 'yt-feng/poly_trade'


class ApiError(Exception):
    def __init__(self, not_found=False, status_code=None):
        self.not_found = not_found
        self.status_code = status_code


def github_api(method, path, payload=None):
    command = ['gh', 'api', '--method', method, path]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=90, check=False)
    if result.returncode:
        match = re.search(r'HTTP (\d{3})', result.stderr)
        status_code = int(match.group(1)) if match else None
        raise ApiError(status_code == 404, status_code=status_code)
    return json.loads(result.stdout)


def blob_id(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def publish(source, repository, run_id, run_attempt, recipient_id, api=github_api, *, cell_id=None):
    if repository != REPOSITORY:
        raise ValueError('UNEXPECTED_REPOSITORY')
    with tempfile.TemporaryDirectory(prefix='sealed-journal-validated-') as temporary:
        receipt = persist_delivery(source, temporary, repository, run_id, run_attempt, recipient_id, cell_id=cell_id)
        episode = Path(temporary) / receipt['episode_id']
        files = {f'research_vault/episodes/{receipt["episode_id"]}/{p.name}': p.read_bytes()
                 for p in episode.iterdir()}
    desired = {path: {'sha': blob_id(data), 'type': 'blob', 'mode': '100644'} for path, data in files.items()}
    prefix = 'repos/' + repository
    ref_path = prefix + '/git/refs/heads/' + BRANCH

    def read_ref():
        try:
            return api('GET', prefix + '/git/ref/heads/' + BRANCH)['object']['sha']
        except ApiError as error:
            if error.not_found:
                return None
            raise

    def read_tree(sha):
        if sha is None:
            return None, {}
        tree_sha = api('GET', prefix + '/git/commits/' + sha)['tree']['sha']
        tree = api('GET', prefix + '/git/trees/' + tree_sha + '?recursive=1')
        if tree.get('truncated'):
            raise ValueError('JOURNAL_TREE_TRUNCATED')
        # Preserve all entry types/modes: an unexpected nested tree, gitlink, or
        # symlink must not be mistaken for the expected flat regular-file log.
        return tree_sha, {entry['path']: {key: entry[key] for key in ('sha', 'type', 'mode')}
                          for entry in tree['tree']}

    uploaded = False
    # Parallel matrix cells can all read the same branch tip and race on the
    # compare-and-swap ref update.  Re-read and rebase with bounded backoff so
    # a transient publication race is not reported as a research failure.
    for attempt in range(6):
        parent = read_ref()
        tree_sha, existing = read_tree(parent)
        episode_prefix = f'research_vault/episodes/{receipt["episode_id"]}/'
        found = {p: sha for p, sha in existing.items() if p.startswith(episode_prefix)}
        if found:
            if found != desired:
                raise ValueError('EXISTING_EPISODE_CONFLICT')
            return dict(receipt, status='verified_remote', branch=BRANCH)
        if not uploaded:
            for path, data in files.items():
                saved = api('POST', prefix + '/git/blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
                if saved['sha'] != desired[path]['sha']:
                    raise ValueError('REMOTE_BLOB_MISMATCH')
            uploaded = True
        body = {'tree': [dict(metadata, path=path) for path, metadata in sorted(desired.items())]}
        if tree_sha:
            body['base_tree'] = tree_sha
        tree = api('POST', prefix + '/git/trees', body)
        commit = api('POST', prefix + '/git/commits', {
            'message': 'Persist validated sealed research journal', 'tree': tree['sha'],
            'parents': [parent] if parent else [],
        })
        try:
            if parent:
                api('PATCH', ref_path, {'sha': commit['sha'], 'force': False})
            else:
                api('POST', prefix + '/git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
        except (ApiError, subprocess.TimeoutExpired):
            # An uncertain write is resolved by readback before any retry.
            time.sleep(min(0.25 * (2 ** attempt), 2.0))
            continue
        # Read the actual branch tree on the next iteration, even after success.
    _, actual = read_tree(read_ref())
    if {p: sha for p, sha in actual.items() if p.startswith(episode_prefix)} != desired:
        raise ValueError('REMOTE_PUBLICATION_NOT_VERIFIED')
    return dict(receipt, status='verified_remote', branch=BRANCH)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--repository', default=REPOSITORY)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--recipient-id', default=os.environ.get('RESEARCH_RECIPIENT_ID'))
    parser.add_argument('--cell-id', help='Optional stable matrix-cell label; omission keeps legacy episode names.')
    args = parser.parse_args()
    try:
        result = publish(args.source, args.repository, args.run_id, args.run_attempt, args.recipient_id, cell_id=args.cell_id)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as error:
        # The class name is safe operational telemetry; exception messages may
        # contain paths, API payloads, or other sensitive publication details.
        print(json.dumps({
            'status': 'publication_failed',
            'plaintext_published': False,
            'error_class': type(error).__name__,
            'error_code': getattr(error, 'status_code', None),
        }, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
