"""Combine completed capture releases and report release-level stability."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from current_capture_quote_markout import evaluate, load_rows


def run(root: Path, *, tte_center: float = 50.0, tte_width: float = 5.0) -> dict:
    rows, labels = load_rows(root, tte_center=tte_center, tte_width=tte_width)
    combined = evaluate(rows, labels)
    by_release = {}
    for release in sorted(p for p in root.iterdir() if p.is_dir()):
        release_rows, release_labels = load_rows(release, tte_center=tte_center, tte_width=tte_width)
        by_release[release.name] = evaluate(release_rows, release_labels)
    return {
        "schema_version": 1,
        "audit": "existing_completed_capture_multi_release_markout",
        "decision_window_seconds_before_end": [tte_center - tte_width, tte_center + tte_width],
        "release_count": len(by_release),
        "combined": combined,
        "per_release": by_release,
        "gate": {
            "minimum_selection_trades": 10,
            "minimum_test_trades": 10,
            "selection_quote_markout_nonnegative": True,
            "test_quote_markout_nonnegative": True,
            "authenticated_execution_receipts_required": True,
        },
        "interpretation": {
            "quote_markout_is_fill_pnl": False,
            "official_outcomes_used": False,
            "live_orders": False,
            "new_capture_collected": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--tte-center", type=float, default=50.0)
    parser.add_argument("--tte-width", type=float, default=5.0)
    args = parser.parse_args()
    result = run(args.input, tte_center=args.tte_center, tte_width=args.tte_width)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    combined = result["combined"]
    print(json.dumps({
        "release_count": result["release_count"],
        "combined_markets": combined["market_count"],
        "combined_test_markets": combined["test_markets"],
        "combined_metrics": combined["metrics"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
