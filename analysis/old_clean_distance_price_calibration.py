"""Causal empirical/isotonic calibration of target distance against ask price.

This is a distinct old-data experiment from the normal structural model.  For
each market, the target-visible four-minute distance is standardized by the
volatility estimated from earlier runs.  The calibration uses only earlier
markets and maps that score, optionally jointly with the executable Up ask,
to an Up probability.  The final market outcome is never used before close.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

from old_clean_structural_probability import (
    CASH_RELEASE_SECONDS,
    EDGE_BUFFERS,
    FEE_RATE,
    FILL_RATIO,
    INITIAL_CASH,
    MINIMUM_ORDER,
    SLIPPAGE,
    build_market_rows,
    fee_per_share,
)


METHODS = ("empirical_z", "empirical_z_price", "isotonic_z", "isotonic_residual")
Z_WIDTH = 0.5
PRICE_WIDTH = 0.05
MIN_CELL_SUPPORT = 25
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def estimate_sigma(training: pd.DataFrame) -> float | None:
    values = pd.to_numeric(training["future_move_after_decision"], errors="coerce").dropna()
    if len(values) < 50:
        return None
    sigma = float(values.std(ddof=1))
    return sigma if math.isfinite(sigma) and sigma > 1e-9 else None


def add_causal_z(frame: pd.DataFrame, runs: list[str]) -> pd.DataFrame:
    out = frame.copy()
    out["causal_sigma"] = np.nan
    out["z_score"] = np.nan
    for index, run_name in enumerate(runs):
        sigma = estimate_sigma(out[out["run_name"].isin(runs[:index])])
        if sigma is None:
            continue
        mask = out["run_name"].eq(run_name)
        out.loc[mask, "causal_sigma"] = sigma
        out.loc[mask, "z_score"] = out.loc[mask, "distance_to_target"] / sigma
    return out


def _z_bin(values: pd.Series) -> pd.Series:
    return np.floor((values.clip(-8.0, 8.0) + 8.0) / Z_WIDTH).astype("Int64")


def _price_bin(values: pd.Series) -> pd.Series:
    return np.floor(values.clip(0.0, 0.999) / PRICE_WIDTH).astype("Int64")


def _smoothed_rate(successes: int, total: int) -> float:
    return float((successes + 1.0) / (total + 2.0))


def predict_probability(method: str, training: pd.DataFrame, current: pd.DataFrame) -> pd.Series:
    train = training.dropna(subset=["z_score", "buy_up_price", "outcome_up"]).copy()
    cur = current.dropna(subset=["z_score", "buy_up_price"]).copy()
    if train.empty or cur.empty:
        return pd.Series(index=current.index, dtype=float)
    if method == "isotonic_z":
        model = IsotonicRegression(y_min=0.01, y_max=0.99, out_of_bounds="clip")
        model.fit(train["z_score"].to_numpy(float), train["outcome_up"].to_numpy(float))
        values = model.predict(cur["z_score"].to_numpy(float))
        return pd.Series(values, index=cur.index).reindex(current.index)
    if method == "isotonic_residual":
        market_logit = np.log(np.clip(train["buy_up_price"].to_numpy(float), 0.01, 0.99) / np.clip(1.0 - train["buy_up_price"].to_numpy(float), 0.01, 0.99))
        cur_logit = np.log(np.clip(cur["buy_up_price"].to_numpy(float), 0.01, 0.99) / np.clip(1.0 - cur["buy_up_price"].to_numpy(float), 0.01, 0.99))
        model = IsotonicRegression(y_min=0.01, y_max=0.99, out_of_bounds="clip")
        model.fit(train["z_score"].to_numpy(float) - market_logit, train["outcome_up"].to_numpy(float))
        values = model.predict(cur["z_score"].to_numpy(float) - cur_logit)
        return pd.Series(values, index=cur.index).reindex(current.index)

    train["z_bin"] = _z_bin(train["z_score"])
    cur["z_bin"] = _z_bin(cur["z_score"])
    if method == "empirical_z":
        grouped = train.groupby("z_bin", dropna=True)["outcome_up"].agg(["sum", "count"])
        global_rate = _smoothed_rate(int(train["outcome_up"].sum()), len(train))
        values = []
        for _, row in cur.iterrows():
            cell = grouped.loc[row["z_bin"]] if row["z_bin"] in grouped.index else None
            values.append(_smoothed_rate(int(cell["sum"]), int(cell["count"])) if cell is not None else global_rate)
        return pd.Series(values, index=cur.index).reindex(current.index)
    if method == "empirical_z_price":
        train["price_bin"] = _price_bin(train["buy_up_price"])
        cur["price_bin"] = _price_bin(cur["buy_up_price"])
        two = train.groupby(["z_bin", "price_bin"], dropna=True)["outcome_up"].agg(["sum", "count"])
        one = train.groupby("z_bin", dropna=True)["outcome_up"].agg(["sum", "count"])
        global_rate = _smoothed_rate(int(train["outcome_up"].sum()), len(train))
        values = []
        for _, row in cur.iterrows():
            key = (row["z_bin"], row["price_bin"])
            cell = two.loc[key] if key in two.index else None
            if cell is not None and int(cell["count"]) >= MIN_CELL_SUPPORT:
                values.append(_smoothed_rate(int(cell["sum"]), int(cell["count"])))
                continue
            cell = one.loc[row["z_bin"]] if row["z_bin"] in one.index else None
            values.append(_smoothed_rate(int(cell["sum"]), int(cell["count"])) if cell is not None else global_rate)
        return pd.Series(values, index=cur.index).reindex(current.index)
    raise ValueError(f"unsupported method: {method}")


def make_events(frame: pd.DataFrame, runs: list[str]) -> dict[str, pd.DataFrame]:
    events: dict[str, list[dict[str, object]]] = {f"{method}_buffer{buffer:.3f}": [] for method in METHODS for buffer in EDGE_BUFFERS}
    for index, run_name in enumerate(runs):
        current = frame[frame["run_name"].eq(run_name)].copy()
        if current.empty or index < 4:
            continue
        training = frame[frame["run_name"].isin(runs[:index])].copy()
        for method in METHODS:
            probs = predict_probability(method, training, current)
            current["p_up"] = probs
            for _, row in current.dropna(subset=["p_up"]).iterrows():
                up_price = float(row["buy_up_price"]) + SLIPPAGE
                down_price = float(row["buy_down_price"]) + SLIPPAGE
                if not (0.0 < up_price < 0.98 and 0.0 < down_price < 0.98):
                    continue
                up_edge = float(row["p_up"]) - up_price - fee_per_share(up_price)
                down_edge = (1.0 - float(row["p_up"])) - down_price - fee_per_share(down_price)
                if up_edge >= down_edge:
                    side, price, size, edge, probability = "up", up_price, float(row["buy_up_size"]), up_edge, float(row["p_up"])
                else:
                    side, price, size, edge, probability = "down", down_price, float(row["buy_down_size"]), down_edge, 1.0 - float(row["p_up"])
                for buffer in EDGE_BUFFERS:
                    if edge < buffer:
                        continue
                    events[f"{method}_buffer{buffer:.3f}"].append({
                        "market_id": row["market_id"], "run_name": row["run_name"], "decision_ts": row["decision_ts"],
                        "side": side, "price": price - SLIPPAGE, "fill_price": price, "quote_size": size,
                        "predicted_probability": probability, "edge_after_fee_and_slippage": edge,
                        "outcome_up": float(row["outcome_up"]), "z_score": float(row["z_score"]),
                    })
    return {name: pd.DataFrame(rows) for name, rows in events.items()}


def replay(events: pd.DataFrame, run_names: list[str]) -> tuple[dict[str, object], pd.DataFrame]:
    if events.empty:
        return {"trades": 0, "ending_cash": INITIAL_CASH, "net_pnl": 0.0, "blocked_cash": 0, "blocked_capacity": 0, "max_drawdown": 0.0}, pd.DataFrame()
    ordered = events[events["run_name"].isin(run_names)].sort_values("decision_ts")
    cash = INITIAL_CASH
    positions: deque[tuple[pd.Timestamp, float]] = deque()
    trades: list[dict[str, object]] = []
    blocked_cash = blocked_capacity = 0
    peak = cash
    max_drawdown = 0.0
    for _, row in ordered.iterrows():
        now = pd.Timestamp(row["decision_ts"])
        while positions and positions[0][0] <= now:
            cash += positions.popleft()[1]
        price = float(row["fill_price"])
        capacity = float(row["quote_size"]) * FILL_RATIO * price
        if cash + 1e-12 < MINIMUM_ORDER:
            blocked_cash += 1
            continue
        if capacity + 1e-12 < MINIMUM_ORDER:
            blocked_capacity += 1
            continue
        cost = min(max(cash * 0.04, MINIMUM_ORDER), cash, capacity)
        shares = cost / price
        entry_fee = shares * fee_per_share(price)
        reserved = cost + entry_fee
        if reserved > cash + 1e-12:
            blocked_cash += 1
            continue
        outcome = float(row["outcome_up"])
        payout = shares * (outcome if row["side"] == "up" else 1.0 - outcome)
        pnl = payout - reserved
        cash -= reserved
        positions.append((now + pd.Timedelta(seconds=CASH_RELEASE_SECONDS), payout))
        trades.append({**row.to_dict(), "cost": cost, "entry_fee": entry_fee, "pnl": pnl, "cash_after_entry": cash})
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, (peak - cash) / peak if peak else 0.0)
    while positions:
        cash += positions.popleft()[1]
    return {"trades": len(trades), "ending_cash": cash, "net_pnl": cash - INITIAL_CASH, "blocked_cash": blocked_cash, "blocked_capacity": blocked_capacity, "max_drawdown": max_drawdown}, pd.DataFrame(trades)


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    frame, runs = build_market_rows(source_root)
    frame = add_causal_z(frame, runs)
    events = make_events(frame, runs)
    test_runs = runs[4:]
    selection_runs, holdout_runs = test_runs[:20], test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        blocks.append(holdout_runs[cursor : cursor + size])
        cursor += size
    metrics: list[dict[str, object]] = []
    for name, candidate in events.items():
        selection = replay(candidate, selection_runs)[0]
        holdout = replay(candidate, holdout_runs)[0]
        block_metrics = [replay(candidate, block)[0] for block in blocks]
        metrics.append({"candidate": name, "selection_trades": selection["trades"], "selection_cash": selection["ending_cash"], "holdout_trades": holdout["trades"], "holdout_cash": holdout["ending_cash"], "holdout_net_pnl": holdout["net_pnl"], "block_metrics": block_metrics, "min_block_cash": min(m["ending_cash"] for m in block_metrics), "min_block_net_pnl": min(m["net_pnl"] for m in block_metrics)})
    table = pd.DataFrame(metrics)
    eligible = table[(table["selection_trades"] >= 10) & (table["selection_cash"] >= MINIMUM_ORDER)].sort_values(["selection_cash", "selection_trades"], ascending=[False, False])
    selected_name = None if eligible.empty else str(eligible.iloc[0]["candidate"])
    selected = events.get(selected_name, pd.DataFrame()) if selected_name else pd.DataFrame()
    holdout, holdout_trades = replay(selected, holdout_runs)
    selected_blocks = [replay(selected, block)[0] for block in blocks] if selected_name else []
    gate = bool(selected_name and holdout["trades"] >= 10 and all(m["trades"] >= 10 and m["ending_cash"] >= 5.0 and m["net_pnl"] >= 0.0 for m in selected_blocks))
    output.mkdir(parents=True, exist_ok=True)
    table.to_json(output / "candidate_metrics.json", orient="records", force_ascii=False, indent=2)
    if not holdout_trades.empty:
        holdout_trades.to_csv(output / "selected_holdout_trades.csv", index=False)
    result = {
        "format": "old-data-causal-distance-price-calibration-v1", "source_snapshot_commit": source_commit, "complete_runs": len(runs), "usable_markets": int(len(frame)),
        "decision_minute": 4, "target_visible_at_decision_required": True,
        "methods": list(METHODS), "edge_buffers": list(EDGE_BUFFERS), "z_width": Z_WIDTH, "price_width": PRICE_WIDTH, "minimum_cell_support": MIN_CELL_SUPPORT,
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "fee_rate": FEE_RATE, "slippage": SLIPPAGE, "fill_ratio": FILL_RATIO, "cash_release_seconds": CASH_RELEASE_SECONDS},
        "selection": {"runs": len(selection_runs), "minimum_selection_trades": 10, "selected_candidate": selected_name},
        "holdout": {"runs": len(holdout_runs), "trades": int(holdout["trades"]), "ending_cash": float(holdout["ending_cash"]), "net_pnl": float(holdout["net_pnl"]), "blocks": selected_blocks},
        "promotion_gate": "holdout >=10 trades and each of five chronological blocks >=10 trades, ending cash >=$5 and nonnegative net PnL",
        "gate_passed": gate, "actual_fills": 0, "verified_canary_fills": 0, "new_data_collected": False, "live_orders_enabled": False,
        "decision": "promote_only_if_distance_price_calibration_gate_passes",
    }
    (output / "meta.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    evaluate(args.source_root, args.output, args.source_commit)


if __name__ == "__main__":
    main()
