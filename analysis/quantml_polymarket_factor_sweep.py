from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1

FEE = 0.01
BANKROLL0 = 100.0
LATEST_72H_EVENTS = 864
WINDOW_36H = 144


@dataclass(frozen=True)
class FactorSpec:
    name: str
    family: str
    formula: str
    entry_minute: int = 2


def as_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)


def clip01(s: pd.Series) -> pd.Series:
    return as_num(s).clip(0.0, 1.0)


def signed_log1p(s: pd.Series) -> pd.Series:
    x = as_num(s).fillna(0.0)
    return np.sign(x) * np.log1p(np.abs(x))


def past_rolling_z(s: pd.Series, window: int, min_periods: int | None = None) -> pd.Series:
    x = as_num(s)
    minp = min_periods or max(5, window // 3)
    mean = x.shift(1).rolling(window, min_periods=minp).mean()
    std = x.shift(1).rolling(window, min_periods=minp).std(ddof=0)
    return ((x - mean) / std.replace(0.0, np.nan)).replace([np.inf, -np.inf], np.nan)


def safe_auc(y: pd.Series, x: pd.Series) -> float:
    yy = as_num(y)
    xx = as_num(x)
    mask = yy.notna() & xx.notna()
    if int(mask.sum()) < 20 or yy[mask].nunique() < 2:
        return float("nan")
    try:
        return float(roc_auc_score(yy[mask].astype(int), xx[mask]))
    except Exception:
        return float("nan")


def safe_corr(y: pd.Series, x: pd.Series) -> float:
    yy = as_num(y)
    xx = as_num(x)
    mask = yy.notna() & xx.notna()
    if int(mask.sum()) < 20 or yy[mask].nunique() < 2 or xx[mask].nunique() < 2:
        return float("nan")
    return float(np.corrcoef(yy[mask], xx[mask])[0, 1])


def prepare_features(source_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    raw, coverage = base.read_all_monthly_runs(source_root)
    quotes = base.prepare_quotes(raw)
    features = base.build_features(quotes)
    if features.empty:
        raise RuntimeError("No usable event features built from monthly_runs")

    features = v1.add_session_labels(features)
    features = features.sort_values("first_quote_ts").reset_index(drop=True)

    spread_up = as_num(features["spread_up_median_first2m"]).fillna(1.0)
    spread_down = as_num(features["spread_down_median_first2m"]).fillna(1.0)
    overround = as_num(features["overround_median_first2m"]).fillna(1.0)
    features["gate_up_quantml"] = (
        (spread_up <= 0.06)
        & (overround <= 0.05)
        & (as_num(features["buy_up_size_2m"]).fillna(0) >= 80)
        & (as_num(features["buy_up_price_2m"]).between(0.05, 0.95))
    )
    features["gate_down_quantml"] = (
        (spread_down <= 0.06)
        & (overround <= 0.05)
        & (as_num(features["buy_down_size_2m"]).fillna(0) >= 80)
        & (as_num(features["buy_down_price_2m"]).between(0.05, 0.95))
    )
    return features, coverage.sort_values("run_name").reset_index(drop=True)


def add_skill_factors(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[FactorSpec]]:
    out = df.copy()
    specs: List[FactorSpec] = []

    btc2 = as_num(out["btc_move_2m"])
    pm_mom = as_num(out["mid_up_prob_change_2m"])
    directional_move = btc2 / 50.0 + pm_mom / 0.05
    vol = np.log1p(as_num(out["trade_volume_sum_first2m"]).fillna(0.0))
    vol_ratio = vol - vol.shift(1).rolling(60, min_periods=12).mean()

    out["skill_trend_momentum_20"] = past_rolling_z(directional_move, 20) * np.sign(vol_ratio.fillna(0.0))
    specs.append(
        FactorSpec(
            "skill_trend_momentum_20",
            "quantml_skill",
            "zscore_20(btc_move_2m/50 + pm_prob_mom_2m/0.05) * sign(volume_ratio_5_60)",
        )
    )

    mean_reversion = -past_rolling_z(directional_move, 20)
    vol_cut = as_num(out["realized_vol_first2m"]).shift(1).rolling(60, min_periods=20).quantile(0.80)
    high_vol = as_num(out["realized_vol_first2m"]) > vol_cut
    out["skill_mean_reversion_5"] = mean_reversion.mask(high_vol, 0.0)
    specs.append(
        FactorSpec(
            "skill_mean_reversion_5",
            "quantml_skill",
            "-zscore_20(short_move), disabled when realized_vol_first2m > past60 80pct",
        )
    )

    out["skill_volume_imbalance"] = as_num(out["size_imbalance_updown_2m"]) * past_rolling_z(vol, 60)
    specs.append(
        FactorSpec(
            "skill_volume_imbalance",
            "quantml_skill",
            "size_imbalance_updown_2m * zscore_60(log1p(trade_volume_first2m))",
        )
    )

    out["skill_composite_20_5_60"] = (
        0.40 * as_num(out["skill_trend_momentum_20"]).fillna(0.0)
        + 0.35 * as_num(out["skill_mean_reversion_5"]).fillna(0.0)
        + 0.25 * as_num(out["skill_volume_imbalance"]).fillna(0.0)
    )
    specs.append(
        FactorSpec(
            "skill_composite_20_5_60",
            "quantml_skill",
            "0.40*trend_momentum_20 + 0.35*mean_reversion_5 + 0.25*volume_imbalance",
        )
    )
    return out, specs


def add_expression_factors(df: pd.DataFrame) -> Tuple[pd.DataFrame, List[FactorSpec]]:
    out = df.copy()
    specs: List[FactorSpec] = []

    spread_min = np.minimum(
        as_num(out["spread_up_median_first2m"]).fillna(1.0),
        as_num(out["spread_down_median_first2m"]).fillna(1.0),
    )
    low_cost = (1.0 - as_num(out["overround_median_first2m"]).fillna(1.0) / 0.08).clip(0.0, 1.0)
    tight_spread = (1.0 - as_num(spread_min).fillna(1.0) / 0.08).clip(0.0, 1.0)
    uncertainty = (1.0 - (as_num(out["mid_up_prob_2m"]).fillna(0.5) - 0.5).abs() * 2.0).clip(0.0, 1.0)
    depth_total = (
        as_num(out["buy_up_size_2m"]).fillna(0.0)
        + as_num(out["buy_down_size_2m"]).fillna(0.0)
        + as_num(out["sell_up_size_2m"]).fillna(0.0)
        + as_num(out["sell_down_size_2m"]).fillna(0.0)
    )
    log_depth = np.log1p(depth_total)
    depth_z = past_rolling_z(log_depth, 60).fillna(0.0)
    path_eff = clip01(out["path_efficiency_first2m"]).fillna(0.0)

    primitives: Dict[str, Tuple[pd.Series, str, int]] = {
        "expr_btc_1m": (as_num(out["btc_move_1m"]) / 50.0, "btc_move_1m / 50", 1),
        "expr_btc_2m": (as_num(out["btc_move_2m"]) / 50.0, "btc_move_2m / 50", 2),
        "expr_btc_4m": (as_num(out["btc_move_4m"]) / 50.0, "btc_move_4m / 50", 4),
        "expr_pm_mom_2m": (as_num(out["mid_up_prob_change_2m"]) / 0.05, "mid_up_prob_change_2m / 0.05", 2),
        "expr_prior_up_2m": (as_num(out["mid_up_prob_2m"]) - 0.5, "mid_up_prob_2m - 0.5", 2),
        "expr_size_imbalance": (as_num(out["size_imbalance_updown_2m"]), "buy_up_vs_down_size_imbalance_2m", 2),
        "expr_sell_size_imbalance": (as_num(out["sell_size_imbalance_updown_2m"]), "sell_up_vs_down_size_imbalance_2m", 2),
        "expr_depth_imbalance": (as_num(out["bid_depth_imbalance_updown_2m"]), "bid_depth_up_vs_down_imbalance_2m", 2),
        "expr_book_pressure_delta": (
            as_num(out["book_pressure_up_2m"]) - as_num(out["book_pressure_down_2m"]),
            "book_pressure_up_2m - book_pressure_down_2m",
            2,
        ),
    }

    for name, (values, formula, minute) in primitives.items():
        out[name] = values
        specs.append(FactorSpec(name, "alphaforge_primitive", formula, minute))
        out[f"{name}_tanh"] = np.tanh(as_num(values))
        specs.append(FactorSpec(f"{name}_tanh", "alphaforge_unary", f"tanh({formula})", minute))
        out[f"{name}_signed_log"] = signed_log1p(values)
        specs.append(FactorSpec(f"{name}_signed_log", "alphaforge_unary", f"signed_log1p({formula})", minute))
        out[f"{name}_rollz60"] = past_rolling_z(values, 60)
        specs.append(FactorSpec(f"{name}_rollz60", "alphaforge_unary", f"past_zscore_60({formula})", minute))

    interaction_terms = {
        "x_path_eff": (path_eff, "path_efficiency_first2m"),
        "x_uncertainty": (uncertainty, "1 - abs(mid_up_prob_2m - 0.5)*2"),
        "x_low_cost": (low_cost, "1 - overround/0.08"),
        "x_tight_spread": (tight_spread, "1 - min_spread/0.08"),
        "x_depth_z": (depth_z, "past_zscore_60(log_depth)"),
    }
    for base_name, (values, formula, minute) in primitives.items():
        if base_name == "expr_btc_4m":
            allowed = {"x_path_eff", "x_low_cost", "x_tight_spread"}
        else:
            allowed = set(interaction_terms)
        for suffix, (modifier, mod_formula) in interaction_terms.items():
            if suffix not in allowed:
                continue
            name = f"{base_name}{suffix}"
            out[name] = as_num(values) * as_num(modifier)
            specs.append(FactorSpec(name, "alphaforge_interaction", f"({formula}) * ({mod_formula})", max(minute, 2)))

    pair_defs = [
        ("expr_btc_2m", "expr_pm_mom_2m", "product"),
        ("expr_btc_2m", "expr_size_imbalance", "product"),
        ("expr_btc_2m", "expr_book_pressure_delta", "product"),
        ("expr_pm_mom_2m", "expr_size_imbalance", "product"),
        ("expr_pm_mom_2m", "expr_depth_imbalance", "product"),
        ("expr_size_imbalance", "expr_depth_imbalance", "product"),
        ("expr_book_pressure_delta", "expr_depth_imbalance", "product"),
        ("expr_prior_up_2m", "expr_pm_mom_2m", "sum"),
        ("expr_btc_2m", "expr_pm_mom_2m", "sum"),
        ("expr_btc_2m", "expr_prior_up_2m", "sum"),
    ]
    for left, right, op in pair_defs:
        if left not in out or right not in out:
            continue
        if op == "product":
            name = f"{left}__mul__{right}"
            out[name] = as_num(out[left]) * as_num(out[right])
            formula = f"{left} * {right}"
        else:
            name = f"{left}__plus__{right}"
            out[name] = as_num(out[left]) + as_num(out[right])
            formula = f"{left} + {right}"
        specs.append(FactorSpec(name, "alphaforge_pair", formula, 2))

    return out, specs


def split_dataset(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    n = len(df)
    i1 = max(1, int(n * 0.50))
    i2 = max(i1 + 1, int(n * 0.75))
    return {
        "train": df.iloc[:i1].copy(),
        "validation": df.iloc[i1:i2].copy(),
        "test": df.iloc[i2:].copy(),
        "latest72h": df.iloc[-LATEST_72H_EVENTS:].copy() if n >= LATEST_72H_EVENTS else df.copy(),
        "full": df.copy(),
    }


def orient_specs(df_train: pd.DataFrame, specs: Iterable[FactorSpec]) -> Dict[str, int]:
    orientation: Dict[str, int] = {}
    y = as_num(df_train["outcome_up"])
    for spec in specs:
        ic = safe_corr(y, df_train.get(spec.name, pd.Series(dtype=float)))
        orientation[spec.name] = -1 if pd.notna(ic) and ic < 0 else 1
    return orientation


def factor_diagnostics(splits: Dict[str, pd.DataFrame], specs: List[FactorSpec], orientation: Dict[str, int]) -> pd.DataFrame:
    rows: List[Dict[str, object]] = []
    for spec in specs:
        row: Dict[str, object] = {
            "factor": spec.name,
            "family": spec.family,
            "entry_minute": spec.entry_minute,
            "formula": spec.formula,
            "orientation": orientation.get(spec.name, 1),
        }
        for split_name, split_df in splits.items():
            x = as_num(split_df.get(spec.name, pd.Series(dtype=float))) * row["orientation"]
            y = as_num(split_df["outcome_up"])
            row[f"{split_name}_n"] = int((x.notna() & y.notna()).sum())
            row[f"{split_name}_ic"] = safe_corr(y, x)
            row[f"{split_name}_auc"] = safe_auc(y, x)
        rows.append(row)
    return pd.DataFrame(rows)


def window_metrics(event_returns: np.ndarray, trade_flags: np.ndarray, window: int) -> Dict[str, float]:
    if len(event_returns) < window:
        return {f"worst_{window}_return": np.nan, f"median_{window}_return": np.nan, f"positive_{window}_rate": np.nan}
    vals = []
    for i in range(0, len(event_returns) - window + 1):
        vals.append(float(np.prod(1.0 + event_returns[i:i + window]) - 1.0))
    arr = np.array(vals, dtype=float)
    return {
        f"worst_{window}_return": float(np.nanmin(arr)),
        f"median_{window}_return": float(np.nanmedian(arr)),
        f"positive_{window}_rate": float(np.nanmean(arr > 0)),
        f"active_{window}_rate": float(np.nanmean([np.sum(trade_flags[i:i + window]) > 0 for i in range(0, len(event_returns) - window + 1)])),
    }


def simulate_threshold_strategy(
    df: pd.DataFrame,
    spec: FactorSpec,
    factor_col: str,
    hi: float,
    lo: float,
    q_level: float | None = None,
    fraction: float = 0.04,
    capture_logs: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, float]]:
    strategy_name = f"{spec.name}__q{int(round((q_level if q_level is not None else hi) * 100))}"
    d = df.reset_index(drop=True)
    vals = as_num(d[factor_col])
    up_mask = (vals >= hi) & d["gate_up_quantml"].fillna(False).astype(bool)
    down_mask = (vals <= lo) & d["gate_down_quantml"].fillna(False).astype(bool)
    signal = up_mask | down_mask

    up_price = as_num(d[f"buy_up_price_{spec.entry_minute}m"])
    down_price = as_num(d[f"buy_down_price_{spec.entry_minute}m"])
    up_size = as_num(d[f"buy_up_size_{spec.entry_minute}m"])
    down_size = as_num(d[f"buy_down_size_{spec.entry_minute}m"])
    price = pd.Series(np.where(up_mask, up_price, np.where(down_mask, down_price, np.nan)))
    size = pd.Series(np.where(up_mask, up_size, np.where(down_mask, down_size, np.nan)))
    outcome = as_num(d["outcome_up"])
    pnl_per_share = pd.Series(np.where(up_mask, outcome - price - FEE, (1.0 - outcome) - price - FEE))
    valid = signal & price.notna() & size.notna() & outcome.notna() & (price > 0) & (size > 0)

    event_returns = np.zeros(len(d), dtype=float)
    # Liquidity gates make a 4% bankroll stake non-binding in normal cases; this keeps the sweep fast.
    event_returns[valid.to_numpy()] = (
        fraction * pnl_per_share[valid].to_numpy(dtype=float) / price[valid].to_numpy(dtype=float)
    )
    trade_flags = valid.to_numpy(dtype=int)
    wealth_rel = np.cumprod(1.0 + event_returns) if len(event_returns) else np.array([], dtype=float)
    bankroll = float(BANKROLL0 * wealth_rel[-1]) if len(wealth_rel) else BANKROLL0
    if len(wealth_rel):
        peak = np.maximum.accumulate(wealth_rel)
        max_dd = float(np.nanmax((peak - wealth_rel) / peak))
        before_rel = np.concatenate([[1.0], wealth_rel[:-1]])
    else:
        max_dd = 0.0
        before_rel = np.array([], dtype=float)

    trade_idx = np.flatnonzero(valid.to_numpy())
    pnl_vals = BANKROLL0 * before_rel[trade_idx] * event_returns[trade_idx] if len(trade_idx) else np.array([], dtype=float)
    pnl_s = pd.Series(pnl_vals, dtype=float)
    trade_log = pd.DataFrame()
    if capture_logs and len(trade_idx):
        trade_log = pd.DataFrame(
            {
                "strategy": strategy_name,
                "factor": spec.name,
                "family": spec.family,
                "first_quote_ts": d.loc[trade_idx, "first_quote_ts"].to_numpy(),
                "run_name": d.loc[trade_idx, "run_name"].to_numpy(),
                "market_id": d.loc[trade_idx, "market_id"].to_numpy(),
                "session_et": d.loc[trade_idx, "session_et"].to_numpy(),
                "side": np.where(up_mask.to_numpy()[trade_idx], "buy_up", "buy_down"),
                "entry_minute": spec.entry_minute,
                "factor_value": vals.iloc[trade_idx].to_numpy(),
                "entry_price": price.iloc[trade_idx].to_numpy(),
                "target_cost": BANKROLL0 * before_rel[trade_idx] * fraction,
                "pnl_usd": pnl_vals,
                "event_ret": event_returns[trade_idx],
                "bankroll_after": BANKROLL0 * wealth_rel[trade_idx],
            }
        )
    wins = float(pnl_s[pnl_s > 0].sum()) if not pnl_s.empty else 0.0
    losses = float(pnl_s[pnl_s < 0].sum()) if not pnl_s.empty else 0.0
    metrics = {
        "strategy": strategy_name,
        "factor": spec.name,
        "family": spec.family,
        "entry_minute": spec.entry_minute,
        "q_threshold": float(q_level) if q_level is not None else float("nan"),
        "trades": int(len(trade_idx)),
        "ending_bankroll": float(bankroll),
        "total_return": float(bankroll / BANKROLL0 - 1.0),
        "max_drawdown": float(max_dd),
        "win_rate": float((pnl_s > 0).mean()) if len(pnl_s) else np.nan,
        "avg_pnl": float(pnl_s.mean()) if len(pnl_s) else np.nan,
        "profit_factor": float(wins / abs(losses)) if losses != 0 else np.nan,
        **window_metrics(event_returns, trade_flags, WINDOW_36H),
    }
    return trade_log, metrics


def evaluate_strategies(
    splits: Dict[str, pd.DataFrame],
    specs: List[FactorSpec],
    orientation: Dict[str, int],
    quantiles: Iterable[float],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    train = splits["train"].copy()
    rows: List[Dict[str, object]] = []
    logs: List[pd.DataFrame] = []
    for spec in specs:
        oriented_col = f"oriented__{spec.name}"
        train_vals = as_num(train.get(spec.name, pd.Series(dtype=float))) * orientation.get(spec.name, 1)
        if int(train_vals.notna().sum()) < 100 or train_vals.nunique(dropna=True) < 10:
            continue
        for q in quantiles:
            hi = float(train_vals.quantile(q))
            lo = float(train_vals.quantile(1.0 - q))
            if not math.isfinite(hi) or not math.isfinite(lo) or hi <= lo:
                continue
            for split_name, split_df in splits.items():
                d = split_df.copy()
                d[oriented_col] = as_num(d.get(spec.name, pd.Series(dtype=float))) * orientation.get(spec.name, 1)
                lg, met = simulate_threshold_strategy(d, spec, oriented_col, hi, lo, q_level=q)
                met["split"] = split_name
                met["hi_value"] = hi
                met["lo_value"] = lo
                rows.append(met)
                if not lg.empty:
                    lg["split"] = split_name
                    lg["q_threshold"] = q
                    logs.append(lg)
    return pd.DataFrame(rows), pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def build_selected_logs(
    splits: Dict[str, pd.DataFrame],
    specs: List[FactorSpec],
    orientation: Dict[str, int],
    ranked: pd.DataFrame,
    top_n: int = 20,
) -> pd.DataFrame:
    if ranked.empty:
        return pd.DataFrame()
    specs_by_factor = {s.name: s for s in specs}
    logs: List[pd.DataFrame] = []
    for rank, (_, row) in enumerate(ranked.head(top_n).iterrows(), start=1):
        factor = str(row["factor"])
        spec = specs_by_factor.get(factor)
        if spec is None:
            continue
        hi = float(row["hi_value"])
        lo = float(row["lo_value"])
        oriented_col = f"oriented__{factor}"
        for split_name in ["validation", "test", "latest72h"]:
            d = splits[split_name].copy()
            d[oriented_col] = as_num(d.get(factor, pd.Series(dtype=float))) * orientation.get(factor, 1)
            lg, _ = simulate_threshold_strategy(
                d,
                spec,
                oriented_col,
                hi,
                lo,
                q_level=float(row["q_threshold"]),
                capture_logs=True,
            )
            if not lg.empty:
                lg["split"] = split_name
                lg["selection_rank"] = rank
                logs.append(lg)
    return pd.concat(logs, ignore_index=True) if logs else pd.DataFrame()


def score_validation(metrics: pd.DataFrame, diag: pd.DataFrame) -> pd.DataFrame:
    val = metrics[metrics["split"] == "validation"].copy()
    if val.empty:
        return val
    test = metrics[metrics["split"] == "test"][["strategy", "total_return", "max_drawdown", "trades", "win_rate", "profit_factor"]].copy()
    test = test.rename(
        columns={
            "total_return": "test_return",
            "max_drawdown": "test_drawdown",
            "trades": "test_trades",
            "win_rate": "test_win_rate",
            "profit_factor": "test_profit_factor",
        }
    )
    val = val.merge(test, on="strategy", how="left")
    d = diag[["factor", "validation_ic", "validation_auc", "test_ic", "test_auc"]].copy()
    val = val.merge(d, on="factor", how="left")
    val["score_return"] = val["total_return"].rank(pct=True)
    val["score_dd"] = (-val["max_drawdown"].fillna(999)).rank(pct=True)
    val["score_pf"] = val["profit_factor"].replace([np.inf, -np.inf], np.nan).fillna(0).rank(pct=True)
    val["score_trades"] = np.minimum(val["trades"].fillna(0) / 25.0, 1.0)
    val["score_ic"] = val["validation_ic"].fillna(0).rank(pct=True)
    val["validation_score"] = (
        0.35 * val["score_return"]
        + 0.20 * val["score_dd"]
        + 0.20 * val["score_pf"]
        + 0.15 * val["score_ic"]
        + 0.10 * val["score_trades"]
    )
    return val.sort_values(["validation_score", "total_return"], ascending=False).reset_index(drop=True)


def selected_split_table(metrics: pd.DataFrame, ranked: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    if ranked.empty:
        return pd.DataFrame()
    selected = ranked.head(n)["strategy"].tolist()
    out = metrics[metrics["strategy"].isin(selected)].copy()
    out["selection_rank"] = out["strategy"].map({s: i + 1 for i, s in enumerate(selected)})
    return out.sort_values(["selection_rank", "split"]).reset_index(drop=True)


def md_table(df: pd.DataFrame, n: int = 30) -> str:
    if df.empty:
        return "(empty)"
    show = df.head(n).copy()
    nums = show.select_dtypes(include=[np.number]).columns
    show[nums] = show[nums].round(4)
    return show.to_markdown(index=False)


def build_report(
    coverage: pd.DataFrame,
    diag: pd.DataFrame,
    metrics: pd.DataFrame,
    ranked: pd.DataFrame,
    selected: pd.DataFrame,
) -> str:
    test_top = metrics[metrics["split"] == "test"].sort_values("total_return", ascending=False)
    latest_top = metrics[metrics["split"] == "latest72h"].sort_values("total_return", ascending=False)
    family = (
        metrics[metrics["split"].isin(["validation", "test"])]
        .groupby(["family", "split"], as_index=False)
        .agg(avg_return=("total_return", "mean"), median_return=("total_return", "median"), best_return=("total_return", "max"), avg_trades=("trades", "mean"))
        .sort_values(["split", "avg_return"], ascending=[True, False])
    )
    factor_top = diag.sort_values(["test_ic", "test_auc"], ascending=False)
    lines = [
        "# quantML Polymarket factor sweep",
        "",
        "这份报告把 `quantML` 里的两类思路迁移到 Polymarket BTC Up/Down 5m 数据：",
        "",
        "- `SKILL.md`: 动量、均值回归、量能失衡、复合分数。",
        "- `FactorMoE/AlphaForge`: 表达式因子池、非线性变换、交互项和 pair 组合。",
        "",
        "原始 FactorMoE 代码依赖 QLib/CSI300 横截面，因此这里跑的是同一类 alpha expression mining 思路在单市场时间序列上的适配版。",
        "",
        "## 数据覆盖",
        md_table(coverage, 140),
        "",
        "## 因子 IC / AUC Top 25",
        md_table(factor_top, 25),
        "",
        "## validation 选策略 Top 25",
        md_table(ranked, 25),
        "",
        "## 被 validation 选中策略的各 split 表现",
        md_table(selected, 80),
        "",
        "## test 盲测收益 Top 25",
        md_table(test_top, 25),
        "",
        "## latest72h 健康检查 Top 25",
        md_table(latest_top, 25),
        "",
        "## family 聚合表现",
        md_table(family, 60),
    ]
    return "\n".join(lines)


def make_plots(metrics: pd.DataFrame, diag: pd.DataFrame, outdir: Path) -> int:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return 0
    figdir = outdir / "figures"
    figdir.mkdir(parents=True, exist_ok=True)
    count = 0
    for split in ["validation", "test", "latest72h"]:
        top = metrics[metrics["split"] == split].sort_values("total_return", ascending=False).head(20)
        if top.empty:
            continue
        plt.figure(figsize=(15, 5))
        plt.bar(top["strategy"], top["total_return"])
        plt.xticks(rotation=65, ha="right")
        plt.title(f"{split} top quantML strategy returns")
        plt.tight_layout()
        plt.savefig(figdir / f"{split}_top_returns.png", dpi=150)
        plt.close()
        count += 1
    top_ic = diag.sort_values("test_ic", ascending=False).head(20)
    if not top_ic.empty:
        plt.figure(figsize=(15, 5))
        plt.bar(top_ic["factor"], top_ic["test_ic"])
        plt.xticks(rotation=65, ha="right")
        plt.title("Top test IC factors")
        plt.tight_layout()
        plt.savefig(figdir / "top_test_ic.png", dpi=150)
        plt.close()
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--quantiles", default="0.70,0.75,0.80,0.85,0.90,0.95")
    args = parser.parse_args()

    features, coverage = prepare_features(Path(args.source_root))
    features, skill_specs = add_skill_factors(features)
    features, expr_specs = add_expression_factors(features)
    specs = skill_specs + expr_specs
    splits = split_dataset(features)
    orientation = orient_specs(splits["train"], specs)
    diag = factor_diagnostics(splits, specs, orientation)
    quantiles = [float(x.strip()) for x in args.quantiles.split(",") if x.strip()]
    metrics, _ = evaluate_strategies(splits, specs, orientation, quantiles)
    ranked = score_validation(metrics, diag)
    selected = selected_split_table(metrics, ranked)
    logs = build_selected_logs(splits, specs, orientation, ranked)

    outdir = Path(args.report_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    fig_count = make_plots(metrics, diag, outdir)
    coverage.to_csv(outdir / "coverage.csv", index=False)
    diag.to_csv(outdir / "factor_diagnostics.csv", index=False)
    metrics.to_csv(outdir / "strategy_results.csv", index=False)
    ranked.to_csv(outdir / "validation_ranking.csv", index=False)
    selected.to_csv(outdir / "selected_strategy_split_performance.csv", index=False)
    if not logs.empty:
        logs.to_csv(outdir / "trade_logs.csv", index=False)
    (outdir / "quantml_factor_sweep_report.md").write_text(build_report(coverage, diag, metrics, ranked, selected), encoding="utf-8")
    meta = {
        "rows_features": int(len(features)),
        "factor_count": int(len(specs)),
        "strategy_rows": int(len(metrics)),
        "trade_log_rows": int(len(logs)),
        "figure_count": int(fig_count),
        "source_root": str(Path(args.source_root).resolve()),
    }
    (outdir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(meta, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
