"""Restore registered candidate/auditor inputs and two narrow saved-data grants.

Grant contents and the scoped restore view remain in the ephemeral bootstrap.
No model, CSV parser, data collection, journal identity, or order API is used.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import os
from pathlib import Path
import re
import traceback

import prepare_exit_retry_inputs as common
import research_vault as vault
import scoped_research_dataset as saved
from restore_quality_capacity_inputs import INPUT_FILES
from verify_saved_research_input import EPISODE_PATH

TRAINING_KEYS = frozenset({'cache_bindings', 'expected_all_cohort', 'expected_test_cohort',
    'expected_test_cohort_per_run', 'private_inputs', 'environment', 'price_model_parameters',
    'model_artifact_contract', 'model_contract'})


def scoped_restore_view(config: dict, repository: str, expected_commit: str) -> dict:
    binding = config.get('training_binding')
    if (config.get('evaluation_mode') != 'offline_net_margin' or config.get('live_orders_enabled') is not False
            or config.get('expected_code_commit') != expected_commit
            or not re.fullmatch(r'[0-9a-f]{40}', expected_commit or '')
            or not isinstance(binding, dict) or set(binding) != TRAINING_KEYS
            or hashlib.sha256(vault.canonical(binding)).hexdigest() != config.get('training_binding_sha256')
            or not isinstance(repository, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', repository)
            or repository.rsplit('/', 1)[1] in {'.', '..'}):
        raise ValueError('invalid scoped training/configuration binding')
    private = binding['private_inputs']
    cache = binding['cache_bindings']
    if not isinstance(private, dict) or set(private) != set(INPUT_FILES) or not isinstance(cache, dict):
        raise ValueError('two saved input bindings required')
    for kind, files in INPUT_FILES.items():
        spec = private[kind]
        if not isinstance(spec, dict) or set(spec) != {'descriptor_sha256', 'source'}:
            raise ValueError('invalid saved input specification')
        source = spec['source']
        if (not isinstance(spec['descriptor_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', spec['descriptor_sha256'])
                or not isinstance(source, dict) or set(source) != {'repository', 'commit', 'path'}
                or source['repository'] != repository or not isinstance(source['commit'], str)
                or not re.fullmatch(r'[0-9a-f]{40}', source['commit'])
                or not isinstance(source['path'], str) or not EPISODE_PATH.fullmatch(source['path'])
                or any(not isinstance(cache.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', cache[key]) for key in files.values())):
            raise ValueError('invalid immutable scoped input source or CSV binding')
    return {'expected_code_commit': expected_commit, 'cache_bindings': cache, 'private_inputs': private}


def decode_capability(encoded: str, descriptor_sha: str, kind: str, view: dict) -> bytes:
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 45000:
        raise ValueError('scoped capability encoded limit')
    raw = base64.b64decode(encoded, validate=True)
    capability = vault.load_json_bytes(raw, 16384)
    descriptor = saved.validate_capability(capability, descriptor_sha)
    members = {row['path']: row for row in descriptor['files']}
    files = INPUT_FILES[kind]
    if set(members) != set(files) | {'provenance.json', 'dataset-manifest.json'}:
        raise ValueError('unexpected scoped input members')
    if any(members[name]['sha256'] != view['cache_bindings'][key] for name, key in files.items()):
        raise ValueError('scoped CSV binding mismatch')
    return raw


def materialize(config_encoded: str, candidates_encoded: str, audit_encoded: str,
                dataset_capability_encoded: str, control_capability_encoded: str,
                request_sha: str, expected_commit: str, private: Path, bootstrap: Path,
                repository: str, root: Path = common.ROOT) -> dict:
    raw = common.decode_xz(config_encoded, 262144)
    if not isinstance(request_sha, str) or not re.fullmatch(r'[0-9a-f]{64}', request_sha) or hashlib.sha256(raw).hexdigest() != request_sha:
        raise ValueError('configuration digest mismatch')
    config = vault.load_json_bytes(raw, 262144)
    view = scoped_restore_view(config, repository, expected_commit)
    # Verify both narrow grants before any restored input can be fetched or
    # written. A bad control grant must not partially consume the dataset grant.
    caps = {kind: decode_capability(encoded, view['private_inputs'][kind]['descriptor_sha256'], kind, view)
            for kind, encoded in (('dataset', dataset_capability_encoded), ('control', control_capability_encoded))}
    result = common.materialize(config_encoded, candidates_encoded, audit_encoded, request_sha,
        expected_commit, private, bootstrap, root, evaluation_mode='offline_net_margin')
    for kind, cap in caps.items():
        vault.write_new(bootstrap / (kind + '-capability.json'), cap, 0o600)
    vault.write_new(bootstrap / 'scoped-restore-config.json', vault.canonical(view), 0o600)
    sensitive = [result['source_commit'], result['source_commit'][:7], result['reader_sha256']]
    for spec in view['private_inputs'].values():
        source = spec['source']
        sensitive.extend((source['commit'], source['commit'][:7], source['path'], source['path'].split('/')[2], spec['descriptor_sha256']))
    sensitive.extend(value for value in view['cache_bindings'].values()
                     if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value))
    return {**result, 'mask_values': sorted(set(sensitive))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private', required=True, type=Path)
    parser.add_argument('--bootstrap', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = materialize(os.environ['AUDIT_CONFIG_B64'], os.environ['CANDIDATE_INPUT_B64'],
            os.environ['PRIVATE_AUDIT_B64'], os.environ['DATA_CAPABILITY_B64'], os.environ['CONTROL_CAPABILITY_B64'],
            os.environ['EXPECTED_SHA256'], os.environ['GITHUB_SHA'], args.private, args.bootstrap,
            os.environ['GITHUB_REPOSITORY'])
        # Mask restored immutable source pins and paths before later steps can
        # print their environment or checkout reference. Never export grants.
        for value in result['mask_values']:
            print('::add-mask::' + value, flush=True)
        with open(os.environ['GITHUB_ENV'], 'a') as handle:
            handle.write('AUDIT_SOURCE_COMMIT=' + result['source_commit'] + '\n')
            handle.write('PRIVATE_AUDIT_SHA=' + result['reader_sha256'] + '\n')
        return 0
    except Exception as exc:
        args.private.mkdir(mode=0o700, parents=True, exist_ok=True)
        failure = args.private / 'setup-failure.json'
        if not failure.exists():
            vault.write_new(failure, vault.canonical({'format': 'net-margin-private-setup-failure-v1',
                'type': type(exc).__name__, 'error': str(exc), 'traceback': traceback.format_exc(),
                'actual_model_fit_calls': 0, 'actual_model_predict_calls': 0, 'replayed_cells': 0,
                'live_orders_enabled': False}), 0o600)
        raise SystemExit('Private net margin inputs missing or invalid') from None


if __name__ == '__main__':
    raise SystemExit(main())
