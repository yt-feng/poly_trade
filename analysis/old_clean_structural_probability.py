"""Causal structural settlement-probability test on the frozen old quotes.

At a fixed four-minute decision point, the model uses only a target price that
was already visible, the observed BTC proxy price, and a volatility estimate
from earlier complete runs.  It converts the distance-to-target into a normal
settlement probability, then requires the probability edge to cover the
actual taker fee and slippage.  The final outcome is used only as a label after
the market closes.  This is paper evidence only; it never submits an order.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

import all_monthly_clob_systematic_research_v2 as base


DECISION_MINUTE = 4
INITIAL_CASH = 10.0
MINIMUM_ORDER = 5.0
FEE_RATE = 0.07
SLIPPAGE = 0.005
FILL_RATIO = 0.25
CASH_RELEASE_SECONDS = 900.0
SIGMA_FACTORS = (0.75, 1.0, 1.25, 1.5)
EDGE_BUFFERS = (0.0, 0.005, 0.01, 0.02)
HOLDOUT_BLOCK_SIZES = (12, 12, 12, 12, 11)


def _first_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[0]) if not values.empty else math.nan


def _last_non_null(series: pd.Series) -> float:
    values = series.dropna()
    return float(values.iloc[-1]) if not values.empty else math.nan


def build_market_rows(source_root: Path) -> tuple[pd.DataFrame, list[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    maximum = int(coverage["file_count"].max())
    runs = coverage.loc[coverage["file_count"].eq(maximum), "run_name"].astype(str).tolist()
    quotes = base.prepare_quotes(raw[raw["run_name"].astype(str).isin(runs)].copy())
    rows: list[dict[str, object]] = []
    for market_id, group in quotes.groupby("market_id", sort=False):
        group = group.sort_values("ts_utc").reset_index(drop=True)
        first_ts = group["ts_utc"].min()
        decision_ts = first_ts + pd.Timedelta(minutes=DECISION_MINUTE)
        prefix = group[group["ts_utc"].le(decision_ts)]
        if prefix.empty:
            continue
        snap = prefix.iloc[-1]
        target = _first_non_null(prefix["target_price"])
        final = _last_non_null(group["final_price"])
        spot = pd.to_numeric(snap.get("final_price"), errors="coerce")
        if not (math.isfinite(target) and math.isfinite(final) and pd.notna(spot)):
            continue
        rows.append(
            {
                "market_id": str(market_id),
                "run_name": str(snap["run_name"]),
                "slug": str(snap["slug"]),
                "decision_ts": decision_ts,
                "outcome_up": float(final > target),
                "target_price": target,
                "spot_at_decision": float(spot),
                "distance_to_target": float(spot - target),
                "future_move_after_decision": float(final - float(spot)),
                "buy_up_price": float(pd.to_numeric(snap.get("buy_up_cents"), errors="coerce")) / 100.0,
                "buy_down_price": float(pd.to_numeric(snap.get("buy_down_cents"), errors="coerce")) / 100.0,
                "buy_up_size": float(pd.to_numeric(snap.get("buy_up_size"), errors="coerce")),
                "buy_down_size": float(pd.to_numeric(snap.get("buy_down_size"), errors="coerce")),
                "spread_up": float(pd.to_numeric(snap.get("spread_up_cents"), errors="coerce")) / 100.0,
                "spread_down": float(pd.to_numeric(snap.get("spread_down_cents"), errors="coerce")) / 100.0,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        raise RuntimeError("NO_TARGET_VISIBLE_FOUR_MINUTE_MARKETS")
    frame = frame.sort_values("decision_ts").reset_index(drop=True)
    return frame, runs


def estimate_sigma(training: pd.DataFrame) -> float | None:
    values = pd.to_numeric(training["future_move_after_decision"], errors="coerce").dropna()
    if len(values) < 50:
        return None
    sigma = float(values.std(ddof=1))
    return sigma if math.isfinite(sigma) and sigma > 1e-9 else None


def probability(distance: float, sigma: float, factor: float) -> float:
    scaled = float(distance) / max(float(sigma) * float(factor), 1e-9)
    return 0.5 * (1.0 + math.erf(scaled / math.sqrt(2.0)))


def fee_per_share(price: float) -> float:
    return FEE_RATE * float(price) * (1.0 - float(price))


def make_events(frame: pd.DataFrame, runs: list[str]) -> dict[str, pd.DataFrame]:
    """Create one causal event per market for each fixed model candidate."""
    candidates: dict[str, list[dict[str, object]]] = {
        f"sigma{factor:.2f}_buffer{buffer:.3f}": []
        for factor in SIGMA_FACTORS
        for buffer in EDGE_BUFFERS
    }
    for index, run_name in enumerate(runs):
        current = frame[frame["run_name"].eq(run_name)]
        if current.empty:
            continue
        sigma = estimate_sigma(frame[frame["run_name"].isin(runs[:index])])
        if sigma is None:
            continue
        for _, row in current.iterrows():
            p_up = {
                factor: probability(float(row["distance_to_target"]), sigma, factor)
                for factor in SIGMA_FACTORS
            }
            for factor in SIGMA_FACTORS:
                up_price = float(row["buy_up_price"]) + SLIPPAGE
                down_price = float(row["buy_down_price"]) + SLIPPAGE
                if not (0.0 < up_price < 0.98 and 0.0 < down_price < 0.98):
                    continue
                up_edge = p_up[factor] - up_price - fee_per_share(up_price)
                down_edge = (1.0 - p_up[factor]) - down_price - fee_per_share(down_price)
                if up_edge >= down_edge:
                    side, price, size, edge = "up", up_price, float(row["buy_up_size"]), up_edge
                    p = p_up[factor]
                else:
                    side, price, size, edge = "down", down_price, float(row["buy_down_size"]), down_edge
                    p = 1.0 - p_up[factor]
                for buffer in EDGE_BUFFERS:
                    if edge < buffer:
                        continue
                    candidates[f"sigma{factor:.2f}_buffer{buffer:.3f}"].append(
                        {
                            "market_id": row["market_id"],
                            "run_name": row["run_name"],
                            "decision_ts": row["decision_ts"],
                            "side": side,
                            "price": price - SLIPPAGE,
                            "fill_price": price,
                            "quote_size": size,
                            "predicted_probability": p,
                            "edge_after_fee_and_slippage": edge,
                            "outcome_up": float(row["outcome_up"]),
                            "sigma": sigma,
                            "distance_to_target": float(row["distance_to_target"]),
                        }
                    )
    return {name: pd.DataFrame(rows) for name, rows in candidates.items()}


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
        required = MINIMUM_ORDER
        if cash + 1e-12 < required:
            blocked_cash += 1
            continue
        if capacity + 1e-12 < required:
            blocked_capacity += 1
            continue
        cost = min(max(cash * 0.04, required), cash, capacity)
        shares = cost / price
        entry_fee = shares * fee_per_share(price)
        reserved = cost + entry_fee
        if reserved > cash + 1e-12:
            blocked_cash += 1
            continue
        payout = shares * (float(row["outcome_up"]) if row["side"] == "up" else 1.0 - float(row["outcome_up"]))
        pnl = payout - reserved
        cash -= reserved
        positions.append((now + pd.Timedelta(seconds=CASH_RELEASE_SECONDS), payout))
        trades.append({**row.to_dict(), "cost": cost, "entry_fee": entry_fee, "pnl": pnl, "cash_after_entry": cash})
        peak = max(peak, cash)
        max_drawdown = max(max_drawdown, (peak - cash) / peak if peak else 0.0)
    while positions:
        cash += positions.popleft()[1]
    metrics = {"trades": len(trades), "ending_cash": cash, "net_pnl": cash - INITIAL_CASH, "blocked_cash": blocked_cash, "blocked_capacity": blocked_capacity, "max_drawdown": max_drawdown}
    return metrics, pd.DataFrame(trades)


def evaluate(source_root: Path, output: Path, source_commit: str) -> dict[str, object]:
    frame, runs = build_market_rows(source_root)
    events = make_events(frame, runs)
    test_runs = runs[4:]
    selection_runs = test_runs[:20]
    holdout_runs = test_runs[20:]
    blocks: list[list[str]] = []
    cursor = 0
    for size in HOLDOUT_BLOCK_SIZES:
        blocks.append(holdout_runs[cursor : cursor + size])
        cursor += size
    rows: list[dict[str, object]] = []
    for name, candidate in events.items():
        selection, _ = replay(candidate, selection_runs)
        holdout, _ = replay(candidate, holdout_runs)
        block_metrics = [replay(candidate, block)[0] for block in blocks]
        rows.append({"candidate": name, "selection_trades": selection["trades"], "selection_cash": selection["ending_cash"], "holdout_trades": holdout["trades"], "holdout_cash": holdout["ending_cash"], "holdout_net_pnl": holdout["net_pnl"], "block_metrics": block_metrics, "min_block_cash": min(m["ending_cash"] for m in block_metrics), "min_block_net_pnl": min(m["net_pnl"] for m in block_metrics)})
    table = pd.DataFrame(rows)
    eligible = table[table["selection_trades"].ge(10)].sort_values(["selection_cash", "selection_trades"], ascending=[False, False])
    selected_name = None if eligible.empty else str(eligible.iloc[0]["candidate"])
    selected = next((events[name] for name in events if name == selected_name), pd.DataFrame())
    selected_holdout, holdout_trades = replay(selected, holdout_runs) if selected_name else ({"trades": 0, "ending_cash": INITIAL_CASH, "net_pnl": 0.0}, pd.DataFrame())
    selected_blocks = [replay(selected, block)[0] for block in blocks] if selected_name else []
    gate = bool(selected_name and selected_holdout["trades"] >= 10 and all(m["trades"] >= 10 and m["ending_cash"] >= 5.0 and m["net_pnl"] >= 0.0 for m in selected_blocks))
    output.mkdir(parents=True, exist_ok=True)
    table.to_json(output / "candidate_metrics.json", orient="records", force_ascii=False, indent=2)
    if not holdout_trades.empty:
        holdout_trades.to_csv(output / "selected_holdout_trades.csv", index=False)
    result = {
        "format": "old-data-causal-structural-settlement-probability-v1",
        "source_snapshot_commit": source_commit,
        "complete_runs": len(runs),
        "target_visible_at_decision_required": True,
        "decision_minute": DECISION_MINUTE,
        "usable_markets": int(len(frame)),
        "model": {"kind": "expanding normal settlement probability", "sigma_factors": list(SIGMA_FACTORS), "edge_buffers": list(EDGE_BUFFERS), "features": ["target_price_visible_by_4m", "spot_at_4m", "prior-run-final-minute-volatility"]},
        "protocol": {"initial_cash": INITIAL_CASH, "minimum_order_notional": MINIMUM_ORDER, "fee_rate": FEE_RATE, "slippage": SLIPPAGE, "fill_ratio": FILL_RATIO, "cash_release_seconds": CASH_RELEASE_SECONDS},
        "selection": {"runs": len(selection_runs), "minimum_selection_trades": 10, "selected_candidate": selected_name},
        "holdout": {"runs": len(holdout_runs), "trades": int(selected_holdout["trades"]), "ending_cash": float(selected_holdout["ending_cash"]), "net_pnl": float(selected_holdout["net_pnl"]), "blocks": selected_blocks},
        "promotion_gate": "holdout >=10 trades and every one of five chronological blocks >=10 trades, ending cash >=$5, nonnegative net PnL",
        "gate_passed": gate,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "decision": "promote_only_if_structural_probability_gate_passes",
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
