#!/usr/bin/env python3
"""Fail-closed, no-corpus connectivity probe for DeepSeek and MinerU.

This probe never loads the QuantML corpus, never prints credentials, and never
submits a MinerU extraction task. It only verifies that the two private
credentials are present and that the documented endpoints answer.
"""
from __future__ import annotations
import json, os, sys, urllib.error, urllib.request

def request(url: str, method: str, headers: dict[str, str], body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read(512).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read(512).decode('utf-8', 'replace')

def main() -> int:
    deep = os.environ.get('DEEPSEEK_API_KEY', '')
    miner = os.environ.get('MINERU_API_TOKEN', '')
    result: dict[str, object] = {'format': 'quantml-semantic-backend-contract-probe-v1', 'corpus_loaded': False, 'credentials_printed': False}
    if not deep or not miner:
        result['status'] = 'not_ready_missing_private_secret'
        result['deepseek_secret_present'] = bool(deep)
        result['mineru_secret_present'] = bool(miner)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 2

    payload = json.dumps({'model': 'deepseek-flash', 'messages': [{'role': 'system', 'content': 'Return JSON only.'}, {'role': 'user', 'content': 'Return {"probe":"ok"}.'}], 'thinking': {'type': 'disabled'}, 'max_tokens': 16, 'response_format': {'type': 'json_object'}}).encode()
    ds_code, _ = request('https://api.deepseek.com/chat/completions', 'POST', {'Authorization': f'Bearer {deep}', 'Content-Type': 'application/json'}, payload)
    # A nonexistent task is a read-only auth/endpoint check; no MinerU task is created.
    mu_code, _ = request('https://mineru.net/api/v4/extract/task/quantml-contract-probe-nonexistent', 'GET', {'Authorization': f'Bearer {miner}', 'Accept': 'application/json'})
    result.update({'status': 'probe_complete', 'deepseek_http_status': ds_code, 'mineru_http_status': mu_code, 'deepseek_authenticated': ds_code == 200, 'mineru_endpoint_reachable': mu_code in {400, 404, 405}, 'mineru_authenticated_or_authorized': mu_code not in {401, 403}, 'semantic_credit': 0, 'strategy_evaluations': 0, 'actual_fills': 0, 'verified_canary_fills': 0, 'live_orders_enabled': False})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if ds_code == 200 and mu_code not in {401, 403} else 1

if __name__ == '__main__':
    sys.exit(main())
