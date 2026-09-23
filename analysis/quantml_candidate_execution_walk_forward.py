from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", category=RuntimeWarning, message="Mean of empty slice")
warnings.filterwarnings("ignore", category=FutureWarning, message="The behavior of DataFrame concatenation")

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1

FEE = 0.01
BANKROLL0 = 100.0
MIN_TRAIN_RUNS = 3
PRICE_MIN = 0.05
PRICE_MAX = 0.95
FILL_PRICE_MAX = 0.98
FILL_RATIO = 0.25
MIN_COST = 0.50
QUOTA_WINDOW_EVENTS = 144
MAX_TRADES_PER_WINDOW = 10
COOLDOWN_AFTER_LOSSES = 2
COOLDOWN_EVENTS = 6


@dataclass(frozen=True)
class CandidateSpec:
    name: str
    family: str
    entry_minute: int
    factor_col: str | None
    quantiles: Tuple[float, ...]
    description: str


@dataclass(frozen=True)
class ExecutionConfig:
    delay_seconds: int
    max_spread: float
    max_overround: float
    min_depth: float
    max_adverse_slippage: float
    fixed_slippage: float
    fraction: float

    @property
    def config_id(self) -> str:
        return (
            f"d{self.delay_seconds}"
            f"_sp{int(round(self.max_spread * 1000))}"
            f"_ov{int(round(self.max_overround * 1000))}"
            f"_dep{int(round(self.min_depth))}"
            f"_adv{int(round(self.max_adverse_slippage * 1000))}"
            f"_fix{int(round(self.fixed_slippage * 10000))}"
            f"_f{int(round(self.fraction * 10000))}"
        )


def as_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)


def complete_runs_from_coverage(coverage: pd.DataFrame) -> List[str]:
    if coverage.empty:
        return []
    max_files = int(coverage["file_count"].max())
    return coverage.loc[coverage["file_count"] == max_files, "run_name"].astype(str).tolist()


def add_candidate_factors(features: pd.DataFrame) -> pd.DataFrame:
    out = features.copy()
    uncertainty = (1.0 - (as_num(out["mid_up_prob_2m"]).fillna(0.5) - 0.5).abs() * 2.0).clip(0.0, 1.0)
    path_eff = as_num(out["path_efficiency_first2m"]).clip(0.0, 1.0).fillna(0.0)
    out["candidate_btc2_path_eff"] = (as_num(out["btc_move_2m"]) / 50.0) * path_eff
    out["candidate_size_imbalance_uncertainty"] = as_num(out["size_imbalance_updown_2m"]) * uncertainty
    return out


def candidate_registry() -> List[CandidateSpec]:
    return [
        CandidateSpec(
            name="classic_early_drop_down",
            family="classic_microstructure",
            entry_minute=1,
            factor_col=None,
            quantiles=(),
            description="Buy Down when BTC is down at least 20 dollars by minute 1; execution filters use only signal/fill quotes.",
        ),
        CandidateSpec(
            name="btc2_path_eff",
            family="alphaforge_interaction",
            entry_minute=2,
            factor_col="candidate_btc2_path_eff",
            quantiles=(0.85, 0.90, 0.95),
            description="(btc_move_2m / 50) * path_efficiency_first2m; high buys Up, low buys Down.",
        ),
        CandidateSpec(
            name="size_imbalance_uncertainty",
            family="alphaforge_interaction",
            entry_minute=2,
            factor_col="candidate_size_imbalance_uncertainty",
            quantiles=(0.90, 0.95),
            description="size_imbalance_updown_2m * uncertainty; high buys Up, low buys Down.",
        ),
    ]


def execution_config_grid() -> List[ExecutionConfig]:
    return [
        ExecutionConfig(*vals)
        for vals in product(
            (4, 8),
            (0.04, 0.06),
            (0.03, 0.05),
            (100.0, 300.0),
            (0.005, 0.015),
            (0.0025, 0.0075),
            (0.01, 0.02),
        )
    ]


def snapshot_values(row: pd.Series | None, side: str) -> Dict[str, object]:
    if row is None:
        return {
            "price": np.nan,
            "size": np.nan,
            "spread": np.nan,
            "overround": np.nan,
            "ts": pd.NaT,
        }
    side_short = "up" if side == "buy_up" else "down"
    return {
        "price": pd.to_numeric(row.get(f"{side}_cents"), errors="coerce") / 100.0,
        "size": pd.to_numeric(row.get(f"{side}_size"), errors="coerce"),
        "spread": pd.to_numeric(row.get(f"spread_{side_short}_cents"), errors="coerce") / 100.0,
        "overround": pd.to_numeric(row.get("mid_overround_cents"), errors="coerce") / 100.0,
        "ts": row.get("ts_utc", pd.NaT),
    }


def attach_execution_snapshots(
    features: pd.DataFrame,
    quotes: pd.DataFrame,
    delays: Iterable[int],
    minutes: Iterable[int],
) -> pd.DataFrame:
    records: List[Dict[str, object]] = []
    delay_list = list(delays)
    minute_list = list(minutes)
    for market_id, g in quotes.groupby("market_id", dropna=False):
        g = g.sort_values("ts_utc").reset_index(drop=True)
        if g.empty:
            continue
        ts = pd.to_datetime(g["ts_utc"], utc=True, errors="coerce")
        ts_ns = ts.astype("int64").to_numpy()
        first_ts = ts.iloc[0]
        rec: Dict[str, object] = {"market_id": market_id}
        for minute in minute_list:
            signal_cut = first_ts + pd.Timedelta(minutes=minute)
            sig_pos = int(np.searchsorted(ts_ns, signal_cut.value, side="right") - 1)
            signal_row = None if sig_pos < 0 else g.iloc[sig_pos]
            signal_ts = pd.NaT if signal_row is None else signal_row.get("ts_utc", pd.NaT)
            for side in ("buy_up", "buy_down"):
                sig = snapshot_values(signal_row, side)
                for delay in delay_list:
                    fill_row = None
                    if pd.notna(signal_ts):
                        fill_cut = pd.Timestamp(signal_ts) + pd.Timedelta(seconds=delay)
                        fill_pos = int(np.searchsorted(ts_ns, fill_cut.value, side="left"))
                        if fill_pos < len(g):
                            fill_row = g.iloc[fill_pos]
                    fill = snapshot_values(fill_row, side)
                    prefix = f"exec_{side}_{minute}m_d{delay}"
                    rec[f"{prefix}_signal_price"] = sig["price"]
                    rec[f"{prefix}_signal_spread"] = sig["spread"]
                    rec[f"{prefix}_signal_overround"] = sig["overround"]
                    rec[f"{prefix}_signal_ts"] = sig["ts"]
                    rec[f"{prefix}_quote_price"] = fill["price"]
                    rec[f"{prefix}_quote_size"] = fill["size"]
                    rec[f"{prefix}_quote_spread"] = fill["spread"]
                    rec[f"{prefix}_quote_overround"] = fill["overround"]
                    rec[f"{prefix}_quote_ts"] = fill["ts"]
                    rec[f"{prefix}_adverse_slippage"] = (
                        np.nan
                        if pd.isna(sig["price"]) or pd.isna(fill["price"])
                        else float(fill["price"] - sig["price"])
                    )
        records.append(rec)
    exec_df = pd.DataFrame(records)
    return features.merge(exec_df, on="market_id", how="left")


def prepare_complete_data(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    coverage = coverage.sort_values("run_name").reset_index(drop=True)
    complete_runs = complete_runs_from_coverage(coverage)
    if len(complete_runs) < MIN_TRAIN_RUNS + 2:
        raise RuntimeError(f"Need at least {MIN_TRAIN_RUNS + 2} complete runs, found {len(complete_runs)}")

    raw_complete = raw[raw["run_name"].astype(str).isin(complete_runs)].copy()
    quotes = base.prepare_quotes(raw_complete)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable event features built from complete monthly_runs")
    features = v1.add_session_labels(features)
    features = add_candidate_factors(features)
    features = attach_execution_snapshots(features, quotes, delays=(4, 8), minutes=(1, 2))
    features = features[pd.notna(features["outcome_up"])].copy()
    features = features.sort_values("first_quote_ts").reset_index(drop=True)
    coverage["is_complete_run"] = coverage["run_name"].astype(str).isin(complete_runs)
    return features, coverage, complete_runs


def make_run_folds(complete_runs: List[str], min_train_runs: int = MIN_TRAIN_RUNS) -> List[Dict[str, object]]:
    folds: List[Dict[str, object]] = []
    for i in range(min_train_runs, len(complete_runs) - 1):
        folds.append(
            {
                "fold_id": len(folds) + 1,
                "train_runs": complete_runs[:i],
                "validation_run": complete_runs[i],
                "test_run": complete_runs[i + 1],
            }
        )
    return folds


def factor_thresholds(train: pd.DataFrame, spec: CandidateSpec, q: float | None) -> Tuple[float, float]:
    if spec.factor_col is None:
        return float("nan"), float("nan")
    vals = as_num(train[spec.factor_col]).dropna()
    if len(vals) < 100 or vals.nunique() < 10 or q is None:
        return float("nan"), float("nan")
    hi = float(vals.quantile(q))
    lo = float(vals.quantile(1.0 - q))
    if not math.isfinite(hi) or not math.isfinite(lo) or hi <= lo:
        return float("nan"), float("nan")
    return hi, lo


def signal_masks(df: pd.DataFrame, spec: CandidateSpec, hi: float, lo: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(df)
    if spec.factor_col is None:
        down = (as_num(df["btc_move_1m"]) <= -20.0).to_numpy(dtype=bool)
        up = np.zeros(n, dtype=bool)
        vals = np.full(n, np.nan, dtype=float)
        return up, down, vals
    vals_s = as_num(df[spec.factor_col])
    vals = vals_s.to_numpy(dtype=float)
    up = (vals_s >= hi).fillna(False).to_numpy(dtype=bool)
    down = (vals_s <= lo).fillna(False).to_numpy(dtype=bool)
    return up, down, vals


def window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray, window: int) -> Dict[str, float]:
    if len(event_returns) < window:
        return {
            f"worst_{window}_return": np.nan,
            f"median_{window}_return": np.nan,
            f"positive_{window}_rate": np.nan,
            f"active_{window}_rate": np.nan,
        }
    vals, active = [], []
    for i in range(0, len(event_returns) - window + 1):
        vals.append(float(np.prod(1.0 + event_returns[i : i + window]) - 1.0))
        active.append(float(np.sum(trade_flags[i : i + window]) > 0))
    arr = np.array(vals, dtype=float)
    act = np.array(active, dtype=float)
    return {
        f"worst_{window}_return": float(np.min(arr)),
        f"median_{window}_return": float(np.median(arr)),
        f"positive_{window}_rate": float(np.mean(arr > 0)),
        f"active_{window}_rate": float(np.mean(act > 0)),
    }


def simulate_candidate(
    df: pd.DataFrame,
    spec: CandidateSpec,
    cfg: ExecutionConfig,
    hi: float = float("nan"),
    lo: float = float("nan"),
    q_threshold: float | None = None,
    capture_logs: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, object]]:
    d = df.sort_values("first_quote_ts").reset_index(drop=True).copy()
    up_signal, down_signal, factor_values = signal_masks(d, spec, hi, lo)
    signal = up_signal | down_signal
    signals = int(signal.sum())
    side = np.where(up_signal, "buy_up", np.where(down_signal, "buy_down", "skip"))
    prefix_up = f"exec_buy_up_{spec.entry_minute}m_d{cfg.delay_seconds}"
    prefix_down = f"exec_buy_down_{spec.entry_minute}m_d{cfg.delay_seconds}"

    quote_price = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_quote_price"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_quote_price"]), np.nan),
    ).astype(float)
    signal_price = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_signal_price"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_signal_price"]), np.nan),
    ).astype(float)
    quote_size = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_quote_size"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_quote_size"]), np.nan),
    ).astype(float)
    quote_spread = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_quote_spread"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_quote_spread"]), np.nan),
    ).astype(float)
    quote_overround = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_quote_overround"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_quote_overround"]), np.nan),
    ).astype(float)
    adverse_slippage = np.where(
        up_signal,
        as_num(d[f"{prefix_up}_adverse_slippage"]),
        np.where(down_signal, as_num(d[f"{prefix_down}_adverse_slippage"]), np.nan),
    ).astype(float)

    fill_price = quote_price + cfg.fixed_slippage
    outcome = as_num(d["outcome_up"]).to_numpy(dtype=float)
    eligible = (
        signal
        & np.isfinite(signal_price)
        & np.isfinite(quote_price)
        & np.isfinite(fill_price)
        & np.isfinite(quote_size)
        & np.isfinite(quote_spread)
        & np.isfinite(quote_overround)
        & np.isfinite(adverse_slippage)
        & np.isfinite(outcome)
        & (quote_price >= PRICE_MIN)
        & (quote_price <= PRICE_MAX)
        & (fill_price < FILL_PRICE_MAX)
        & (quote_size >= cfg.min_depth)
        & (quote_spread <= cfg.max_spread)
        & (quote_overround <= cfg.max_overround)
        & (adverse_slippage <= cfg.max_adverse_slippage)
    )

    bankroll, peak, max_dd = BANKROLL0, BANKROLL0, 0.0
    event_returns = np.zeros(len(d), dtype=float)
    trade_flags = np.zeros(len(d), dtype=int)
    logs: List[Dict[str, object]] = []
    recent_trades: List[int] = []
    loss_streak = 0
    cooldown = 0
    eligible_count = int(eligible.sum())

    for i in np.flatnonzero(signal):
        recent_trades = [x for x in recent_trades if x > i - QUOTA_WINDOW_EVENTS]
        if cooldown > 0:
            cooldown -= 1
            continue
        if len(recent_trades) >= MAX_TRADES_PER_WINDOW:
            continue
        if not eligible[i] or bankroll <= 0:
            continue
        capacity_cost = float(quote_size[i]) * FILL_RATIO * float(fill_price[i])
        cost = min(bankroll * cfg.fraction, bankroll, capacity_cost)
        if not math.isfinite(cost) or cost < MIN_COST or fill_price[i] <= 0:
            continue
        if side[i] == "buy_up":
            pnl_per_share = outcome[i] - fill_price[i] - FEE
        else:
            pnl_per_share = (1.0 - outcome[i]) - fill_price[i] - FEE
        pnl = (cost / fill_price[i]) * pnl_per_share
        before = bankroll
        bankroll += pnl
        event_ret = pnl / before if before > 0 else 0.0
        event_returns[i] = event_ret
        trade_flags[i] = 1
        recent_trades.append(i)
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= COOLDOWN_AFTER_LOSSES:
                cooldown = COOLDOWN_EVENTS
                loss_streak = 0
        else:
            loss_streak = 0
        if capture_logs:
            logs.append(
                {
                    "candidate": spec.name,
                    "family": spec.family,
                    "config_id": cfg.config_id,
                    "q_threshold": q_threshold,
                    "hi_value": hi,
                    "lo_value": lo,
                    "first_quote_ts": d.at[i, "first_quote_ts"],
                    "run_name": d.at[i, "run_name"],
                    "market_id": d.at[i, "market_id"],
                    "session_et": d.at[i, "session_et"] if "session_et" in d.columns else None,
                    "side": side[i],
                    "entry_minute": spec.entry_minute,
                    "delay_seconds": cfg.delay_seconds,
                    "factor_value": factor_values[i],
                    "signal_price": signal_price[i],
                    "quote_price": quote_price[i],
                    "fill_price": fill_price[i],
                    "quote_size": quote_size[i],
                    "quote_spread": quote_spread[i],
                    "quote_overround": quote_overround[i],
                    "adverse_slippage": adverse_slippage[i],
                    "fraction": cfg.fraction,
                    "target_cost": before * cfg.fraction,
                    "filled_cost": cost,
                    "pnl_usd": pnl,
                    "event_ret": event_ret,
                    "bankroll_after": bankroll,
                }
            )

    pnl_vals = [x["pnl_usd"] for x in logs] if capture_logs else []
    if capture_logs:
        pnl_s = pd.Series(pnl_vals, dtype=float)
    else:
        trade_idx = np.flatnonzero(trade_flags)
        pnl_s = pd.Series(BANKROLL0 * event_returns[trade_idx], dtype=float)
    wins = float(pnl_s[pnl_s > 0].sum()) if not pnl_s.empty else 0.0
    losses = float(pnl_s[pnl_s < 0].sum()) if not pnl_s.empty else 0.0
    trades = int(trade_flags.sum())
    metrics: Dict[str, object] = {
        "candidate": spec.name,
        "family": spec.family,
        "entry_minute": spec.entry_minute,
        "config_id": cfg.config_id,
        "q_threshold": q_threshold,
        "hi_value": hi,
        "lo_value": lo,
        "signals": signals,
        "eligible_fills": eligible_count,
        "trades": trades,
        "fill_rate_vs_signals": float(eligible_count / signals) if signals else np.nan,
        "trade_rate_vs_signals": float(trades / signals) if signals else np.nan,
        "ending_bankroll": float(bankroll),
        "total_return": float(bankroll / BANKROLL0 - 1.0),
        "max_drawdown": float(max_dd),
        "win_rate": float((pnl_s > 0).mean()) if len(pnl_s) else np.nan,
        "avg_pnl": float(pnl_s.mean()) if len(pnl_s) else np.nan,
        "profit_factor": float(wins / abs(losses)) if losses != 0 else np.nan,
        **asdict(cfg),
        **window_metrics(event_returns, trade_flags, QUOTA_WINDOW_EVENTS),
    }
    return pd.DataFrame(logs), metrics


def rank_validation(metrics: pd.DataFrame) -> pd.DataFrame:
    out = metrics.copy()
    if out.empty:
        return out
    enough = out["trades"].fillna(0) >= 2
    score_base = out[enough].copy() if enough.any() else out.copy()
    score_base["score_return"] = score_base["total_return"].rank(pct=True)
    score_base["score_dd"] = (-score_base["max_drawdown"].fillna(999.0)).rank(pct=True)
    pf = score_base["profit_factor"].replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(upper=5.0)
    score_base["score_pf"] = pf.rank(pct=True)
    score_base["score_fill"] = score_base["fill_rate_vs_signals"].fillna(0.0).rank(pct=True)
    score_base["score_trades"] = (score_base["trades"].fillna(0).clip(upper=8) / 8.0).astype(float)
    score_base["validation_score"] = (
        0.35 * score_base["score_return"]
        + 0.25 * score_base["score_dd"]
        + 0.20 * score_base["score_pf"]
        + 0.10 * score_base["score_fill"]
        + 0.10 * score_base["score_trades"]
    )
    return score_base.sort_values(["validation_score", "total_return"], ascending=False).reset_index(drop=True)


def evaluate_validation_grid(
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    spec: CandidateSpec,
    configs: List[ExecutionConfig],
) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    q_values: Tuple[float | None, ...] = (None,) if spec.factor_col is None else tuple(spec.quantiles)
    for q in q_values:
        hi, lo = factor_thresholds(train_df, spec, q)
        if spec.factor_col is not None and (not math.isfinite(hi) or not math.isfinite(lo)):
            continue
        for cfg in configs:
            _, met = simulate_candidate(val_df, spec, cfg, hi=hi, lo=lo, q_threshold=q, capture_logs=False)
            rows.append(met)
    return pd.DataFrame(rows)


def run_walk_forward(
    features: pd.DataFrame,
    folds: List[Dict[str, object]],
    specs: List[CandidateSpec],
    configs: List[ExecutionConfig],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    all_val_grid: List[pd.DataFrame] = []
    selected_rows: List[Dict[str, object]] = []
    selected_perf: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []

    for fold in folds:
        fold_id = int(fold["fold_id"])
        train_runs = list(fold["train_runs"])
        val_run = str(fold["validation_run"])
        test_run = str(fold["test_run"])
        train_df = features[features["run_name"].isin(train_runs)].copy()
        val_df = features[features["run_name"] == val_run].copy()
        test_df = features[features["run_name"] == test_run].copy()

        for spec in specs:
            val_grid = evaluate_validation_grid(train_df, val_df, spec, configs)
            if val_grid.empty:
                continue
            val_grid["fold_id"] = fold_id
            val_grid["validation_run"] = val_run
            val_grid["test_run"] = test_run
            all_val_grid.append(val_grid)

            ranked = rank_validation(val_grid)
            if ranked.empty:
                continue
            best = ranked.iloc[0].to_dict()
            cfg = ExecutionConfig(
                delay_seconds=int(best["delay_seconds"]),
                max_spread=float(best["max_spread"]),
                max_overround=float(best["max_overround"]),
                min_depth=float(best["min_depth"]),
                max_adverse_slippage=float(best["max_adverse_slippage"]),
                fixed_slippage=float(best["fixed_slippage"]),
                fraction=float(best["fraction"]),
            )
            hi = float(best["hi_value"]) if pd.notna(best.get("hi_value")) else float("nan")
            lo = float(best["lo_value"]) if pd.notna(best.get("lo_value")) else float("nan")
            q_threshold = None if pd.isna(best.get("q_threshold")) else float(best["q_threshold"])
            selected_rows.append(
                {
                    "fold_id": fold_id,
                    "candidate": spec.name,
                    "family": spec.family,
                    "train_runs": ",".join(train_runs),
                    "validation_run": val_run,
                    "test_run": test_run,
                    "selected_config_id": cfg.config_id,
                    "selected_q_threshold": q_threshold,
                    "selected_hi_value": hi,
                    "selected_lo_value": lo,
                    "validation_score": float(best.get("validation_score", np.nan)),
                    "validation_return": float(best["total_return"]),
                    "validation_trades": int(best["trades"]),
                    "validation_fill_rate": float(best["fill_rate_vs_signals"]) if pd.notna(best["fill_rate_vs_signals"]) else np.nan,
                    **asdict(cfg),
                }
            )

            for split_name, split_df, run_label in [
                ("train", train_df, "+".join(train_runs)),
                ("validation", val_df, val_run),
                ("test", test_df, test_run),
            ]:
                trade_log, met = simulate_candidate(
                    split_df,
                    spec,
                    cfg,
                    hi=hi,
                    lo=lo,
                    q_threshold=q_threshold,
                    capture_logs=True,
                )
                met["split"] = split_name
                met["fold_id"] = fold_id
                met["run_label"] = run_label
                selected_perf.append(met)
                if not trade_log.empty:
                    trade_log["split"] = split_name
                    trade_log["fold_id"] = fold_id
                    trade_log["run_label"] = run_label
                    logs.append(trade_log)

    val_grid_df = pd.concat(all_val_grid, ignore_index=True) if all_val_grid else pd.DataFrame()
    selections_df = pd.DataFrame(selected_rows)
    selected_perf_df = pd.DataFrame(selected_perf)
    logs_df = pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()
    return val_grid_df, selections_df, selected_perf_df, logs_df


def aggregate_selected_tests(selected_perf: pd.DataFrame, selections: pd.DataFrame) -> pd.DataFrame:
    test = selected_perf[selected_perf["split"] == "test"].copy()
    if test.empty:
        return pd.DataFrame()
    grp = test.groupby(["candidate", "family"], as_index=False).agg(
        folds_tested=("fold_id", "nunique"),
        avg_test_return=("total_return", "mean"),
        median_test_return=("total_return", "median"),
        min_test_return=("total_return", "min"),
        pct_positive_test=("total_return", lambda s: float((s > 0).mean())),
        avg_test_drawdown=("max_drawdown", "mean"),
        max_test_drawdown=("max_drawdown", "max"),
        avg_test_trades=("trades", "mean"),
        avg_test_fill_rate=("fill_rate_vs_signals", "mean"),
        avg_test_trade_rate=("trade_rate_vs_signals", "mean"),
        avg_test_win_rate=("win_rate", "mean"),
        avg_profit_factor=("profit_factor", "mean"),
    )
    sel_count = selections.groupby("candidate", as_index=False).size().rename(columns={"size": "selected_count"})
    grp = grp.merge(sel_count, on="candidate", how="left")
    grp["selected_count"] = grp["selected_count"].fillna(0).astype(int)
    grp["execution_wf_score"] = (
        0.30 * grp["avg_test_return"].rank(pct=True)
        + 0.25 * grp["pct_positive_test"].rank(pct=True)
        + 0.20 * (-grp["max_test_drawdown"].fillna(999.0)).rank(pct=True)
        + 0.15 * grp["avg_profit_factor"].replace([np.inf, -np.inf], np.nan).fillna(0.0).rank(pct=True)
        + 0.10 * grp["avg_test_fill_rate"].fillna(0.0).rank(pct=True)
    )
    return grp.sort_values(["execution_wf_score", "avg_test_return"], ascending=False).reset_index(drop=True)


def selected_config_summary(selections: pd.DataFrame, selected_perf: pd.DataFrame) -> pd.DataFrame:
    if selections.empty:
        return pd.DataFrame()
    test = selected_perf[selected_perf["split"] == "test"][
        ["fold_id", "candidate", "total_return", "max_drawdown", "trades", "fill_rate_vs_signals"]
    ].rename(
        columns={
            "total_return": "test_return",
            "max_drawdown": "test_drawdown",
            "trades": "test_trades",
            "fill_rate_vs_signals": "test_fill_rate",
        }
    )
    merged = selections.merge(test, on=["fold_id", "candidate"], how="left")
    keys = [
        "candidate",
        "selected_q_threshold",
        "delay_seconds",
        "max_spread",
        "max_overround",
        "min_depth",
        "max_adverse_slippage",
        "fixed_slippage",
        "fraction",
    ]
    return (
        merged.groupby(keys, dropna=False, as_index=False)
        .agg(
            selected_folds=("fold_id", "nunique"),
            avg_validation_return=("validation_return", "mean"),
            avg_test_return=("test_return", "mean"),
            pct_positive_test=("test_return", lambda s: float((s > 0).mean())),
            avg_test_drawdown=("test_drawdown", "mean"),
            avg_test_trades=("test_trades", "mean"),
            avg_test_fill_rate=("test_fill_rate", "mean"),
        )
        .sort_values(["candidate", "selected_folds", "avg_test_return"], ascending=[True, False, False])
        .reset_index(drop=True)
    )


def latest_complete_test(selected_perf: pd.DataFrame) -> pd.DataFrame:
    test = selected_perf[selected_perf["split"] == "test"].copy()
    if test.empty:
        return pd.DataFrame()
    max_fold = int(test["fold_id"].max())
    return test[test["fold_id"] == max_fold].sort_values("total_return", ascending=False).reset_index(drop=True)


def md_table(df: pd.DataFrame, n: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(n).copy()
    nums = show.select_dtypes(include=[np.number]).columns
    show[nums] = show[nums].round(4)
    return show.to_markdown(index=False)


def make_plots(outdir: Path, summary: pd.DataFrame, latest_test: pd.DataFrame) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    count = 0
    if not summary.empty:
        plt.figure(figsize=(10, 4))
        plt.bar(summary["candidate"], summary["avg_test_return"])
        plt.xticks(rotation=20, ha="right")
        plt.title("Execution walk-forward average test return")
        plt.tight_layout()
        plt.savefig(figdir / "avg_test_return_by_candidate.png", dpi=150)
        plt.close()
        count += 1

        plt.figure(figsize=(10, 4))
        plt.bar(summary["candidate"], summary["pct_positive_test"])
        plt.xticks(rotation=20, ha="right")
        plt.title("Execution walk-forward positive test fold rate")
        plt.tight_layout()
        plt.savefig(figdir / "positive_test_rate_by_candidate.png", dpi=150)
        plt.close()
        count += 1
    if not latest_test.empty:
        plt.figure(figsize=(10, 4))
        plt.bar(latest_test["candidate"], latest_test["total_return"])
        plt.xticks(rotation=20, ha="right")
        plt.title("Latest complete run selected-config return")
        plt.tight_layout()
        plt.savefig(figdir / "latest_complete_run_returns.png", dpi=150)
        plt.close()
        count += 1
    return count


def build_report(
    coverage: pd.DataFrame,
    complete_runs: List[str],
    folds: List[Dict[str, object]],
    specs: List[CandidateSpec],
    summary: pd.DataFrame,
    selections: pd.DataFrame,
    config_summary: pd.DataFrame,
    selected_perf: pd.DataFrame,
    latest_test: pd.DataFrame,
) -> str:
    lines: List[str] = []
    lines.append("# Candidate execution walk-forward")
    lines.append("")
    lines.append("## Scope")
    lines.append("This run tests three previously shortlisted candidates with run-level walk-forward validation and stricter execution assumptions.")
    lines.append("")
    lines.append("## Candidates")
    for spec in specs:
        lines.append(f"- `{spec.name}`: {spec.description}")
    lines.append("")
    lines.append("## Execution assumptions")
    lines.append("- Signal uses the 1m or 2m snapshot, then fills at the first later quote after a 4s or 8s delay.")
    lines.append("- A fill is rejected if the delayed quote moves against the signal by more than the selected limit.")
    lines.append("- The selected fill quote receives an added adverse slippage of 0.25c or 0.75c.")
    lines.append("- Fill must pass spread, overround, depth, and price bounds at the delayed quote.")
    lines.append("- Only 25% of visible size is treated as fillable, with 1% or 2% bankroll sizing.")
    lines.append("- Max 10 trades per rolling 144-event window; after two losses, skip the next six events.")
    lines.append("")
    lines.append("## Data coverage")
    lines.append(md_table(coverage, 120))
    lines.append("")
    lines.append("## Complete runs used")
    lines.append("`" + "`, `".join(complete_runs) + "`")
    lines.append("")
    lines.append("## Fold design")
    lines.append(md_table(pd.DataFrame(folds), 80))
    lines.append("")
    lines.append("## Test summary")
    lines.append(md_table(summary, 20))
    lines.append("")
    lines.append("## Latest complete run test")
    lines.append(md_table(latest_test, 20))
    lines.append("")
    lines.append("## Selected configs by fold")
    lines.append(md_table(selections, 80))
    lines.append("")
    lines.append("## Selected config summary")
    lines.append(md_table(config_summary, 80))
    lines.append("")
    lines.append("## Selected split performance sample")
    lines.append(md_table(selected_perf, 80))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--report-dir", required=True)
    args = parser.parse_args()

    features, coverage, complete_runs = prepare_complete_data(Path(args.source_root))
    folds = make_run_folds(complete_runs)
    specs = candidate_registry()
    configs = execution_config_grid()
    val_grid, selections, selected_perf, trade_logs = run_walk_forward(features, folds, specs, configs)
    summary = aggregate_selected_tests(selected_perf, selections)
    config_summary = selected_config_summary(selections, selected_perf)
    latest_test = latest_complete_test(selected_perf)

    outdir = Path(args.report_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(outdir, summary, latest_test)

    coverage.to_csv(outdir / "coverage_complete_runs.csv", index=False)
    pd.DataFrame(folds).to_csv(outdir / "execution_walk_forward_folds.csv", index=False)
    val_grid.to_csv(outdir / "validation_grid_metrics.csv", index=False)
    selections.to_csv(outdir / "selected_configs_by_fold.csv", index=False)
    selected_perf.to_csv(outdir / "selected_config_split_metrics.csv", index=False)
    trade_logs.to_csv(outdir / "selected_config_trade_logs.csv", index=False)
    summary.to_csv(outdir / "candidate_execution_summary.csv", index=False)
    config_summary.to_csv(outdir / "candidate_config_summary.csv", index=False)
    latest_test.to_csv(outdir / "latest_complete_run_selected_configs.csv", index=False)
    (outdir / "execution_walk_forward_report.md").write_text(
        build_report(
            coverage,
            complete_runs,
            folds,
            specs,
            summary,
            selections,
            config_summary,
            selected_perf,
            latest_test,
        ),
        encoding="utf-8",
    )
    meta = {
        "complete_run_count": len(complete_runs),
        "fold_count": len(folds),
        "feature_rows": int(len(features)),
        "candidate_count": len(specs),
        "execution_config_count": len(configs),
        "validation_grid_rows": int(len(val_grid)),
        "selected_metric_rows": int(len(selected_perf)),
        "trade_log_rows": int(len(trade_logs)),
        "figure_count": fig_count,
        "price_min": PRICE_MIN,
        "price_max": PRICE_MAX,
        "fill_price_max": FILL_PRICE_MAX,
        "fill_ratio": FILL_RATIO,
        "quota_window_events": QUOTA_WINDOW_EVENTS,
        "max_trades_per_window": MAX_TRADES_PER_WINDOW,
        "cooldown_after_losses": COOLDOWN_AFTER_LOSSES,
        "cooldown_events": COOLDOWN_EVENTS,
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
