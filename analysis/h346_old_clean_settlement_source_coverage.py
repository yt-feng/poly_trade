"""H346: audit settlement-source fields in the pinned old-clean archive.

This is a read-only provenance audit.  It does not fit a model, score a
strategy, infer a fill, or use a resolved field as a contemporaneous feature.
The archive is a quote CSV projection, so the audit records both field
presence and the clocks that are actually observable in that projection.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path


SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
WINDOW_MS = 300_000
REQUIRED_FIELDS = {
    "ts_iso", "slug", "target_price", "final_price", "trade_count_1s",
    "trade_volume_1s", "buy_up_cents", "buy_down_cents", "ask_depth_up_5",
    "ask_depth_down_5",
}
PROVENANCE_FIELD_GROUPS = {
    "ptb_receipt_clock": {"price_to_beat_received_ms", "target_price_received_ms"},
    "twap": {"twap", "twap60", "twap_price", "window_seconds", "window_s"},
    "provider": {"provider", "source", "resolution_source"},
    "sequence": {"sequence", "seq", "connection_id", "reconnect", "dropped"},
}


def complete_run_names(source_root: Path) -> tuple[list[str], dict[str, int]]:
    counts = Counter(path.parent.name for path in source_root.glob("*/*quotes.csv"))
    if not counts:
        raise ValueError("NO_QUOTE_FILES")
    maximum = max(counts.values())
    return sorted(name for name, count in counts.items() if count == maximum), dict(counts)


def _timestamp_ms(value: str) -> int:
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp_must_be_timezone_aware")
    return int(stamp.timestamp() * 1000)


def _market_start_ms(slug: str) -> int:
    try:
        return int(slug.rsplit("-", 1)[1]) * 1000
    except (IndexError, ValueError) as exc:
        raise ValueError("invalid_btc5m_slug") from exc


def classify_headers(headers: set[str]) -> dict[str, object]:
    present = {name: sorted(headers & values) for name, values in PROVENANCE_FIELD_GROUPS.items()}
    return {
        "required_fields_missing": sorted(REQUIRED_FIELDS - headers),
        "provenance_fields_present": present,
        "twap_fields_present": bool(present["twap"]),
        "provider_fields_present": bool(present["provider"]),
        "sequence_fields_present": bool(present["sequence"]),
        "ptb_receipt_clock_present": bool(present["ptb_receipt_clock"]),
    }


def summarize_rows(rows: list[dict[str, str]]) -> dict[str, object]:
    """Summarize a small synthetic row set; production uses ``audit_archive``."""
    target_first: dict[str, int] = {}
    reference_pre_end_rows = 0
    reference_pre_end_markets: set[str] = set()
    for row in rows:
        slug = row["slug"]
        start = _market_start_ms(slug)
        observed = _timestamp_ms(row["ts_iso"])
        if row.get("target_price", ""):
            target_first[slug] = min(target_first.get(slug, observed), observed)
        if row.get("final_price", "") and observed < start + WINDOW_MS:
            reference_pre_end_rows += 1
            reference_pre_end_markets.add(slug)
    return {
        "markets": len({row["slug"] for row in rows}),
        "target_markets": len(target_first),
        "target_first_seen_before_start_markets": sum(
            value < _market_start_ms(slug) for slug, value in target_first.items()
        ),
        "reference_pre_end_rows": reference_pre_end_rows,
        "reference_pre_end_markets": len(reference_pre_end_markets),
    }


def audit_archive(source_root: Path, source_commit: str, output: Path) -> dict[str, object]:
    source_root = source_root.resolve()
    repo = source_root.parent.parent
    observed_commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed_commit != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed_commit}:{source_commit}")

    complete, file_counts = complete_run_names(source_root)
    headers: set[str] = set()
    header_shapes: Counter[tuple[str, ...]] = Counter()
    field_rows: Counter[str] = Counter()
    field_nonnull: Counter[str] = Counter()
    market_count: set[str] = set()
    target_first: dict[str, int] = {}
    target_rows = 0
    final_rows = 0
    reference_pre_end_rows = 0
    reference_pre_end_markets: set[str] = set()
    final_after_end_rows = 0
    total_rows = 0

    for run_name in complete:
        for path in sorted((source_root / run_name).glob("*quotes.csv")):
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                shape = tuple(reader.fieldnames or ())
                header_shapes[shape] += 1
                headers.update(shape)
                for row in reader:
                    total_rows += 1
                    for field in shape:
                        field_rows[field] += 1
                        if row.get(field) not in (None, ""):
                            field_nonnull[field] += 1
                    slug = row.get("slug", "")
                    if not slug:
                        continue
                    market_count.add(slug)
                    start = _market_start_ms(slug)
                    observed = _timestamp_ms(row["ts_iso"])
                    target = row.get("target_price", "")
                    if target:
                        target_rows += 1
                        target_first[slug] = min(target_first.get(slug, observed), observed)
                    final = row.get("final_price", "")
                    if final:
                        final_rows += 1
                        if observed < start + WINDOW_MS:
                            reference_pre_end_rows += 1
                            reference_pre_end_markets.add(slug)
                        else:
                            final_after_end_rows += 1

    header_report = classify_headers(headers)
    target_pre_start = sum(
        value < _market_start_ms(slug) for slug, value in target_first.items()
    )
    target_at_or_after_start = sum(
        value >= _market_start_ms(slug) for slug, value in target_first.items()
    )
    report = {
        "format": "old-clean-h347-corrected-settlement-source-coverage-v1",
        "hypothesis_id": "H346-old-clean-settlement-source-fields-are-causally-usable",
        "source_root": str(source_root),
        "source_commit": source_commit,
        "observed_source_commit": observed_commit,
        "raw_run_directories": len(file_counts),
        "complete_runs": len(complete),
        "complete_run_file_count": max(file_counts.values()),
        "quote_files_scanned": sum(file_counts[name] for name in complete),
        "header_shapes": len(header_shapes),
        "total_rows": total_rows,
        "markets": len(market_count),
        "header": header_report,
        "coverage": {
            "target_price_rows": target_rows,
            "target_price_markets": len(target_first),
            "target_first_seen_before_window_start_markets": target_pre_start,
            "target_first_seen_at_or_after_window_start_markets": target_at_or_after_start,
            "final_price_rows": final_rows,
            "reference_price_pre_end_rows": reference_pre_end_rows,
            "reference_price_pre_end_markets": len(reference_pre_end_markets),
            "final_price_after_end_rows": final_after_end_rows,
            "causal_ptb_receipt_clock_rows": 0,
            "causal_twap_rows": 0,
            "causal_provider_rows": 0,
            "causal_sequence_rows": 0,
        },
        "field_nonnull_fraction": {
            field: field_nonnull[field] / field_rows[field]
            for field in sorted(field_rows)
        },
        "field_semantics": {
            "target_price": "window_start_reference_price; archive has no separate receipt clock",
            "final_price": "current_reference_price_sampled_at_snapshot; not a future settlement label",
        },
        "decision": "old_clean_supports_price_path_not_settlement_provenance",
        "root_cause": (
            "The pinned old-clean projection contains a window-start target_price and a current "
            "final_price reference path, but no PTB receipt clock, TWAP value/window/provider, "
            "sequence, reconnect or dropped-message provenance. target_price first appears at or "
            "after the window start, so its external availability clock is unknown. final_price is "
            "observed during the window and remains valid for causal price-path replay; it cannot by "
            "itself prove the Chainlink TWAP settlement source or reusable cash."
        ),
        "evidence_boundary": "old-clean quote CSV schema and observed row timestamps only; no new capture, model fit, order or fill",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "next_action": (
            "Keep existing price/depth replay separate from any settlement-aware model. Use a separately "
            "registered source capture with causal PTB/TWAP clocks if settlement probability is tested."
        ),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "metrics.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["field", "rows", "nonnull_rows", "nonnull_fraction"])
        for field in sorted(field_rows):
            writer.writerow([field, field_rows[field], field_nonnull[field], report["field_nonnull_fraction"][field]])
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_archive(args.source_root, args.source_commit, args.output)
    print(json.dumps({key: report[key] for key in ("complete_runs", "total_rows", "markets", "decision")}, sort_keys=True))


if __name__ == "__main__":
    main()
