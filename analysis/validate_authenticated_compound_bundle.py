"""Offline CLI for a captured authenticated compound-receipt bundle.

The bundle is an input artifact, not a connector.  This command never opens a
socket, reads a wallet, signs, submits, cancels, sells, redeems, or fetches a
balance.  It prints only a compact validation summary so raw account payloads
are not copied into terminal logs.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from authenticated_compound_reconciler import reconcile_authenticated_chain


SCHEMA = "authenticated-compound-bundle-v1"


def validate_bundle_document(document: Any, *, required_fills: int = 10) -> dict[str, Any]:
    """Validate one JSON bundle and return a sanitized compound result."""
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return {"gate_passed": False, "failure_reason": "invalid_bundle_schema"}
    # The v1 shape has no ledger identity, sequence/checkpoint links or numeric
    # cash reconciliation.  Keeping it executable would allow the old helper
    # to replay concatenated/reset rows.  Admission is v2-only now.
    return {"gate_passed": False, "failure_reason": "legacy_schema_not_admissible_use_v2"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--required-fills", type=int, default=10)
    args = parser.parse_args()
    try:
        document = json.loads(args.bundle.read_text(encoding="utf-8"))
        result = validate_bundle_document(document, required_fills=args.required_fills)
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"gate_passed": False, "failure_reason": "bundle_read_error"}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("gate_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
