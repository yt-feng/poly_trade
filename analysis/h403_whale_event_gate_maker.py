"""Add one causal large-print event gate to the H402 maker protocol.

The gate is deliberately orthogonal to H401's signed-flow imbalance: it only
uses the largest pre-entry print as a share of pre-entry volume.  A window is
accepted when that share is at most the frozen 0.30 cap.  The H402 inventory
balanced quote policy and H387 continuous cash ledger remain unchanged.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

try:
    from .h402_inventory_balanced_maker import _load_windows, build_strategy
except ImportError:  # direct ``python analysis/...`` execution on Actions
    from h402_inventory_balanced_maker import _load_windows, build_strategy


HYPOTHESIS_ID = "H403-whale-event-gate-inventory-balanced-maker"
WHALE_WINDOW_SECONDS = 60.0
WHALE_DOMINANCE_CAP = Decimal("0.30")


def _size(row: Any) -> float:
    raw = getattr(row, "size", None)
    if raw is None:
        raw = getattr(row, "shares", None)
    if raw is None:
        raise AttributeError("print_size_missing")
    return float(raw)


def whale_dominance(prints: Iterable[Any], start: int, *, horizon_seconds: float = WHALE_WINDOW_SECONDS) -> float | None:
    lower = float(start)
    upper = lower + float(horizon_seconds)
    sizes = []
    for row in prints:
        ts = float(getattr(row, "ts"))
        if ts > 100_000_000_000:
            ts /= 1000.0
        if lower <= ts < upper:
            value = _size(row)
            if value > 0:
                sizes.append(value)
    if not sizes:
        return None
    return max(sizes) / sum(sizes)


def filter_windows(windows: Iterable[Any], cap: Decimal = WHALE_DOMINANCE_CAP) -> tuple[list[Any], list[dict[str, Any]]]:
    cap = Decimal(str(cap))
    if not Decimal("0") < cap <= Decimal("1"):
        raise ValueError("invalid_whale_dominance_cap")
    kept = []
    diagnostics = []
    for window in sorted(windows, key=lambda item: int(item.start)):
        dominance = whale_dominance(window.prints, int(window.start))
        accepted = dominance is not None and dominance <= float(cap)
        diagnostics.append({
            "start": int(window.start),
            "whale_dominance": None if dominance is None else round(float(dominance), 8),
            "whale_dominance_cap": str(cap),
            "accepted": accepted,
        })
        if accepted:
            kept.append(window)
    return kept, diagnostics


def build_backtest_payload(*, source_root: Path, fills_dir: Path, labels_dir: Path,
                           queue_values: list[Decimal], cap: Decimal,
                           source_commit: str, dataset_revision: str) -> dict[str, Any]:
    backtest, windows = _load_windows(source_root=source_root, fills_dir=fills_dir, labels_dir=labels_dir)
    filtered, diagnostics = filter_windows(windows, cap)
    config_module = __import__("spork30.config", fromlist=["StrategyConfig"])
    strategy = build_strategy(config_module)
    summaries = backtest.summarize(filtered, queue_values, maker_fee_bps=Decimal("0"), seed=30,
                                   variants=[strategy], realised_vol=False)
    per_window = {}
    summary_rows = []
    for summary in summaries:
        key = f"{summary.label},queue={summary.queue}"
        per_window[key] = [
            {"start": result.start, "winner": result.winner, "pnl": str(result.pnl),
             "fills": result.fills, "sold_up": str(result.sold_up),
             "sold_down": str(result.sold_down), "places": result.places}
            for result in summary.results
        ]
        summary_rows.append(summary.as_dict())
    return {
        "format": "h403-whale-event-gate-maker-input-v1",
        "hypothesis_id": HYPOTHESIS_ID,
        "source": {"dataset_revision": dataset_revision, "source_commit": source_commit,
                    "windows": len(windows), "prints": sum(len(w.prints) for w in windows),
                    "labels": "Gamma resolved outcomePrices; paper label only"},
        "protocol": {"initial_cash_usd": "10", "split_size_usd": "5", "queues": [str(x) for x in queue_values],
                     "max_lean_shares": "1", "improve_spread_ticks": 1,
                     "whale_window_seconds": WHALE_WINDOW_SECONDS, "whale_dominance_cap": str(cap),
                     "same_evolving_cash_ledger": True, "no_reset": True, "no_concatenation": True,
                     "paper_only": True},
        "windows": [w.start for w in windows], "accepted_windows": [w.start for w in filtered],
        "summaries": summary_rows, "per_window": per_window, "whale_diagnostics": diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--queue", default="0,5,10")
    parser.add_argument("--whale-dominance-cap", default="0.30")
    args = parser.parse_args()
    payload = build_backtest_payload(
        source_root=args.source_root, fills_dir=args.fills_dir, labels_dir=args.labels_dir,
        queue_values=[Decimal(x.strip()) for x in args.queue.split(",") if x.strip()],
        cap=Decimal(args.whale_dominance_cap), source_commit=args.source_commit,
        dataset_revision=args.dataset_revision,
    )
    args.output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": payload["format"], "accepted_windows": len(payload["accepted_windows"]),
                      "summaries": len(payload["summaries"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
