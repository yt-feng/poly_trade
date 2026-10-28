#!/usr/bin/env python3
"""Publish a small sealed episode without recursive-tree enumeration.

The normal publisher fails closed when the journal's recursive tree response is
truncated. This helper verifies only the exact episode paths, then creates a
new nested tree from the current root tree, preserving the branch history.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path

from persist_quality_first_journal import episode_identity, inspect_delivery, persist_delivery, read_regular

BRANCH = 'sealed-research-journal'
REPOSITORY = 'yt-feng/poly_trade'


class ApiFailure(RuntimeError):
    pass


def api(method: str, path: str, payload: dict | None = None) -> dict:
    command = ['gh', 'api', '--method', method, path]
    if payload is not None:
        command += ['--input', '-']
    proc = subprocess.run(command, input=json.dumps(payload) if payload is not None else None, capture_output=True, text=True, timeout=90, check=False)
    if proc.returncode:
        text = proc.stderr.strip()
        if '404' in text:
            raise FileNotFoundError(path)
        raise ApiFailure(text[-500:])
    try:
        value = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ApiFailure('invalid_json_response') from exc
    if not isinstance(value, dict):
        raise ApiFailure('non_object_response')
    return value


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--run-attempt', required=True)
    parser.add_argument('--recipient-id', required=True)
    parser.add_argument('--cell-id', required=True)
    parser.add_argument('--receipt', required=True)
    args = parser.parse_args()
    source = Path(args.source)
    inspect_delivery(source, args.recipient_id)
    episode = episode_identity(REPOSITORY, args.run_id, args.run_attempt, cell_id=args.cell_id)
    prefix = f'research_vault/episodes/{episode}/'
    paths = [prefix + 'part-0000.vault', prefix + 'status.json']

    # Exact-path readback prevents duplicate writes even when the recursive tree
    # endpoint is too large. A missing path is the only admission for mutation.
    existing = {}
    for path in paths:
        try:
            existing[path] = api('GET', f'repos/{REPOSITORY}/contents/{path}?ref={BRANCH}')
        except FileNotFoundError:
            continue
    if existing:
        if len(existing) != len(paths):
            raise ApiFailure('partial_episode_exists')
        # Existing paths are accepted only if their blob content matches.
        for path, item in existing.items():
            raw = base64.b64decode(''.join((item.get('content') or '').split()))
            local = read_regular(source / Path(path).name, 4 * 1024 * 1024)
            if raw != local:
                raise ApiFailure('existing_episode_conflict')
        receipt = {'format': 'targeted-sealed-journal-publication-v1', 'status': 'verified_remote', 'remote_branch': BRANCH, 'remote_episode_id': episode, 'remote_commit': None, 'plaintext_published': False, 'sealed_parts': 1, 'validation_scope': 'exact_episode_paths_and_recipient'}
        Path(args.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return

    ref = api('GET', f'repos/{REPOSITORY}/git/ref/heads/{BRANCH}')
    parent = ref['object']['sha']
    commit = api('GET', f'repos/{REPOSITORY}/git/commits/{parent}')
    root_tree = commit['tree']['sha']
    part = read_regular(source / 'part-0000.vault', 4 * 1024 * 1024)
    status = read_regular(source / 'status.json', 4096)
    files = {'part-0000.vault': part, 'status.json': status}
    entries = []
    for name, data in files.items():
        saved = api('POST', f'repos/{REPOSITORY}/git/blobs', {'encoding': 'base64', 'content': base64.b64encode(data).decode()})
        if saved.get('sha') != blob_sha(data):
            raise ApiFailure('remote_blob_mismatch')
        entries.append({'path': prefix + name, 'mode': '100644', 'type': 'blob', 'sha': saved['sha']})
    tree = api('POST', f'repos/{REPOSITORY}/git/trees', {'base_tree': root_tree, 'tree': entries})
    new_commit = api('POST', f'repos/{REPOSITORY}/git/commits', {'message': 'Persist validated sealed research journal', 'tree': tree['sha'], 'parents': [parent]})
    updated = api('PATCH', f'repos/{REPOSITORY}/git/refs/heads/{BRANCH}', {'sha': new_commit['sha'], 'force': False})
    if updated.get('object', {}).get('sha') != new_commit['sha']:
        raise ApiFailure('ref_ack_mismatch')
    for path in paths:
        item = api('GET', f'repos/{REPOSITORY}/contents/{path}?ref={BRANCH}')
        raw = base64.b64decode(''.join((item.get('content') or '').split()))
        local = read_regular(source / Path(path).name, 4 * 1024 * 1024)
        if raw != local:
            raise ApiFailure('post_write_readback_mismatch')
    receipt = {'format': 'targeted-sealed-journal-publication-v1', 'status': 'verified_remote', 'remote_branch': BRANCH, 'remote_commit': new_commit['sha'], 'remote_episode_id': episode, 'plaintext_published': False, 'sealed_parts': 1, 'validation_scope': 'exact_episode_paths_and_recipient'}
    Path(args.receipt).write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
