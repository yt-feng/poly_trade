"""Seal the H600 causal supersession summary for the private journal."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
SOURCE = BASE / 'quantml-full-study/h600-causal-supersession-20261026.json'
OUT = BASE / 'quantml-full-study/h600-causal-supersession-delivery'
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def main() -> None:
    summary = json.loads(SOURCE.read_text(encoding='utf-8'))
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    payload = {
        'format': 'h600-causal-supersession-sealed-v1',
        'summary': summary,
        'plaintext_published': False,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }
    OUT.mkdir(mode=0o700, exist_ok=True)
    vault.write_new(OUT / 'part-0000.vault', vault.seal(vault.canonical(payload), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(OUT / 'status.json', vault.canonical({
        'version': 1,
        'status': 'complete',
        'stage': 'sealed_delivery',
        'strategy_evaluated': True,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'sealed_parts': 1,
    }) + b'\n', 0o644)
    print(json.dumps({'status': 'prepared', 'output': str(OUT)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
