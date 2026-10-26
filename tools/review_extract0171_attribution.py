#!/usr/bin/env python3
"""Review targeted extract-0171 source/unit attribution without retaining text."""
from __future__ import annotations

import collections
import datetime as dt
import getpass
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path('/Users/ytfeng/Code_Pj/poly_trade/research-repair-20261015')
BASE = Path('/Users/ytfeng/Code_Pj/poly_trade/output/continuous-research-20261016')
STUDY = BASE / 'quantml-full-study'
PART = STUDY / 'artifact-extract-0171-v1/corpus-direct-readback-40303497318-1/part-0000.vault'
OUT = STUDY / 'extract-0171-attribution-review-20261026.json'
DELIVERY = STUDY / 'extract-0171-attribution-review-delivery'
RECEIPT = STUDY / 'extract-0171-attribution-review-publication.json'
IDENTITY = Path('/Users/ytfeng/.codex/private/poly-quality-first-20261015')
RECIPIENT = ROOT / 'research_vault' / 'recipient.json'
RECIPIENT_ID = '5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9'
RUN_ID = '40303497318'
CELL_ID = 'quantml-extract-0171-attribution-review'

sys.path.insert(0, str(ROOT / 'tools'))
import research_vault as vault  # noqa: E402


def write_json(path: Path, value: dict) -> None:
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    if RECEIPT.exists():
        existing = json.loads(RECEIPT.read_text(encoding='utf-8'))
        if existing.get('publication', {}).get('status') == 'verified_remote':
            print(json.dumps({'status': 'already_verified', 'cell_id': CELL_ID}, ensure_ascii=False))
            return
    if OUT.exists() or DELIVERY.exists():
        raise SystemExit('attribution_review_already_exists')
    if not PART.is_file() or PART.stat().st_size > vault.MAX_ENVELOPE:
        raise SystemExit('missing_or_oversized_ciphertext')
    pin = getpass.getpass('Local recovery PIN (not echoed): ')
    try:
        private = vault.unlock_identity((IDENTITY / 'identity.keybundle').read_bytes(),
                                        (IDENTITY / 'recovery.key').read_bytes(), pin)
    finally:
        del pin
    try:
        if vault.recipient_for(private)['recipient_id'] != RECIPIENT_ID:
            raise SystemExit('wrong_identity')
        document = vault.load_json_bytes(vault.unseal(PART.read_bytes(), private), 16 * 1024 * 1024)
    finally:
        del private
    extraction = document.get('extraction') or {}
    units = extraction.get('unit_texts') or []
    rows = []
    for item in units:
        unit = item.get('unit') or {}
        metadata = unit.get('metadata') or {}
        rows.append({
            'unit_key': item.get('unit_key'),
            'evidence_kind': item.get('evidence_kind'),
            'semantic_read_credited': bool(item.get('semantic_read_credited')),
            'unit_evidence_complete': bool(item.get('unit_evidence_complete')),
            'source_id': unit.get('source_id'),
            'unit_index': unit.get('unit_index'),
            'unit_kind': unit.get('unit_kind'),
            'unit_status': unit.get('status'),
            'page': metadata.get('page'),
            'native_text_preserved': bool(metadata.get('native_text_preserved')),
            'ocr_status': metadata.get('ocr_status'),
            'ocr_quality_verified': bool(metadata.get('ocr_quality_verified')),
            'visual_review_required': bool(metadata.get('visual_review_required')),
            'full_resolution_visual_pending': bool(metadata.get('full_resolution_visual_pending')),
        })
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    evidence_counts = collections.Counter(str(r.get('evidence_kind')) for r in rows)
    unit_kind_counts = collections.Counter(str(r.get('unit_kind')) for r in rows)
    source_counts = collections.Counter(str(r.get('source_id')) for r in rows)
    summary = {
        'format': 'quantml-extract-0171-attribution-review-v1',
        'created_utc': now,
        'workflow_run_id': int(RUN_ID),
        'review_scope': 'targeted transport attribution only; no native/OCR text retained and no semantic credit',
        'unit_texts_returned': len(rows),
        'unique_source_ids_returned': len(source_counts),
        'evidence_counts': dict(sorted(evidence_counts.items())),
        'unit_kind_counts': dict(sorted(unit_kind_counts.items())),
        'source_id_multiplicity_histogram': dict(sorted(collections.Counter(source_counts.values()).items())),
        'all_semantic_read_credited': all(not r['semantic_read_credited'] for r in rows),
        'source_count_including_discovered_children': (extraction.get('coverage') or {}).get('source_count_including_discovered_children'),
        'new_units_written_this_run': (extraction.get('coverage') or {}).get('units_written_this_run'),
        'new_bytes_written_this_run': (extraction.get('coverage') or {}).get('bytes_written_this_run'),
        'metadata_cursor_committed': False,
        'full_corpus_complete': False,
        'strategy_evaluations': 0,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
        'plaintext_published': False,
        'rows': rows,
        'next_action': 'Use attribution only to choose a future distinct normal cursor tranche; do not infer semantic learning or strategy value from transport evidence.',
    }
    write_json(OUT, summary)
    recipient = vault.load_json_bytes(vault.read_bounded(RECIPIENT, 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    DELIVERY.mkdir(mode=0o700)
    vault.write_new(DELIVERY / 'part-0000.vault', vault.seal(vault.canonical({
        'format': 'quantml-extract-0171-attribution-review-sealed-v1',
        'summary': summary,
        'plaintext_published': False,
        'actual_fills': 0,
        'verified_canary_fills': 0,
        'live_orders_enabled': False,
    }), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(DELIVERY / 'status.json', vault.canonical({
        'version': 1, 'status': 'complete', 'stage': 'sealed_delivery',
        'strategy_evaluated': False, 'live_orders_enabled': False,
        'plaintext_published': False, 'sealed_parts': 1,
    }) + b'\n', 0o644)
    proc = subprocess.run([
        sys.executable, str(ROOT / 'tools/publish_quality_first_journal.py'),
        '--source', str(DELIVERY), '--repository', 'yt-feng/poly_trade',
        '--run-id', RUN_ID, '--run-attempt', '1', '--recipient-id', RECIPIENT_ID,
        '--cell-id', CELL_ID,
    ], cwd=ROOT, capture_output=True, text=True, timeout=180, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.stdout.strip() or 'attribution_publication_failed')
    publication = json.loads(proc.stdout)
    write_json(RECEIPT, {
        'format': 'quantml-extract-0171-attribution-review-publication-v1',
        'cell_id': CELL_ID,
        'source': str(OUT),
        'source_sha256': hashlib.sha256(OUT.read_bytes()).hexdigest(),
        'recipient_id': RECIPIENT_ID,
        'plaintext_published': False,
        'publication': publication,
    })
    print(json.dumps({'status': publication.get('status'), 'unit_texts_returned': len(rows), 'unique_sources': len(source_counts), 'semantic_credit': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
