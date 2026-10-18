"""H201: zero-tick maker-touch upper bound for the frozen H38 signal.

H38 is the existing old-clean paper challenger.  This experiment changes only
the execution proxy: a resting bid is counted when a later observed ask reaches
the bid price (same tick), instead of requiring a one-cent trade-through.  The
proxy is deliberately optimistic and does not establish queue position, order
acceptance, a fill, settlement, or reusable cash.  A zero-fee maker ledger is
used because the question is whether the capital-floor obstruction can be
removed at all.  No model is refit and no new data is collected.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import v1_execution_walk_forward as study
from cash_release_sensitivity import simulate_cash_locked
from maker_path_efficiency_shadow import inject_maker_execution
from sequential_compound_gate import evaluate_compound_chain

SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"
STRATEGY = "h201_maker_touch0_h38"
INITIAL_CASH = 10.0
MINIMUM_ORDER_NOTIONAL = 5.0
MINIMUM_ORDER_SHARES = 5.0
FEE_RATE = 0.0
DELAY_SECONDS = 600.0
ENTRY_DELAY_SECONDS = 4
FRACTION = 0.04
HIGH_THRESHOLD = 13.257220000004239
LOW_THRESHOLD = -12.702899999999412
BLOCK_SIZES = (12, 12, 12, 12, 11)


def add_frozen_h38_signal(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    move = pd.to_numeric(out["btc_move_2m"], errors="coerce")
    probability = pd.to_numeric(out["mid_up_prob_2m"], errors="coerce")
    path = pd.to_numeric(out["path_efficiency_first2m"], errors="coerce")
    score = move * (1.0 - 2.0 * (probability - 0.5).abs())
    path_gate = path.ge(0.25) & path.lt(0.50)
    side = pd.Series("skip", index=out.index, dtype="object")
    side.loc[path_gate & score.ge(HIGH_THRESHOLD)] = "buy_up"
    side.loc[path_gate & score.le(LOW_THRESHOLD)] = "buy_down"
    out[f"{STRATEGY}_side"] = side
    out[f"{STRATEGY}_minute"] = 2
    out[f"{STRATEGY}_fraction"] = FRACTION
    out[f"{STRATEGY}_component"] = "frozen_h38_move_uncertainty_midpath"
    return out


def _touch_at_or_below(asks: pd.Series, maker_cents: float) -> bool:
    """Return the inclusive same-tick touch proxy used by H201."""
    values = pd.to_numeric(asks, errors="coerce").dropna()
    return bool(not values.empty and (values <= float(maker_cents) + 1e-12).any())


def attach_touch0_snapshots(features: pd.DataFrame, quotes: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """Attach inclusive touch snapshots for selected H38 markets only."""
    selected = features.loc[
        features[f"{STRATEGY}_side"].ne("skip"),
        ["market_id", f"{STRATEGY}_side", f"{STRATEGY}_minute"],
    ].copy()
    selected["market_id"] = selected["market_id"].astype(str)
    selected = selected.drop_duplicates("market_id").set_index("market_id")
    records: list[dict[str, Any]] = []
    touch0 = 0
    strict_touch = 0
    selected_markets = 0
    for market_id, group in quotes.groupby("market_id", sort=False):
        key = str(market_id)
        if key not in selected.index:
            continue
        selected_row = selected.loc[key]
        side = str(selected_row[f"{STRATEGY}_side"])
        minute = int(selected_row[f"{STRATEGY}_minute"])
        token = "up" if side == "buy_up" else "down"
        group = group.sort_values("ts_utc").reset_index(drop=True)
        timestamps = pd.to_datetime(group["ts_utc"], utc=True, errors="coerce")
        first_ts = timestamps.iloc[0]
        signal_cut = first_ts + pd.Timedelta(minutes=minute)
        signal_pos = int(timestamps.searchsorted(signal_cut, side="right") - 1)
        if signal_pos < 0:
            continue
        signal_row = group.iloc[signal_pos]
        signal_ts = signal_row["ts_utc"]
        maker_cents = pd.to_numeric(signal_row.get(f"sell_{token}_cents"), errors="coerce")
        if pd.isna(maker_cents):
            continue
        after = group[timestamps >= pd.Timestamp(signal_ts) + pd.Timedelta(seconds=ENTRY_DELAY_SECONDS)].copy()
        asks = pd.to_numeric(after.get(f"buy_{token}_cents"), errors="coerce")
        after = after[asks.notna()]
        close_ts = group["close_ts_utc"].dropna().iloc[0] if group["close_ts_utc"].notna().any() else pd.NaT
        if pd.notna(close_ts):
            after = after[after["ts_utc"] <= close_ts]
        asks = pd.to_numeric(after.get(f"buy_{token}_cents"), errors="coerce")
        inclusive = _touch_at_or_below(asks, float(maker_cents))
        strict = _touch_at_or_below(asks, float(maker_cents) - 1.0)
        prefix = f"maker_{side}_{minute}m_d4"
        records.append(
            {
                "market_id": key,
                f"{prefix}_price": float(maker_cents) / 100.0,
                f"{prefix}_size": pd.to_numeric(signal_row.get(f"sell_{token}_size"), errors="coerce"),
                f"{prefix}_spread": pd.to_numeric(signal_row.get(f"spread_{token}_cents"), errors="coerce") / 100.0,
                f"{prefix}_overround": pd.to_numeric(signal_row.get("mid_overround_cents"), errors="coerce") / 100.0,
                f"{prefix}_touch": int(inclusive),
            }
        )
        selected_markets += 1
        touch0 += int(inclusive)
        strict_touch += int(strict)
    attached = features.merge(pd.DataFrame(records), on="market_id", how="left")
    return attached, {
        "selected_markets": selected_markets,
        "inclusive_touch_rows": touch0,
        "strict_one_cent_touch_rows": strict_touch,
    }


def _paper_events(logs: pd.DataFrame) -> list[dict[str, str]]:
    if logs.empty:
        return []
    return [
        {
            "status": "paper",
            "entry_ts": str(pd.Timestamp(row.first_quote_ts).timestamp()),
            "cash_available_ts": str(pd.Timestamp(row.release_ts).timestamp()),
            "entry_cost": str(row.filled_cost),
            "fee": str(row.fee_usd),
            "payout": str(row.payout_at_settlement),
        }
        for row in logs.sort_values("first_quote_ts", kind="stable").itertuples(index=False)
    ]


def replay(frame: pd.DataFrame, run_names: list[str]) -> dict[str, Any]:
    config = study.ExecutionConfig(
        delay_seconds=ENTRY_DELAY_SECONDS,
        max_spread=0.04,
        max_overround=0.03,
        min_depth=100.0,
        max_adverse_slippage=0.0,
        fixed_slippage=0.0,
    )
    logs, metrics = simulate_cash_locked(
        frame[frame["run_name"].isin(run_names)],
        STRATEGY,
        config,
        INITIAL_CASH,
        MINIMUM_ORDER_NOTIONAL,
        DELAY_SECONDS,
        cooldown_events=0,
        capture_logs=True,
        fee_mode="polymarket_taker",
        fee_rate=FEE_RATE,
        min_order_shares=MINIMUM_ORDER_SHARES,
    )
    events = _paper_events(logs)
    chain = evaluate_compound_chain(
        events,
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER_NOTIONAL),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    prefix = evaluate_compound_chain(
        events[:10],
        initial_cash=str(INITIAL_CASH),
        minimum_order=str(MINIMUM_ORDER_NOTIONAL),
        required_fills=10,
        evidence_mode="paper_upper_bound",
    )
    return {
        **metrics,
        "paper_chain_fills": int(chain["fills"]),
        "paper_chain_ending_cash": float(chain["ending_cash"]),
        "paper_chain_minimum_cash": float(chain["minimum_cash"]),
        "paper_chain_passed": bool(chain["paper_replay_passed"]),
        "paper_chain_failure_reason": chain["failure_reason"],
        "first_ten_fills": int(prefix["fills"]),
        "first_ten_ending_cash": float(prefix["ending_cash"]),
        "first_ten_minimum_cash": float(prefix["minimum_cash"]),
        "first_ten_passed": bool(prefix["paper_replay_passed"]),
    }


def evaluate(source_root: Path, *, source_commit: str, output: Path) -> dict[str, Any]:
    observed = subprocess.check_output(
        ["git", "-C", str(source_root.parent.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if observed != source_commit:
        raise RuntimeError(f"SOURCE_COMMIT_MISMATCH:{observed}:{source_commit}")
    features, _coverage, runs = study.prepare_data(source_root)
    if len(runs) != 83:
        raise RuntimeError(f"SOURCE_SNAPSHOT_RUN_COUNT_MISMATCH:{len(runs)}")
    frame = add_frozen_h38_signal(features)
    raw, _ = base.read_all_monthly_runs(source_root)
    raw = raw[raw["run_name"].astype(str).isin(runs)].copy()
    quotes = base.prepare_quotes(raw)
    frame, touch_counts = attach_touch0_snapshots(frame, quotes)
    frame = inject_maker_execution(frame, STRATEGY)
    touched = pd.Series(False, index=frame.index)
    for side in ("buy_up", "buy_down"):
        mask = frame[f"{STRATEGY}_side"].eq(side)
        touched.loc[mask] = pd.to_numeric(
            frame.loc[mask, f"maker_{side}_2m_d4_touch"], errors="coerce"
        ).fillna(0).eq(1)
    frame.loc[~touched, [f"{STRATEGY}_side", f"{STRATEGY}_minute", f"{STRATEGY}_fraction"]] = [
        "skip",
        -1,
        0.0,
    ]
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    splits: list[tuple[str, list[str]]] = [("selection20", selection_runs), ("holdout59", holdout_runs)]
    cursor = 0
    for index, size in enumerate(BLOCK_SIZES, start=1):
        splits.append((f"holdout_block_{index}", holdout_runs[cursor : cursor + size]))
        cursor += size
    rows = [{"split": name, **replay(frame, names)} for name, names in splits]
    table = pd.DataFrame(rows)
    selection = table[table["split"].eq("selection20")].iloc[0]
    holdout = table[table["split"].eq("holdout59")].iloc[0]
    blocks = table[table["split"].str.startswith("holdout_block_")]
    selection_prefix_gate = bool(
        int(selection["first_ten_fills"]) == 10
        and bool(selection["first_ten_passed"])
        and float(selection["first_ten_ending_cash"]) >= INITIAL_CASH
    )
    holdout_prefix_gate = bool(
        int(holdout["first_ten_fills"]) == 10
        and bool(holdout["first_ten_passed"])
        and float(holdout["first_ten_ending_cash"]) >= INITIAL_CASH
    )
    block_gate = bool(
        len(blocks) == len(BLOCK_SIZES)
        and (blocks["paper_chain_fills"] >= 10).all()
        and (blocks["ending_cash"] >= MINIMUM_ORDER_NOTIONAL).all()
        and (blocks["net_return"] >= 0.0).all()
    )
    result: dict[str, Any] = {
        "format": "old-clean-h201-maker-touch0-h38-compound-upper-bound-v1",
        "hypothesis_id": "H201_MAKER_TOUCH0_H38_COMPOUND",
        "source_commit": source_commit,
        "observed_source_commit": observed,
        "complete_runs": len(runs),
        "feature_rows": len(features),
        "candidate": "frozen_h38_move_uncertainty_midpath",
        "touch_proxy": "later observed ask <= resting bid at the same cent tick",
        "strict_proxy_comparison": "later observed ask <= resting bid - 1 cent",
        "touch_counts": touch_counts,
        "protocol": {
            "initial_cash_usd": INITIAL_CASH,
            "minimum_order_notional_usd": MINIMUM_ORDER_NOTIONAL,
            "minimum_order_shares": MINIMUM_ORDER_SHARES,
            "maker_fee_rate": FEE_RATE,
            "settlement_seconds": 300.0,
            "cash_release_delay_seconds": DELAY_SECONDS,
            "same_ledger": True,
            "required_fills": 10,
            "old_clean_dataset_only": True,
            "no_refit": True,
        },
        "rows": rows,
        "selection_prefix_gate": selection_prefix_gate,
        "holdout_prefix_gate": holdout_prefix_gate,
        "holdout_block_gate": block_gate,
        "gate_passed": bool(selection_prefix_gate and holdout_prefix_gate and block_gate),
        "decision": "retain_maker_path_for_receipt_design" if selection_prefix_gate and holdout_prefix_gate and block_gate else "reject_maker_touch0_upper_bound",
        "evidence_boundary": "optimistic historical quote-touch proxy; no queue, order acceptance, fill, settlement, or reusable-cash receipt",
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "new_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
    }
    output.mkdir(parents=True, exist_ok=True)
    table.to_csv(output / "split_metrics.csv", index=False)
    (output / "summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=True) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: result[k] for k in ("touch_counts", "selection_prefix_gate", "holdout_prefix_gate", "holdout_block_gate", "gate_passed", "decision")}, ensure_ascii=False, sort_keys=True))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.source_root, source_commit=args.source_commit, output=args.output)


if __name__ == "__main__":
    main()
