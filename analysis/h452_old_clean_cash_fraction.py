"""H452: cash-aware sizing on the frozen H447 paper candidate.

H447/H450 keep the ExtraTrees high-confidence entry signal, fee arithmetic,
900-second cash-release clock, full-size break-even exit and one evolving $10
ledger fixed.  H452 changes only the pre-registered gross-entry sizing rule:
the position targets 50%, 75% or 90% of the free cash available before entry,
subject to the existing $5 notional and five-share floors.  Exits must still
be observable before the market-end-minus-30-second cutoff; terminal outcome
fallback is not allowed.  The result is paper evidence only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import v1_execution_walk_forward as study
from h432_old_clean_extratrees_compound import (
    CASH_RELEASE_SECONDS,
    INITIAL_CASH,
    MINIMUM_ORDER_SHARES,
    MINIMUM_ORDER_USD,
    SLIPPAGES,
    TAKER_FEE_RATE,
    _candidate_frame,
    _fit,
)
from h434_old_clean_extratrees_high_confidence import apply_confidence_gate
from h440_h434_rolling_stability import rolling_windows
from h447_old_clean_breakeven_exit import (
    EXPECTED_COMPLETE_RUNS,
    SOURCE_COMMIT,
    WINDOW_RUNS,
    WINDOW_STRIDE,
    _quote_index,
)


GROSS_CASH_FRACTIONS = (0.50, 0.75, 0.90)
EXIT_CUTOFF_SECONDS = 30.0
REQUIRED_FILLS = 10


def _decision_label(hypothesis_id: str) -> str:
    """Keep derived H453 runs from inheriting H452's result label."""
    prefix = str(hypothesis_id).split("_", 1)[0].strip().lower()
    return prefix if prefix else "h452"


def _fee(shares: float, fill_price: float) -> float:
    return shares * TAKER_FEE_RATE * fill_price * (1.0 - fill_price)


def _size_from_cash(row: Any, cash: float, fraction: float) -> dict[str, float] | None:
    """Return a cash-feasible entry or None when the venue floor cannot fit."""
    if cash < MINIMUM_ORDER_USD - 1e-12:
        return None
    fill_price = float(row.fill_price)
    if not 0.0 < fill_price < 1.0:
        return None
    gross_target = max(MINIMUM_ORDER_USD, float(fraction) * cash)
    shares = max(MINIMUM_ORDER_SHARES, gross_target / fill_price)
    reserved = shares * fill_price + _fee(shares, fill_price)
    if reserved > cash + 1e-12:
        # Keep the same fraction target when it fits; otherwise scale the
        # gross cost down just enough to include the price-dependent fee.
        per_share = fill_price + TAKER_FEE_RATE * fill_price * (1.0 - fill_price)
        shares = min(shares, cash / per_share)
        reserved = shares * per_share
    if shares < MINIMUM_ORDER_SHARES - 1e-9:
        return None
    if shares * fill_price < MINIMUM_ORDER_USD - 1e-9:
        return None
    if reserved > cash + 1e-9:
        return None
    return {
        "shares": float(shares),
        "cost": float(shares * fill_price),
        "fee": float(_fee(shares, fill_price)),
        "reserved": float(reserved),
    }


def _first_break_even_exit(
    row: Any,
    quotes: pd.DataFrame | None,
    *,
    shares: float,
    entry_outlay: float,
    cutoff_seconds: float = EXIT_CUTOFF_SECONDS,
) -> dict[str, Any] | None:
    """Find a causal, full-size, fee-aware exit before the fixed cutoff."""
    if quotes is None or quotes.empty:
        return None
    side = str(row.side)
    bid_column = "sell_up_cents" if side == "up" else "sell_down_cents"
    size_column = "sell_up_size" if side == "up" else "sell_down_size"
    entry = pd.Timestamp(row.entry_ts)
    market_end = pd.Timestamp(row.settlement_ts) + pd.Timedelta(seconds=300.0)
    cutoff = market_end - pd.Timedelta(seconds=float(cutoff_seconds))
    observed = quotes[(quotes["ts_iso"] >= entry) & (quotes["ts_iso"] < cutoff)]
    if observed.empty:
        return None
    bid = pd.to_numeric(observed[bid_column], errors="coerce") / 100.0
    size = pd.to_numeric(observed[size_column], errors="coerce")
    exit_fill = bid - float(row.slippage)
    exit_fee = shares * TAKER_FEE_RATE * exit_fill * (1.0 - exit_fill)
    proceeds = shares * exit_fill - exit_fee
    eligible = (
        bid.notna()
        & size.notna()
        & size.ge(shares)
        & exit_fill.gt(0.0)
        & exit_fill.lt(1.0)
        & proceeds.ge(entry_outlay - 1e-9)
    )
    if not bool(eligible.any()):
        return None
    index = eligible[eligible].index[0]
    return {
        "exit_used": True,
        "exit_payout": float(proceeds.loc[index]),
        "exit_ts": pd.Timestamp(observed.loc[index, "ts_iso"]).isoformat(),
        "exit_fill_price": float(exit_fill.loc[index]),
    }


def _replay_fraction(
    candidates: pd.DataFrame,
    run_names: list[str],
    quotes: dict[str, pd.DataFrame],
    fraction: float,
    *,
    exit_cache: dict[tuple[str, str, float, float], dict[str, Any] | None],
) -> dict[str, Any]:
    if candidates.empty:
        return {
            "candidate_rows": 0,
            "fills": 0,
            "blocked_before_release": 0,
            "ending_cash": INITIAL_CASH,
            "first_ten_fills": 0,
            "first_ten_ending_cash": INITIAL_CASH,
            "first_ten_early_exits": 0,
            "first_ten_min_cash_before_entry": INITIAL_CASH,
            "first_ten_passed": False,
            "failure_reason": "no_candidates",
        }
    allowed = {str(name) for name in run_names}
    ordered = candidates[candidates["run_name"].astype(str).isin(allowed)].sort_values(
        ["entry_ts", "slug"], kind="mergesort"
    )
    cash = float(INITIAL_CASH)
    pending: tuple[pd.Timestamp, float] | None = None
    chain: list[dict[str, Any]] = []
    blocked_before_release = 0
    failure_reason: str | None = None
    for row in ordered.itertuples(index=False):
        entry = pd.Timestamp(row.entry_ts)
        if pending is not None:
            if entry <= pending[0]:
                blocked_before_release += 1
                continue
            cash += pending[1]
            pending = None
        if cash < MINIMUM_ORDER_USD - 1e-12:
            failure_reason = "insufficient_cash_for_minimum_order"
            break
        terms = _size_from_cash(row, cash, fraction)
        if terms is None:
            failure_reason = "cash_fraction_cannot_fit_minimum_order"
            break
        key = (str(row.slug), str(row.side), round(float(terms["shares"]), 8), float(row.slippage))
        if key not in exit_cache:
            exit_cache[key] = _first_break_even_exit(
                row,
                quotes.get(str(row.slug)),
                shares=float(terms["shares"]),
                entry_outlay=float(terms["reserved"]),
            )
        exit = exit_cache[key]
        if exit is None:
            failure_reason = "no_full_size_break_even_exit_before_30s_cutoff"
            break
        before = cash
        cash -= terms["reserved"]
        pending = (pd.Timestamp(row.cash_release_ts), float(exit["exit_payout"]))
        chain.append(
            {
                "run_name": str(row.run_name),
                "entry_ts": entry.isoformat(),
                "cash_release_ts": pd.Timestamp(row.cash_release_ts).isoformat(),
                "shares": float(terms["shares"]),
                "cost": float(terms["cost"]),
                "fee": float(terms["fee"]),
                "payout": float(exit["exit_payout"]),
                "exit_ts": str(exit["exit_ts"]),
                "cash_before_entry": before,
                "cash_after_entry": cash,
            }
        )
        if len(chain) >= REQUIRED_FILLS:
            break
    if pending is not None:
        cash += pending[1]
    prefix = chain[:REQUIRED_FILLS]
    ending = float(cash) if len(prefix) == REQUIRED_FILLS else float(prefix[-1]["cash_after_entry"] if prefix else INITIAL_CASH)
    min_before = float(min([INITIAL_CASH] + [float(item["cash_before_entry"]) for item in prefix]))
    passed = bool(len(prefix) == REQUIRED_FILLS and min_before >= MINIMUM_ORDER_USD and ending >= INITIAL_CASH)
    if failure_reason is None and len(prefix) < REQUIRED_FILLS:
        failure_reason = "fewer_than_required_consecutive_fills"
    return {
        "candidate_rows": int(len(ordered)),
        "fills": int(len(chain)),
        "blocked_before_release": int(blocked_before_release),
        "ending_cash": ending,
        "first_ten_fills": int(len(prefix)),
        "first_ten_ending_cash": ending,
        "first_ten_early_exits": int(len(prefix)),
        "first_ten_min_cash_before_entry": min_before,
        "first_ten_passed": passed,
        "failure_reason": failure_reason,
        "chain": prefix,
    }


def _strip_chain(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_chain(item) for key, item in value.items() if key != "chain"}
    if isinstance(value, list):
        return [_strip_chain(item) for item in value]
    return value


def evaluate(
    source_root: Path,
    *,
    source_commit: str,
    output: Path,
    gross_cash_fractions: tuple[float, ...] = GROSS_CASH_FRACTIONS,
    hypothesis_id: str = "H452_H447_CASH_AWARE_FRACTIONAL_SIZING",
    result_format: str = "old-clean-h452-h447-cash-fraction-v1",
) -> dict[str, Any]:
    if not gross_cash_fractions or any(float(value) <= 0.0 or float(value) > 1.0 for value in gross_cash_fractions):
        raise ValueError("gross_cash_fractions must be in (0, 1]")
    gross_cash_fractions = tuple(float(value) for value in gross_cash_fractions)
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.resolve().parents[1]), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root, strict_quality=True, max_snapshot_age_seconds=90.0)
    if len(runs) != EXPECTED_COMPLETE_RUNS:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    by_run = {name: features[features["run_name"].astype(str).eq(str(name))].copy() for name in runs}
    candidates_by_slippage: dict[float, list[pd.DataFrame]] = {float(s): [] for s in SLIPPAGES}
    for index, run_name in enumerate(runs):
        if index < 4:
            continue
        train = features[features["run_name"].astype(str).isin([str(item) for item in runs[:index]])]
        model, medians = _fit(train)
        if model is None:
            continue
        for slippage in SLIPPAGES:
            candidate = apply_confidence_gate(_candidate_frame(model, medians, by_run[run_name], float(slippage)))
            if not candidate.empty:
                current = candidate.copy()
                current["model_run_index"] = index
                candidates_by_slippage[float(slippage)].append(current)
    all_candidates = pd.concat(
        [frame for frames in candidates_by_slippage.values() for frame in frames], ignore_index=True
    )
    quotes = _quote_index(source_root, set(all_candidates["slug"].astype(str))) if not all_candidates.empty else {}
    windows = rolling_windows(runs[4:], window_runs=WINDOW_RUNS, stride=WINDOW_STRIDE)
    rows: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    for fraction in gross_cash_fractions:
        for slippage in SLIPPAGES:
            base = pd.concat(candidates_by_slippage[float(slippage)], ignore_index=True) if candidates_by_slippage[float(slippage)] else pd.DataFrame()
            cache: dict[tuple[str, str, float, float], dict[str, Any] | None] = {}
            selected: list[dict[str, Any]] = []
            for window in windows:
                result = _replay_fraction(base, window["runs"], quotes, fraction, exit_cache=cache)
                row = {
                    "gross_cash_fraction": float(fraction),
                    "slippage": float(slippage),
                    "window_index": window["window_index"],
                    "start_run": window["start_run"],
                    "end_run": window["end_run"],
                    **_strip_chain(result),
                }
                rows.append(row)
                selected.append(row)
            passed = [row for row in selected if bool(row["first_ten_passed"])]
            cells.append(
                {
                    "gross_cash_fraction": float(fraction),
                    "slippage": float(slippage),
                    "window_count": len(selected),
                    "all_windows_passed": bool(selected) and len(passed) == len(selected),
                    "growth_window_fraction": len(passed) / len(selected) if selected else 0.0,
                    "minimum_first_ten_fills": min((int(row["first_ten_fills"]) for row in selected), default=0),
                    "minimum_first_ten_cash_usd": min((float(row["first_ten_ending_cash"]) for row in selected), default=INITIAL_CASH),
                    "minimum_cash_before_entry_usd": min((float(row["first_ten_min_cash_before_entry"]) for row in selected), default=INITIAL_CASH),
                    "total_first_ten_early_exits": sum(int(row["first_ten_early_exits"]) for row in selected),
                }
            )
    all_pass = bool(cells) and all(cell["all_windows_passed"] for cell in cells)
    result: dict[str, Any] = {
        "format": result_format,
        "hypothesis_id": hypothesis_id,
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "strict_quality": True,
        "complete_runs": len(runs),
        "feature_rows": int(len(features)),
        "candidate": "H447_H434_CAUSAL_BREAKEVEN_EXIT",
        "gross_cash_fractions": list(gross_cash_fractions),
        "exit_cutoff_seconds_before_market_end": EXIT_CUTOFF_SECONDS,
        "window_count": len(windows),
        "cell_summaries": cells,
        "all_cells_passed": all_pass,
        "decision": f"{_decision_label(hypothesis_id)}_cash_fraction_passes_paper_gate" if all_pass else f"{_decision_label(hypothesis_id)}_cash_fraction_rejects_or_weakens_paper_gate",
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "required_fills": REQUIRED_FILLS,
            "minimum_order_usd": MINIMUM_ORDER_USD,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "taker_fee_rate": TAKER_FEE_RATE,
            "cash_release_delay_seconds": CASH_RELEASE_SECONDS,
            "same_ledger_within_window": True,
            "no_cross_window_concatenation": True,
            "old_clean_dataset_only": True,
            "exit_cutoff_is_predeclared": True,
            "no_terminal_outcome_fallback": True,
            "gross_fraction_applies_to_available_cash_before_entry": True,
            "paper_only": True,
        },
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": True,
        "live_orders_enabled": False,
        "evidence_boundary": "strict-quality old-clean quote replay; displayed bid/size is not authenticated execution or reusable cash",
        "root_cause": "tests whether variable cash-aware sizing improves the frozen H447 paper chain without breaking the same-ledger $5 floor or relying on terminal outcomes",
        "window_rows": rows,
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "rolling_window_metrics.csv").write_text(pd.DataFrame(rows).to_csv(index=False), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result["summary_sha256"] = hashlib.sha256((output / "summary.json").read_bytes()).hexdigest()
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"cell_summaries": cells, "decision": result["decision"]}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--gross-cash-fractions", type=float, nargs="+", default=list(GROSS_CASH_FRACTIONS))
    parser.add_argument("--hypothesis-id", default="H452_H447_CASH_AWARE_FRACTIONAL_SIZING")
    parser.add_argument("--result-format", default="old-clean-h452-h447-cash-fraction-v1")
    args = parser.parse_args()
    evaluate(
        args.source_root,
        source_commit=args.source_commit,
        output=args.output,
        gross_cash_fractions=tuple(args.gross_cash_fractions),
        hypothesis_id=args.hypothesis_id,
        result_format=args.result_format,
    )


if __name__ == "__main__":
    main()
