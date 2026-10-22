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
ERROR_CODES = {400, 401, 403, 404, 405, 408, 409, 410, 415, 422, 429, 500, 501, 502, 503, 504}
ERROR_CATEGORIES = {'unknown', 'authentication', 'permission', 'validation', 'conflict',
                    'not_found', 'rate_limit', 'ref_non_fast_forward', 'ref_already_exists',
                    'timeout', 'server_error', 'invalid_response', 'unverified_write'}
ERROR_CLASSES = {'ApiError', 'ValueError', 'TimeoutExpired', 'JSONDecodeError', 'OSError',
                 'FileNotFoundError', 'PermissionError', 'KeyError', 'TypeError'}


class ApiError(Exception):
    def __init__(self, not_found=False, status_code=None, category='unknown'):
        self.not_found = not_found
        self.status_code = status_code
        self.category = category


def github_api(method, path, payload=None):
    command = ['gh', 'api', '--method', method, path]
    if payload is not None:
        command += ['--input', '-']
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, timeout=90, check=False)
    if result.returncode:
        match = re.search(r'HTTP (\d{3})', result.stderr)
        status_code = int(match.group(1)) if match else None
        try:
            body = json.loads(result.stdout)
        except json.JSONDecodeError:
            body = {}
        body = body if isinstance(body, dict) else {}
        message = body.get('message')
        text = result.stderr.lower()
        # Status alone is not evidence of a ref race: 422 also covers invalid
        # requests and spam rejection. Require the exact message AND operation.
        # https://docs.github.com/en/rest/git/refs#update-a-reference
        ref_root = 'repos/' + REPOSITORY + '/git/refs'
        race = None
        if status_code == 422 and not body.get('errors') and isinstance(payload, dict):
            if (method == 'PATCH' and path == ref_root + '/heads/' + BRANCH
                    and payload.get('force') is False and message == 'Update is not a fast forward'):
                race = 'ref_non_fast_forward'
            elif (method == 'POST' and path == ref_root and payload.get('ref') == 'refs/heads/' + BRANCH
                    and message == 'Reference already exists'):
                race = 'ref_already_exists'
        if status_code == 401:
            category = 'authentication'
        elif status_code == 429 or 'rate limit' in text or 'too many requests' in text:
            category = 'rate_limit'
        elif 'forbidden' in text or 'permission' in text or status_code == 403:
            category = 'permission'
        elif status_code == 404:
            category = 'not_found'
        elif race:
            category = race
        elif 'validation' in text or status_code in {400, 422}:
            category = 'validation'
        elif 'conflict' in text or status_code == 409:
            category = 'conflict'
        elif 'not found' in text or status_code == 404:
            category = 'not_found'
        elif status_code == 408 or 'timeout' in text or 'timed out' in text:
            category = 'timeout'
        elif status_code in {500, 501, 502, 503, 504}:
            category = 'server_error'
        else:
            category = 'unknown'
        raise ApiError(status_code == 404, status_code=status_code, category=category)
    try:
        body = json.loads(result.stdout)
    except json.JSONDecodeError:
        # A successful HTTP write with an unreadable response may have applied.
        raise ApiError(category='invalid_response') from None
    if not isinstance(body, dict):
        raise ApiError(category='invalid_response')
    return body


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

    def read_state():
        try:
            parent = read_ref()
            tree_sha, existing = read_tree(parent)
            return parent, tree_sha, existing
        except (KeyError, TypeError, AttributeError):
            raise ApiError(category='invalid_response') from None

    episode_prefix = f'research_vault/episodes/{receipt["episode_id"]}/'

    def verified(existing):
        found = {p: sha for p, sha in existing.items() if p.startswith(episode_prefix)}
        if found and found != desired:
            raise ValueError('EXISTING_EPISODE_CONFLICT')
        return found == desired

    parent, tree_sha, existing = read_state()
    if verified(existing):
        return dict(receipt, status='verified_remote', branch=BRANCH)
    uploaded = False
    # Only a proven ref race with a newly observed parent can repeat writes.
    # Object creation errors remain outside this retry boundary.
    for attempt in range(6):
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
        race_category = 'ref_non_fast_forward' if parent else 'ref_already_exists'
        failure = ApiError(category='unverified_write')
        try:
            if parent:
                api('PATCH', ref_path, {'sha': commit['sha'], 'force': False})
            else:
                api('POST', prefix + '/git/refs', {'ref': 'refs/heads/' + BRANCH, 'sha': commit['sha']})
        except subprocess.TimeoutExpired:
            failure = ApiError(category='timeout')
        except ApiError as error:
            if not (error.status_code == 422 and error.category == race_category
                    or error.category in {'timeout', 'server_error', 'invalid_response'}):
                raise
            failure = error
        try:
            new_parent, new_tree, actual = read_state()
        except (ApiError, subprocess.TimeoutExpired, ValueError):
            # A failed read cannot establish that an uncertain write did not
            # execute. Preserve its cause and never replay it blindly.
            raise failure from None
        if verified(actual):
            return dict(receipt, status='verified_remote', branch=BRANCH)
        if (failure.status_code != 422 or failure.category != race_category
                or new_parent is None or new_parent == parent or attempt == 5):
            raise failure
        parent, tree_sha = new_parent, new_tree
        time.sleep(min(0.25 * (2 ** attempt), 2.0))


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
        # Only allowlisted telemetry is public, even for unexpected exceptions.
        error_class = type(error).__name__
        error_code = getattr(error, 'status_code', None)
        category = getattr(error, 'category', 'unknown')
        print(json.dumps({
            'status': 'publication_failed',
            'plaintext_published': False,
            'error_class': error_class if error_class in ERROR_CLASSES else 'Exception',
            'error_code': error_code if type(error_code) is int and error_code in ERROR_CODES else None,
            'error_category': category if isinstance(category, str) and category in ERROR_CATEGORIES else 'unknown',
        }, sort_keys=True))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
