"""Public-feed acquisition dry-run report.

This module records one bounded, read-only attempt to collect BTC 5-minute
public data.  It deliberately does not retry a failed network, synthesize v3
rows, or turn quote markouts into resolution labels.  The report is an audit
artifact and remains a blocker until a later, separately supplied package
passes the canonical observation and label contracts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REPORT_SCHEMA_VERSION = 1
REPORT_ID = "public-acquisition-dry-run-20261005"
ADAPTER_COMMIT = "89f7b149cced7506dae94f2e2b0902933d273fc8"
ARCHIVE_FILES = (
    {
        "file": "snapshots-2026-10-05-000001.jsonl.gz",
        "kind": "snapshots",
        "rows": 8,
        "bytes": 2220,
        "sha256": "174310d0cf976712ea23462188b4f0ad1dabd9e085b5cdeb926fbefbe2c3cf97",
    },
    {
        "file": "labels-2026-10-05-000002.jsonl.gz",
        "kind": "labels",
        "rows": 8,
        "bytes": 311,
        "sha256": "ade496aad3e15941bc693d81b869ec0e55b9658c61e42cce1376d752019a6f39",
    },
    {
        "file": "raw-2026-10-05-000003.jsonl.gz",
        "kind": "raw",
        "rows": 36,
        "bytes": 1819,
        "sha256": "e2147df37258666ba37d6e7b96dc06553c6a10c024af9d071566e54717a43424",
    },
)

PUBLIC_HOSTS = (
    "gamma-api.polymarket.com",
    "ws-subscriptions-clob.polymarket.com",
    "ws-live-data.polymarket.com",
    "fapi.binance.com",
    "fstream.binance.com",
    "data-api.binance.vision",
)

BLOCKED_REASONS = (
    "public_feed_network_unreachable",
    "no_canonical_v3_observations",
    "missing_market_condition_token_identity",
    "missing_source_event_and_receive_canonical_pair",
    "missing_fee_tick_min_order_metadata",
    "no_independent_resolution_labels",
    "labels_are_quote_markout_not_resolution",
    "public_replay_does_not_qualify_canary",
)


def minimum_user_package() -> dict[str, Any]:
    """Return the smallest public-safe package request for the next attempt."""
    return {
        "observations": {
            "path": "/private/observations-v3.jsonl",
            "format": "JSONL, one canonical record per line",
            "schema": "research/strategy/schema/v3_observation.schema.json",
            "required_fields": [
                "schema_version",
                "observation_id",
                "source_event_time_ms",
                "received_time_ms",
                "market_id",
                "condition_id",
                "token_ids.up",
                "token_ids.down",
                "observation_type",
                "books.up.bids",
                "books.up.asks",
                "books.down.bids",
                "books.down.asks",
                "fees.rate",
                "fees.exponent",
                "fees.asset",
                "rules.tick_size",
                "rules.min_order_size",
                "provenance.source",
                "provenance.capture_id",
                "provenance.source_sha256",
                "provenance.retrieved_at_ms",
            ],
            "preserve": [
                "immutable raw source bytes for the capture",
                "sha256 for each raw source object and the JSONL file",
                "exact UTC capture window and adapter/code commit",
            ],
        },
        "resolution_labels": {
            "path": "/private/resolution-labels.jsonl",
            "format": "JSONL, one independent label per market_id",
            "schema": "research/strategy/schema/walk_forward_label.schema.json",
            "required_fields": [
                "label_version",
                "market_id",
                "condition_id",
                "outcome",
                "resolved_time_ms",
                "label_available_time_ms",
                "source",
                "source_sha256",
            ],
            "independence": "official resolution source kept separate from quote/book capture",
            "forbidden_substitutes": [
                "quote_markout_not_fill_pnl",
                "public price replay",
                "midpoint or final_price inferred outcome",
            ],
        },
        "provenance": {
            "required": [
                "source bytes and sha256",
                "capture start/end UTC timestamps",
                "market/condition/token IDs",
                "contemporaneous fee, tick, and minimum-order metadata",
                "adapter repository and immutable commit",
            ],
            "secrets": "Do not include credentials, private keys, or ARCHIVE_KEY.",
        },
        "acceptance_gates": {
            "canonical_observation_records_minimum": 300,
            "independent_windows_minimum": 300,
            "independent_utc_dates_minimum": 7,
            "independent_resolution_labels": True,
            "source_and_receive_times_required": True,
            "private_fill_canary_gate_still_required": True,
        },
        "next_commands": [
            "python analysis/walk_forward_intake.py --observations /private/observations-v3.jsonl --labels /private/resolution-labels.jsonl --output /tmp/intake.json",
            "python analysis/walk_forward_split_audit.py ...",
            "python analysis/preregistered_strategy.py ...",
            "python analysis/walk_forward_execution.py ...",
        ],
    }


def build_report() -> dict[str, Any]:
    """Build the checked-in result of the single bounded dry-run."""
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_id": REPORT_ID,
        "status": "blocked",
        "canary_blocked": True,
        "canary_allowed": False,
        "evidence_qualifies": False,
        "attempt": {
            "adapter": "poly/capture_v3.py",
            "adapter_commit": ADAPTER_COMMIT,
            "asset_scope": ["btc"],
            "duration_seconds": 8,
            "captured_utc_date": "2026-10-05",
            "mode": "public_read_only",
            "credentials_used": False,
            "orders_enabled": False,
            "retry_count": 0,
            "command": "python capture_v3.py --assets btc --seconds 8 --output <temporary-output> --require-core --require-microstructure",
            "temporary_output_retained": False,
        },
        "network": {
            "hosts_attempted": list(PUBLIC_HOSTS),
            "dns_or_transport_unavailable": True,
            "error_class": "Temporary failure in name resolution",
            "error_count": 12,
            "http_timing_count": 10,
            "connection_count": 14,
            "fallback_or_bypass_used": False,
        },
        "archives": {
            "manifest_schema_version": 2,
            "manifest_sha256": "48ceecb58cc2e9c8383302c2d6f64aca0fb6b81630b20511a2b0f60a0f7c4b4d",
            "files": list(ARCHIVE_FILES),
            "bytes_committed": False,
            "hashes_committed": True,
            "meaning": "Hashes identify the temporary failed-attempt artifacts; they do not prove feed data was received.",
        },
        "observations": {
            "legacy_snapshot_rows": 8,
            "accepted_canonical_v3_records": 0,
            "market_condition_token_identity_records": 0,
            "source_event_receive_pairs": 0,
            "full_book_depth_records": 0,
            "fee_tick_min_order_metadata_records": 0,
            "poly_valid_records": 0,
            "event_completeness_certified": False,
        },
        "labels": {
            "candidate_rows": 8,
            "independent_resolution_labels": 0,
            "kind": "quote_markout_not_fill_pnl",
            "independent": False,
            "official_resolution_source_present": False,
        },
        "metrics": {
            "oos": None,
            "brier": None,
            "ece": None,
            "gross_pnl_usdc": None,
            "fees_usdc": None,
            "net_pnl_usdc": None,
            "net_return": None,
            "real_fills": 0,
        },
        "blocked_reasons": list(BLOCKED_REASONS),
        "minimum_user_package": minimum_user_package(),
        "public_replay_boundary": "Public quotes and quote markouts are research inputs only; they cannot satisfy the private order/fill/cancel/fee/settlement/account canary gate.",
    }


def write_report(output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_report(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    write_report(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
