"""Replay one inventory-balanced, paired-maker protocol on the fixed H391 tape.

H402 is a new protocol derived from the external maker literature review: keep
the two binary legs nearly balanced and improve both asks by one tick only when
the observed spread permits it.  It reuses the exact H391 public tape and
labels, with no new market data, and still feeds the one-ledger H387 reconciler.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from h391_hf_maker_replay import load_label, load_prints


HYPOTHESIS_ID = "H402-inventory-balanced-paired-maker"
SOURCE_COMMIT = "accd1511a08748302c8116ab65575930e1240068"
DATASET_REVISION = "f3bfb79f19611ea3115ad29c18720166ee6dd0b5"


def _load_windows(*, source_root: Path, fills_dir: Path, labels_dir: Path) -> tuple[Any, list[Any]]:
    sys.path.insert(0, str(source_root))
    backtest = importlib.import_module("spork30.backtest")
    windows = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        label = load_label(labels_dir / f"{start}.json", start)
        prints = load_prints(path, label, backtest.Print)
        if prints:
            windows.append(backtest.WindowData(
                start=start,
                slug=label["slug"],
                condition="",
                winner=label["winner"],
                prints=prints,
            ))
    windows.sort(key=lambda row: row.start)
    if not windows:
        raise ValueError("no_usable_windows")
    return backtest, windows


def build_strategy(config_module: Any) -> Any:
    """Freeze the single inventory-balanced variant; no parameter sweep."""
    return config_module.StrategyConfig(
        split_size=Decimal("5"),
        max_lean_shares=Decimal("1"),
        fair_edge=Decimal("0"),
        maker_rebate_bps=Decimal("0"),
        ask_offset_ticks=0,
        requote_threshold_ticks=0,
        requote_min_seconds=0,
        improve_spread_ticks=1,
    )


def build_backtest_payload(
    *, source_root: Path, fills_dir: Path, labels_dir: Path,
    queue_values: list[Decimal], source_commit: str, dataset_revision: str,
) -> dict[str, Any]:
    backtest, windows = _load_windows(
        source_root=source_root, fills_dir=fills_dir, labels_dir=labels_dir
    )
    config_module = importlib.import_module("spork30.config")
    strategy = build_strategy(config_module)
    summaries = backtest.summarize(
        windows,
        queue_values,
        maker_fee_bps=Decimal("0"),
        seed=30,
        variants=[strategy],
        realised_vol=False,
    )
    per_window: dict[str, list[dict[str, Any]]] = {}
    summary_rows: list[dict[str, Any]] = []
    for summary in summaries:
        key = f"{summary.label},queue={summary.queue}"
        per_window[key] = [
            {
                "start": result.start,
                "winner": result.winner,
                "pnl": str(result.pnl),
                "fills": result.fills,
                "sold_up": str(result.sold_up),
                "sold_down": str(result.sold_down),
                "places": result.places,
            }
            for result in summary.results
        ]
        summary_rows.append(summary.as_dict())
    return {
        "format": "h402-inventory-balanced-paired-maker-input-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "source": {
            "dataset_revision": dataset_revision,
            "source_commit": source_commit,
            "windows": len(windows),
            "prints": sum(len(window.prints) for window in windows),
            "labels": "Gamma resolved outcomePrices; paper label only",
        },
        "protocol": {
            "initial_cash_usd": "10",
            "split_size_usd": "5",
            "queues": [str(value) for value in queue_values],
            "max_lean_shares": "1",
            "improve_spread_ticks": 1,
            "maker_fee_bps": "0",
            "maker_rebate_bps": "0",
            "same_evolving_cash_ledger": True,
            "no_reset": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "windows": [window.start for window in windows],
        "summaries": summary_rows,
        "per_window": per_window,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--dataset-revision", default=DATASET_REVISION)
    parser.add_argument("--queue", default="0,5,10")
    args = parser.parse_args()
    queues = [Decimal(item.strip()) for item in args.queue.split(",") if item.strip()]
    payload = build_backtest_payload(
        source_root=args.source_root,
        fills_dir=args.fills_dir,
        labels_dir=args.labels_dir,
        queue_values=queues,
        source_commit=args.source_commit,
        dataset_revision=args.dataset_revision,
    )
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"format": payload["format"], "windows": len(payload["windows"]), "summaries": len(payload["summaries"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
