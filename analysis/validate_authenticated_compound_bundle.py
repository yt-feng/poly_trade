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
    owner = document.get("owner")
    events = document.get("events")
    if not isinstance(owner, str) or not owner.strip():
        return {"gate_passed": False, "failure_reason": "bundle_owner_required"}
    if not isinstance(events, list):
        return {"gate_passed": False, "failure_reason": "bundle_events_required"}
    result = reconcile_authenticated_chain(
        events,
        owner=owner,
        required_fills=required_fills,
    )
    # Deliberately omit the event ledger and all account identifiers from the
    # CLI result.  The sealed source artifact remains the evidence record.
    return {
        "schema": SCHEMA,
        "required_fills": int(required_fills),
        "event_count": len(events),
        "fills": int(result.get("fills", 0)),
        "consecutive": bool(result.get("consecutive", False)),
        "paper_replay_passed": bool(result.get("paper_replay_passed", False)),
        "gate_passed": bool(result.get("gate_passed", False)),
        "failure_reason": result.get("failure_reason"),
        "ending_cash": result.get("ending_cash"),
        "net_pnl": result.get("net_pnl"),
        "owner_scoped": bool(result.get("owner_scoped", False)),
        "identity_complete": bool(result.get("identity_complete", False)),
    }


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
