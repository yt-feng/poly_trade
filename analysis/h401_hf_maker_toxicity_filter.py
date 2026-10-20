"""Apply a causal pre-entry flow-toxicity gate to the fixed H391 maker tape.

This is a separately registered challenger, not a queue or cost sweep.  The
gate only reads public prints from the first bounded interval of each window;
it never uses the resolved winner or the maker PnL.  The resulting windows
still pass through the pinned maker engine and the existing one-ledger H387
reconciler, so the output remains paper evidence only.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from h391_hf_maker_replay import load_label, load_prints


TOXICITY_WINDOW_SECONDS = 60.0
TOXICITY_CAPS = (Decimal("0.20"), Decimal("0.40"))


def _seconds(value: Any) -> float:
    result = float(value)
    # The public tape has appeared in both second and millisecond timestamps.
    return result / 1000.0 if result > 100_000_000_000 else result


def signed_flow_imbalance(prints: Iterable[Any], start: int, *, horizon_seconds: float = TOXICITY_WINDOW_SECONDS) -> float | None:
    """Return absolute signed Up/Down flow imbalance before quoting.

    BUY Up and SELL Down are positive directional flow; BUY Down and SELL Up
    are negative.  A missing pre-entry tape is unknown and therefore rejected
    by the caller rather than treated as neutral.
    """
    total = 0.0
    signed = 0.0
    lower = float(start)
    upper = lower + float(horizon_seconds)
    for row in prints:
        ts = _seconds(getattr(row, "ts"))
        if not lower <= ts < upper:
            continue
        shares = float(getattr(row, "shares"))
        if shares <= 0:
            continue
        outcome = str(getattr(row, "outcome", "")).lower()
        side = str(getattr(row, "side", "")).upper()
        if outcome not in {"up", "down"} or side not in {"BUY", "SELL"}:
            continue
        direction = 1.0 if ((outcome == "up") == (side == "BUY")) else -1.0
        total += shares
        signed += direction * shares
    if total <= 0:
        return None
    return abs(signed) / total


def filter_windows(windows: Iterable[Any], cap: Decimal) -> tuple[list[Any], list[dict[str, Any]]]:
    """Keep only windows with observed, bounded pre-entry toxicity."""
    cap = Decimal(str(cap))
    if not (Decimal("0") < cap <= Decimal("1")):
        raise ValueError("invalid_toxicity_cap")
    kept: list[Any] = []
    diagnostics: list[dict[str, Any]] = []
    for window in sorted(windows, key=lambda item: int(item.start)):
        imbalance = signed_flow_imbalance(window.prints, int(window.start))
        accepted = imbalance is not None and imbalance <= float(cap)
        diagnostics.append({
            "start": int(window.start),
            "pre_entry_flow_imbalance": None if imbalance is None else round(float(imbalance), 8),
            "toxicity_cap": str(cap),
            "accepted": accepted,
        })
        if accepted:
            kept.append(window)
    return kept, diagnostics


def _load_windows(*, source_root: Path, fills_dir: Path, labels_dir: Path) -> tuple[Any, Any, list[Any]]:
    sys.path.insert(0, str(source_root))
    backtest = importlib.import_module("spork30.backtest")
    config_module = importlib.import_module("spork30.config")
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
    return backtest, config_module, windows


def build_backtest_payload(
    *, source_root: Path, fills_dir: Path, labels_dir: Path,
    queue_values: list[Decimal], toxicity_caps: list[Decimal],
    source_commit: str, dataset_revision: str,
) -> dict[str, Any]:
    backtest, config_module, windows = _load_windows(
        source_root=source_root, fills_dir=fills_dir, labels_dir=labels_dir
    )
    strategy = config_module.StrategyConfig(
        split_size=Decimal("5"), max_lean_shares=Decimal("3"), fair_edge=Decimal("0"),
        maker_rebate_bps=Decimal("0"), ask_offset_ticks=0,
        requote_threshold_ticks=0, requote_min_seconds=0,
        improve_spread_ticks=0,
    )
    per_window: dict[str, list[dict[str, Any]]] = {}
    summary_rows: list[dict[str, Any]] = []
    diagnostics: dict[str, list[dict[str, Any]]] = {}
    for cap in toxicity_caps:
        filtered, audit = filter_windows(windows, cap)
        cap_key = str(cap)
        diagnostics[cap_key] = audit
        summaries = backtest.summarize(
            filtered, queue_values, maker_fee_bps=Decimal("0"), seed=30,
            variants=[strategy], realised_vol=False,
        )
        for summary in summaries:
            key = f"toxicity_cap={cap_key},{summary.label},queue={summary.queue}"
            rows = [
                {"start": result.start, "winner": result.winner, "pnl": str(result.pnl),
                 "fills": result.fills, "sold_up": str(result.sold_up),
                 "sold_down": str(result.sold_down), "places": result.places}
                for result in summary.results
            ]
            per_window[key] = rows
            row = summary.as_dict()
            row["toxicity_cap"] = cap_key
            row["accepted_windows"] = len(filtered)
            summary_rows.append(row)
    return {
        "format": "h401-hf-maker-toxicity-filter-input-v1",
        "hypothesis_id": "H401-causal-maker-toxicity-filter",
        "source": {
            "dataset_revision": dataset_revision, "source_commit": source_commit,
            "windows": len(windows), "prints": sum(len(window.prints) for window in windows),
            "labels": "Gamma resolved outcomePrices; paper label only",
        },
        "protocol": {
            "initial_cash_usd": "10", "split_size_usd": "5",
            "queues": [str(value) for value in queue_values],
            "toxicity_caps": [str(value) for value in toxicity_caps],
            "toxicity_window_seconds": TOXICITY_WINDOW_SECONDS,
            "same_evolving_cash_ledger": True, "no_reset": True,
            "no_concatenation": True, "paper_only": True,
        },
        "windows": [window.start for window in windows],
        "summaries": summary_rows,
        "per_window": per_window,
        "toxicity_diagnostics": diagnostics,
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
    parser.add_argument("--toxicity-cap", default="0.20,0.40")
    args = parser.parse_args()
    queues = [Decimal(item.strip()) for item in args.queue.split(",") if item.strip()]
    caps = [Decimal(item.strip()) for item in args.toxicity_cap.split(",") if item.strip()]
    payload = build_backtest_payload(
        source_root=args.source_root, fills_dir=args.fills_dir, labels_dir=args.labels_dir,
        queue_values=queues, toxicity_caps=caps,
        source_commit=args.source_commit, dataset_revision=args.dataset_revision,
    )
    args.output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": payload["format"], "windows": len(payload["windows"]), "summaries": len(payload["summaries"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
