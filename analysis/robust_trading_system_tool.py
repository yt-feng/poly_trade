from __future__ import annotations

import argparse
import csv
import html
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import systematic_intraday_strategy_tool as base

try:
    import numpy as np
except ModuleNotFoundError:  # pragma: no cover - keeps the tool dependency-light.
    np = None


TRAIN_DAYS_DEFAULT = 20
VALIDATION_DAYS_DEFAULT = 5
TEST_DAYS_DEFAULT = 5
EMBARGO_SECONDS_DEFAULT = 300
STARTING_BANKROLL = 100.0
NORMAL_EDGE_GRID = [0.012, 0.020, 0.035]
BOUNDARY_EDGE_GRID = [0.060, 999.0]
MIN_DEPTH_GRID = [50.0, 150.0]
EVENT_CAP_GRID = [0.005, 0.010]
MAX_TRADES_PER_MARKET_GRID = [1, 2, 4]
MIN_ENTRY_GAP_SECONDS_GRID = [4, 8, 16]
KELLY_SHRINK = 0.25
BOUNDARY_SHRINK = 0.35
MAX_DAILY_LOSS = 0.02
MAX_DD_HALT = 0.10
BOUNDARY_PRICE = 0.90
LOW_BOUNDARY_PRICE = 0.10
NO_NEW_TRADE_SECONDS = 10
ENTRY_MODE_DEFAULT = "dense4s"
ENTRY_STEP_SECONDS_DEFAULT = 4
ENTRY_START_SECONDS_DEFAULT = 4
ENTRY_END_BUFFER_SECONDS_DEFAULT = 10
MODEL_TRAIN_SAMPLE_DEFAULT = 120_000
MODEL_VALIDATION_SAMPLE_DEFAULT = 60_000
MODEL_COMPLEXITY_PENALTY = 0.0008
BLEND_MEMBER_COUNTS = [2, 3, 5]
BLEND_L2_GRID = [0.01, 0.05, 0.10, 0.50, 1.00, 2.00]
WALK_FORWARD_TRAIN_DAYS_DEFAULT = 10
WALK_FORWARD_VALIDATION_DAYS_DEFAULT = 3
WALK_FORWARD_TEST_DAYS_DEFAULT = 3
WALK_FORWARD_STEP_DAYS_DEFAULT = 5
WALK_FORWARD_MAX_FOLDS_DEFAULT = 2
WALK_FORWARD_TRAIN_SAMPLE_DEFAULT = 20_000
WALK_FORWARD_VALIDATION_SAMPLE_DEFAULT = 8_000
PAPER_REPLAY_LIMIT_DEFAULT = 600
FIXED_SELECTED_CONFIG_NAME = "q_edge_ne0.02_be0.06_d50_cap0.01_mt1_gap4s__settle"
FAST_EXECUTION_SCAN_NORMAL_EDGE_GRID = [0.020, 0.035, 0.050]
FAST_EXECUTION_SCAN_EVENT_CAP_GRID = [0.005, 0.010]
FAST_EXECUTION_SCAN_MAX_TRADES_GRID = [1, 2, 4]
FAST_EXECUTION_SCAN_GAP_GRID = [4, 8]
MODEL_FEATURES = [
    "logit_market_p",
    "signed_btc_move_entry",
    "signed_btc_move_2m",
    "abs_btc_move_entry",
    "btc_move_entry_x_uncertainty",
    "btc_move_2m_x_uncertainty",
    "btc_move_entry_x_depth",
    "signed_market_momentum_2m",
    "market_momentum_x_uncertainty",
    "path_efficiency_first2m",
    "realized_vol_first2m",
    "spread_side",
    "overround_median_first2m",
    "log_depth_side",
    "signed_size_imbalance_2m",
    "book_pressure_side_2m",
    "entry_minute_1",
    "entry_minute_2",
    "entry_minute_4",
    "entry_progress",
    "seconds_to_close_norm",
    "session_asia",
    "session_london",
    "session_us_open",
    "session_us_afternoon",
    "boundary_price_flag",
]

FEATURE_GROUPS = {
    "market_prior": ["logit_market_p"],
    "btc": ["signed_btc_move_entry", "signed_btc_move_2m", "abs_btc_move_entry"],
    "market_path": ["signed_market_momentum_2m", "path_efficiency_first2m"],
    "interactions": [
        "btc_move_entry_x_uncertainty",
        "btc_move_2m_x_uncertainty",
        "btc_move_entry_x_depth",
        "market_momentum_x_uncertainty",
    ],
    "liquidity_micro": [
        "realized_vol_first2m",
        "spread_side",
        "overround_median_first2m",
        "log_depth_side",
        "signed_size_imbalance_2m",
        "book_pressure_side_2m",
    ],
    "timing": [
        "entry_minute_1",
        "entry_minute_2",
        "entry_minute_4",
        "entry_progress",
        "seconds_to_close_norm",
        "session_asia",
        "session_london",
        "session_us_open",
        "session_us_afternoon",
    ],
    "boundary": ["boundary_price_flag"],
}

GROUP_DESCRIPTIONS = {
    "market_prior": "盘口 mid/可买价给出的市场隐含胜率，是模型的先验基线。",
    "btc": "BTC 现货相对该 5min 合约目标价的方向和幅度，用买入方向统一成 signed 特征。",
    "market_path": "Polymarket 自身价格在前 2 分钟的漂移和路径效率。",
    "interactions": "把 BTC/盘口方向和不确定性、深度相乘，测试“信号在什么状态下更有用”。",
    "liquidity_micro": "价差、overround、深度、买卖盘不平衡，用来描述可成交性和盘口质量。",
    "timing": "入场时间、距离收盘、交易时段，捕捉 5 分钟内和日内时段差异。",
    "boundary": "接近 0/1 的边界价格单独标记，因为尾部亏损不对称。",
    "intercept": "不依赖单个因子的基础偏置项。",
    "mixed_meta": "mixed/blended 模型的上层权重，输入是基础模型的 raw logit。",
}

FEATURE_DESCRIPTIONS = {
    "logit_market_p": "把 market mid 概率转成 logit，代表市场当前共识。",
    "signed_btc_move_entry": "按 buy_up/buy_down 方向统一后的入场时 BTC 价格偏移。",
    "signed_btc_move_2m": "按交易方向统一后的前 2 分钟 BTC 价格偏移。",
    "abs_btc_move_entry": "入场时 BTC 偏移绝对值，表示行情运动强度。",
    "btc_move_entry_x_uncertainty": "入场 BTC 方向信号乘以价格不确定性，测试 50/50 附近信号是否更强。",
    "btc_move_2m_x_uncertainty": "2 分钟 BTC 方向信号乘以价格不确定性。",
    "btc_move_entry_x_depth": "BTC 方向信号乘以可买深度，测试信号和成交深度是否同向。",
    "signed_market_momentum_2m": "Polymarket mid 概率的方向性漂移。",
    "market_momentum_x_uncertainty": "Polymarket 动量乘以不确定性。",
    "path_efficiency_first2m": "前 2 分钟路径是否顺滑，避免只看头尾。",
    "realized_vol_first2m": "前 2 分钟报价波动。",
    "spread_side": "当前方向的买卖价差。",
    "overround_median_first2m": "up/down mid 之和超过 1 的程度，过高说明盘口成本重。",
    "log_depth_side": "当前方向可买深度的 log1p。",
    "signed_size_imbalance_2m": "买 up/down 可买量不平衡，按方向统一符号。",
    "book_pressure_side_2m": "当前方向近端 bid/ask 深度压力。",
    "entry_minute_1": "入场落在第一段时间桶。",
    "entry_minute_2": "入场落在第二段时间桶。",
    "entry_minute_4": "入场落在后段时间桶。",
    "entry_progress": "入场时间占 5 分钟窗口的比例。",
    "seconds_to_close_norm": "距离收盘秒数归一化。",
    "session_asia": "美东夜盘/亚洲时段。",
    "session_london": "伦敦时段。",
    "session_us_open": "美股开盘附近时段。",
    "session_us_afternoon": "美股下午时段。",
    "boundary_price_flag": "entry price >=0.90 或 <=0.10。",
}


def feature_set(*groups: str, exclude: Sequence[str] = ()) -> List[str]:
    wanted = set()
    for group in groups:
        wanted.update(FEATURE_GROUPS[group])
    wanted.difference_update(exclude)
    return [name for name in MODEL_FEATURES if name in wanted]


FEATURE_SET_CANDIDATES = [
    ("full", MODEL_FEATURES),
    ("core_no_timing", feature_set("market_prior", "btc", "market_path", "interactions", "liquidity_micro", "boundary")),
    ("prior_btc_interactions", feature_set("market_prior", "btc", "interactions", "boundary")),
    ("prior_btc_market", feature_set("market_prior", "btc", "market_path", "interactions", "boundary")),
    ("prior_market_liquidity", feature_set("market_prior", "market_path", "interactions", "liquidity_micro", "boundary")),
    ("prior_btc_liquidity", feature_set("market_prior", "btc", "interactions", "liquidity_micro", "boundary")),
    ("no_btc", feature_set("market_prior", "market_path", "interactions", "liquidity_micro", "timing", "boundary")),
    ("no_liquidity_micro", feature_set("market_prior", "btc", "market_path", "interactions", "timing", "boundary")),
    ("no_timing_boundary", feature_set("market_prior", "btc", "market_path", "interactions", "liquidity_micro")),
    ("prior_plus_momentum", feature_set("market_prior", "market_path")),
    ("prior_plus_btc", feature_set("market_prior", "btc")),
    ("market_prior_only", feature_set("market_prior")),
]

WALK_FORWARD_FEATURE_SET_CANDIDATES = [
    ("prior_btc_liquidity", feature_set("market_prior", "btc", "interactions", "liquidity_micro", "boundary")),
    ("no_timing_boundary", feature_set("market_prior", "btc", "market_path", "interactions", "liquidity_micro")),
    ("prior_plus_btc", feature_set("market_prior", "btc")),
    ("full", MODEL_FEATURES),
    ("market_prior_only", feature_set("market_prior")),
]

FEATURE_TO_GROUP = {
    feature: group
    for group, features in FEATURE_GROUPS.items()
    for feature in features
}


@dataclass(frozen=True)
class SplitSpec:
    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime
    test_start: datetime
    test_end: datetime
    embargo_seconds: int


@dataclass(frozen=True)
class ModelBundle:
    feature_set_name: str
    feature_names: List[str]
    means: Dict[str, float]
    stds: Dict[str, float]
    weights: List[float]
    l2: float
    cal_intercept: float
    cal_slope: float
    blend_members: Tuple[Tuple[str, "ModelBundle", float], ...] = ()
    blend_l2: float = 0.0


@dataclass(frozen=True)
class DecisionConfig:
    name: str
    normal_edge: float
    boundary_edge: float
    min_depth: float
    event_cap: float
    exit_policy: base.ExitPolicy
    max_trades_per_market: int
    min_entry_gap_seconds: int


EXIT_POLICIES = [
    base.SETTLE_POLICY,
    base.ExitPolicy("tp04_sl02_buf3s", "intraday", take_profit=0.04, stop_loss=0.02, hold_buffer_seconds=3, close_buffer_seconds=3),
    base.ExitPolicy("tp06_sl03_buf3s", "intraday", take_profit=0.06, stop_loss=0.03, hold_buffer_seconds=3, close_buffer_seconds=3),
    base.ExitPolicy("tp08_sl04_buf5s", "intraday", take_profit=0.08, stop_loss=0.04),
    base.ExitPolicy("trail08_gap03_buf5s", "intraday", trail_start=0.08, trail_gap=0.03, stop_loss=0.05),
]


def full_execution_config_count() -> int:
    return (
        len(EXIT_POLICIES)
        * len(NORMAL_EDGE_GRID)
        * len(BOUNDARY_EDGE_GRID)
        * len(MIN_DEPTH_GRID)
        * len(EVENT_CAP_GRID)
        * len(MAX_TRADES_PER_MARKET_GRID)
        * len(MIN_ENTRY_GAP_SECONDS_GRID)
    )


def selected_fixed_decision_config() -> DecisionConfig:
    return DecisionConfig(
        FIXED_SELECTED_CONFIG_NAME,
        normal_edge=0.020,
        boundary_edge=0.060,
        min_depth=50.0,
        event_cap=0.010,
        exit_policy=base.SETTLE_POLICY,
        max_trades_per_market=1,
        min_entry_gap_seconds=4,
    )


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(float(value)) and not math.isinf(float(value))


def val(value: object, default: float = 0.0) -> float:
    return float(value) if finite(value) else default


def sigmoid(x: float) -> float:
    if x >= 35:
        return 1.0 - 1e-15
    if x <= -35:
        return 1e-15
    return 1.0 / (1.0 + math.exp(-x))


def logit(p: float) -> float:
    p = max(0.01, min(0.99, p))
    return math.log(p / (1.0 - p))


def logloss(y: int, p: float) -> float:
    p = max(1e-6, min(1.0 - 1e-6, p))
    return -(y * math.log(p) + (1 - y) * math.log(1.0 - p))


def dot(weights: Sequence[float], xs: Sequence[float]) -> float:
    return sum(w * x for w, x in zip(weights, xs))


def mean(xs: Iterable[float]) -> float:
    values = [x for x in xs if finite(x)]
    return statistics.mean(values) if values else math.nan


def stdev(xs: Iterable[float]) -> float:
    values = [x for x in xs if finite(x)]
    return statistics.stdev(values) if len(values) >= 2 else 1.0


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    pairs = [(x, y) for x, y in zip(xs, ys) if finite(x) and finite(y)]
    if len(pairs) < 3:
        return math.nan
    mx = statistics.mean(x for x, _ in pairs)
    my = statistics.mean(y for _, y in pairs)
    vx = sum((x - mx) ** 2 for x, _ in pairs)
    vy = sum((y - my) ** 2 for _, y in pairs)
    if vx <= 0 or vy <= 0:
        return math.nan
    return sum((x - mx) * (y - my) for x, y in pairs) / math.sqrt(vx * vy)


def auc_score(labels: Sequence[int], scores: Sequence[float]) -> float:
    pairs = [(score, label) for label, score in zip(labels, scores) if finite(score)]
    pos = sum(label for _, label in pairs)
    neg = len(pairs) - pos
    if pos == 0 or neg == 0:
        return math.nan
    pairs.sort(key=lambda item: item[0])
    rank_sum = 0.0
    for rank, (_, label) in enumerate(pairs, start=1):
        if label:
            rank_sum += rank
    return (rank_sum - pos * (pos + 1) / 2.0) / (pos * neg)


def price_bucket(price: float) -> str:
    if price >= 0.90:
        return "p>=0.90"
    if price <= LOW_BOUNDARY_PRICE:
        return "p<=0.10"
    if price >= 0.75:
        return "0.75-0.90"
    if price <= 0.25:
        return "0.10-0.25"
    return "0.25-0.75"


def is_boundary_price(price: float) -> bool:
    return price >= BOUNDARY_PRICE or price <= LOW_BOUNDARY_PRICE


def default_paths() -> Tuple[Path, Path, Path]:
    repo = Path(__file__).resolve().parents[1]
    code_root = repo.parent
    return code_root / "poly" / "data" / "monthly_runs", repo / "docs", repo / "reports" / "robust_trading_system"


def make_split(markets: List[Dict[str, object]], train_days: int, validation_days: int, test_days: int, embargo_seconds: int) -> SplitSpec:
    end = max(row["first_quote_ts"] for row in markets if isinstance(row.get("first_quote_ts"), datetime))
    test_end = end
    test_start = test_end - timedelta(days=test_days)
    validation_end = test_start - timedelta(seconds=embargo_seconds)
    validation_start = validation_end - timedelta(days=validation_days)
    train_end = validation_start - timedelta(seconds=embargo_seconds)
    train_start = train_end - timedelta(days=train_days)
    return SplitSpec(train_start, train_end, validation_start, validation_end, test_start, test_end, embargo_seconds)


def split_name(ts: datetime, split: SplitSpec) -> str:
    if split.train_start <= ts <= split.train_end:
        return "train"
    if split.validation_start <= ts <= split.validation_end:
        return "validation"
    if split.test_start <= ts <= split.test_end:
        return "test"
    return "unused"


def candidate_snapshot(market: Dict[str, object], minute: int) -> Optional[Dict[str, object]]:
    rows = market.get("rows", [])
    first_ts = market.get("first_quote_ts")
    if not isinstance(rows, list) or not isinstance(first_ts, datetime):
        return None
    return base.row_after_time(rows, first_ts, minute)


def dense_snapshots(
    market: Dict[str, object],
    entry_step_seconds: int,
    entry_start_seconds: int,
    entry_end_buffer_seconds: int,
) -> List[Tuple[Dict[str, object], float]]:
    rows = market.get("rows", [])
    first_ts = market.get("first_quote_ts")
    close_ts = market.get("market_close_ts")
    if not isinstance(rows, list) or not isinstance(first_ts, datetime) or not isinstance(close_ts, datetime):
        return []
    start_ts = first_ts + timedelta(seconds=max(0, entry_start_seconds))
    end_ts = close_ts - timedelta(seconds=max(NO_NEW_TRADE_SECONDS, entry_end_buffer_seconds))
    next_allowed = start_ts
    out: List[Tuple[Dict[str, object], float]] = []
    for row in rows:
        ts = row.get("ts_utc")
        if not isinstance(ts, datetime):
            continue
        if ts < next_allowed:
            continue
        if ts > end_ts:
            break
        out.append((row, (ts - first_ts).total_seconds()))
        next_allowed = ts + timedelta(seconds=max(1, entry_step_seconds))
    return out


def sparse_snapshots(market: Dict[str, object]) -> List[Tuple[Dict[str, object], float]]:
    out: List[Tuple[Dict[str, object], float]] = []
    first_ts = market.get("first_quote_ts")
    if not isinstance(first_ts, datetime):
        return out
    for minute in (1, 2, 4):
        snap = candidate_snapshot(market, minute)
        if snap is None:
            continue
        ts = snap.get("ts_utc")
        if isinstance(ts, datetime):
            out.append((snap, (ts - first_ts).total_seconds()))
    return out


def candidate_for_snapshot(
    market: Dict[str, object],
    snap: Dict[str, object],
    entry_second: float,
    side: str,
    idx: int,
    split: SplitSpec,
    fee: float,
) -> Optional[Dict[str, object]]:
    first_ts = market.get("first_quote_ts")
    close_ts = market.get("market_close_ts")
    if not isinstance(first_ts, datetime) or not isinstance(close_ts, datetime):
        return None
    ts = snap.get("ts_utc")
    if not isinstance(ts, datetime):
        return None
    seconds_to_close = (close_ts - ts).total_seconds()
    if seconds_to_close <= NO_NEW_TRADE_SECONDS:
        return None

    entry_bucket = 1 if entry_second <= 90 else 2 if entry_second <= 180 else 4

    if side == "buy_up":
        entry_price = base.price_from_row(snap, "buy_up_cents")
        entry_size = base.size_from_row(snap, "buy_up_size")
        mid_price = base.price_from_row(snap, "mid_up_cents")
        spread = base.price_from_row(snap, "spread_up_cents")
        label = int(float(market["outcome_up"]) > 0.5)
        signed = 1.0
        pressure = val(market.get("book_pressure_up_2m"))
    else:
        entry_price = base.price_from_row(snap, "buy_down_cents")
        entry_size = base.size_from_row(snap, "buy_down_size")
        mid_price = base.price_from_row(snap, "mid_down_cents")
        spread = base.price_from_row(snap, "spread_down_cents")
        label = int(float(market["outcome_up"]) < 0.5)
        signed = -1.0
        pressure = val(market.get("book_pressure_down_2m"))

    if not finite(entry_price) or entry_price <= 0 or entry_price >= 1 or not finite(entry_size) or entry_size <= 0:
        return None
    market_p = mid_price if finite(mid_price) and 0 < mid_price < 1 else entry_price
    rows_obj = market.get("rows")
    target_price = math.nan
    if isinstance(rows_obj, list) and rows_obj and isinstance(rows_obj[0], dict):
        target_price = val(rows_obj[0].get("target_price"), math.nan)
    if finite(snap.get("final_price")) and finite(target_price):
        move_entry = float(snap["final_price"]) - target_price
    else:
        move_entry = val(market.get(f"btc_move_{entry_bucket}m"), val(market.get("btc_move_2m")))
    signed_move_entry = signed * move_entry
    signed_move_2m = signed * val(market.get("btc_move_2m"))
    mid_open = val(market.get("mid_up_prob_open"), math.nan)
    mid_now = base.price_from_row(snap, "mid_up_cents")
    signed_pm = signed * ((mid_now - mid_open) if finite(mid_now) and finite(mid_open) else val(market.get("mid_up_prob_change_2m")))
    signed_size_imb = signed * base.imbalance(base.size_from_row(snap, "buy_up_size"), base.size_from_row(snap, "buy_down_size"))
    bid_depth = base.size_from_row(snap, "bid_depth_up_5" if side == "buy_up" else "bid_depth_down_5")
    ask_depth = base.size_from_row(snap, "ask_depth_up_5" if side == "buy_up" else "ask_depth_down_5")
    pressure_now = base.imbalance(bid_depth, ask_depth)
    log_depth = math.log1p(entry_size)
    price_uncertainty = max(0.0, 1.0 - min(1.0, abs(market_p - 0.5) * 2.0))
    sess = str(market.get("session_et", "other"))
    features = {
        "logit_market_p": logit(market_p),
        "signed_btc_move_entry": signed_move_entry,
        "signed_btc_move_2m": signed_move_2m,
        "abs_btc_move_entry": abs(move_entry),
        "btc_move_entry_x_uncertainty": signed_move_entry * price_uncertainty,
        "btc_move_2m_x_uncertainty": signed_move_2m * price_uncertainty,
        "btc_move_entry_x_depth": signed_move_entry * log_depth,
        "signed_market_momentum_2m": signed_pm,
        "market_momentum_x_uncertainty": signed_pm * price_uncertainty,
        "path_efficiency_first2m": val(market.get("path_efficiency_first2m"), 0.0),
        "realized_vol_first2m": val(market.get("realized_vol_first2m"), 0.0),
        "spread_side": spread if finite(spread) else 1.0,
        "overround_median_first2m": val(snap.get("mid_overround_cents"), 100.0) / 100.0 if finite(snap.get("mid_overround_cents")) else val(market.get("overround_median_first2m"), 1.0),
        "log_depth_side": log_depth,
        "signed_size_imbalance_2m": signed_size_imb if finite(signed_size_imb) else signed * val(market.get("size_imbalance_updown_2m")),
        "book_pressure_side_2m": pressure_now if finite(pressure_now) else pressure,
        "entry_minute_1": 1.0 if entry_bucket == 1 else 0.0,
        "entry_minute_2": 1.0 if entry_bucket == 2 else 0.0,
        "entry_minute_4": 1.0 if entry_bucket == 4 else 0.0,
        "entry_progress": max(0.0, min(1.0, entry_second / 300.0)),
        "seconds_to_close_norm": max(0.0, min(1.0, seconds_to_close / 300.0)),
        "session_asia": 1.0 if sess == "asia" else 0.0,
        "session_london": 1.0 if sess == "london" else 0.0,
        "session_us_open": 1.0 if sess == "us_open" else 0.0,
        "session_us_afternoon": 1.0 if sess == "us_afternoon" else 0.0,
        "boundary_price_flag": 1.0 if is_boundary_price(entry_price) else 0.0,
    }
    pnl_per_share_settle = label - entry_price - fee
    return {
        "candidate_id": f"{market['market_id']}::{int(entry_second)}::{side}",
        "market_index": idx,
        "market_id": market["market_id"],
        "run_name": market["run_name"],
        "slug": market["slug"],
        "split": split_name(first_ts, split),
        "first_quote_ts": first_ts,
        "first_quote_cst": base.to_tz_text(first_ts, base.SH_TZ),
        "entry_ts": ts,
        "entry_minute": entry_second / 60.0,
        "entry_second": entry_second,
        "entry_bucket": entry_bucket,
        "side": side,
        "session_et": sess,
        "entry_price": entry_price,
        "market_p": market_p,
        "entry_size": entry_size,
        "label": label,
        "settle_pnl_per_share": pnl_per_share_settle,
        "price_bucket": price_bucket(entry_price),
        "seconds_to_close": seconds_to_close,
        "features": features,
        "market": market,
    }


def build_candidates(
    markets: List[Dict[str, object]],
    split: SplitSpec,
    fee: float,
    entry_mode: str,
    entry_step_seconds: int,
    entry_start_seconds: int,
    entry_end_buffer_seconds: int,
) -> List[Dict[str, object]]:
    candidates: List[Dict[str, object]] = []
    for idx, market in enumerate(markets):
        if entry_mode == "sparse_minutes":
            snapshots = sparse_snapshots(market)
        elif entry_mode == "dense4s":
            snapshots = dense_snapshots(market, entry_step_seconds, entry_start_seconds, entry_end_buffer_seconds)
        else:
            raise ValueError(f"Unknown entry mode: {entry_mode}")
        for snap, entry_second in snapshots:
            for side in ("buy_up", "buy_down"):
                candidate = candidate_for_snapshot(market, snap, entry_second, side, idx, split, fee)
                if candidate is not None:
                    candidates.append(candidate)
    return candidates


def fit_scaler(rows: List[Dict[str, object]], feature_names: List[str]) -> Tuple[Dict[str, float], Dict[str, float]]:
    means: Dict[str, float] = {}
    stds: Dict[str, float] = {}
    for name in feature_names:
        xs = [val(row["features"].get(name)) for row in rows]  # type: ignore[index]
        means[name] = mean(xs)
        s = stdev(xs)
        stds[name] = s if finite(s) and s > 1e-9 else 1.0
    return means, stds


def vectorize(row: Dict[str, object], feature_names: List[str], means: Dict[str, float], stds: Dict[str, float]) -> List[float]:
    features: Dict[str, float] = row["features"]  # type: ignore[assignment]
    xs = [1.0]
    for name in feature_names:
        z = (val(features.get(name)) - means[name]) / stds[name]
        if not finite(z):
            z = 0.0
        xs.append(max(-20.0, min(20.0, z)))
    return xs


def train_logistic(feature_set_name: str, rows: List[Dict[str, object]], feature_names: List[str], l2: float, epochs: int = 450, lr: float = 0.03) -> Tuple[ModelBundle, List[float]]:
    means, stds = fit_scaler(rows, feature_names)
    ys = [int(row["label"]) for row in rows]
    pos_rate = max(1e-4, min(1.0 - 1e-4, sum(ys) / len(ys)))
    if np is not None:
        x_np = np.asarray([vectorize(row, feature_names, means, stds) for row in rows], dtype=float)
        x_np = np.nan_to_num(x_np, nan=0.0, posinf=0.0, neginf=0.0)
        y_np = np.asarray(ys, dtype=float)
        weights_np = np.zeros(len(feature_names) + 1, dtype=float)
        weights_np[0] = logit(pos_rate)
        n = len(rows)
        for _ in range(epochs):
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                z_raw = x_np @ weights_np
            z = np.clip(np.nan_to_num(z_raw, nan=0.0, posinf=35.0, neginf=-35.0), -35.0, 35.0)
            p = 1.0 / (1.0 + np.exp(-z))
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                grad = (x_np.T @ (p - y_np)) / n
            grad[1:] += l2 * weights_np[1:] / n
            grad = np.nan_to_num(grad, nan=0.0, posinf=0.0, neginf=0.0)
            weights_np -= lr * grad
            weights_np = np.clip(weights_np, -20.0, 20.0)
        weights = weights_np.tolist()
        with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
            raw = x_np @ weights_np
        raw = np.nan_to_num(raw, nan=0.0, posinf=35.0, neginf=-35.0)
        return ModelBundle(feature_set_name, feature_names, means, stds, weights, l2, 0.0, 1.0), raw.tolist()

    xs = [vectorize(row, feature_names, means, stds) for row in rows]
    weights = [0.0 for _ in range(len(feature_names) + 1)]
    weights[0] = logit(pos_rate)
    n = len(rows)
    for _ in range(epochs):
        grad = [0.0 for _ in weights]
        for x, y in zip(xs, ys):
            p = sigmoid(dot(weights, x))
            err = p - y
            for j, xv in enumerate(x):
                grad[j] += err * xv
        for j in range(len(weights)):
            grad[j] /= n
            if j > 0:
                grad[j] += l2 * weights[j] / n
            weights[j] -= lr * grad[j]
    return ModelBundle(feature_set_name, feature_names, means, stds, weights, l2, 0.0, 1.0), [dot(weights, x) for x in xs]


def union_feature_names(models: Sequence[ModelBundle]) -> List[str]:
    wanted = {feature for model in models for feature in model.feature_names}
    return [name for name in MODEL_FEATURES if name in wanted]


def train_logit_blend(
    blend_name: str,
    member_models: Sequence[ModelBundle],
    rows: List[Dict[str, object]],
    labels: Sequence[int],
    l2: float,
    epochs: int = 450,
    lr: float = 0.03,
) -> ModelBundle:
    if not member_models:
        raise ValueError("Blend requires at least one member model")
    pos_rate = max(1e-4, min(1.0 - 1e-4, sum(labels) / len(labels))) if labels else 0.5
    if np is not None:
        x_np = np.asarray([[1.0] + [predict_logit(member, row) for member in member_models] for row in rows], dtype=float)
        x_np = np.nan_to_num(x_np, nan=0.0, posinf=20.0, neginf=-20.0)
        y_np = np.asarray(labels, dtype=float)
        weights_np = np.zeros(len(member_models) + 1, dtype=float)
        weights_np[0] = logit(pos_rate)
        n = len(rows)
        for _ in range(epochs):
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                z_raw = x_np @ weights_np
            z = np.clip(np.nan_to_num(z_raw, nan=0.0, posinf=35.0, neginf=-35.0), -35.0, 35.0)
            p = 1.0 / (1.0 + np.exp(-z))
            with np.errstate(divide="ignore", over="ignore", invalid="ignore"):
                grad = (x_np.T @ (p - y_np)) / max(1, n)
            grad[1:] += l2 * weights_np[1:] / max(1, n)
            grad = np.nan_to_num(grad, nan=0.0, posinf=0.0, neginf=0.0)
            weights_np -= lr * grad
            weights_np = np.clip(weights_np, -20.0, 20.0)
        weights = weights_np.tolist()
    else:
        xs = [[1.0] + [predict_logit(member, row) for member in member_models] for row in rows]
        weights = [0.0 for _ in range(len(member_models) + 1)]
        weights[0] = logit(pos_rate)
        n = len(rows)
        for _ in range(epochs):
            grad = [0.0 for _ in weights]
            for x, y in zip(xs, labels):
                p = sigmoid(dot(weights, x))
                err = p - y
                for j, xv in enumerate(x):
                    grad[j] += err * xv
            for j in range(len(weights)):
                grad[j] /= max(1, n)
                if j > 0:
                    grad[j] += l2 * weights[j] / max(1, n)
                weights[j] -= lr * grad[j]
    members = tuple((f"{member.feature_set_name}@l2={member.l2:g}", member, float(weights[i + 1])) for i, member in enumerate(member_models))
    return ModelBundle(blend_name, union_feature_names(member_models), {}, {}, [float(weights[0])], l2, 0.0, 1.0, members, l2)


def base_linear_logit(model: ModelBundle, row: Dict[str, object]) -> float:
    return dot(model.weights, vectorize(row, model.feature_names, model.means, model.stds))


def predict_logit(model: ModelBundle, row: Dict[str, object]) -> float:
    if model.blend_members:
        return model.weights[0] + sum(weight * predict_logit(member, row) for _, member, weight in model.blend_members)
    return base_linear_logit(model, row)


def predict_raw(model: ModelBundle, row: Dict[str, object]) -> float:
    return sigmoid(predict_logit(model, row))


def evaluate_probability(rows: List[Dict[str, object]], probs: Sequence[float], name: str) -> Dict[str, object]:
    if not rows:
        return {"split": name, "rows": 0}
    ys = [int(row["label"]) for row in rows]
    baselines = [val(row.get("market_p"), 0.5) for row in rows]
    return {
        "split": name,
        "rows": len(rows),
        "positive_rate": sum(ys) / len(ys),
        "logloss_model": statistics.mean(logloss(y, p) for y, p in zip(ys, probs)),
        "logloss_market_baseline": statistics.mean(logloss(y, p) for y, p in zip(ys, baselines)),
        "logloss_delta_vs_market": statistics.mean(logloss(y, p) for y, p in zip(ys, probs)) - statistics.mean(logloss(y, p) for y, p in zip(ys, baselines)),
        "brier_model": statistics.mean((p - y) ** 2 for y, p in zip(ys, probs)),
        "brier_market_baseline": statistics.mean((p - y) ** 2 for y, p in zip(ys, baselines)),
        "auc_model": auc_score(ys, probs),
        "auc_market_baseline": auc_score(ys, baselines),
        "ece_model_10": ece(ys, probs, 10),
    }


def fit_platt(raw_logits: Sequence[float], labels: Sequence[int], epochs: int = 350, lr: float = 0.05) -> Tuple[float, float]:
    a, b = 0.0, 1.0
    n = len(labels)
    if n == 0:
        return a, b
    for _ in range(epochs):
        ga = 0.0
        gb = 0.0
        for z, y in zip(raw_logits, labels):
            p = sigmoid(a + b * z)
            err = p - y
            ga += err
            gb += err * z
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def calibrated_prob(model: ModelBundle, row: Dict[str, object]) -> float:
    z = predict_logit(model, row)
    return sigmoid(model.cal_intercept + model.cal_slope * z)


def ece(labels: Sequence[int], probs: Sequence[float], buckets: int) -> float:
    total = len(labels)
    if total == 0:
        return math.nan
    err = 0.0
    for b in range(buckets):
        lo = b / buckets
        hi = (b + 1) / buckets
        pairs = [(y, p) for y, p in zip(labels, probs) if (lo <= p < hi) or (b == buckets - 1 and lo <= p <= hi)]
        if not pairs:
            continue
        acc = statistics.mean(y for y, _ in pairs)
        conf = statistics.mean(p for _, p in pairs)
        err += len(pairs) / total * abs(acc - conf)
    return err


def select_model(
    train_rows: List[Dict[str, object]],
    validation_rows: List[Dict[str, object]],
    feature_sets: Sequence[Tuple[str, List[str]]],
) -> Tuple[ModelBundle, List[Dict[str, object]]]:
    rows: List[Dict[str, object]] = []
    trained_models: List[Tuple[ModelBundle, float]] = []
    best_model: Optional[ModelBundle] = None
    best_selection_loss = float("inf")
    labels = [int(row["label"]) for row in validation_rows]
    for feature_set_name, feature_names in feature_sets:
        for l2 in [0.01, 0.05, 0.10, 0.50, 1.00, 2.00]:
            model, _ = train_logistic(feature_set_name, train_rows, feature_names, l2)
            probs = [predict_raw(model, row) for row in validation_rows]
            loss = statistics.mean(logloss(y, p) for y, p in zip(labels, probs)) if labels else float("inf")
            selection_loss = loss
            rows.append(
                {
                    "feature_set": feature_set_name,
                    "model_kind": "single",
                    "feature_count": len(feature_names),
                    "member_count": 1,
                    "l2": l2,
                    "validation_logloss": loss,
                    "complexity_penalty": 0.0,
                    "selection_loss": selection_loss,
                    "members": feature_set_name,
                }
            )
            trained_models.append((model, loss))
            if feature_set_name != "market_prior_only" and selection_loss < best_selection_loss:
                best_selection_loss = selection_loss
                best_model = model

    best_by_feature_set: Dict[str, Tuple[ModelBundle, float]] = {}
    for model, loss in trained_models:
        if model.feature_set_name == "market_prior_only":
            continue
        current = best_by_feature_set.get(model.feature_set_name)
        if current is None or loss < current[1]:
            best_by_feature_set[model.feature_set_name] = (model, loss)
    diverse_models = [item[0] for item in sorted(best_by_feature_set.values(), key=lambda item: item[1])]
    for member_count in BLEND_MEMBER_COUNTS:
        if len(diverse_models) < member_count:
            continue
        members = diverse_models[:member_count]
        for l2 in BLEND_L2_GRID:
            blend_name = f"mixed_top{member_count}_logit_blend"
            model = train_logit_blend(blend_name, members, validation_rows, labels, l2)
            probs = [predict_raw(model, row) for row in validation_rows]
            loss = statistics.mean(logloss(y, p) for y, p in zip(labels, probs)) if labels else float("inf")
            penalty = MODEL_COMPLEXITY_PENALTY * max(0, member_count - 1)
            selection_loss = loss + penalty
            member_names = " + ".join(name for name, _, _ in model.blend_members)
            rows.append(
                {
                    "feature_set": blend_name,
                    "model_kind": "mixed_blend",
                    "feature_count": len(model.feature_names),
                    "member_count": member_count,
                    "l2": l2,
                    "validation_logloss": loss,
                    "complexity_penalty": penalty,
                    "selection_loss": selection_loss,
                    "members": member_names,
                }
            )
            if selection_loss < best_selection_loss:
                best_selection_loss = selection_loss
                best_model = model
    if best_model is None:
        raise RuntimeError("Could not fit model")
    val_logits = [predict_logit(best_model, row) for row in validation_rows]
    a, b = fit_platt(val_logits, labels)
    best_model = ModelBundle(
        best_model.feature_set_name,
        best_model.feature_names,
        best_model.means,
        best_model.stds,
        best_model.weights,
        best_model.l2,
        a,
        b,
        best_model.blend_members,
        best_model.blend_l2,
    )
    rows.sort(key=lambda row: float(row.get("selection_loss", row.get("validation_logloss", float("inf")))))
    return best_model, rows


def evenly_sample(rows: List[Dict[str, object]], limit: int) -> List[Dict[str, object]]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[min(len(rows) - 1, int(i * step))] for i in range(limit)]


def attach_scores(rows: List[Dict[str, object]], model: ModelBundle, fee: float) -> None:
    for row in rows:
        q = calibrated_prob(model, row)
        entry_price = float(row["entry_price"])
        edge = q - entry_price - fee
        row["q_model"] = q
        row["edge"] = edge
        row["full_kelly_fraction"] = max(0.0, edge / max(1e-6, 1.0 - entry_price))


def factor_diagnostics(rows_by_split: Dict[str, List[Dict[str, object]]], feature_names: List[str]) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for name in feature_names:
        row: Dict[str, object] = {"feature": name}
        for split_name_, rows in rows_by_split.items():
            labels = [int(r["label"]) for r in rows]
            values = [val(r["features"].get(name)) for r in rows]  # type: ignore[index]
            pnl_edges = [float(r["settle_pnl_per_share"]) for r in rows]
            row[f"{split_name_}_auc"] = auc_score(labels, values)
            row[f"{split_name_}_corr_label"] = pearson(values, labels)
            row[f"{split_name_}_corr_pnl"] = pearson(values, pnl_edges)
        stability = 0
        signs = []
        for split_name_ in ("train", "validation", "test"):
            corr = row.get(f"{split_name_}_corr_pnl")
            if finite(corr):
                signs.append(1 if float(corr) > 0 else -1)
        if signs and all(s == signs[0] for s in signs):
            stability = 1
        row["stable_corr_pnl_sign"] = stability
        out.append(row)
    return out


def model_weight_rows(model: ModelBundle) -> List[Dict[str, object]]:
    if model.blend_members:
        rows: List[Dict[str, object]] = [
            {
                "feature": "mixed_meta_intercept",
                "group": "mixed_meta",
                "weight": model.weights[0],
                "mean": 0.0,
                "std": 1.0,
                "abs_weight": abs(model.weights[0]),
                "note": "mixed logit 截距",
            }
        ]
        for member_name, member, member_weight in model.blend_members:
            rows.append(
                {
                    "feature": f"mixed_member::{member_name}",
                    "group": "mixed_meta",
                    "weight": member_weight,
                    "mean": 0.0,
                    "std": 1.0,
                    "abs_weight": abs(member_weight),
                    "note": f"输入为 {member_name} 的 raw logit",
                }
            )
            for item in model_weight_rows(member)[:8]:
                rows.append(
                    {
                        "feature": f"{member_name}::{item['feature']}",
                        "group": FEATURE_TO_GROUP.get(str(item["feature"]), "member_weight"),
                        "weight": item["weight"],
                        "mean": item["mean"],
                        "std": item["std"],
                        "abs_weight": item["abs_weight"],
                        "note": "成员模型内部权重",
                    }
                )
        rows.sort(key=lambda row: float(row["abs_weight"]), reverse=True)
        return rows
    rows = [{"feature": "intercept", "group": "intercept", "weight": model.weights[0], "mean": 0.0, "std": 1.0, "abs_weight": abs(model.weights[0])}]
    for i, name in enumerate(model.feature_names, start=1):
        rows.append({"feature": name, "group": FEATURE_TO_GROUP.get(name, "other"), "weight": model.weights[i], "mean": model.means[name], "std": model.stds[name], "abs_weight": abs(model.weights[i])})
    rows.sort(key=lambda row: float(row["abs_weight"]), reverse=True)
    return rows


def contribution_by_group(model: ModelBundle, row: Dict[str, object]) -> Dict[str, float]:
    if model.blend_members:
        out: Dict[str, float] = {"intercept": model.weights[0]}
        for _, member, member_weight in model.blend_members:
            for group, value in contribution_by_group(member, row).items():
                out[group] = out.get(group, 0.0) + member_weight * value
        return out
    xs = vectorize(row, model.feature_names, model.means, model.stds)
    out: Dict[str, float] = {"intercept": model.weights[0]}
    for i, feature in enumerate(model.feature_names, start=1):
        group = FEATURE_TO_GROUP.get(feature, "other")
        out[group] = out.get(group, 0.0) + model.weights[i] * xs[i]
    return out


def contribution_summary(rows: List[Dict[str, object]], logs: List[Dict[str, object]], model: ModelBundle) -> List[Dict[str, object]]:
    by_candidate = {str(row.get("candidate_id")): row for row in rows}
    grouped_values: Dict[Tuple[str, str], List[float]] = defaultdict(list)
    for log in logs:
        row = by_candidate.get(str(log.get("candidate_id")))
        if row is None:
            continue
        split = str(log.get("split", ""))
        for group, value in contribution_by_group(model, row).items():
            grouped_values[(split, group)].append(value)
    out: List[Dict[str, object]] = []
    for (split, group), values in sorted(grouped_values.items()):
        out.append(
            {
                "split": split,
                "group": group,
                "trades": len(values),
                "avg_logit_contribution": statistics.mean(values) if values else math.nan,
                "avg_abs_logit_contribution": statistics.mean(abs(v) for v in values) if values else math.nan,
                "positive_contribution_rate": sum(1 for v in values if v > 0) / len(values) if values else math.nan,
            }
        )
    return out


def trade_explanation_rows(rows: List[Dict[str, object]], logs: List[Dict[str, object]], model: ModelBundle, limit: int = 5000) -> List[Dict[str, object]]:
    by_candidate = {str(row.get("candidate_id")): row for row in rows}
    out: List[Dict[str, object]] = []
    for log in logs[:limit]:
        row = by_candidate.get(str(log.get("candidate_id")))
        if row is None:
            continue
        contrib = contribution_by_group(model, row)
        non_intercept = {key: value for key, value in contrib.items() if key != "intercept"}
        dominant_group = max(non_intercept.items(), key=lambda item: abs(item[1]))[0] if non_intercept else ""
        raw_logit = predict_logit(model, row)
        calibrated_logit = model.cal_intercept + model.cal_slope * raw_logit
        out.append(
            {
                "split": log.get("split"),
                "first_quote_cst": log.get("first_quote_cst"),
                "slug": log.get("slug"),
                "side": log.get("side"),
                "entry_second": log.get("entry_second"),
                "entry_price": log.get("entry_price"),
                "market_p": log.get("market_p"),
                "q_model": log.get("q_model"),
                "raw_logit": raw_logit,
                "calibrated_logit": calibrated_logit,
                "edge": log.get("edge"),
                "pnl_usd": log.get("pnl_usd"),
                "exit_reason": log.get("exit_reason"),
                "dominant_group": dominant_group,
                "market_prior_contrib": contrib.get("market_prior", 0.0),
                "btc_contrib": contrib.get("btc", 0.0),
                "interaction_contrib": contrib.get("interactions", 0.0),
                "liquidity_micro_contrib": contrib.get("liquidity_micro", 0.0),
                "market_path_contrib": contrib.get("market_path", 0.0),
                "timing_contrib": contrib.get("timing", 0.0),
                "boundary_contrib": contrib.get("boundary", 0.0),
                "intercept_contrib": contrib.get("intercept", 0.0),
            }
        )
    return out


def candidate_passes(row: Dict[str, object], cfg: DecisionConfig) -> bool:
    edge = float(row.get("edge", -999.0))
    price = float(row["entry_price"])
    threshold = cfg.boundary_edge if is_boundary_price(price) else cfg.normal_edge
    if edge < threshold:
        return False
    if float(row["entry_size"]) < cfg.min_depth:
        return False
    features: Dict[str, float] = row["features"]  # type: ignore[assignment]
    if val(features.get("spread_side"), 1.0) > 0.08:
        return False
    if val(features.get("overround_median_first2m"), 1.0) > 0.06:
        return False
    return True


def stake_fraction(row: Dict[str, object], cfg: DecisionConfig) -> float:
    price = float(row["entry_price"])
    full_kelly = max(0.0, float(row.get("full_kelly_fraction", 0.0)))
    per_trade_cap = cfg.event_cap / max(1, cfg.max_trades_per_market)
    frac = min(per_trade_cap, KELLY_SHRINK * full_kelly)
    if is_boundary_price(price):
        frac *= BOUNDARY_SHRINK
    return max(0.0, frac)


def group_logs_by_market(logs: List[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    grouped: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in logs:
        grouped[str(row.get("market_id", ""))].append(row)
    return grouped


def candidates_by_market_for_split(candidates: List[Dict[str, object]], split_filter: str) -> Dict[int, List[Dict[str, object]]]:
    by_market: Dict[int, List[Dict[str, object]]] = defaultdict(list)
    for row in candidates:
        if row.get("split") == split_filter:
            by_market[int(row["market_index"])].append(row)
    return by_market


def simulate_decision_system(
    markets: List[Dict[str, object]],
    candidates: List[Dict[str, object]],
    cfg: DecisionConfig,
    split_filter: str,
    fee: float,
    pregrouped_by_market: Optional[Dict[int, List[Dict[str, object]]]] = None,
) -> Tuple[Dict[str, object], List[Dict[str, object]], List[float], List[float]]:
    by_market = pregrouped_by_market if pregrouped_by_market is not None else candidates_by_market_for_split(candidates, split_filter)

    bankroll = STARTING_BANKROLL
    peak = STARTING_BANKROLL
    day_start_bankroll = STARTING_BANKROLL
    current_day = ""
    daily_halted = False
    event_returns: List[float] = []
    trade_flags: List[int] = []
    logs: List[Dict[str, object]] = []
    curve: List[float] = []

    market_indices = sorted(by_market)
    for idx in market_indices:
        market = markets[idx]
        ts = market.get("first_quote_ts")
        if not isinstance(ts, datetime):
            continue
        day = ts.date().isoformat()
        if day != current_day:
            current_day = day
            day_start_bankroll = bankroll
            daily_halted = False
        if day_start_bankroll > 0 and bankroll <= day_start_bankroll * (1.0 - MAX_DAILY_LOSS):
            daily_halted = True

        event_ret = 0.0
        traded = 0
        trades_in_market = 0
        last_entry_ts: Optional[datetime] = None
        event_start_bankroll = bankroll
        used_event_cost = 0.0
        if not daily_halted and (peak <= 0 or (peak - bankroll) / peak < MAX_DD_HALT):
            grouped_by_ts: Dict[datetime, List[Dict[str, object]]] = defaultdict(list)
            for row in by_market[idx]:
                entry_ts = row.get("entry_ts")
                if isinstance(entry_ts, datetime):
                    grouped_by_ts[entry_ts].append(row)
            for entry_ts in sorted(grouped_by_ts):
                if trades_in_market >= cfg.max_trades_per_market:
                    break
                if last_entry_ts is not None and (entry_ts - last_entry_ts).total_seconds() < cfg.min_entry_gap_seconds:
                    continue
                if used_event_cost >= event_start_bankroll * cfg.event_cap:
                    break
                options = [row for row in grouped_by_ts[entry_ts] if candidate_passes(row, cfg)]
                if not options:
                    continue
                chosen = max(options, key=lambda row: float(row.get("edge", -999.0)))
                frac = stake_fraction(chosen, cfg)
                if frac > 0:
                    before = bankroll
                    entry_price = float(chosen["entry_price"])
                    remaining_event_cost = max(0.0, event_start_bankroll * cfg.event_cap - used_event_cost)
                    target_cost = min(bankroll * frac, bankroll, remaining_event_cost, float(chosen["entry_size"]) * entry_price)
                    if target_cost > 0:
                        shares = target_cost / entry_price
                        exit_reason, pnl_per_share, exit_ts, exit_price, skipped_liq = base.simulate_exit(
                            chosen["market"], str(chosen["side"]), shares, entry_price, chosen["entry_ts"], cfg.exit_policy, fee
                        )
                        pnl = shares * pnl_per_share
                        bankroll += pnl
                        event_ret += pnl / event_start_bankroll if event_start_bankroll > 0 else 0.0
                        traded = 1
                        trades_in_market += 1
                        last_entry_ts = entry_ts
                        used_event_cost += target_cost
                        peak = max(peak, bankroll)
                        logs.append(
                            {
                                "system": cfg.name,
                                "split": split_filter,
                                "first_quote_ts": ts.isoformat(),
                                "first_quote_cst": base.to_tz_text(ts, base.SH_TZ),
                                "market_id": chosen["market_id"],
                                "candidate_id": chosen["candidate_id"],
                                "slug": chosen["slug"],
                                "session_et": chosen["session_et"],
                                "side": chosen["side"],
                                "entry_minute": chosen["entry_minute"],
                                "entry_second": chosen["entry_second"],
                                "trade_number_in_market": trades_in_market,
                                "entry_price": entry_price,
                                "market_p": chosen["market_p"],
                                "q_model": chosen["q_model"],
                                "edge": chosen["edge"],
                                "price_bucket": chosen["price_bucket"],
                                "entry_size": chosen["entry_size"],
                                "stake_fraction": frac,
                                "target_cost": target_cost,
                                "event_cap_used_fraction": used_event_cost / event_start_bankroll if event_start_bankroll > 0 else 0.0,
                                "shares": shares,
                                "exit_policy": cfg.exit_policy.name,
                                "max_trades_per_market": cfg.max_trades_per_market,
                                "min_entry_gap_seconds": cfg.min_entry_gap_seconds,
                                "exit_reason": exit_reason,
                                "exit_ts": exit_ts.isoformat() if exit_ts else "",
                                "exit_price": exit_price,
                                "pnl_usd": pnl,
                                "event_ret": event_ret,
                                "bankroll_after": bankroll,
                                "daily_halted_before": daily_halted,
                                "skipped_exit_liquidity_quotes": skipped_liq,
                            }
                        )
        peak = max(peak, bankroll)
        curve.append(bankroll)
        event_returns.append(event_ret)
        trade_flags.append(traded)

    pnl_values = [float(row["pnl_usd"]) for row in logs]
    wins = sum(x for x in pnl_values if x > 0)
    losses = sum(x for x in pnl_values if x < 0)
    max_dd = max_drawdown(curve)
    metrics: Dict[str, object] = {
        "system": cfg.name,
        "split": split_filter,
        "normal_edge": cfg.normal_edge,
        "boundary_edge": cfg.boundary_edge,
        "min_depth": cfg.min_depth,
        "event_cap": cfg.event_cap,
        "kelly_shrink": KELLY_SHRINK,
        "exit_policy": cfg.exit_policy.name,
        "max_trades_per_market": cfg.max_trades_per_market,
        "min_entry_gap_seconds": cfg.min_entry_gap_seconds,
        "trades": len(logs),
        "ending_bankroll": bankroll,
        "total_return": bankroll / STARTING_BANKROLL - 1.0,
        "win_rate": sum(1 for x in pnl_values if x > 0) / len(pnl_values) if pnl_values else math.nan,
        "profit_factor": wins / abs(losses) if losses < 0 else math.nan,
        "max_drawdown": max_dd,
        "avg_edge": mean(float(row["edge"]) for row in logs),
        "avg_q": mean(float(row["q_model"]) for row in logs),
        "avg_entry_price": mean(float(row["entry_price"]) for row in logs),
        "boundary_trades": sum(1 for row in logs if row["price_bucket"] in {"p>=0.90", "p<=0.10"}),
        "markets_traded": len({row["market_id"] for row in logs}),
        "multi_trade_markets": sum(1 for rows in group_logs_by_market(logs).values() if len(rows) > 1),
        "avg_trades_per_traded_market": len(logs) / max(1, len({row["market_id"] for row in logs})),
        "max_trades_in_market": max((len(rows) for rows in group_logs_by_market(logs).values()), default=0),
    }
    metrics.update(base.rolling_window_metrics(event_returns, trade_flags, base.WINDOW_12H))
    metrics.update(base.rolling_window_metrics(event_returns, trade_flags, base.WINDOW_36H))
    return metrics, logs, curve, event_returns


def max_drawdown(curve: Sequence[float]) -> float:
    peak = STARTING_BANKROLL
    dd = 0.0
    for value in curve:
        peak = max(peak, value)
        if peak > 0:
            dd = max(dd, (peak - value) / peak)
    return dd


def validation_score(metrics: Dict[str, object]) -> float:
    trades = int(metrics.get("trades", 0))
    if trades < 8:
        return -999.0
    total_return = val(metrics.get("total_return"))
    max_dd = val(metrics.get("max_drawdown"))
    worst36 = val(metrics.get(f"worst_{base.WINDOW_36H}_return"))
    pf = val(metrics.get("profit_factor"), 0.0)
    boundary_penalty = 0.02 * int(metrics.get("boundary_trades", 0))
    return total_return - 1.8 * max_dd + 0.08 * min(pf, 3.0) + 0.6 * min(worst36, 0.0) - boundary_penalty


def tune_decision_config(markets: List[Dict[str, object]], candidates: List[Dict[str, object]], fee: float) -> Tuple[DecisionConfig, List[Dict[str, object]]]:
    return tune_decision_config_grid(
        markets,
        candidates,
        fee,
        EXIT_POLICIES,
        NORMAL_EDGE_GRID,
        BOUNDARY_EDGE_GRID,
        MIN_DEPTH_GRID,
        EVENT_CAP_GRID,
        MAX_TRADES_PER_MARKET_GRID,
        MIN_ENTRY_GAP_SECONDS_GRID,
    )


def tune_decision_config_grid(
    markets: List[Dict[str, object]],
    candidates: List[Dict[str, object]],
    fee: float,
    exit_policies: Sequence[base.ExitPolicy],
    normal_edge_grid: Sequence[float],
    boundary_edge_grid: Sequence[float],
    min_depth_grid: Sequence[float],
    event_cap_grid: Sequence[float],
    max_trades_grid: Sequence[int],
    min_entry_gap_grid: Sequence[int],
) -> Tuple[DecisionConfig, List[Dict[str, object]]]:
    rows: List[Dict[str, object]] = []
    best_cfg: Optional[DecisionConfig] = None
    best_score = -9999.0
    validation_by_market = candidates_by_market_for_split(candidates, "validation")
    for exit_policy in exit_policies:
        for normal_edge in normal_edge_grid:
            for boundary_edge in boundary_edge_grid:
                for min_depth in min_depth_grid:
                    for event_cap in event_cap_grid:
                        for max_trades in max_trades_grid:
                            for min_gap in min_entry_gap_grid:
                                cfg = DecisionConfig(
                                    f"q_edge_ne{normal_edge:g}_be{boundary_edge:g}_d{int(min_depth)}_cap{event_cap:g}_mt{max_trades}_gap{min_gap}s__{exit_policy.name}",
                                    normal_edge,
                                    boundary_edge,
                                    min_depth,
                                    event_cap,
                                    exit_policy,
                                    max_trades,
                                    min_gap,
                                )
                                metrics, _, _, _ = simulate_decision_system(markets, candidates, cfg, "validation", fee, validation_by_market)
                                score = validation_score(metrics)
                                row = dict(metrics)
                                row["validation_score"] = score
                                rows.append(row)
                                if score > best_score:
                                    best_score = score
                                    best_cfg = cfg
    if best_cfg is None:
        raise RuntimeError("No decision config selected")
    rows.sort(key=lambda row: float(row.get("validation_score", -999.0)), reverse=True)
    return best_cfg, rows


def fixed_config_search_row(
    markets: List[Dict[str, object]],
    candidates: List[Dict[str, object]],
    cfg: DecisionConfig,
    fee: float,
    split_filter: str = "validation",
) -> Dict[str, object]:
    metrics, _, _, _ = simulate_decision_system(markets, candidates, cfg, split_filter, fee)
    row = dict(metrics)
    row["validation_score"] = validation_score(metrics) if split_filter == "validation" else math.nan
    row["config_search_mode"] = "fixed_selected"
    return row


def fast_execution_scan_rows(markets: List[Dict[str, object]], candidates: List[Dict[str, object]], fee: float, wide: bool = False) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    validation_by_market = candidates_by_market_for_split(candidates, "validation")
    test_by_market = candidates_by_market_for_split(candidates, "test")
    normal_edges = FAST_EXECUTION_SCAN_NORMAL_EDGE_GRID if wide else [0.020, 0.035]
    event_caps = FAST_EXECUTION_SCAN_EVENT_CAP_GRID if wide else [0.010]
    for normal_edge in normal_edges:
        for event_cap in event_caps:
            for max_trades in FAST_EXECUTION_SCAN_MAX_TRADES_GRID:
                for min_gap in FAST_EXECUTION_SCAN_GAP_GRID:
                    cfg = DecisionConfig(
                        f"q_edge_ne{normal_edge:g}_be0.06_d50_cap{event_cap:g}_mt{max_trades}_gap{min_gap}s__settle",
                        normal_edge,
                        0.060,
                        50.0,
                        event_cap,
                        base.SETTLE_POLICY,
                        max_trades,
                        min_gap,
                    )
                    val_metrics, _, _, _ = simulate_decision_system(markets, candidates, cfg, "validation", fee, validation_by_market)
                    score = validation_score(val_metrics)
                    test_metrics, _, _, _ = simulate_decision_system(markets, candidates, cfg, "test", fee, test_by_market)
                    for split_name_, metrics in (("validation", val_metrics), ("test", test_metrics)):
                        row = dict(metrics)
                        row.update(
                            {
                                "scan_split": split_name_,
                                "validation_score": score,
                                "scan_family": "fast_multi_trade_edge_scan",
                                "is_current_selected": cfg.name == FIXED_SELECTED_CONFIG_NAME,
                            }
                        )
                        rows.append(row)
    rows.sort(
        key=lambda row: (
            -float(row.get("validation_score", -999.0)),
            str(row.get("system", "")),
            str(row.get("split", "")),
        )
    )
    return rows


def execution_scan_pair_rows(scan_rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    grouped: Dict[str, Dict[str, Dict[str, object]]] = defaultdict(dict)
    for row in scan_rows:
        grouped[str(row.get("system", ""))][str(row.get("split", ""))] = row
    out: List[Dict[str, object]] = []
    for system, split_rows in grouped.items():
        val_row = split_rows.get("validation", {})
        test_row = split_rows.get("test", {})
        source = val_row or test_row
        if not source:
            continue
        out.append(
            {
                "system": system,
                "normal_edge": source.get("normal_edge", math.nan),
                "event_cap": source.get("event_cap", math.nan),
                "max_trades_per_market": source.get("max_trades_per_market", math.nan),
                "min_entry_gap_seconds": source.get("min_entry_gap_seconds", math.nan),
                "validation_score": val_row.get("validation_score", math.nan),
                "validation_trades": val_row.get("trades", 0),
                "validation_ending_bankroll": val_row.get("ending_bankroll", math.nan),
                "validation_return": val_row.get("total_return", math.nan),
                "validation_drawdown": val_row.get("max_drawdown", math.nan),
                "test_trades": test_row.get("trades", 0),
                "test_ending_bankroll": test_row.get("ending_bankroll", math.nan),
                "test_return": test_row.get("total_return", math.nan),
                "test_drawdown": test_row.get("max_drawdown", math.nan),
                "test_win_rate": test_row.get("win_rate", math.nan),
                "test_multi_trade_markets": test_row.get("multi_trade_markets", 0),
                "is_current_selected": bool(source.get("is_current_selected")),
            }
        )
    out.sort(key=lambda row: float(row.get("validation_score", -999.0)), reverse=True)
    return out


def bucket_metrics(logs: List[Dict[str, object]]) -> List[Dict[str, object]]:
    groups: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    for row in logs:
        groups[(str(row.get("split", "")), str(row.get("price_bucket", "")))].append(row)
    out: List[Dict[str, object]] = []
    for (split, bucket), rows in sorted(groups.items()):
        pnls = [float(row["pnl_usd"]) for row in rows]
        out.append(
            {
                "split": split,
                "price_bucket": bucket,
                "trades": len(rows),
                "total_pnl": sum(pnls),
                "avg_pnl": statistics.mean(pnls) if pnls else math.nan,
                "win_rate": sum(1 for x in pnls if x > 0) / len(pnls) if pnls else math.nan,
                "avg_edge": mean(float(row["edge"]) for row in rows),
                "avg_q": mean(float(row["q_model"]) for row in rows),
                "avg_entry_price": mean(float(row["entry_price"]) for row in rows),
            }
        )
    return out


def model_family_summary(model_search: List[Dict[str, object]]) -> List[Dict[str, object]]:
    best_by_set: Dict[str, Dict[str, object]] = {}
    for row in model_search:
        name = str(row.get("feature_set", ""))
        current = best_by_set.get(name)
        if current is None or float(row.get("selection_loss", float("inf"))) < float(current.get("selection_loss", float("inf"))):
            best_by_set[name] = row
    rows = [dict(row) for row in best_by_set.values()]
    rows.sort(key=lambda row: float(row.get("selection_loss", row.get("validation_logloss", float("inf")))))
    return rows


def execution_group_summary(config_search: List[Dict[str, object]]) -> List[Dict[str, object]]:
    best_by_group: Dict[Tuple[int, int, str], Dict[str, object]] = {}
    for row in config_search:
        key = (int(row.get("max_trades_per_market", 1)), int(row.get("min_entry_gap_seconds", 0)), str(row.get("exit_policy", "")))
        current = best_by_group.get(key)
        if current is None or float(row.get("validation_score", -999.0)) > float(current.get("validation_score", -999.0)):
            best_by_group[key] = row
    out: List[Dict[str, object]] = []
    for (max_trades, min_gap, exit_policy), row in best_by_group.items():
        out.append(
            {
                "max_trades_per_market": max_trades,
                "min_entry_gap_seconds": min_gap,
                "exit_policy": exit_policy,
                "best_system": row.get("system", ""),
                "validation_score": row.get("validation_score", math.nan),
                "trades": row.get("trades", 0),
                "ending_bankroll": row.get("ending_bankroll", math.nan),
                "total_return": row.get("total_return", math.nan),
                "max_drawdown": row.get("max_drawdown", math.nan),
                "profit_factor": row.get("profit_factor", math.nan),
                "multi_trade_markets": row.get("multi_trade_markets", 0),
            }
        )
    out.sort(key=lambda row: float(row.get("validation_score", -999.0)), reverse=True)
    return out


def calibration_bins(rows_by_split: Dict[str, List[Dict[str, object]]], buckets: int = 10) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for split_name_, rows in rows_by_split.items():
        for b in range(buckets):
            lo = b / buckets
            hi = (b + 1) / buckets
            bucket_rows = [
                row
                for row in rows
                if finite(row.get("q_model")) and ((lo <= float(row["q_model"]) < hi) or (b == buckets - 1 and lo <= float(row["q_model"]) <= hi))
            ]
            if not bucket_rows:
                continue
            labels = [int(row["label"]) for row in bucket_rows]
            out.append(
                {
                    "split": split_name_,
                    "bucket": f"{lo:.1f}-{hi:.1f}",
                    "rows": len(bucket_rows),
                    "avg_q": mean(float(row["q_model"]) for row in bucket_rows),
                    "win_rate": sum(labels) / len(labels),
                    "avg_market_p": mean(float(row["market_p"]) for row in bucket_rows),
                    "avg_edge": mean(float(row["edge"]) for row in bucket_rows),
                }
            )
    return out


def model_step_rows(model: ModelBundle, cfg: DecisionConfig) -> List[Dict[str, object]]:
    return [
        {
            "step": "1 数据切分",
            "input": "本地 quote path + market 结算结果",
            "operation": f"按时间切成 train/validation/test，中间留 {EMBARGO_SECONDS_DEFAULT} 秒 embargo",
            "output": "互不重叠的候选集合",
            "learned_on": "固定规则",
            "why": "避免相邻 5min market 的信息泄漏到测试段。",
        },
        {
            "step": "2 候选生成",
            "input": "每个 5min market 的逐秒报价",
            "operation": f"从第 {ENTRY_START_SECONDS_DEFAULT} 秒开始，每 {ENTRY_STEP_SECONDS_DEFAULT} 秒取一次 buy_up/buy_down 快照，最后 {ENTRY_END_BUFFER_SECONDS_DEFAULT} 秒不新开仓",
            "output": "候选交易点",
            "learned_on": "固定规则",
            "why": "允许 5 分钟内多次观察，但保留 4 秒 buffer，不按超高频假设。",
        },
        {
            "step": "3 特征工程",
            "input": "候选点的价格、深度、BTC 偏移、时段",
            "operation": "按交易方向把 up/down 统一成 signed 特征，并按 train 均值/标准差标准化",
            "output": f"{len(model.feature_names)} 个候选特征进入选中模型",
            "learned_on": "train scaler",
            "why": "让权重可比较，避免 up/down 两套规则互相割裂。",
        },
        {
            "step": "4 概率模型",
            "input": "标准化特征或基础模型 logit",
            "operation": "single logistic 或 mixed logit blend 计算 raw logit",
            "output": "raw z",
            "learned_on": "train 或 validation blend",
            "why": "先估计真实胜率 q，而不是直接写死策略规则。",
        },
        {
            "step": "5 概率校准",
            "input": "raw z",
            "operation": f"q = sigmoid({model.cal_intercept:.4f} + {model.cal_slope:.4f} * z)",
            "output": "校准后胜率 q",
            "learned_on": "validation",
            "why": "logistic 分数排序好不等于概率刻度准，交易 edge 需要概率刻度。",
        },
        {
            "step": "6 交易边际",
            "input": "q、可买价、fee",
            "operation": "edge = q - entry_price - fee",
            "output": "扣掉价格和费用后的可交易优势",
            "learned_on": "固定公式",
            "why": "Polymarket 不是预测比赛，q 高但价格更高仍然不能买。",
        },
        {
            "step": "7 Gate 和仓位",
            "input": "edge、depth、spread、overround、boundary flag",
            "operation": f"edge 阈值 {cfg.normal_edge:.3f}/{cfg.boundary_edge:.3f}，深度>={cfg.min_depth:.0f}，fractional Kelly={KELLY_SHRINK:.2f}",
            "output": "是否下单 + stake fraction",
            "learned_on": "validation 搜索",
            "why": "把预测优势变成受限风险，而不是无限加仓。",
        },
        {
            "step": "8 执行/退出",
            "input": "通过 gate 的候选",
            "operation": f"每 market 最多 {cfg.max_trades_per_market} 笔，最小间隔 {cfg.min_entry_gap_seconds}s，exit={cfg.exit_policy.name}",
            "output": "交易日志和权益曲线",
            "learned_on": "validation 搜索",
            "why": "把 5min 内可多次交易作为执行层参数，而不是模型层偷看未来。",
        },
    ]


def feature_blueprint_rows(model: ModelBundle) -> List[Dict[str, object]]:
    selected = set(model.feature_names)
    direct_weight = {}
    if not model.blend_members:
        direct_weight = {name: model.weights[i] for i, name in enumerate(model.feature_names, start=1)}
    rows: List[Dict[str, object]] = []
    for group, features in FEATURE_GROUPS.items():
        for feature in features:
            rows.append(
                {
                    "group": group,
                    "group_description": GROUP_DESCRIPTIONS.get(group, ""),
                    "feature": feature,
                    "selected": 1 if feature in selected else 0,
                    "weight": direct_weight.get(feature, math.nan),
                    "description": FEATURE_DESCRIPTIONS.get(feature, ""),
                }
            )
    rows.sort(key=lambda row: (0 if row["selected"] else 1, str(row["group"]), str(row["feature"])))
    return rows


def model_mix_rows(model: ModelBundle) -> List[Dict[str, object]]:
    if not model.blend_members:
        return [
            {
                "layer": "single_logistic",
                "name": model.feature_set_name,
                "weight": 1.0,
                "features": len(model.feature_names),
                "l2": model.l2,
                "formula": "z = intercept + sum(w_i * standardized_feature_i)",
            }
        ]
    rows: List[Dict[str, object]] = [
        {
            "layer": "mixed_meta",
            "name": "intercept",
            "weight": model.weights[0],
            "features": 0,
            "l2": model.blend_l2,
            "formula": "z_mix = intercept + sum(alpha_j * z_j)",
        }
    ]
    for member_name, member, weight in model.blend_members:
        rows.append(
            {
                "layer": "mixed_member",
                "name": member_name,
                "weight": weight,
                "features": len(member.feature_names),
                "l2": member.l2,
                "formula": "member raw logit 输入上层 mixed meta",
            }
        )
    return rows


def decision_funnel_rows(candidates: List[Dict[str, object]], logs: List[Dict[str, object]], cfg: DecisionConfig) -> List[Dict[str, object]]:
    executed_by_split: Dict[str, int] = defaultdict(int)
    executed_markets_by_split: Dict[str, set] = defaultdict(set)
    for log in logs:
        split = str(log.get("split", ""))
        executed_by_split[split] += 1
        executed_markets_by_split[split].add(log.get("market_id"))

    out: List[Dict[str, object]] = []
    for split in ("train", "validation", "test"):
        rows = [row for row in candidates if row.get("split") == split]
        edge_rows = [
            row
            for row in rows
            if float(row.get("edge", -999.0)) >= (cfg.boundary_edge if is_boundary_price(float(row["entry_price"])) else cfg.normal_edge)
        ]
        depth_rows = [row for row in edge_rows if float(row.get("entry_size", 0.0)) >= cfg.min_depth]
        spread_rows = [row for row in depth_rows if val(row["features"].get("spread_side"), 1.0) <= 0.08]  # type: ignore[index]
        overround_rows = [row for row in spread_rows if val(row["features"].get("overround_median_first2m"), 1.0) <= 0.06]  # type: ignore[index]
        stages = [
            ("候选", rows),
            ("过 edge", edge_rows),
            ("过 depth", depth_rows),
            ("过 spread", spread_rows),
            ("过 overround", overround_rows),
        ]
        total = max(1, len(rows))
        previous = len(rows)
        for stage, stage_rows in stages:
            count = len(stage_rows)
            out.append(
                {
                    "split": split,
                    "stage": stage,
                    "count": count,
                    "share_of_candidates": count / total,
                    "retention_from_previous": count / previous if previous else math.nan,
                }
            )
            previous = count
        out.append(
            {
                "split": split,
                "stage": "实际执行",
                "count": executed_by_split.get(split, 0),
                "share_of_candidates": executed_by_split.get(split, 0) / total,
                "retention_from_previous": executed_by_split.get(split, 0) / previous if previous else math.nan,
                "executed_markets": len(executed_markets_by_split.get(split, set())),
            }
        )
    return out


def stress_test_rows(markets: List[Dict[str, object]], candidates: List[Dict[str, object]], cfg: DecisionConfig, fee: float) -> List[Dict[str, object]]:
    scenarios = [
        ("base", 0.0, 0.0, "原始假设"),
        ("fee_plus_0.5c", 0.005, 0.0, "每份额额外 0.5c 成本，近似滑点/失败补偿"),
        ("fee_plus_1.0c", 0.010, 0.0, "每份额额外 1.0c 成本"),
        ("edge_plus_1.0c", 0.0, 0.010, "交易阈值提高 1.0c，测试 edge 安全边际"),
    ]
    out: List[Dict[str, object]] = []
    for scenario, extra_fee, extra_edge_threshold, note in scenarios:
        boundary_edge = cfg.boundary_edge if cfg.boundary_edge > 100 else cfg.boundary_edge + extra_edge_threshold
        stressed_cfg = DecisionConfig(
            f"{cfg.name}__stress_{scenario}",
            cfg.normal_edge + extra_edge_threshold,
            boundary_edge,
            cfg.min_depth,
            cfg.event_cap,
            cfg.exit_policy,
            cfg.max_trades_per_market,
            cfg.min_entry_gap_seconds,
        )
        stressed_fee = fee + extra_fee
        for split in ("validation", "test"):
            adjusted_rows: List[Dict[str, object]] = []
            for row in candidates:
                if row.get("split") != split:
                    continue
                copy = dict(row)
                entry_price = float(copy["entry_price"])
                edge = float(copy["q_model"]) - entry_price - stressed_fee
                copy["edge"] = edge
                copy["full_kelly_fraction"] = max(0.0, edge / max(1e-6, 1.0 - entry_price))
                adjusted_rows.append(copy)
            metrics, _, _, _ = simulate_decision_system(markets, adjusted_rows, stressed_cfg, split, stressed_fee)
            out.append(
                {
                    "scenario": scenario,
                    "note": note,
                    "split": split,
                    "extra_fee": extra_fee,
                    "extra_edge_threshold": extra_edge_threshold,
                    "trades": metrics.get("trades", 0),
                    "ending_bankroll": metrics.get("ending_bankroll", math.nan),
                    "total_return": metrics.get("total_return", math.nan),
                    "max_drawdown": metrics.get("max_drawdown", math.nan),
                    "profit_factor": metrics.get("profit_factor", math.nan),
                    "avg_edge": metrics.get("avg_edge", math.nan),
                }
            )
    return out


def rows_in_window(candidates: List[Dict[str, object]], start: datetime, end: datetime, split_name_: str) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    for row in candidates:
        ts = row.get("first_quote_ts")
        if isinstance(ts, datetime) and start <= ts <= end:
            copy = dict(row)
            copy["split"] = split_name_
            out.append(copy)
    return out


def walk_forward_rows(
    markets: List[Dict[str, object]],
    candidates: List[Dict[str, object]],
    fee: float,
    train_days: int,
    validation_days: int,
    test_days: int,
    step_days: int,
    max_folds: int,
    embargo_seconds: int,
    train_sample: int,
    validation_sample: int,
    tune_config: bool,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    starts = [row.get("first_quote_ts") for row in markets if isinstance(row.get("first_quote_ts"), datetime)]
    if not starts:
        return [], [], []
    window_start = min(starts)
    window_end = max(starts)
    metrics_rows: List[Dict[str, object]] = []
    trade_rows: List[Dict[str, object]] = []
    probability_rows: List[Dict[str, object]] = []
    fold = 0
    cursor = window_start
    while fold < max_folds:
        train_start = cursor
        train_end = train_start + timedelta(days=train_days)
        validation_start = train_end + timedelta(seconds=embargo_seconds)
        validation_end = validation_start + timedelta(days=validation_days)
        test_start = validation_end + timedelta(seconds=embargo_seconds)
        test_end = test_start + timedelta(days=test_days)
        if test_end > window_end + timedelta(seconds=1):
            break
        train_rows = rows_in_window(candidates, train_start, train_end, "train")
        validation_rows = rows_in_window(candidates, validation_start, validation_end, "validation")
        test_rows = rows_in_window(candidates, test_start, test_end, "test")
        fold_id = f"fold_{fold + 1}"
        if len(train_rows) < 5000 or len(validation_rows) < 1000 or len(test_rows) < 1000:
            metrics_rows.append(
                {
                    "fold": fold_id,
                    "status": "skipped_low_rows",
                    "train_rows": len(train_rows),
                    "validation_rows": len(validation_rows),
                    "test_rows": len(test_rows),
                    "train_start_cst": base.to_tz_text(train_start, base.SH_TZ),
                    "test_end_cst": base.to_tz_text(test_end, base.SH_TZ),
                }
            )
            fold += 1
            cursor += timedelta(days=step_days)
            continue

        model_train_rows = evenly_sample(train_rows, train_sample)
        model_validation_rows = evenly_sample(validation_rows, validation_sample)
        fold_model, fold_model_search = select_model(model_train_rows, model_validation_rows, WALK_FORWARD_FEATURE_SET_CANDIDATES)
        fold_rows = train_rows + validation_rows + test_rows
        attach_scores(fold_rows, fold_model, fee)
        if tune_config:
            fold_cfg, fold_config_search = tune_decision_config_grid(
                markets,
                fold_rows,
                fee,
                [base.SETTLE_POLICY],
                [0.020, 0.035],
                [0.060, 999.0],
                [50.0],
                [0.005, 0.010],
                [1, 2],
                [4, 8],
            )
            fold_config_combinations = 32
            fold_execution_mode = "fold_validation_search_32"
        else:
            fold_cfg = selected_fixed_decision_config()
            fold_config_search = [fixed_config_search_row(markets, fold_rows, fold_cfg, fee)]
            fold_config_combinations = 1
            fold_execution_mode = "fixed_selected_execution"
        val_metrics, _, _, _ = simulate_decision_system(markets, fold_rows, fold_cfg, "validation", fee)
        test_metrics, test_logs, _, _ = simulate_decision_system(markets, fold_rows, fold_cfg, "test", fee)
        labels_by_split = {"validation": validation_rows, "test": test_rows}
        for split_name_, split_rows in labels_by_split.items():
            probability = evaluate_probability(split_rows, [calibrated_prob(fold_model, row) for row in split_rows], split_name_)
            probability.update({"fold": fold_id})
            probability_rows.append(probability)
        best_model_row = next((row for row in fold_model_search if row.get("feature_set") == fold_model.feature_set_name), {})
        top_cfg_row = fold_config_search[0] if fold_config_search else {}
        for split_name_, metrics in (("validation", val_metrics), ("test", test_metrics)):
            row = dict(metrics)
            row.update(
                {
                    "fold": fold_id,
                    "status": "ok",
                    "fold_split": split_name_,
                    "train_start_cst": base.to_tz_text(train_start, base.SH_TZ),
                    "train_end_cst": base.to_tz_text(train_end, base.SH_TZ),
                    "validation_start_cst": base.to_tz_text(validation_start, base.SH_TZ),
                    "validation_end_cst": base.to_tz_text(validation_end, base.SH_TZ),
                    "test_start_cst": base.to_tz_text(test_start, base.SH_TZ),
                    "test_end_cst": base.to_tz_text(test_end, base.SH_TZ),
                    "train_rows": len(train_rows),
                    "validation_rows": len(validation_rows),
                    "test_rows": len(test_rows),
                    "selected_feature_set": fold_model.feature_set_name,
                    "selected_model_kind": "mixed_blend" if fold_model.blend_members else "single",
                    "selected_members": " + ".join(name for name, _, _ in fold_model.blend_members),
                    "selected_model_validation_logloss": best_model_row.get("validation_logloss", math.nan),
                    "selected_model_selection_loss": best_model_row.get("selection_loss", math.nan),
                    "selected_config": fold_cfg.name,
                    "execution_config_mode": fold_execution_mode,
                    "fold_config_validation_score": top_cfg_row.get("validation_score", math.nan),
                    "config_combinations": fold_config_combinations,
                }
            )
            metrics_rows.append(row)
        for log in test_logs:
            copy = dict(log)
            copy["fold"] = fold_id
            copy["fold_split"] = "test"
            trade_rows.append(copy)
        fold += 1
        cursor += timedelta(days=step_days)
    return metrics_rows, trade_rows, probability_rows


def quantile_thresholds(values: List[float]) -> Tuple[float, float]:
    xs = sorted(v for v in values if finite(v))
    if len(xs) < 3:
        return math.nan, math.nan
    return xs[int((len(xs) - 1) * 0.33)], xs[int((len(xs) - 1) * 0.67)]


def bucket3(value: object, lo: float, hi: float) -> str:
    if not finite(value) or not finite(lo) or not finite(hi):
        return "unknown"
    x = float(value)
    if x <= lo:
        return "low"
    if x <= hi:
        return "mid"
    return "high"


def stability_regime_rows(candidates: List[Dict[str, object]], logs: List[Dict[str, object]]) -> List[Dict[str, object]]:
    by_candidate = {str(row.get("candidate_id")): row for row in candidates}
    enriched: List[Dict[str, object]] = []
    for log in logs:
        row = by_candidate.get(str(log.get("candidate_id")))
        if row is None:
            continue
        features: Dict[str, float] = row["features"]  # type: ignore[assignment]
        first_ts = row.get("first_quote_ts")
        date_text = base.to_tz_text(first_ts, base.SH_TZ)[:10] if isinstance(first_ts, datetime) else ""
        enriched.append(
            {
                "split": log.get("split"),
                "date": date_text,
                "session": row.get("session_et"),
                "side": log.get("side"),
                "price_bucket": log.get("price_bucket"),
                "entry_second_bucket": "<=60s" if float(log.get("entry_second", 999)) <= 60 else "<=180s" if float(log.get("entry_second", 999)) <= 180 else ">180s",
                "vol": features.get("realized_vol_first2m"),
                "depth": row.get("entry_size"),
                "spread": features.get("spread_side"),
                "overround": features.get("overround_median_first2m"),
                "pnl_usd": log.get("pnl_usd"),
                "edge": log.get("edge"),
                "q_model": log.get("q_model"),
                "entry_price": log.get("entry_price"),
            }
        )
    vol_lo, vol_hi = quantile_thresholds([val(row.get("vol"), math.nan) for row in enriched])
    depth_lo, depth_hi = quantile_thresholds([val(row.get("depth"), math.nan) for row in enriched])
    spread_lo, spread_hi = quantile_thresholds([val(row.get("spread"), math.nan) for row in enriched])
    over_lo, over_hi = quantile_thresholds([val(row.get("overround"), math.nan) for row in enriched])
    for row in enriched:
        row["vol_bucket"] = bucket3(row.get("vol"), vol_lo, vol_hi)
        row["depth_bucket"] = bucket3(row.get("depth"), depth_lo, depth_hi)
        row["spread_bucket"] = bucket3(row.get("spread"), spread_lo, spread_hi)
        row["overround_bucket"] = bucket3(row.get("overround"), over_lo, over_hi)

    groups: Dict[Tuple[str, str, str, str], List[Dict[str, object]]] = defaultdict(list)
    dimensions = ["date", "session", "side", "price_bucket", "entry_second_bucket", "vol_bucket", "depth_bucket", "spread_bucket", "overround_bucket"]
    for row in enriched:
        split = str(row.get("split", ""))
        for dimension in dimensions:
            groups[(split, dimension, str(row.get(dimension, "")), "selected_trades")].append(row)

    out: List[Dict[str, object]] = []
    for (split, dimension, bucket, sample), rows in sorted(groups.items()):
        pnls = [float(row["pnl_usd"]) for row in rows if finite(row.get("pnl_usd"))]
        if not pnls:
            continue
        wins = sum(x for x in pnls if x > 0)
        losses = sum(x for x in pnls if x < 0)
        total_pnl = sum(pnls)
        trades = len(pnls)
        out.append(
            {
                "split": split,
                "dimension": dimension,
                "bucket": bucket,
                "sample": sample,
                "trades": trades,
                "total_pnl": total_pnl,
                "avg_pnl": statistics.mean(pnls),
                "win_rate": sum(1 for x in pnls if x > 0) / trades,
                "profit_factor": wins / abs(losses) if losses < 0 else math.nan,
                "avg_edge": mean(float(row["edge"]) for row in rows if finite(row.get("edge"))),
                "avg_q": mean(float(row["q_model"]) for row in rows if finite(row.get("q_model"))),
                "avg_entry_price": mean(float(row["entry_price"]) for row in rows if finite(row.get("entry_price"))),
                "verdict": "strong" if trades >= 20 and total_pnl > 0 and sum(1 for x in pnls if x > 0) / trades >= 0.55 else "thin" if trades < 20 else "weak",
            }
        )
    out.sort(key=lambda row: (str(row["split"]), str(row["dimension"]), -int(row["trades"])))
    return out


def row_at_or_after_ts(rows: List[Dict[str, object]], ts: datetime) -> Optional[Dict[str, object]]:
    for row in rows:
        row_ts = row.get("ts_utc")
        if isinstance(row_ts, datetime) and row_ts >= ts:
            return row
    return None


def paper_replay_rows(
    candidates: List[Dict[str, object]],
    logs: List[Dict[str, object]],
    cfg: DecisionConfig,
    fee: float,
    delay_seconds: int = 4,
    max_adverse_slippage: float = 0.01,
    limit: int = PAPER_REPLAY_LIMIT_DEFAULT,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]], List[Dict[str, object]]]:
    by_candidate = {str(row.get("candidate_id")): row for row in candidates}
    logs_sorted = sorted(logs, key=lambda row: str(row.get("first_quote_ts", "")))
    bankroll_by_split: Dict[str, float] = defaultdict(lambda: STARTING_BANKROLL)
    rows_out: List[Dict[str, object]] = []
    summary_groups: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    scenarios = [(4, 0.01), (8, 0.01), (12, 0.01), (4, 0.005)]
    scenario_rows: List[Dict[str, object]] = []

    for delay, slip in scenarios:
        bankroll_scenario: Dict[str, float] = defaultdict(lambda: STARTING_BANKROLL)
        scenario_logs: List[Dict[str, object]] = []
        for log in logs_sorted:
            candidate = by_candidate.get(str(log.get("candidate_id")))
            if candidate is None:
                continue
            split = str(log.get("split", ""))
            market_rows = candidate.get("market", {}).get("rows", []) if isinstance(candidate.get("market"), dict) else []
            entry_ts = candidate.get("entry_ts")
            if not isinstance(market_rows, list) or not isinstance(entry_ts, datetime):
                continue
            delayed = row_at_or_after_ts(market_rows, entry_ts + timedelta(seconds=delay))
            side = str(log.get("side", ""))
            delayed_price = base.price_from_row(delayed, "buy_up_cents" if side == "buy_up" else "buy_down_cents")
            delayed_size = base.size_from_row(delayed, "buy_up_size" if side == "buy_up" else "buy_down_size")
            signal_price = float(log.get("entry_price", math.nan))
            status = "no_quote"
            fill_cost = 0.0
            pnl = 0.0
            fill_ratio = 0.0
            exit_reason = ""
            if delayed is not None and finite(delayed_price) and finite(delayed_size) and delayed_size > 0:
                if float(delayed_price) <= signal_price + slip:
                    stake_fraction_ = float(log.get("stake_fraction", 0.0)) if finite(log.get("stake_fraction")) else 0.0
                    target_cost = min(bankroll_scenario[split] * stake_fraction_, bankroll_scenario[split], float(delayed_size) * float(delayed_price))
                    if target_cost > 0:
                        shares = target_cost / float(delayed_price)
                        exit_reason, pnl_per_share, _, _, _ = base.simulate_exit(candidate["market"], side, shares, float(delayed_price), delayed["ts_utc"], cfg.exit_policy, fee)  # type: ignore[index]
                        pnl = shares * pnl_per_share
                        bankroll_scenario[split] += pnl
                        fill_cost = target_cost
                        intended_cost = float(log.get("target_cost", target_cost)) if finite(log.get("target_cost")) else target_cost
                        fill_ratio = target_cost / intended_cost if intended_cost > 0 else 1.0
                        status = "full_fill" if fill_ratio >= 0.99 else "partial_fill"
                    else:
                        status = "zero_size"
                else:
                    status = "price_moved"
            scenario_logs.append({"split": split, "status": status, "pnl_usd": pnl, "fill_cost": fill_cost, "fill_ratio": fill_ratio})
        for split in ("train", "validation", "test"):
            split_logs = [row for row in scenario_logs if row["split"] == split]
            fills = [row for row in split_logs if row["status"] in {"full_fill", "partial_fill"}]
            scenario_rows.append(
                {
                    "scenario": f"delay{delay}s_slip{slip:.3f}",
                    "delay_seconds": delay,
                    "max_adverse_slippage": slip,
                    "split": split,
                    "signals": len(split_logs),
                    "fills": len(fills),
                    "fill_rate": len(fills) / len(split_logs) if split_logs else math.nan,
                    "ending_bankroll": bankroll_scenario[split],
                    "total_return": bankroll_scenario[split] / STARTING_BANKROLL - 1.0,
                    "avg_fill_ratio": mean(float(row["fill_ratio"]) for row in fills),
                    "total_pnl": sum(float(row["pnl_usd"]) for row in fills),
                }
            )

    for log in logs_sorted:
        candidate = by_candidate.get(str(log.get("candidate_id")))
        if candidate is None:
            continue
        split = str(log.get("split", ""))
        market_rows = candidate.get("market", {}).get("rows", []) if isinstance(candidate.get("market"), dict) else []
        entry_ts = candidate.get("entry_ts")
        if not isinstance(market_rows, list) or not isinstance(entry_ts, datetime):
            continue
        side = str(log.get("side", ""))
        delayed = row_at_or_after_ts(market_rows, entry_ts + timedelta(seconds=delay_seconds))
        delayed_ts = delayed.get("ts_utc") if isinstance(delayed, dict) else None
        delayed_price = base.price_from_row(delayed, "buy_up_cents" if side == "buy_up" else "buy_down_cents")
        delayed_size = base.size_from_row(delayed, "buy_up_size" if side == "buy_up" else "buy_down_size")
        signal_price = float(log.get("entry_price", math.nan))
        status = "no_quote"
        fill_cost = 0.0
        pnl = 0.0
        fill_ratio = 0.0
        exit_reason = ""
        if delayed is not None and finite(delayed_price) and finite(delayed_size) and delayed_size > 0:
            if float(delayed_price) <= signal_price + max_adverse_slippage:
                stake_fraction_ = float(log.get("stake_fraction", 0.0)) if finite(log.get("stake_fraction")) else 0.0
                target_cost = min(bankroll_by_split[split] * stake_fraction_, bankroll_by_split[split], float(delayed_size) * float(delayed_price))
                if target_cost > 0:
                    shares = target_cost / float(delayed_price)
                    exit_reason, pnl_per_share, exit_ts, exit_price, skipped_liq = base.simulate_exit(candidate["market"], side, shares, float(delayed_price), delayed["ts_utc"], cfg.exit_policy, fee)  # type: ignore[index]
                    pnl = shares * pnl_per_share
                    bankroll_by_split[split] += pnl
                    fill_cost = target_cost
                    intended_cost = float(log.get("target_cost", target_cost)) if finite(log.get("target_cost")) else target_cost
                    fill_ratio = target_cost / intended_cost if intended_cost > 0 else 1.0
                    status = "full_fill" if fill_ratio >= 0.99 else "partial_fill"
                else:
                    status = "zero_size"
            else:
                status = "price_moved"
        row_out = {
            "split": split,
            "first_quote_cst": log.get("first_quote_cst"),
            "market_id": log.get("market_id"),
            "side": side,
            "signal_entry_second": log.get("entry_second"),
            "signal_price": signal_price,
            "q_model": log.get("q_model"),
            "edge": log.get("edge"),
            "paper_delay_seconds": delay_seconds,
            "max_adverse_slippage": max_adverse_slippage,
            "paper_check_ts": delayed_ts.isoformat() if isinstance(delayed_ts, datetime) else "",
            "paper_price": delayed_price,
            "paper_size": delayed_size,
            "price_slippage": float(delayed_price) - signal_price if finite(delayed_price) and finite(signal_price) else math.nan,
            "status": status,
            "fill_cost": fill_cost,
            "fill_ratio": fill_ratio,
            "paper_pnl_usd": pnl,
            "original_backtest_pnl_usd": log.get("pnl_usd"),
            "exit_reason": exit_reason,
            "paper_bankroll_after": bankroll_by_split[split],
        }
        summary_groups[(split, status)].append(row_out)
        if len(rows_out) < limit or split == "test":
            rows_out.append(row_out)
    summary_rows: List[Dict[str, object]] = []
    for split in ("train", "validation", "test"):
        split_rows = [row for row in rows_out if row["split"] == split]
        all_split_signals = [row for row in logs_sorted if row.get("split") == split]
        fills = [row for row in split_rows if row["status"] in {"full_fill", "partial_fill"}]
        summary_rows.append(
            {
                "split": split,
                "scenario": f"delay{delay_seconds}s_slip{max_adverse_slippage:.3f}",
                "signals": len(all_split_signals),
                "displayed_rows": len(split_rows),
                "fills_displayed": len(fills),
                "ending_bankroll": bankroll_by_split[split],
                "total_return": bankroll_by_split[split] / STARTING_BANKROLL - 1.0,
                "display_fill_rate": len(fills) / len(split_rows) if split_rows else math.nan,
                "avg_fill_ratio_displayed": mean(float(row["fill_ratio"]) for row in fills),
                "display_total_pnl": sum(float(row["paper_pnl_usd"]) for row in fills),
            }
        )
    for (split, status), rows in summary_groups.items():
        summary_rows.append(
            {
                "split": split,
                "scenario": f"status::{status}",
                "signals": len(rows),
                "displayed_rows": len(rows),
                "fills_displayed": sum(1 for row in rows if row["status"] in {"full_fill", "partial_fill"}),
                "ending_bankroll": math.nan,
                "total_return": math.nan,
                "display_fill_rate": math.nan,
                "avg_fill_ratio_displayed": mean(float(row["fill_ratio"]) for row in rows),
                "display_total_pnl": sum(float(row["paper_pnl_usd"]) for row in rows),
            }
        )
    return rows_out, summary_rows, scenario_rows


def equity_curve_rows(curves: Dict[str, List[float]]) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for split_name_, values in curves.items():
        for i, value in enumerate(values):
            rows.append({"split": split_name_, "event_index": i, "bankroll": value})
    return rows


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: List[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: clean_csv_value(row.get(key, "")) for key in fieldnames})


def clean_csv_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def json_ready(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: json_ready(val_) for key, val_ in value.items() if key != "market"}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def fmt_money(value: object) -> str:
    return "" if not finite(value) else f"${float(value):,.2f}"


def fmt_pct(value: object) -> str:
    return "" if not finite(value) else f"{float(value):.2%}"


def build_report(
    manifest: Dict[str, object],
    model: ModelBundle,
    best_cfg: DecisionConfig,
    probability_rows: List[Dict[str, object]],
    selected_metrics: List[Dict[str, object]],
    weights: List[Dict[str, object]],
    factor_rows: List[Dict[str, object]],
    bucket_rows: List[Dict[str, object]],
    model_family_rows: List[Dict[str, object]],
    execution_group_rows: List[Dict[str, object]],
    contribution_rows: List[Dict[str, object]],
    model_mix: List[Dict[str, object]],
    decision_funnel: List[Dict[str, object]],
    stress_rows: List[Dict[str, object]],
) -> str:
    test = next((row for row in selected_metrics if row["split"] == "test"), {})
    val_row = next((row for row in selected_metrics if row["split"] == "validation"), {})
    lines = [
        "# Polymarket 5min 稳健交易系统",
        "",
        "## 研究过程和约束",
        "",
        "- 目标不是再堆一个固定策略，而是按 workbook 做成 `q -> edge -> execution gate -> sizing -> validation search -> test` 的交易系统。",
        "- `q` 是校准后的胜率估计；实际下单只看 `q - 可成交买价 - fee` 是否超过阈值。",
        "- 当前入口模式是 dense4s：从 quote path 中按 4 秒节流生成候选，不代表机械地每 4 秒下单；是否下单仍由 edge、depth、spread、risk gate 决定。",
        "- 多次交易被放在执行层穷举：每个 5min market 最多 1/2/4 次，最小入场间隔 4/8/16 秒；同一时间只选一边最高 edge。",
        "- 每个 5min market 有总 event cap；多次交易会把单笔 cap 拆小，避免同一个事件里隐性加杠杆。",
        "- `market_prior_only` 只作为市场基准展示，不允许被选成交易模型，因为它通常没有扣除价差和 fee 后的可交易 edge。",
        "- 模型/参数只用 train + validation；test split 不参与选模型、不参与选执行参数。",
        "",
        "## 数据和切分",
        "",
        f"- 数据窗口：{manifest.get('window_start_cst')} 到 {manifest.get('window_end_cst')}",
        f"- Market 数：{manifest.get('window_events')}",
        f"- 候选入场数：{manifest.get('candidates')}",
        f"- Train/validation/test 候选：{manifest.get('train_rows')} / {manifest.get('validation_rows')} / {manifest.get('test_rows')}",
        f"- 模型训练/校准抽样：{manifest.get('model_train_rows')} / {manifest.get('model_validation_rows')}",
        f"- Embargo：{manifest.get('embargo_seconds')} 秒",
        f"- 本次执行配置评估：{manifest.get('config_combinations')} 组，模式 `{manifest.get('config_search_mode')}`；完整执行网格是 {manifest.get('full_config_combinations')} 组。",
        f"- 快速多交易/edge 扫描：{'已开启' if manifest.get('fast_execution_scan_enabled') else '默认关闭'}，本次 {manifest.get('fast_execution_scan_combinations')} 组（{'宽扫描' if manifest.get('wide_fast_execution_scan') else '默认扫描'}）。",
        f"- Rolling walk-forward：{manifest.get('walk_forward_folds_run')} 个 fold，执行参数{'每折重选' if manifest.get('walk_forward_tune_config') else '固定为主报告选中配置'}。",
        "",
        "## 选中的交易系统",
        "",
        f"- 模型类型：`{manifest.get('selected_model_kind')}`",
        f"- 模型因子组合：`{model.feature_set_name}`（{len(model.feature_names)} 个底层因子），L2 {model.l2:g}",
        f"- 执行配置：`{best_cfg.name}`",
        f"- 普通价格 edge 阈值：{best_cfg.normal_edge:.4f}",
        f"- 边界价格 edge 阈值：{'禁止边界价入场' if best_cfg.boundary_edge > 100 else f'{best_cfg.boundary_edge:.4f}'}",
        f"- 最小盘口深度：{best_cfg.min_depth:.0f}",
        f"- 单 market 总 cap：{best_cfg.event_cap:.2%}",
        f"- 每个 market 最多交易次数：{best_cfg.max_trades_per_market}",
        f"- 最小入场间隔：{best_cfg.min_entry_gap_seconds} 秒",
        f"- 出场策略：`{best_cfg.exit_policy.name}`",
        f"- 边界价格定义：p>={BOUNDARY_PRICE:.2f} 或 p<={LOW_BOUNDARY_PRICE:.2f}",
        "",
        "## 模型混合结构",
        "",
    ]
    if model.blend_members:
        lines.append("- 这是 mixed/blended 模型：上层输入不是原始因子，而是几个基础模型的 raw logit。")
    else:
        lines.append("- 这是 single logistic 模型：直接把标准化因子线性相加成 raw logit。")
    for row in model_mix:
        lines.append(
            f"- `{row['layer']}` `{row['name']}`：weight {float(row.get('weight', math.nan)):.4f}，features {row.get('features')}，L2 {float(row.get('l2', math.nan)):.4g}"
        )
    lines.extend(
        [
            "",
            "## 最终 OOS 结果",
            "",
            f"- Validation：{fmt_money(val_row.get('ending_bankroll'))}，收益 {fmt_pct(val_row.get('total_return'))}，交易 {val_row.get('trades', 0)}，DD {fmt_pct(val_row.get('max_drawdown'))}",
            f"- Test：{fmt_money(test.get('ending_bankroll'))}，收益 {fmt_pct(test.get('total_return'))}，交易 {test.get('trades', 0)}，DD {fmt_pct(test.get('max_drawdown'))}",
            "",
            "## 概率质量",
            "",
        ]
    )
    for row in probability_rows:
        lines.append(
            f"- {row['split']}：model logloss {float(row.get('logloss_model', math.nan)):.4f}，market baseline {float(row.get('logloss_market_baseline', math.nan)):.4f}，delta {float(row.get('logloss_delta_vs_market', math.nan)):.4f}，AUC {float(row.get('auc_model', math.nan)):.3f}，ECE {float(row.get('ece_model_10', math.nan)):.3f}"
        )
    lines.extend(["", "## 因子组合 / mixed 搜索", ""])
    for row in model_family_rows[:8]:
        lines.append(
            f"- `{row['feature_set']}`：kind {row.get('model_kind')}，validation logloss {float(row.get('validation_logloss', math.nan)):.4f}，selection loss {float(row.get('selection_loss', math.nan)):.4f}，penalty {float(row.get('complexity_penalty', 0.0)):.4f}，因子数 {row.get('feature_count')}，L2 {float(row.get('l2', math.nan)):g}"
        )
    lines.extend(["", "## 最大模型权重", ""])
    for row in weights[:10]:
        lines.append(f"- `{row['feature']}`: {float(row['weight']):.4f}")
    lines.extend(["", "## 选中交易的因子组贡献", ""])
    test_contrib = [row for row in contribution_rows if row.get("split") == "test"]
    test_contrib.sort(key=lambda row: abs(float(row.get("avg_logit_contribution", 0.0))), reverse=True)
    for row in test_contrib[:8]:
        lines.append(
            f"- `{row['group']}`：avg logit contribution {float(row.get('avg_logit_contribution', math.nan)):.4f}，avg abs {float(row.get('avg_abs_logit_contribution', math.nan)):.4f}，正贡献比例 {fmt_pct(row.get('positive_contribution_rate'))}"
        )
    lines.extend(["", "## 稳定因子提示", ""])
    stable = [row for row in factor_rows if row.get("stable_corr_pnl_sign") == 1]
    stable.sort(
        key=lambda row: sum(abs(float(row.get(f"{name}_corr_pnl", 0.0))) for name in ("train", "validation", "test") if finite(row.get(f"{name}_corr_pnl"))),
        reverse=True,
    )
    for row in stable[:12]:
        lines.append(
            f"- `{row['feature']}`：train pnl corr {float(row.get('train_corr_pnl', math.nan)):.3f}，validation {float(row.get('validation_corr_pnl', math.nan)):.3f}，test {float(row.get('test_corr_pnl', math.nan)):.3f}"
        )
    lines.extend(["", "## 多交易执行组合摘要", ""])
    for row in execution_group_rows[:10]:
        lines.append(
            f"- max_trades={row['max_trades_per_market']}，gap={row['min_entry_gap_seconds']}s，exit={row['exit_policy']}：validation score {float(row.get('validation_score', math.nan)):.3f}，ending {fmt_money(row.get('ending_bankroll'))}，trades {row.get('trades')}"
        )
    lines.extend(["", "## 决策漏斗", ""])
    for row in decision_funnel:
        if row.get("split") in {"validation", "test"}:
            lines.append(
                f"- {row['split']} {row['stage']}：{row['count']}（候选占比 {fmt_pct(row.get('share_of_candidates'))}，较上一层保留 {fmt_pct(row.get('retention_from_previous'))}）"
            )
    lines.extend(["", "## 成本/滑点压力测试", ""])
    for row in stress_rows:
        if row.get("split") == "test":
            lines.append(
                f"- {row['scenario']}：ending {fmt_money(row.get('ending_bankroll'))}，return {fmt_pct(row.get('total_return'))}，trades {row.get('trades')}，DD {fmt_pct(row.get('max_drawdown'))}"
            )
    lines.extend(["", "## 价格桶执行表现", ""])
    for row in bucket_rows:
        lines.append(
            f"- {row['split']} {row['price_bucket']}：交易 {row['trades']}，总 PnL {float(row.get('total_pnl', 0.0)):.2f}，胜率 {fmt_pct(row.get('win_rate'))}，avg edge {float(row.get('avg_edge', math.nan)):.4f}"
        )
    lines.extend(
        [
            "",
            "## 稳健性解释",
            "",
            "- 如果 validation 很强但 test 变弱，不应该反向调 test；下一步应简化模型或等更多月份数据做 walk-forward。",
            "- 边界价格单独 gate，因为 p>=0.90 / p<=0.10 的尾部风险不对称。",
            "- 低买高卖 exit 已进入 validation 搜索；如果最终没选中，说明在当前数据和风控下不如被选配置。",
            "- 当前仍没有真实队列位置、订单失败率和滑点模型；paper trading 前不建议放大仓位。",
        ]
    )
    return "\n".join(lines) + "\n"


def build_html(report_md: str, payload: Dict[str, object]) -> str:
    payload_json = json.dumps(json_ready(payload), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket 5min 稳健交易系统</title>
  <style>
    :root {{ --bg:#f7f7f4; --panel:#fff; --line:#d9d7cf; --ink:#202124; --muted:#666b73; --good:#127a3a; --bad:#b42318; --accent:#0f766e; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    header {{ position:sticky; top:0; z-index:5; background:#fbfbf8; border-bottom:1px solid var(--line); padding:16px 22px; }}
    h1 {{ font-size:20px; margin:0 0 8px; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:8px; color:var(--muted); }}
    .chip {{ border:1px solid var(--line); background:#eef3f1; border-radius:6px; padding:4px 8px; }}
    main {{ padding:18px 22px 30px; }}
    .grid {{ display:grid; grid-template-columns:repeat(4,minmax(160px,1fr)); gap:10px; margin-bottom:14px; }}
    .metric {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .metric .label {{ color:var(--muted); font-size:12px; }}
    .metric .value {{ font-size:22px; font-weight:700; margin-top:4px; }}
    h2 {{ font-size:16px; margin:24px 0 8px; }}
    table {{ width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line); margin:12px 0 18px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:8px 9px; text-align:left; white-space:nowrap; }}
    th {{ background:#efeee8; color:#3f4348; font-size:12px; }}
    .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .good {{ color:var(--good); font-weight:650; }}
    .bad {{ color:var(--bad); font-weight:650; }}
    .scroll {{ overflow-x:auto; }}
    .notes {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin:0 0 16px; }}
    .notes ul {{ margin:8px 0 0 18px; padding:0; }}
    .notes li {{ margin:4px 0; }}
    .sectionLead {{ color:var(--muted); max-width:980px; margin:0 0 10px; }}
    .three {{ display:grid; grid-template-columns:repeat(3,minmax(220px,1fr)); gap:10px; margin:10px 0 16px; }}
    .box {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    .box b {{ display:block; margin-bottom:6px; }}
    .box .small {{ color:var(--muted); font-size:12px; }}
    .equation {{ font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; background:#fff; border:1px solid var(--line); border-radius:8px; padding:12px; margin:8px 0 16px; overflow-x:auto; }}
    .tag {{ display:inline-block; border:1px solid var(--line); background:#f4f4ee; border-radius:5px; padding:2px 6px; font-size:12px; margin-right:4px; }}
    .selected {{ color:var(--good); font-weight:650; }}
    .muted {{ color:var(--muted); }}
    .flow {{ display:grid; grid-template-columns:repeat(6,minmax(120px,1fr)); gap:8px; margin:10px 0 16px; }}
    .flow .step {{ background:#ffffff; border:1px solid var(--line); border-radius:8px; padding:10px; min-height:92px; }}
    .flow .step b {{ display:block; margin-bottom:5px; }}
    .formula {{ display:grid; grid-template-columns:repeat(4,minmax(180px,1fr)); gap:8px; margin:10px 0 16px; }}
    .formula div {{ background:#fff; border:1px solid var(--line); border-radius:8px; padding:10px; }}
    code {{ background:#f0f0eb; border:1px solid #dedbd2; border-radius:4px; padding:1px 4px; }}
    .chartWrap {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; margin:12px 0 18px; }}
    canvas {{ width:100%; height:260px; display:block; }}
    pre {{ white-space:pre-wrap; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; }}
    @media(max-width:900px) {{ .grid,.three {{ grid-template-columns:1fr 1fr; }} .flow,.formula {{ grid-template-columns:1fr 1fr; }} }}
    @media(max-width:620px) {{ .grid,.three,.flow,.formula {{ grid-template-columns:1fr; }} main {{ padding:14px; }} }}
  </style>
</head>
<body>
<header>
  <h1>Polymarket 5min 稳健交易系统</h1>
  <div class="chips" id="chips"></div>
</header>
<main>
      <section class="notes">
        <h2>先看结论：这是概率模型 + 执行系统，不是单条硬编码策略</h2>
        <ul>
          <li>模型层只回答一个问题：在当前买价和当前时刻，buy_up 或 buy_down 的真实胜率 q 大概是多少。</li>
          <li>执行层再回答第二个问题：q 扣掉买价、fee、流动性和风险限制后，是否值得下单、下多少、什么时候退出。</li>
          <li>mixed/blended 候选已经加入搜索；如果没有被选中，说明它在 validation 上扣除复杂度惩罚后没有赢过更简单的模型。</li>
          <li>5 分钟内可以多次观察，候选按 4 秒节流；但同一时刻只选一边，并且每个 market 共用一个 event cap。</li>
          <li>所有模型和执行参数只看 train + validation；test 是最终验收，不用于反向调参。</li>
        </ul>
      </section>
      <h2>整体结构</h2>
      <p class="sectionLead">可以把系统看成三层：第一层把 quote path 变成候选交易点；第二层把候选点映射成校准胜率 q；第三层把 q 变成受风控约束的交易。</p>
      <section class="three">
        <div class="box"><b>A. 数据到候选</b><span class="tag">4s buffer</span><span class="tag">buy_up/down</span><br><span class="small">每个 5min market 内按时间顺序取可买快照，不用未来信息。</span></div>
        <div class="box"><b>B. 候选到 q</b><span class="tag">logistic</span><span class="tag">Platt</span><br><span class="small">市场先验、BTC 方向、盘口、时间等因子线性混合成 logit，再校准成概率。</span></div>
        <div class="box"><b>C. q 到交易</b><span class="tag">edge gate</span><span class="tag">Kelly cap</span><br><span class="small">只有 q - price - fee 超过阈值，且深度/价差/overround 过关，才进入执行。</span></div>
      </section>
      <h2>公式链路</h2>
      <div class="equation">
        x_i = clip((feature_i - train_mean_i) / train_std_i, -20, 20)<br>
        single: z = b + Σ w_i x_i<br>
        mixed: z_mix = b_meta + Σ α_j z_j，其中 z_j 是某个基础模型的 raw logit<br>
        q = sigmoid(calibration_intercept + calibration_slope * z)<br>
        edge = q - entry_price - fee<br>
        full_kelly = edge / (1 - entry_price)，stake_fraction = min(event_cap / max_trades, 0.25 * full_kelly)
      </div>
      <h2>模型步骤拆解</h2>
      <div class="scroll"><table id="modelStepTable"></table></div>
      <h2>模型混合结构</h2>
      <div class="scroll"><table id="modelMixTable"></table></div>
      <h2>因子蓝图</h2>
      <p class="sectionLead">这里不是说所有因子都进了最终模型。selected=1 才是本轮选中的底层因子；没选中的因子仍被试过或作为诊断展示。</p>
      <div class="scroll"><table id="featureBlueprintTable"></table></div>
	  <section class="grid" id="cards"></section>
	  <h2>权益曲线</h2>
	  <div class="chartWrap"><canvas id="curveChart" width="1200" height="320"></canvas></div>
	  <h2>Train / Validation / Test 指标</h2>
	  <div class="scroll"><table id="splitTable"></table></div>
	  <h2>概率质量</h2>
	  <div class="scroll"><table id="probTable"></table></div>
	  <h2>校准分桶</h2>
	  <div class="scroll"><table id="calibrationTable"></table></div>
	  <h2>因子组合搜索</h2>
	  <div class="scroll"><table id="familyTable"></table></div>
	  <h2>模型权重</h2>
	  <div class="scroll"><table id="weightTable"></table></div>
	  <h2>因子组贡献</h2>
	  <div class="scroll"><table id="contribTable"></table></div>
	  <h2>单笔交易解释</h2>
      <p class="sectionLead">单笔解释是 logit 贡献，不是美元 PnL 归因。看它的顺序：因子组贡献推高/压低 raw logit，校准后得到 q，再和价格相减得到 edge。</p>
	  <div class="scroll"><table id="explainTable"></table></div>
	  <h2>决策漏斗</h2>
      <p class="sectionLead">这张表显示候选经过 edge、深度、价差、overround、顺序执行限制之后还剩多少。实际执行远少于 4 秒候选数。</p>
	  <div class="scroll"><table id="funnelTable"></table></div>
	  <h2>成本 / 滑点压力测试</h2>
      <p class="sectionLead">这些场景不参与选模型，只是把同一个系统放到更差的 fee/slippage 或更高 edge 阈值下看敏感性。</p>
	  <div class="scroll"><table id="stressTable"></table></div>
	  <h2>稳定因子诊断</h2>
	  <div class="scroll"><table id="factorTable"></table></div>
	  <h2>执行配置搜索</h2>
	  <div class="scroll"><table id="configTable"></table></div>
	  <h2>多交易组合摘要</h2>
	  <div class="scroll"><table id="executionGroupTable"></table></div>
	  <h2>价格桶表现</h2>
	  <div class="scroll"><table id="bucketTable"></table></div>
  <h2>中文报告</h2>
  <pre>{html.escape(report_md)}</pre>
</main>
<script id="payload" type="application/json">{payload_json}</script>
<script>
	const data = JSON.parse(document.getElementById('payload').textContent);
		const fmtMoney = v => v == null ? '' : '$' + Number(v).toFixed(2);
		const fmtPct = v => v == null ? '' : (Number(v)*100).toFixed(2)+'%';
		const fmtNum = v => v == null ? '' : Number(v).toFixed(4);
        const fmtInt = v => v == null ? '' : Number(v).toLocaleString('en-US');
        const yesNo = v => Number(v) ? '<span class="selected">是</span>' : '<span class="muted">否</span>';
		const clsDelta = v => Number(v) < 0 ? 'num good' : 'num bad';
	function table(id, rows, cols) {{
	  const el=document.getElementById(id);
	  el.innerHTML='<thead><tr>'+cols.map(c=>'<th>'+c[0]+'</th>').join('')+'</tr></thead><tbody>'+
	    rows.map(r=>'<tr>'+cols.map(c=>'<td class="'+(typeof c[2]==='function'?c[2](r):(c[2]||''))+'">'+(c[1](r))+'</td>').join('')+'</tr>').join('')+'</tbody>';
	}}
	function drawCurve() {{
	  const canvas=document.getElementById('curveChart'), ctx=canvas.getContext('2d');
	  const curves=data.equityCurves || {{}};
	  const names=['train','validation','test'];
	  const colors={{train:'#65758b', validation:'#0f766e', test:'#b45309'}};
	  const all=names.flatMap(n=>curves[n]||[]);
	  if(!all.length) return;
	  const w=canvas.width, h=canvas.height, pad=34;
	  const min=Math.min(...all), max=Math.max(...all), span=Math.max(1e-9,max-min);
	  ctx.clearRect(0,0,w,h);
	  ctx.strokeStyle='#d9d7cf'; ctx.lineWidth=1;
	  ctx.beginPath(); ctx.moveTo(pad,h-pad); ctx.lineTo(w-pad,h-pad); ctx.lineTo(w-pad,pad); ctx.stroke();
	  names.forEach((name, idx)=>{{
	    const values=curves[name]||[];
	    if(values.length<2) return;
	    const x0=pad + idx*(w-2*pad)/3;
	    const xw=(w-2*pad)/3 - 10;
	    ctx.beginPath();
	    values.forEach((v,i)=>{{
	      const x=x0 + (i/Math.max(1,values.length-1))*xw;
	      const y=h-pad - ((v-min)/span)*(h-2*pad);
	      if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
	    }});
	    ctx.strokeStyle=colors[name]; ctx.lineWidth=2; ctx.stroke();
	    ctx.fillStyle=colors[name]; ctx.fillText(name+' '+fmtMoney(values[values.length-1]), x0, 18+idx*16);
	  }});
	}}
	const m=data.manifest, test=data.selectedMetrics.find(r=>r.split==='test') || {{}}, val=data.selectedMetrics.find(r=>r.split==='validation') || {{}};
document.getElementById('chips').innerHTML=[
  `窗口: ${{m.window_start_cst}} - ${{m.window_end_cst}}`,
  `Markets: ${{m.window_events}}`,
  `候选: ${{m.candidates}}`,
  `入口: ${{m.entry_mode}}/${{m.entry_step_seconds}}s`,
  `执行评估: ${{m.config_combinations}}/${{m.full_config_combinations}}`,
  `模式: ${{m.config_search_mode}}`,
  `选中: ${{m.selected_config}}`
].map(x=>`<span class="chip">${{x}}</span>`).join('');
document.getElementById('cards').innerHTML=[
  ['Validation', fmtMoney(val.ending_bankroll), fmtPct(val.total_return)],
  ['Test', fmtMoney(test.ending_bankroll), fmtPct(test.total_return)],
  ['选中模型', m.selected_feature_set, m.selected_model_kind],
  ['Test 交易数', fmtInt(test.trades || 0), ''],
  ['Test 回撤', fmtPct(test.max_drawdown), ''],
  ['模型 LL', fmtNum(m.selected_model_validation_logloss), 'selection '+fmtNum(m.selected_model_selection_loss)]
		].map(c=>`<div class="metric"><div class="label">${{c[0]}}</div><div class="value">${{c[1]}}</div><div class="label">${{c[2]||''}}</div></div>`).join('');
		drawCurve();
        table('modelStepTable', data.modelSteps || [], [
          ['步骤', r=>r.step], ['输入', r=>r.input], ['操作', r=>r.operation], ['输出', r=>r.output], ['学习位置', r=>r.learned_on], ['为什么需要', r=>r.why]
        ]);
        table('modelMixTable', data.modelMix || [], [
          ['layer', r=>r.layer], ['name', r=>r.name], ['weight', r=>fmtNum(r.weight),'num'], ['features', r=>r.features,'num'], ['L2', r=>fmtNum(r.l2),'num'], ['formula', r=>r.formula]
        ]);
        table('featureBlueprintTable', (data.featureBlueprint || []).slice(0,80), [
          ['selected', r=>yesNo(r.selected)], ['group', r=>r.group], ['feature', r=>r.feature], ['weight', r=>fmtNum(r.weight),'num'], ['说明', r=>r.description]
        ]);
		table('splitTable', data.selectedMetrics, [
  ['split', r=>r.split], ['trades', r=>r.trades,'num'], ['ending', r=>fmtMoney(r.ending_bankroll),'num'],
  ['return', r=>fmtPct(r.total_return),'num'], ['drawdown', r=>fmtPct(r.max_drawdown),'num'],
  ['win', r=>fmtPct(r.win_rate),'num'], ['PF', r=>fmtNum(r.profit_factor),'num'], ['boundary', r=>r.boundary_trades,'num']
]);
	table('probTable', data.probabilityMetrics, [
	  ['split', r=>r.split], ['rows', r=>r.rows,'num'], ['model LL', r=>fmtNum(r.logloss_model),'num'],
	  ['market LL', r=>fmtNum(r.logloss_market_baseline),'num'], ['delta', r=>fmtNum(r.logloss_delta_vs_market), r=>clsDelta(r.logloss_delta_vs_market)],
	  ['AUC', r=>fmtNum(r.auc_model),'num'], ['ECE', r=>fmtNum(r.ece_model_10),'num']
	]);
	table('calibrationTable', data.calibrationBuckets, [
	  ['split', r=>r.split], ['bucket', r=>r.bucket], ['rows', r=>r.rows,'num'], ['avg q', r=>fmtNum(r.avg_q),'num'],
	  ['actual win', r=>fmtPct(r.win_rate),'num'], ['market p', r=>fmtNum(r.avg_market_p),'num'], ['avg edge', r=>fmtNum(r.avg_edge),'num']
	]);
		table('familyTable', data.modelFamilySummary, [
		  ['feature set', r=>r.feature_set], ['kind', r=>r.model_kind], ['members', r=>r.member_count,'num'],
          ['features', r=>r.feature_count,'num'], ['L2', r=>fmtNum(r.l2),'num'], ['val LL', r=>fmtNum(r.validation_logloss),'num'],
          ['penalty', r=>fmtNum(r.complexity_penalty),'num'], ['selection', r=>fmtNum(r.selection_loss),'num'], ['members used', r=>r.members || '']
		]);
		table('weightTable', data.modelWeights.slice(0,40), [
		  ['group', r=>r.group || ''], ['feature', r=>r.feature], ['weight', r=>fmtNum(r.weight),'num'],
          ['mean', r=>fmtNum(r.mean),'num'], ['std', r=>fmtNum(r.std),'num'], ['note', r=>r.note || '']
		]);
	table('contribTable', data.contributionSummary, [
	  ['split', r=>r.split], ['因子组', r=>r.group], ['trades', r=>r.trades,'num'],
	  ['avg contrib', r=>fmtNum(r.avg_logit_contribution),'num'], ['avg abs', r=>fmtNum(r.avg_abs_logit_contribution),'num'],
	  ['正贡献率', r=>fmtPct(r.positive_contribution_rate),'num']
	]);
		table('explainTable', (data.tradeExplanations||[]).filter(r=>r.split==='test').slice(0,80), [
		  ['time', r=>r.first_quote_cst], ['side', r=>r.side], ['sec', r=>fmtNum(r.entry_second),'num'],
		  ['price', r=>fmtNum(r.entry_price),'num'], ['market p', r=>fmtNum(r.market_p),'num'], ['raw z', r=>fmtNum(r.raw_logit),'num'],
          ['cal z', r=>fmtNum(r.calibrated_logit),'num'], ['q', r=>fmtNum(r.q_model),'num'], ['edge', r=>fmtNum(r.edge),'num'],
          ['dominant', r=>r.dominant_group || ''], ['prior', r=>fmtNum(r.market_prior_contrib),'num'],
		  ['btc', r=>fmtNum(r.btc_contrib),'num'], ['interact', r=>fmtNum(r.interaction_contrib),'num'],
		  ['liq', r=>fmtNum(r.liquidity_micro_contrib),'num'], ['pnl', r=>fmtNum(r.pnl_usd),'num']
		]);
        table('funnelTable', data.decisionFunnel || [], [
          ['split', r=>r.split], ['stage', r=>r.stage], ['count', r=>fmtInt(r.count),'num'],
          ['候选占比', r=>fmtPct(r.share_of_candidates),'num'], ['较上一层保留', r=>fmtPct(r.retention_from_previous),'num'],
          ['执行markets', r=>r.executed_markets == null ? '' : fmtInt(r.executed_markets),'num']
        ]);
        table('stressTable', data.stressTests || [], [
          ['scenario', r=>r.scenario], ['split', r=>r.split], ['说明', r=>r.note],
          ['extra fee', r=>fmtNum(r.extra_fee),'num'], ['extra edge', r=>fmtNum(r.extra_edge_threshold),'num'],
          ['trades', r=>fmtInt(r.trades),'num'], ['ending', r=>fmtMoney(r.ending_bankroll),'num'],
          ['return', r=>fmtPct(r.total_return),'num'], ['DD', r=>fmtPct(r.max_drawdown),'num']
        ]);
		const stableFactors=(data.factorDiagnostics||[]).filter(r=>r.stable_corr_pnl_sign===1).sort((a,b)=>{{
	  const score=x=>Math.abs(Number(x.train_corr_pnl)||0)+Math.abs(Number(x.validation_corr_pnl)||0)+Math.abs(Number(x.test_corr_pnl)||0);
	  return score(b)-score(a);
	}});
	table('factorTable', stableFactors.slice(0,20), [
	  ['feature', r=>r.feature], ['train pnl corr', r=>fmtNum(r.train_corr_pnl),'num'], ['val pnl corr', r=>fmtNum(r.validation_corr_pnl),'num'],
	  ['test pnl corr', r=>fmtNum(r.test_corr_pnl),'num'], ['test AUC', r=>fmtNum(r.test_auc),'num']
	]);
	table('configTable', data.configSearchTop.slice(0,25), [
	  ['system', r=>r.system], ['trades', r=>r.trades,'num'], ['ending', r=>fmtMoney(r.ending_bankroll),'num'],
	  ['return', r=>fmtPct(r.total_return),'num'], ['DD', r=>fmtPct(r.max_drawdown),'num'], ['PF', r=>fmtNum(r.profit_factor),'num'],
	  ['max trades', r=>r.max_trades_per_market,'num'], ['gap', r=>r.min_entry_gap_seconds,'num'], ['score', r=>fmtNum(r.validation_score),'num']
	]);
	table('executionGroupTable', data.executionGroupSummary.slice(0,30), [
	  ['max trades', r=>r.max_trades_per_market,'num'], ['gap 秒', r=>r.min_entry_gap_seconds,'num'], ['exit', r=>r.exit_policy],
	  ['best ending', r=>fmtMoney(r.ending_bankroll),'num'], ['return', r=>fmtPct(r.total_return),'num'],
	  ['trades', r=>r.trades,'num'], ['multi markets', r=>r.multi_trade_markets,'num'], ['score', r=>fmtNum(r.validation_score),'num']
	]);
	table('bucketTable', data.bucketMetrics, [
  ['split', r=>r.split], ['bucket', r=>r.price_bucket], ['trades', r=>r.trades,'num'], ['PnL', r=>fmtNum(r.total_pnl),'num'],
  ['win', r=>fmtPct(r.win_rate),'num'], ['avg edge', r=>fmtNum(r.avg_edge),'num'], ['avg price', r=>fmtNum(r.avg_entry_price),'num']
]);
</script>
</body>
</html>"""


def build_html(report_md: str, payload: Dict[str, object]) -> str:
    payload_json = json.dumps(json_ready(payload), ensure_ascii=False)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket 5min 稳健交易系统</title>
  <style>
    :root {{
      --bg:#f4f5f2; --panel:#ffffff; --panel2:#fafaf7; --ink:#1f2328; --muted:#667085;
      --line:#d8dcd2; --soft:#eef1ea; --accent:#0f766e; --accent2:#b45309;
      --good:#137333; --bad:#b42318; --blue:#2563eb; --purple:#7c3aed;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--ink); font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    header {{ position:sticky; top:0; z-index:20; background:rgba(250,250,247,.96); border-bottom:1px solid var(--line); backdrop-filter:blur(10px); }}
    .top {{ padding:14px 22px 10px; display:flex; align-items:flex-start; justify-content:space-between; gap:16px; }}
    h1 {{ font-size:20px; margin:0 0 6px; }}
    .sub {{ color:var(--muted); font-size:13px; }}
    .chips {{ display:flex; flex-wrap:wrap; gap:7px; justify-content:flex-end; }}
    .chip {{ border:1px solid var(--line); background:#eef3f1; border-radius:6px; padding:4px 8px; color:#344054; font-size:12px; }}
    .tabs {{ display:flex; gap:4px; padding:0 22px 10px; overflow-x:auto; }}
    .tabs button {{ border:1px solid transparent; background:transparent; color:#475467; padding:7px 10px; border-radius:7px; cursor:pointer; font:inherit; white-space:nowrap; }}
    .tabs button.active {{ border-color:var(--line); background:var(--panel); color:var(--ink); box-shadow:0 1px 2px rgba(16,24,40,.06); }}
    main {{ padding:18px 22px 34px; max-width:1500px; margin:0 auto; }}
    .view {{ display:none; }}
    .view.active {{ display:block; }}
    h2 {{ font-size:16px; margin:24px 0 8px; }}
    .lead {{ color:var(--muted); max-width:1000px; margin:0 0 12px; }}
    .guideGrid {{ display:grid; grid-template-columns:repeat(3,minmax(230px,1fr)); gap:10px; margin:10px 0 16px; }}
    .explain {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:13px; }}
    .explain b {{ display:block; margin-bottom:6px; }}
    .explain p {{ margin:0; color:var(--muted); }}
    .walk {{ background:#fbfbf8; border:1px solid var(--line); border-radius:8px; padding:12px; margin:10px 0 14px; }}
    .walkSteps {{ display:grid; grid-template-columns:repeat(5,minmax(150px,1fr)); gap:8px; margin-top:10px; }}
    .walkStep {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; min-height:98px; }}
    .walkStep .k {{ color:var(--muted); font-size:12px; }}
    .walkStep .v {{ font-size:20px; font-weight:720; margin:4px 0; }}
    .plainList {{ margin:8px 0 0 18px; padding:0; color:var(--muted); }}
    .plainList li {{ margin:5px 0; }}
    .cards {{ display:grid; grid-template-columns:repeat(6,minmax(150px,1fr)); gap:10px; margin:0 0 14px; }}
    .card {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; min-height:82px; }}
    .card .label {{ color:var(--muted); font-size:12px; }}
    .card .value {{ font-size:21px; font-weight:720; margin:5px 0 2px; line-height:1.15; overflow-wrap:anywhere; }}
    .card .note {{ color:var(--muted); font-size:12px; }}
    .chartGrid {{ display:grid; grid-template-columns:1.35fr 1fr; gap:12px; }}
    .chartGrid.three {{ grid-template-columns:1fr 1fr 1fr; }}
    .chartBox {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; min-height:320px; }}
    .chartTitle {{ display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:8px; }}
    .chartTitle b {{ font-size:14px; }}
    .chartTitle span {{ color:var(--muted); font-size:12px; }}
    canvas {{ width:100%; height:260px; display:block; }}
    .flowDiagram {{ display:grid; grid-template-columns:repeat(6,minmax(130px,1fr)); gap:12px; margin:10px 0 18px; }}
    .flowNode {{ position:relative; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:12px; min-height:118px; box-shadow:0 1px 2px rgba(16,24,40,.04); }}
    .flowNode:not(:last-child)::after {{ content:""; position:absolute; right:-12px; top:50%; width:12px; height:1px; background:var(--line); }}
    .flowNode .num {{ display:inline-grid; place-items:center; width:22px; height:22px; border-radius:50%; background:#e7f0ed; color:var(--accent); font-weight:700; font-size:12px; margin-bottom:8px; }}
    .flowNode b {{ display:block; margin-bottom:5px; }}
    .flowNode p {{ margin:0; color:var(--muted); font-size:12px; }}
    .formulaGrid {{ display:grid; grid-template-columns:repeat(3,minmax(240px,1fr)); gap:10px; margin:10px 0 14px; }}
    .formula {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:13px; }}
    .formula b {{ display:block; margin-bottom:8px; }}
    .math {{ font-family:ui-serif,Georgia,serif; font-size:18px; line-height:1.55; color:#111827; }}
    .math small {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--muted); font-size:12px; }}
    .pill {{ display:inline-block; border:1px solid var(--line); background:var(--soft); border-radius:999px; padding:2px 8px; font-size:12px; color:#475467; margin-right:4px; }}
    .two {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; }}
    .scroll {{ overflow:auto; border:1px solid var(--line); border-radius:8px; background:var(--panel); }}
    table {{ width:100%; border-collapse:collapse; min-width:760px; }}
    th,td {{ border-bottom:1px solid var(--line); padding:8px 9px; text-align:left; white-space:nowrap; vertical-align:top; }}
    th {{ position:sticky; top:0; background:#eff1ec; color:#344054; font-size:12px; z-index:1; }}
    tr:hover td {{ background:#fbfbf8; }}
    .numCell {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .good {{ color:var(--good); font-weight:650; }}
    .bad {{ color:var(--bad); font-weight:650; }}
    .muted {{ color:var(--muted); }}
    .selected {{ color:var(--good); font-weight:650; }}
    pre {{ white-space:pre-wrap; background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; overflow:auto; }}
    @media(max-width:1100px) {{ .cards {{ grid-template-columns:repeat(3,1fr); }} .chartGrid,.chartGrid.three,.two {{ grid-template-columns:1fr; }} .flowDiagram,.formulaGrid,.guideGrid,.walkSteps {{ grid-template-columns:1fr 1fr; }} }}
    @media(max-width:680px) {{ main {{ padding:14px; }} .top {{ display:block; }} .chips {{ justify-content:flex-start; }} .cards,.flowDiagram,.formulaGrid,.guideGrid,.walkSteps {{ grid-template-columns:1fr; }} .flowNode::after {{ display:none; }} }}
  </style>
</head>
<body>
<header>
  <div class="top">
    <div>
      <h1>Polymarket 5min 稳健交易系统</h1>
      <div class="sub">从 quote path 到 q、edge、gate、仓位和回测结果的完整可解释链路</div>
    </div>
    <div class="chips" id="chips"></div>
  </div>
  <nav class="tabs" id="tabs">
    <button class="active" data-tab="overview">Overview</button>
    <button data-tab="guide">Guide</button>
    <button data-tab="model">Model</button>
    <button data-tab="execution">Execution</button>
    <button data-tab="trades">Trades</button>
    <button data-tab="stress">Stress</button>
    <button data-tab="walkforward">Walk-forward</button>
    <button data-tab="stability">Stability</button>
    <button data-tab="paper">Paper Replay</button>
    <button data-tab="report">Report</button>
  </nav>
</header>
<main>
  <section id="overview" class="view active">
    <section class="cards" id="cards"></section>
    <h2>系统 Flow</h2>
    <p class="lead">每一层只做一件事：先把 5min quote path 变成候选点，再估计校准胜率 q，再用交易成本和风控决定是否执行。</p>
    <section class="flowDiagram">
      <div class="flowNode"><span class="num">1</span><b>数据切分</b><p>30 天窗口，train / validation / test，保留 embargo。</p></div>
      <div class="flowNode"><span class="num">2</span><b>4s 候选</b><p>每 4 秒观察 buy_up / buy_down，不等于每 4 秒下单。</p></div>
      <div class="flowNode"><span class="num">3</span><b>因子映射</b><p>市场先验、BTC、盘口、交互项按方向统一。</p></div>
      <div class="flowNode"><span class="num">4</span><b>mixed q</b><p>基础模型 logit 进入 mixed meta，再做概率校准。</p></div>
      <div class="flowNode"><span class="num">5</span><b>edge gate</b><p>q - price - fee 过阈值，且 depth/spread/overround 过关。</p></div>
      <div class="flowNode"><span class="num">6</span><b>执行风控</b><p>fractional Kelly、event cap、exit policy，按时间回放。</p></div>
    </section>
    <section class="chartGrid">
      <div class="chartBox"><div class="chartTitle"><b>权益曲线</b><span>train / validation / test 分段展示</span></div><canvas id="equityChart" width="1200" height="340"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>资金结果</b><span>每个 split 的 ending bankroll</span></div><canvas id="bankrollChart" width="720" height="340"></canvas></div>
    </section>
    <section class="chartGrid three" style="margin-top:12px">
      <div class="chartBox"><div class="chartTitle"><b>概率质量</b><span>model vs market logloss</span></div><canvas id="probChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>Test 因子组贡献</b><span>avg logit contribution</span></div><canvas id="contribChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>Test 决策漏斗</b><span>候选到实际执行</span></div><canvas id="funnelChart" width="720" height="320"></canvas></div>
    </section>
  </section>

  <section id="guide" class="view">
    <h2>先用大白话看这套系统</h2>
    <section class="guideGrid">
      <div class="explain"><b>1. q 是模型认为“这边会赢”的概率</b><p>Polymarket 的价格也像概率，但价格里有价差、流动性和市场噪声。模型的任务是估一个更接近真实胜率的 q。</p></div>
      <div class="explain"><b>2. edge 是“值得不值得买”的核心</b><p>如果 q=58%，但买价是56%，fee=1%，edge=1%。edge 太小就不值得，因为真实成交会有失败、滑点和排队成本。</p></div>
      <div class="explain"><b>3. test 收益不是直接 live 结论</b><p>这里是本地 quote replay。它能说明信号在历史数据里强，但还没有真实队列位置、部分成交和订单失败模型。</p></div>
    </section>
    <h2>拿一笔 test 交易走完整条链</h2>
    <p class="lead">下面自动取 test 里 edge 较高的一笔交易，展示从模型分数到实际下单判断的每一步。</p>
    <div class="walk" id="walkthrough"></div>
    <h2>公式里的变量分别是什么意思</h2>
    <div class="scroll"><table id="formulaLegendTable"></table></div>
    <h2>概率质量和交易指标怎么读</h2>
    <div class="scroll"><table id="metricGuideTable"></table></div>
    <h2>系统变量词典</h2>
    <div class="scroll"><table id="conceptTable"></table></div>
  </section>

  <section id="model" class="view">
    <h2>公式链路</h2>
    <section class="formulaGrid">
      <div class="formula"><b>特征标准化</b><div class="math">x<sub>i</sub> = clip((f<sub>i</sub> - μ<sub>train</sub>) / σ<sub>train</sub>)<br><small>只用 train 均值/方差，避免 test 泄漏。</small></div></div>
      <div class="formula"><b>single / mixed logit</b><div class="math">z = b + Σ w<sub>i</sub>x<sub>i</sub><br>z<sub>mix</sub> = b<sub>m</sub> + Σ α<sub>j</sub>z<sub>j</sub></div></div>
      <div class="formula"><b>校准和 edge</b><div class="math">q = σ(a + c z)<br>edge = q - p<sub>ask</sub> - fee</div></div>
    </section>
    <div class="explain">
      <b>按顺序读这三个公式</b>
      <ul class="plainList">
        <li>先把不同量纲的东西变成可比较的分数：BTC 价格偏移、盘口深度、价差都变成标准化后的 x。</li>
        <li>再把这些 x 加权求和成 z。z 可以理解成“模型的倾向分”：z 越大，越倾向这边会赢。</li>
        <li>最后把 z 转成概率 q，并和真实买价比较。只有 q 明显高于买价加费用，才有交易价值。</li>
      </ul>
    </div>
    <div class="two">
      <div><h2>模型混合结构</h2><div class="scroll"><table id="modelMixTable"></table></div></div>
      <div><h2>校准散点</h2><div class="chartBox"><canvas id="calibrationChart" width="720" height="320"></canvas></div></div>
    </div>
    <h2>模型步骤拆解</h2>
    <div class="scroll"><table id="modelStepTable"></table></div>
    <h2>因子蓝图</h2>
    <p class="lead">selected=是 表示进入了本轮选中的底层特征空间。mixed 模型的直接权重在“模型混合结构”和“模型权重”里看。</p>
    <div class="scroll"><table id="featureBlueprintTable"></table></div>
    <h2>模型搜索</h2>
    <div class="scroll"><table id="familyTable"></table></div>
    <h2>模型权重</h2>
    <div class="scroll"><table id="weightTable"></table></div>
  </section>

  <section id="execution" class="view">
    <section class="chartGrid">
      <div class="chartBox"><div class="chartTitle"><b>执行配置 Top</b><span>validation score</span></div><canvas id="configChart" width="1000" height="340"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>价格桶 PnL</b><span>按 split / price bucket</span></div><canvas id="bucketChart" width="720" height="340"></canvas></div>
    </section>
    <h2>Train / Validation / Test 指标</h2>
    <div class="scroll"><table id="splitTable"></table></div>
    <h2>概率质量</h2>
    <div class="scroll"><table id="probTable"></table></div>
    <h2>决策漏斗</h2>
    <div class="scroll"><table id="funnelTable"></table></div>
    <h2>快速多交易 / edge 扫描</h2>
    <p class="lead">默认报告不跑这段扫描，避免每次重放过慢；用 --fast-execution-scan 开启后，会固定已选 q 模型，扫描普通 edge 阈值、event cap、每个 5min market 最多交易次数和 4/8 秒最小间隔。未开启时，下表显示当前固定执行配置。</p>
    <div class="scroll"><table id="configTable"></table></div>
    <h2>多交易组合摘要</h2>
    <div class="scroll"><table id="executionGroupTable"></table></div>
    <h2>价格桶表现</h2>
    <div class="scroll"><table id="bucketTable"></table></div>
  </section>

  <section id="trades" class="view">
    <h2>单笔交易解释</h2>
    <p class="lead">这里展示 logit 贡献、水位、q、edge 和最终 PnL。它是模型解释，不是因果归因。</p>
    <div class="scroll"><table id="explainTable"></table></div>
    <h2>因子组贡献</h2>
    <div class="scroll"><table id="contribTable"></table></div>
    <h2>稳定因子诊断</h2>
    <div class="scroll"><table id="factorTable"></table></div>
    <h2>校准分桶</h2>
    <div class="scroll"><table id="calibrationTable"></table></div>
  </section>

  <section id="stress" class="view">
    <section class="chartGrid">
      <div class="chartBox"><div class="chartTitle"><b>压力测试 Ending Bankroll</b><span>validation / test</span></div><canvas id="stressChart" width="1000" height="340"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>压力测试交易数</b><span>成本变化后的 gate 影响</span></div><canvas id="stressTradesChart" width="720" height="340"></canvas></div>
    </section>
    <h2>成本 / 滑点压力测试</h2>
    <div class="scroll"><table id="stressTable"></table></div>
  </section>

  <section id="walkforward" class="view">
    <h2>Rolling Walk-forward</h2>
    <p class="lead">这里不是固定一个切分看结果，而是每个 fold 都重新训练 q 模型、重新选择模型结构，再只看 fold test。默认执行参数固定为主报告选中的配置，避免每个 fold 都重新调 gate 造成隐性过拟合；如开启 --walk-forward-tune-config，表格会显示每折重选执行参数。</p>
    <section class="chartGrid three">
      <div class="chartBox"><div class="chartTitle"><b>Fold Test 收益</b><span>每个滚动 test 段</span></div><canvas id="wfReturnChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>Fold Test Ending</b><span>$100 起始本金</span></div><canvas id="wfBankrollChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>Fold 概率优势</b><span>model LL - market LL，越低越好</span></div><canvas id="wfProbChart" width="720" height="320"></canvas></div>
    </section>
    <h2>Fold 结果</h2>
    <div class="scroll"><table id="walkForwardTable"></table></div>
    <h2>Fold 概率质量</h2>
    <div class="scroll"><table id="walkForwardProbTable"></table></div>
    <h2>Fold Test 交易样本</h2>
    <div class="scroll"><table id="walkForwardTradesTable"></table></div>
  </section>

  <section id="stability" class="view">
    <h2>不同日期 / 波动 / 盘口条件下是否稳定</h2>
    <p class="lead">这里把已选交易按日期、session、方向、价格桶、波动、深度、价差、overround 分桶。不是看整体赚，而是看 edge 是否只来自少数几个特殊桶。</p>
    <section class="chartGrid">
      <div class="chartBox"><div class="chartTitle"><b>Test Regime PnL</b><span>波动/深度/价差/overround 分桶</span></div><canvas id="stabilityChart" width="1000" height="340"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>弱桶 / 薄样本</b><span>需要警惕或继续收集数据</span></div><canvas id="stabilityWeakChart" width="720" height="340"></canvas></div>
    </section>
    <h2>强桶</h2>
    <div class="scroll"><table id="stabilityStrongTable"></table></div>
    <h2>弱桶 / 薄样本桶</h2>
    <div class="scroll"><table id="stabilityWeakTable"></table></div>
    <h2>全部稳定性分桶</h2>
    <div class="scroll"><table id="stabilityTable"></table></div>
  </section>

  <section id="paper" class="view">
    <h2>Paper-trading Replay</h2>
    <p class="lead">这是历史数据上的“纸面执行流程”：信号出现后等 4/8/12 秒，再检查盘口价格和深度，判断 full fill、partial fill、price moved 或 no quote。它不是联网实盘，但比原始 replay 更接近真实下单流程。</p>
    <section class="chartGrid three">
      <div class="chartBox"><div class="chartTitle"><b>Paper Ending</b><span>不同延迟/滑点场景</span></div><canvas id="paperEndingChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>Fill Rate</b><span>信号里有多少还能成交</span></div><canvas id="paperFillChart" width="720" height="320"></canvas></div>
      <div class="chartBox"><div class="chartTitle"><b>失败原因</b><span>test 状态计数</span></div><canvas id="paperStatusChart" width="720" height="320"></canvas></div>
    </section>
    <h2>Paper 场景汇总</h2>
    <div class="scroll"><table id="paperScenarioTable"></table></div>
    <h2>Paper 流程汇总</h2>
    <div class="scroll"><table id="paperSummaryTable"></table></div>
    <h2>Paper 交易流水</h2>
    <div class="scroll"><table id="paperReplayTable"></table></div>
  </section>

  <section id="report" class="view">
    <h2>中文报告</h2>
    <pre>{html.escape(report_md)}</pre>
  </section>
</main>
<script id="payload" type="application/json">{payload_json}</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const m = data.manifest;
const selected = data.selectedMetrics || [];
const test = selected.find(r => r.split === 'test') || {{}};
const val = selected.find(r => r.split === 'validation') || {{}};
const fmtMoney = v => v == null ? '' : '$' + Number(v).toFixed(2);
const fmtPct = v => v == null ? '' : (Number(v) * 100).toFixed(2) + '%';
const fmtNum = v => v == null ? '' : Number(v).toFixed(4);
const fmtInt = v => v == null ? '' : Number(v).toLocaleString('en-US');
const yesNo = v => Number(v) ? '<span class="selected">是</span>' : '<span class="muted">否</span>';
const clsDelta = v => Number(v) < 0 ? 'numCell good' : 'numCell bad';
const formulaLegend = [
  {{symbol:'f_i', plain:'一个原始因子。比如 BTC 相对目标价涨了多少、当前买价、盘口深度、价差。', why:'原始输入，不同因子量纲不同，不能直接相加。'}},
  {{symbol:'μ_train / σ_train', plain:'训练集里的平均值和标准差。', why:'用 train 的统计量做标准化，避免把 validation/test 信息偷用进模型。'}},
  {{symbol:'x_i', plain:'标准化后的因子分数。', why:'把 BTC 价格、盘口深度、价差这些不同量纲压到可比较的尺度。'}},
  {{symbol:'w_i', plain:'模型给某个因子的权重。', why:'正权重表示这个因子变大时更倾向买这边；负权重相反。'}},
  {{symbol:'z', plain:'raw logit，可以理解成模型内部的“倾向分”。', why:'z 越大，模型越认为这边会赢；但 z 还不是概率。'}},
  {{symbol:'q', plain:'校准后的胜率概率。', why:'这是交易系统最重要的数：模型认为这张合约最终值为 1 的概率。'}},
  {{symbol:'p_ask / entry_price', plain:'现在买入这边需要付的价格。', why:'Polymarket 价格本身像概率，但它是你真实要付的钱。'}},
  {{symbol:'fee', plain:'每份额交易成本近似。', why:'如果 q 只比价格高一点，扣掉 fee 后可能根本没有优势。'}},
  {{symbol:'edge', plain:'q - entry_price - fee。', why:'大白话：模型觉得“便宜了多少”。edge 为正且足够大，才考虑交易。'}},
  {{symbol:'event_cap', plain:'单个 5min market 最多投入本金比例。', why:'防止同一个 5 分钟事件里仓位过大。'}},
  {{symbol:'Kelly', plain:'根据 edge 和亏损空间估算的下注比例。', why:'这里只用 0.25 倍 fractional Kelly，再被 event_cap 限住。'}}
];
const metricGuide = [
  {{metric:'logloss', plain:'概率预测的惩罚分，越低越好。', how:'模型说 90% 会赢但结果输了，会被重罚；所以它看概率准不准，不只看方向。'}},
  {{metric:'market baseline', plain:'直接相信 Polymarket 当前价格的表现。', how:'如果 model logloss 低于 market baseline，说明模型比市场价格更会估概率。'}},
  {{metric:'AUC', plain:'排序能力，越接近 1 越好，0.5 接近瞎猜。', how:'它问的是：赢的候选是不是通常排在输的候选前面。'}},
  {{metric:'ECE', plain:'校准误差，越低越好。', how:'模型说 60% 的那些交易，实际是不是大约 60% 赢。'}},
  {{metric:'max drawdown', plain:'从最高点回撤最多多少。', how:'收益高但回撤也高，实盘心理和爆仓风险都会更难受。'}},
  {{metric:'profit factor', plain:'总盈利 / 总亏损的绝对值。', how:'大于 1 才赚钱；越高说明赚钱交易覆盖亏损交易的能力越强。'}},
  {{metric:'validation', plain:'用来选模型和执行参数的样本。', how:'可以看，但不能当最终结论，因为参数就是按它调出来的。'}},
  {{metric:'test', plain:'最后保留不用来调参的样本。', how:'它更接近“没见过的新数据”，但仍然只是历史 replay。'}}
];
const conceptGuide = [
  {{name:'market prior', plain:'市场价格给出的先验判断。', role:'相当于先问市场现在认为 up/down 各有多大概率。'}},
  {{name:'BTC signed move', plain:'BTC 相对目标价向当前买入方向移动了多少。', role:'买 up 时 BTC 上涨是正信号；买 down 时 BTC 下跌是正信号。'}},
  {{name:'liquidity / depth', plain:'盘口里能买多少、价差有多宽。', role:'信号再好，如果买不到或价差太大，也不应该下单。'}},
  {{name:'interaction', plain:'两个因子相乘。', role:'例如 BTC 信号在 50/50 附近可能更有价值，在价格已经 0.95 时可能没那么有用。'}},
  {{name:'boundary price', plain:'价格接近 0 或 1。', role:'这类交易表面胜率高，但一旦错了亏损很不对称，所以单独 gate。'}},
  {{name:'mixed blend', plain:'不是把所有因子硬塞进一个模型，而是把几个基础模型的判断再混合。', role:'本轮选中的是 prior_plus_btc 和 prior_btc_liquidity 的 logit 混合。'}},
  {{name:'decision funnel', plain:'候选一层层过滤后还剩多少。', role:'证明系统不是每 4 秒乱买，而是 edge、depth、spread、overround 都过关才执行。'}},
  {{name:'stress test', plain:'故意把成本变差或阈值提高。', role:'看策略是否只在理想成交假设下赚钱。'}}
];
function cellClass(c, r) {{ return typeof c[2] === 'function' ? c[2](r) : (c[2] || ''); }}
function table(id, rows, cols) {{
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML = '<thead><tr>' + cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>' +
    (rows || []).map(r => '<tr>' + cols.map(c => '<td class="' + cellClass(c, r) + '">' + (c[1](r) ?? '') + '</td>').join('') + '</tr>').join('') + '</tbody>';
}}
function renderWalkthrough() {{
  const el = document.getElementById('walkthrough');
  if (!el) return;
  const rows = (data.tradeExplanations || []).filter(r => r.split === 'test').sort((a,b) => Number(b.edge || 0) - Number(a.edge || 0));
  const t = rows[0];
  if (!t) {{ el.textContent = '没有 test 交易样本。'; return; }}
  const fee = Number(m.fee || 0);
  const price = Number(t.entry_price || 0);
  const q = Number(t.q_model || 0);
  const edge = q - price - fee;
  const sideText = t.side === 'buy_up' ? '买 UP' : '买 DOWN';
  el.innerHTML =
    '<b>样本：' + sideText + '，时间 ' + (t.first_quote_cst || '') + '</b>' +
    '<p class="lead">这一笔不是说最典型，只是拿一笔 edge 较高的 test 交易，把公式展开成人话。</p>' +
    '<div class="walkSteps">' +
      '<div class="walkStep"><div class="k">1. 模型内部分数 raw z</div><div class="v">' + fmtNum(t.raw_logit) + '</div><div class="muted">越大越偏向这边会赢，但还不是概率。</div></div>' +
      '<div class="walkStep"><div class="k">2. 校准后分数</div><div class="v">' + fmtNum(t.calibrated_logit) + '</div><div class="muted">把模型分数重新校准到概率刻度。</div></div>' +
      '<div class="walkStep"><div class="k">3. q：模型胜率</div><div class="v">' + fmtPct(q) + '</div><div class="muted">模型认为这边最终值为 1 的概率。</div></div>' +
      '<div class="walkStep"><div class="k">4. 买价 + fee</div><div class="v">' + fmtPct(price + fee) + '</div><div class="muted">你要付的概率价格，加上成本。</div></div>' +
      '<div class="walkStep"><div class="k">5. edge</div><div class="v ' + (edge >= 0 ? 'good' : 'bad') + '">' + fmtPct(edge) + '</div><div class="muted">q - price - fee；足够大才允许进入 gate。</div></div>' +
    '</div>' +
    '<ul class="plainList">' +
      '<li>这一笔的主要 logit 贡献：market prior ' + fmtNum(t.market_prior_contrib) + '，BTC ' + fmtNum(t.btc_contrib) + '，liquidity ' + fmtNum(t.liquidity_micro_contrib) + '。</li>' +
      '<li>通过 edge 后，还要继续过 depth、spread、overround、同 market 间隔和 event cap。通过不等于一定满仓。</li>' +
    '</ul>';
}}
function canvas(id) {{
  const el = document.getElementById(id);
  if (!el) return null;
  const ctx = el.getContext('2d');
  ctx.clearRect(0,0,el.width,el.height);
  ctx.font = '12px -apple-system,BlinkMacSystemFont,Segoe UI,sans-serif';
  return [el, ctx];
}}
function axes(ctx, w, h, pad) {{
  ctx.strokeStyle = '#d8dcd2'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(pad, h-pad); ctx.lineTo(w-pad, h-pad); ctx.lineTo(w-pad, pad); ctx.stroke();
}}
function drawBars(id, rows, labelFn, valueFn, opts={{}}) {{
  const got = canvas(id); if (!got || !rows.length) return;
  const [el, ctx] = got, w = el.width, h = el.height, pad = 42;
  const values = rows.map(valueFn).map(Number).filter(Number.isFinite);
  const min = Math.min(0, ...(opts.allowNegative ? values : [0]));
  const max = Math.max(1e-9, ...values);
  axes(ctx, w, h, pad);
  const barW = Math.max(10, (w - pad*2) / rows.length * 0.62);
  rows.forEach((r, i) => {{
    const v = Number(valueFn(r)) || 0;
    const x = pad + (i + .2) * (w - pad*2) / rows.length;
    const y = h - pad - (v - min) / Math.max(1e-9, max - min) * (h - pad*2);
    const base = h - pad - (0 - min) / Math.max(1e-9, max - min) * (h - pad*2);
    ctx.fillStyle = opts.colorFn ? opts.colorFn(r, i, v) : '#0f766e';
    ctx.fillRect(x, Math.min(y, base), barW, Math.max(1, Math.abs(base-y)));
    ctx.fillStyle = '#667085';
    ctx.save(); ctx.translate(x + barW/2, h - pad + 10); ctx.rotate(-Math.PI/5); ctx.textAlign='right'; ctx.fillText(String(labelFn(r)).slice(0,18), 0, 0); ctx.restore();
    if (opts.valueLabel) {{ ctx.fillStyle = '#1f2328'; ctx.textAlign='center'; ctx.fillText(opts.valueLabel(v), x+barW/2, Math.min(y, base)-6); }}
  }});
}}
function drawHorizontalBars(id, rows, labelFn, valueFn, opts={{}}) {{
  const got = canvas(id); if (!got || !rows.length) return;
  const [el, ctx] = got, w = el.width, h = el.height, padL = 118, padR = 28, padT = 20, rowH = (h - 46) / rows.length;
  const max = Math.max(1e-9, ...rows.map(valueFn).map(Number));
  ctx.strokeStyle='#d8dcd2'; ctx.beginPath(); ctx.moveTo(padL, h-26); ctx.lineTo(w-padR, h-26); ctx.stroke();
  rows.forEach((r,i) => {{
    const v = Number(valueFn(r)) || 0;
    const y = padT + i*rowH + rowH*.22;
    const bw = (w - padL - padR) * v / max;
    ctx.fillStyle = opts.colorFn ? opts.colorFn(r,i,v) : '#2563eb';
    ctx.fillRect(padL, y, bw, Math.max(8, rowH*.48));
    ctx.fillStyle='#475467'; ctx.textAlign='right'; ctx.fillText(String(labelFn(r)).slice(0,16), padL-8, y+12);
    ctx.fillStyle='#1f2328'; ctx.textAlign='left'; ctx.fillText(opts.valueLabel ? opts.valueLabel(v) : fmtInt(v), padL+bw+5, y+12);
  }});
}}
function drawEquity() {{
  const got = canvas('equityChart'); if (!got) return;
  const [el, ctx] = got, w = el.width, h = el.height, pad = 42;
  const curves = data.equityCurves || {{}};
  const names = ['train','validation','test'], colors = {{train:'#667085', validation:'#0f766e', test:'#b45309'}};
  const all = names.flatMap(n => curves[n] || []);
  if (!all.length) return;
  const min = Math.min(...all), max = Math.max(...all), span = Math.max(1e-9, max-min);
  axes(ctx, w, h, pad);
  names.forEach((name, idx) => {{
    const values = curves[name] || []; if (values.length < 2) return;
    const x0 = pad + idx*(w-2*pad)/3, xw = (w-2*pad)/3 - 12;
    ctx.beginPath();
    values.forEach((v,i) => {{
      const x = x0 + i/Math.max(1,values.length-1)*xw;
      const y = h-pad - (v-min)/span*(h-2*pad);
      if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    }});
    ctx.strokeStyle = colors[name]; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = colors[name]; ctx.fillText(name + ' ' + fmtMoney(values[values.length-1]), x0, 18 + idx*16);
  }});
}}
function drawGroupedLogloss() {{
  const rows = [];
  (data.probabilityMetrics || []).forEach(r => {{
    rows.push({{label:r.split+' model', value:r.logloss_model, kind:'model'}});
    rows.push({{label:r.split+' market', value:r.logloss_market_baseline, kind:'market'}});
  }});
  drawBars('probChart', rows, r=>r.label, r=>r.value, {{colorFn:r=>r.kind==='model'?'#0f766e':'#b45309', valueLabel:v=>v.toFixed(3)}});
}}
function drawCalibration() {{
  const got = canvas('calibrationChart'); if (!got) return;
  const [el, ctx] = got, w = el.width, h = el.height, pad = 42;
  axes(ctx,w,h,pad);
  ctx.strokeStyle='#98a2b3'; ctx.setLineDash([4,4]); ctx.beginPath(); ctx.moveTo(pad,h-pad); ctx.lineTo(w-pad,pad); ctx.stroke(); ctx.setLineDash([]);
  const rows = (data.calibrationBuckets || []).filter(r=>r.split==='test');
  ctx.fillStyle='#0f766e';
  rows.forEach(r => {{
    const x = pad + Number(r.avg_q)*(w-2*pad);
    const y = h-pad - Number(r.win_rate)*(h-2*pad);
    ctx.beginPath(); ctx.arc(x,y,4,0,Math.PI*2); ctx.fill();
  }});
  ctx.fillStyle='#667085'; ctx.fillText('x=avg q, y=actual win rate', pad, 18);
}}
function renderCharts() {{
  drawEquity();
  drawBars('bankrollChart', selected, r=>r.split, r=>r.ending_bankroll, {{colorFn:r=>r.split==='test'?'#b45309':r.split==='validation'?'#0f766e':'#667085', valueLabel:fmtMoney}});
  drawGroupedLogloss();
  drawBars('contribChart', (data.contributionSummary||[]).filter(r=>r.split==='test'), r=>r.group, r=>r.avg_logit_contribution, {{allowNegative:true, colorFn:(r,i,v)=>v>=0?'#0f766e':'#b42318', valueLabel:v=>v.toFixed(2)}});
  drawHorizontalBars('funnelChart', (data.decisionFunnel||[]).filter(r=>r.split==='test'), r=>r.stage, r=>r.count, {{colorFn:(r,i)=>i===0?'#667085':'#2563eb'}});
  drawCalibration();
  const scanPairs = (data.fastExecutionPairs || []);
  const configChartRows = scanPairs.length ? scanPairs.slice(0,12) : (data.configSearchTop||[]).slice(0,12);
  drawBars('configChart', configChartRows, r=>String(r.system).replace('q_edge_',''), r=>r.validation_score, {{colorFn:r=>r.is_current_selected?'#b45309':'#0f766e', valueLabel:v=>v.toFixed(2)}});
  drawBars('bucketChart', (data.bucketMetrics||[]).filter(r=>r.split==='test'), r=>r.price_bucket, r=>r.total_pnl, {{allowNegative:true, colorFn:(r,i,v)=>v>=0?'#0f766e':'#b42318', valueLabel:v=>v.toFixed(1)}});
  drawBars('stressChart', (data.stressTests||[]).filter(r=>r.split==='test'), r=>r.scenario, r=>r.ending_bankroll, {{colorFn:()=> '#7c3aed', valueLabel:fmtMoney}});
  drawBars('stressTradesChart', (data.stressTests||[]).filter(r=>r.split==='test'), r=>r.scenario, r=>r.trades, {{colorFn:()=> '#2563eb', valueLabel:v=>String(Math.round(v))}});
  const wfTest = (data.walkForwardMetrics || []).filter(r=>r.status==='ok' && r.fold_split==='test');
  drawBars('wfReturnChart', wfTest, r=>r.fold, r=>r.total_return, {{allowNegative:true, colorFn:(r,i,v)=>v>=0?'#0f766e':'#b42318', valueLabel:fmtPct}});
  drawBars('wfBankrollChart', wfTest, r=>r.fold, r=>r.ending_bankroll, {{colorFn:()=> '#b45309', valueLabel:fmtMoney}});
  drawBars('wfProbChart', (data.walkForwardProbability||[]).filter(r=>r.split==='test'), r=>r.fold, r=>r.logloss_delta_vs_market, {{allowNegative:true, colorFn:(r,i,v)=>v<=0?'#0f766e':'#b42318', valueLabel:v=>v.toFixed(3)}});
  const stableRows = (data.stabilityRegimes || []).filter(r=>r.split==='test' && ['vol_bucket','depth_bucket','spread_bucket','overround_bucket'].includes(r.dimension));
  drawBars('stabilityChart', stableRows, r=>r.dimension.replace('_bucket','') + ':' + r.bucket, r=>r.total_pnl, {{allowNegative:true, colorFn:(r,i,v)=>v>=0?'#0f766e':'#b42318', valueLabel:v=>v.toFixed(1)}});
  const weakRows = (data.stabilityRegimes || []).filter(r=>r.split==='test' && r.verdict!=='strong').slice(0,16);
  drawHorizontalBars('stabilityWeakChart', weakRows, r=>r.dimension + ':' + r.bucket, r=>r.trades, {{colorFn:r=>r.verdict==='weak'?'#b42318':'#667085'}});
  const paperTest = (data.paperReplayScenarios || []).filter(r=>r.split==='test');
  drawBars('paperEndingChart', paperTest, r=>r.scenario, r=>r.ending_bankroll, {{colorFn:()=> '#7c3aed', valueLabel:fmtMoney}});
  drawBars('paperFillChart', paperTest, r=>r.scenario, r=>r.fill_rate, {{colorFn:()=> '#2563eb', valueLabel:fmtPct}});
  const paperStatus = (data.paperReplaySummary || []).filter(r=>r.split==='test' && String(r.scenario||'').startsWith('status::'));
  drawHorizontalBars('paperStatusChart', paperStatus, r=>String(r.scenario).replace('status::',''), r=>r.signals, {{colorFn:r=>String(r.scenario).includes('fill')?'#0f766e':'#b42318'}});
}}
document.getElementById('chips').innerHTML = [
  '窗口: ' + m.window_start_cst + ' - ' + m.window_end_cst,
  'Markets: ' + fmtInt(m.window_events),
  '候选: ' + fmtInt(m.candidates),
  '模型: ' + m.selected_feature_set,
  '执行: ' + m.selected_config,
  '快速扫描: ' + fmtInt(m.fast_execution_scan_combinations || 0)
].map(x => '<span class="chip">' + x + '</span>').join('');
document.getElementById('cards').innerHTML = [
  ['Validation', fmtMoney(val.ending_bankroll), fmtPct(val.total_return)],
  ['Test', fmtMoney(test.ending_bankroll), fmtPct(test.total_return)],
  ['选中模型', m.selected_feature_set, m.selected_model_kind],
  ['Test 交易数', fmtInt(test.trades || 0), 'max DD ' + fmtPct(test.max_drawdown)],
  ['Test AUC', fmtNum((data.probabilityMetrics||[]).find(r=>r.split==='test')?.auc_model), 'ECE ' + fmtNum((data.probabilityMetrics||[]).find(r=>r.split==='test')?.ece_model_10)],
  ['执行评估', fmtInt(m.config_combinations) + '/' + fmtInt(m.full_config_combinations), m.config_search_mode]
].map(c => '<div class="card"><div class="label">' + c[0] + '</div><div class="value">' + c[1] + '</div><div class="note">' + (c[2] || '') + '</div></div>').join('');
renderWalkthrough();
table('formulaLegendTable', formulaLegend, [
  ['符号', r=>r.symbol], ['大白话', r=>r.plain], ['为什么要有它', r=>r.why]
]);
table('metricGuideTable', metricGuide, [
  ['指标', r=>r.metric], ['大白话', r=>r.plain], ['怎么看', r=>r.how]
]);
table('conceptTable', conceptGuide, [
  ['概念', r=>r.name], ['大白话', r=>r.plain], ['在系统里的作用', r=>r.role]
]);
table('modelMixTable', data.modelMix || [], [
  ['layer', r=>r.layer], ['name', r=>r.name], ['weight', r=>fmtNum(r.weight),'numCell'], ['features', r=>r.features,'numCell'], ['L2', r=>fmtNum(r.l2),'numCell'], ['formula', r=>r.formula]
]);
table('modelStepTable', data.modelSteps || [], [
  ['步骤', r=>r.step], ['输入', r=>r.input], ['操作', r=>r.operation], ['输出', r=>r.output], ['学习位置', r=>r.learned_on], ['为什么需要', r=>r.why]
]);
table('featureBlueprintTable', data.featureBlueprint || [], [
  ['selected', r=>yesNo(r.selected)], ['group', r=>r.group], ['feature', r=>r.feature], ['weight', r=>fmtNum(r.weight),'numCell'], ['说明', r=>r.description]
]);
table('familyTable', data.modelFamilySummary || [], [
  ['feature set', r=>r.feature_set], ['kind', r=>r.model_kind], ['members', r=>r.member_count,'numCell'], ['features', r=>r.feature_count,'numCell'],
  ['val LL', r=>fmtNum(r.validation_logloss),'numCell'], ['penalty', r=>fmtNum(r.complexity_penalty),'numCell'], ['selection', r=>fmtNum(r.selection_loss),'numCell'], ['members used', r=>r.members || '']
]);
table('weightTable', (data.modelWeights || []).slice(0,45), [
  ['group', r=>r.group || ''], ['feature', r=>r.feature], ['weight', r=>fmtNum(r.weight),'numCell'], ['mean', r=>fmtNum(r.mean),'numCell'], ['std', r=>fmtNum(r.std),'numCell'], ['note', r=>r.note || '']
]);
table('splitTable', selected, [
  ['split', r=>r.split], ['trades', r=>fmtInt(r.trades),'numCell'], ['ending', r=>fmtMoney(r.ending_bankroll),'numCell'],
  ['return', r=>fmtPct(r.total_return),'numCell'], ['drawdown', r=>fmtPct(r.max_drawdown),'numCell'], ['win', r=>fmtPct(r.win_rate),'numCell'], ['PF', r=>fmtNum(r.profit_factor),'numCell']
]);
table('probTable', data.probabilityMetrics || [], [
  ['split', r=>r.split], ['rows', r=>fmtInt(r.rows),'numCell'], ['model LL', r=>fmtNum(r.logloss_model),'numCell'],
  ['market LL', r=>fmtNum(r.logloss_market_baseline),'numCell'], ['delta', r=>fmtNum(r.logloss_delta_vs_market), r=>clsDelta(r.logloss_delta_vs_market)], ['AUC', r=>fmtNum(r.auc_model),'numCell'], ['ECE', r=>fmtNum(r.ece_model_10),'numCell']
]);
table('funnelTable', data.decisionFunnel || [], [
  ['split', r=>r.split], ['stage', r=>r.stage], ['count', r=>fmtInt(r.count),'numCell'], ['候选占比', r=>fmtPct(r.share_of_candidates),'numCell'], ['较上一层保留', r=>fmtPct(r.retention_from_previous),'numCell'], ['执行markets', r=>r.executed_markets == null ? '' : fmtInt(r.executed_markets),'numCell']
]);
table('configTable', (scanPairs.length ? scanPairs : (data.configSearchTop || [])).slice(0,40), [
  ['current', r=>yesNo(r.is_current_selected)], ['system', r=>r.system],
  ['val ending', r=>fmtMoney(r.validation_ending_bankroll ?? r.ending_bankroll),'numCell'], ['val return', r=>fmtPct(r.validation_return ?? r.total_return),'numCell'],
  ['test ending', r=>fmtMoney(r.test_ending_bankroll),'numCell'], ['test return', r=>fmtPct(r.test_return),'numCell'], ['test DD', r=>fmtPct(r.test_drawdown ?? r.max_drawdown),'numCell'],
  ['max trades', r=>r.max_trades_per_market,'numCell'], ['gap', r=>r.min_entry_gap_seconds,'numCell'], ['cap', r=>fmtPct(r.event_cap),'numCell'], ['edge', r=>fmtNum(r.normal_edge),'numCell'],
  ['score', r=>fmtNum(r.validation_score),'numCell']
]);
table('executionGroupTable', ((data.fastExecutionGroupSummary || []).length ? data.fastExecutionGroupSummary : (data.executionGroupSummary || [])).slice(0,40), [
  ['max trades', r=>r.max_trades_per_market,'numCell'], ['gap 秒', r=>r.min_entry_gap_seconds,'numCell'], ['exit', r=>r.exit_policy], ['best ending', r=>fmtMoney(r.ending_bankroll),'numCell'],
  ['return', r=>fmtPct(r.total_return),'numCell'], ['trades', r=>fmtInt(r.trades),'numCell'], ['multi markets', r=>fmtInt(r.multi_trade_markets),'numCell'], ['score', r=>fmtNum(r.validation_score),'numCell']
]);
table('bucketTable', data.bucketMetrics || [], [
  ['split', r=>r.split], ['bucket', r=>r.price_bucket], ['trades', r=>fmtInt(r.trades),'numCell'], ['PnL', r=>fmtNum(r.total_pnl),'numCell'], ['win', r=>fmtPct(r.win_rate),'numCell'], ['avg edge', r=>fmtNum(r.avg_edge),'numCell'], ['avg price', r=>fmtNum(r.avg_entry_price),'numCell']
]);
table('explainTable', (data.tradeExplanations || []).filter(r=>r.split==='test').slice(0,120), [
  ['time', r=>r.first_quote_cst], ['side', r=>r.side], ['sec', r=>fmtNum(r.entry_second),'numCell'], ['price', r=>fmtNum(r.entry_price),'numCell'], ['market p', r=>fmtNum(r.market_p),'numCell'],
  ['raw z', r=>fmtNum(r.raw_logit),'numCell'], ['cal z', r=>fmtNum(r.calibrated_logit),'numCell'], ['q', r=>fmtNum(r.q_model),'numCell'], ['edge', r=>fmtNum(r.edge),'numCell'], ['dominant', r=>r.dominant_group || ''], ['btc', r=>fmtNum(r.btc_contrib),'numCell'], ['liq', r=>fmtNum(r.liquidity_micro_contrib),'numCell'], ['pnl', r=>fmtNum(r.pnl_usd),'numCell']
]);
table('contribTable', data.contributionSummary || [], [
  ['split', r=>r.split], ['因子组', r=>r.group], ['trades', r=>fmtInt(r.trades),'numCell'], ['avg contrib', r=>fmtNum(r.avg_logit_contribution),'numCell'], ['avg abs', r=>fmtNum(r.avg_abs_logit_contribution),'numCell'], ['正贡献率', r=>fmtPct(r.positive_contribution_rate),'numCell']
]);
const stableFactors = (data.factorDiagnostics || []).filter(r=>r.stable_corr_pnl_sign===1).sort((a,b) => {{
  const score = x => Math.abs(Number(x.train_corr_pnl)||0)+Math.abs(Number(x.validation_corr_pnl)||0)+Math.abs(Number(x.test_corr_pnl)||0);
  return score(b)-score(a);
}});
table('factorTable', stableFactors.slice(0,30), [
  ['feature', r=>r.feature], ['train pnl corr', r=>fmtNum(r.train_corr_pnl),'numCell'], ['val pnl corr', r=>fmtNum(r.validation_corr_pnl),'numCell'], ['test pnl corr', r=>fmtNum(r.test_corr_pnl),'numCell'], ['test AUC', r=>fmtNum(r.test_auc),'numCell']
]);
table('calibrationTable', data.calibrationBuckets || [], [
  ['split', r=>r.split], ['bucket', r=>r.bucket], ['rows', r=>fmtInt(r.rows),'numCell'], ['avg q', r=>fmtNum(r.avg_q),'numCell'], ['actual win', r=>fmtPct(r.win_rate),'numCell'], ['market p', r=>fmtNum(r.avg_market_p),'numCell'], ['avg edge', r=>fmtNum(r.avg_edge),'numCell']
]);
table('stressTable', data.stressTests || [], [
  ['scenario', r=>r.scenario], ['split', r=>r.split], ['说明', r=>r.note], ['extra fee', r=>fmtNum(r.extra_fee),'numCell'], ['extra edge', r=>fmtNum(r.extra_edge_threshold),'numCell'], ['trades', r=>fmtInt(r.trades),'numCell'], ['ending', r=>fmtMoney(r.ending_bankroll),'numCell'], ['return', r=>fmtPct(r.total_return),'numCell'], ['DD', r=>fmtPct(r.max_drawdown),'numCell']
]);
table('walkForwardTable', data.walkForwardMetrics || [], [
  ['fold', r=>r.fold], ['split', r=>r.fold_split || r.split || ''], ['status', r=>r.status || ''],
  ['test window', r=>(r.test_start_cst || '') + ' -> ' + (r.test_end_cst || '')],
  ['model', r=>r.selected_feature_set || ''], ['kind', r=>r.selected_model_kind || ''],
  ['config', r=>r.selected_config || ''], ['trades', r=>fmtInt(r.trades),'numCell'],
  ['ending', r=>fmtMoney(r.ending_bankroll),'numCell'], ['return', r=>fmtPct(r.total_return),'numCell'],
  ['DD', r=>fmtPct(r.max_drawdown),'numCell'], ['win', r=>fmtPct(r.win_rate),'numCell'], ['PF', r=>fmtNum(r.profit_factor),'numCell']
]);
table('walkForwardProbTable', data.walkForwardProbability || [], [
  ['fold', r=>r.fold], ['split', r=>r.split], ['rows', r=>fmtInt(r.rows),'numCell'],
  ['model LL', r=>fmtNum(r.logloss_model),'numCell'], ['market LL', r=>fmtNum(r.logloss_market_baseline),'numCell'],
  ['delta', r=>fmtNum(r.logloss_delta_vs_market), r=>clsDelta(r.logloss_delta_vs_market)],
  ['AUC', r=>fmtNum(r.auc_model),'numCell'], ['ECE', r=>fmtNum(r.ece_model_10),'numCell']
]);
table('walkForwardTradesTable', (data.walkForwardTrades || []).slice(0,200), [
  ['fold', r=>r.fold], ['time', r=>r.first_quote_cst], ['side', r=>r.side],
  ['price', r=>fmtNum(r.entry_price),'numCell'], ['q', r=>fmtNum(r.q_model),'numCell'],
  ['edge', r=>fmtNum(r.edge),'numCell'], ['pnl', r=>fmtNum(r.pnl_usd),'numCell'], ['bankroll', r=>fmtMoney(r.bankroll_after),'numCell']
]);
const strongRegimes = (data.stabilityRegimes || []).filter(r=>r.split==='test' && r.verdict==='strong').sort((a,b)=>Number(b.total_pnl||0)-Number(a.total_pnl||0));
const weakRegimes = (data.stabilityRegimes || []).filter(r=>r.split==='test' && r.verdict!=='strong').sort((a,b)=>Number(a.total_pnl||0)-Number(b.total_pnl||0));
table('stabilityStrongTable', strongRegimes.slice(0,80), [
  ['dimension', r=>r.dimension], ['bucket', r=>r.bucket], ['trades', r=>fmtInt(r.trades),'numCell'],
  ['PnL', r=>fmtNum(r.total_pnl),'numCell'], ['avg PnL', r=>fmtNum(r.avg_pnl),'numCell'],
  ['win', r=>fmtPct(r.win_rate),'numCell'], ['PF', r=>fmtNum(r.profit_factor),'numCell'], ['avg edge', r=>fmtNum(r.avg_edge),'numCell']
]);
table('stabilityWeakTable', weakRegimes.slice(0,80), [
  ['dimension', r=>r.dimension], ['bucket', r=>r.bucket], ['verdict', r=>r.verdict],
  ['trades', r=>fmtInt(r.trades),'numCell'], ['PnL', r=>fmtNum(r.total_pnl),'numCell'],
  ['win', r=>fmtPct(r.win_rate),'numCell'], ['avg edge', r=>fmtNum(r.avg_edge),'numCell']
]);
table('stabilityTable', data.stabilityRegimes || [], [
  ['split', r=>r.split], ['dimension', r=>r.dimension], ['bucket', r=>r.bucket], ['verdict', r=>r.verdict],
  ['trades', r=>fmtInt(r.trades),'numCell'], ['PnL', r=>fmtNum(r.total_pnl),'numCell'],
  ['avg PnL', r=>fmtNum(r.avg_pnl),'numCell'], ['win', r=>fmtPct(r.win_rate),'numCell'],
  ['PF', r=>fmtNum(r.profit_factor),'numCell'], ['avg edge', r=>fmtNum(r.avg_edge),'numCell'], ['avg q', r=>fmtNum(r.avg_q),'numCell']
]);
table('paperScenarioTable', data.paperReplayScenarios || [], [
  ['scenario', r=>r.scenario], ['split', r=>r.split], ['signals', r=>fmtInt(r.signals),'numCell'],
  ['fills', r=>fmtInt(r.fills),'numCell'], ['fill rate', r=>fmtPct(r.fill_rate),'numCell'],
  ['ending', r=>fmtMoney(r.ending_bankroll),'numCell'], ['return', r=>fmtPct(r.total_return),'numCell'],
  ['avg fill ratio', r=>fmtPct(r.avg_fill_ratio),'numCell'], ['total pnl', r=>fmtNum(r.total_pnl),'numCell']
]);
table('paperSummaryTable', data.paperReplaySummary || [], [
  ['split', r=>r.split], ['scenario/status', r=>r.scenario], ['signals', r=>fmtInt(r.signals),'numCell'],
  ['displayed', r=>fmtInt(r.displayed_rows),'numCell'], ['fills displayed', r=>fmtInt(r.fills_displayed),'numCell'],
  ['ending', r=>fmtMoney(r.ending_bankroll),'numCell'], ['return', r=>fmtPct(r.total_return),'numCell'],
  ['display fill rate', r=>fmtPct(r.display_fill_rate),'numCell'], ['display pnl', r=>fmtNum(r.display_total_pnl),'numCell']
]);
table('paperReplayTable', (data.paperReplay || []).filter(r=>r.split==='test').slice(0,250), [
  ['time', r=>r.first_quote_cst], ['side', r=>r.side], ['status', r=>r.status],
  ['signal price', r=>fmtNum(r.signal_price),'numCell'], ['paper price', r=>fmtNum(r.paper_price),'numCell'],
  ['slippage', r=>fmtNum(r.price_slippage),'numCell'], ['fill ratio', r=>fmtPct(r.fill_ratio),'numCell'],
  ['paper pnl', r=>fmtNum(r.paper_pnl_usd),'numCell'], ['orig pnl', r=>fmtNum(r.original_backtest_pnl_usd),'numCell'], ['paper bankroll', r=>fmtMoney(r.paper_bankroll_after),'numCell']
]);
document.querySelectorAll('#tabs button').forEach(btn => btn.addEventListener('click', () => {{
  document.querySelectorAll('#tabs button').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById(btn.dataset.tab).classList.add('active');
  requestAnimationFrame(renderCharts);
}}));
renderCharts();
</script>
</body>
</html>"""


def run(args: argparse.Namespace) -> Dict[str, object]:
    source_root = Path(args.source_root).expanduser().resolve()
    docs_dir = Path(args.docs_dir).expanduser().resolve()
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    grouped, coverage, raw_stats = base.read_monthly_runs(source_root)
    markets_all = base.build_markets(grouped)
    window_run_level, start, end = base.select_window(markets_all, args.lookback_days)
    markets, duplicate_slugs, max_runs_per_slug = base.dedupe_by_slug(window_run_level)
    split = make_split(markets, args.train_days, args.validation_days, args.test_days, args.embargo_seconds)
    candidates = build_candidates(
        markets,
        split,
        args.fee,
        args.entry_mode,
        args.entry_step_seconds,
        args.entry_start_seconds,
        args.entry_end_buffer_seconds,
    )
    rows_by_split = {name: [row for row in candidates if row["split"] == name] for name in ("train", "validation", "test")}
    train_rows = rows_by_split["train"]
    validation_rows = rows_by_split["validation"]
    test_rows = rows_by_split["test"]
    if len(train_rows) < 100 or len(validation_rows) < 50 or len(test_rows) < 50:
        raise RuntimeError("Not enough candidates after split")

    model_train_rows = evenly_sample(train_rows, args.model_train_sample)
    model_validation_rows = evenly_sample(validation_rows, args.model_validation_sample)
    model, model_search = select_model(model_train_rows, model_validation_rows, FEATURE_SET_CANDIDATES)
    all_model_rows = train_rows + validation_rows + test_rows
    attach_scores(all_model_rows, model, args.fee)
    probability_metrics = [
        evaluate_probability(rows_by_split[name], [calibrated_prob(model, row) for row in rows_by_split[name]], name)
        for name in ("train", "validation", "test")
    ]
    factors = factor_diagnostics(rows_by_split, MODEL_FEATURES)
    weights = model_weight_rows(model)
    model_family_rows = model_family_summary(model_search)
    calibration_rows = calibration_bins(rows_by_split)
    if args.full_config_search:
        best_cfg, config_search = tune_decision_config(markets, all_model_rows, args.fee)
        config_search_mode = "full_validation_search"
    else:
        best_cfg = selected_fixed_decision_config()
        config_search = [fixed_config_search_row(markets, all_model_rows, best_cfg, args.fee)]
        config_search_mode = "fixed_selected_execution"
    execution_group_rows = execution_group_summary(config_search)

    selected_metrics: List[Dict[str, object]] = []
    all_logs: List[Dict[str, object]] = []
    curves: Dict[str, List[float]] = {}
    for name in ("train", "validation", "test"):
        metrics, logs, curve, _ = simulate_decision_system(markets, all_model_rows, best_cfg, name, args.fee)
        selected_metrics.append(metrics)
        all_logs.extend(logs)
        curves[name] = curve
    bucket_rows = bucket_metrics(all_logs)
    equity_rows = equity_curve_rows(curves)
    contribution_rows = contribution_summary(all_model_rows, all_logs, model)
    trade_explanation = trade_explanation_rows(all_model_rows, all_logs, model)
    model_mix = model_mix_rows(model)
    model_steps = model_step_rows(model, best_cfg)
    feature_blueprint = feature_blueprint_rows(model)
    decision_funnel = decision_funnel_rows(all_model_rows, all_logs, best_cfg)
    run_fast_execution_scan = args.fast_execution_scan and not args.skip_fast_execution_scan
    fast_execution_scan = [] if not run_fast_execution_scan else fast_execution_scan_rows(markets, all_model_rows, args.fee, args.wide_fast_execution_scan)
    fast_execution_pairs = execution_scan_pair_rows(fast_execution_scan)
    fast_execution_group_rows = execution_group_summary([row for row in fast_execution_scan if row.get("split") == "validation"])
    stress_rows = stress_test_rows(markets, all_model_rows, best_cfg, args.fee)
    stability_rows = stability_regime_rows(all_model_rows, all_logs)
    paper_rows, paper_summary_rows, paper_scenario_rows = paper_replay_rows(
        all_model_rows,
        all_logs,
        best_cfg,
        args.fee,
        args.paper_delay_seconds,
        args.paper_max_adverse_slippage,
        args.paper_replay_limit,
    )
    walk_forward_metrics, walk_forward_trades, walk_forward_probability = walk_forward_rows(
        markets,
        candidates,
        args.fee,
        args.walk_forward_train_days,
        args.walk_forward_validation_days,
        args.walk_forward_test_days,
        args.walk_forward_step_days,
        args.walk_forward_max_folds,
        args.embargo_seconds,
        args.walk_forward_train_sample,
        args.walk_forward_validation_sample,
        args.walk_forward_tune_config,
    )
    selected_search_row = next(
        (
            row
            for row in model_search
            if row.get("feature_set") == model.feature_set_name
            and abs(float(row.get("l2", math.nan)) - float(model.l2)) < 1e-12
        ),
        {},
    )

    manifest = {
        "source_root": str(source_root),
        "docs_dir": str(docs_dir),
        "lookback_days": args.lookback_days,
        "raw_runs": raw_stats["runs"],
        "raw_files": raw_stats["files"],
        "raw_rows": raw_stats["raw_rows"],
        "feature_markets_all": len(markets_all),
        "window_run_level_markets": len(window_run_level),
        "window_events": len(markets),
        "duplicate_slugs_in_window": duplicate_slugs,
        "max_runs_per_slug_in_window": max_runs_per_slug,
        "window_start_utc": start.isoformat(),
        "window_end_utc": end.isoformat(),
        "window_start_cst": base.to_tz_text(start, base.SH_TZ),
        "window_end_cst": base.to_tz_text(end, base.SH_TZ),
        "train_start_cst": base.to_tz_text(split.train_start, base.SH_TZ),
        "train_end_cst": base.to_tz_text(split.train_end, base.SH_TZ),
        "validation_start_cst": base.to_tz_text(split.validation_start, base.SH_TZ),
        "validation_end_cst": base.to_tz_text(split.validation_end, base.SH_TZ),
        "test_start_cst": base.to_tz_text(split.test_start, base.SH_TZ),
        "test_end_cst": base.to_tz_text(split.test_end, base.SH_TZ),
        "embargo_seconds": split.embargo_seconds,
        "candidates": len(candidates),
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "test_rows": len(test_rows),
        "model_train_rows": len(model_train_rows),
        "model_validation_rows": len(model_validation_rows),
        "entry_mode": args.entry_mode,
        "entry_step_seconds": args.entry_step_seconds,
        "entry_start_seconds": args.entry_start_seconds,
        "entry_end_buffer_seconds": args.entry_end_buffer_seconds,
        "config_search_mode": config_search_mode,
        "config_combinations": len(config_search),
        "full_config_combinations": full_execution_config_count(),
        "selected_config": best_cfg.name,
        "selected_model_kind": selected_search_row.get("model_kind", "mixed_blend" if model.blend_members else "single"),
        "selected_feature_set": model.feature_set_name,
        "selected_feature_count": len(model.feature_names),
        "selected_l2": model.l2,
        "selected_model_validation_logloss": selected_search_row.get("validation_logloss", math.nan),
        "selected_model_selection_loss": selected_search_row.get("selection_loss", math.nan),
        "selected_model_complexity_penalty": selected_search_row.get("complexity_penalty", 0.0),
        "selected_model_members": [name for name, _, _ in model.blend_members],
        "selected_max_trades_per_market": best_cfg.max_trades_per_market,
        "selected_min_entry_gap_seconds": best_cfg.min_entry_gap_seconds,
        "calibration_intercept": model.cal_intercept,
        "calibration_slope": model.cal_slope,
        "fee": args.fee,
        "starting_bankroll": STARTING_BANKROLL,
        "boundary_definition": f"entry_price >= {BOUNDARY_PRICE:.2f} or <= {LOW_BOUNDARY_PRICE:.2f}",
        "exit_policies_tested": [policy.name for policy in EXIT_POLICIES],
        "execution_search_grid": {
            "normal_edge": NORMAL_EDGE_GRID,
            "boundary_edge": BOUNDARY_EDGE_GRID,
            "min_depth": MIN_DEPTH_GRID,
            "event_cap": EVENT_CAP_GRID,
            "max_trades_per_market": MAX_TRADES_PER_MARKET_GRID,
            "min_entry_gap_seconds": MIN_ENTRY_GAP_SECONDS_GRID,
        },
        "workbook_alignment": "q_model + calibration + edge decision + fractional Kelly + price bucket / depth / drawdown gates",
        "walk_forward_train_days": args.walk_forward_train_days,
        "walk_forward_validation_days": args.walk_forward_validation_days,
        "walk_forward_test_days": args.walk_forward_test_days,
        "walk_forward_step_days": args.walk_forward_step_days,
        "walk_forward_max_folds": args.walk_forward_max_folds,
        "walk_forward_folds_run": len({row.get("fold") for row in walk_forward_metrics if row.get("status") == "ok"}),
        "walk_forward_tune_config": args.walk_forward_tune_config,
        "fast_execution_scan_combinations": len(fast_execution_pairs),
        "fast_execution_scan_enabled": run_fast_execution_scan,
        "wide_fast_execution_scan": args.wide_fast_execution_scan,
        "paper_delay_seconds": args.paper_delay_seconds,
        "paper_max_adverse_slippage": args.paper_max_adverse_slippage,
    }

    report_md = build_report(
        manifest,
        model,
        best_cfg,
        probability_metrics,
        selected_metrics,
        weights,
        factors,
        bucket_rows,
        model_family_rows,
        execution_group_rows,
        contribution_rows,
        model_mix,
        decision_funnel,
        stress_rows,
    )
    payload = {
        "manifest": manifest,
        "modelSteps": model_steps,
        "modelMix": model_mix,
        "featureBlueprint": feature_blueprint,
        "probabilityMetrics": probability_metrics,
        "selectedMetrics": selected_metrics,
        "modelWeights": weights,
        "factorDiagnostics": factors,
        "bucketMetrics": bucket_rows,
        "modelFamilySummary": model_family_rows,
        "calibrationBuckets": calibration_rows,
        "equityCurves": curves,
        "configSearchTop": config_search[:100],
        "executionGroupSummary": execution_group_rows,
        "fastExecutionScan": fast_execution_scan,
        "fastExecutionPairs": fast_execution_pairs,
        "fastExecutionGroupSummary": fast_execution_group_rows,
        "contributionSummary": contribution_rows,
        "tradeExplanations": trade_explanation,
        "decisionFunnel": decision_funnel,
        "stressTests": stress_rows,
        "stabilityRegimes": stability_rows,
        "paperReplay": paper_rows,
        "paperReplaySummary": paper_summary_rows,
        "paperReplayScenarios": paper_scenario_rows,
        "walkForwardMetrics": walk_forward_metrics,
        "walkForwardTrades": walk_forward_trades[:1000],
        "walkForwardProbability": walk_forward_probability,
        "modelSearch": model_search,
    }
    write_csv(report_dir / "probability_metrics.csv", probability_metrics)
    write_csv(report_dir / "selected_system_split_metrics.csv", selected_metrics)
    write_csv(report_dir / "selected_system_trade_logs.csv", all_logs)
    write_csv(report_dir / "model_weights.csv", weights)
    write_csv(report_dir / "factor_diagnostics.csv", factors)
    write_csv(report_dir / "bucket_metrics.csv", bucket_rows)
    write_csv(report_dir / "model_family_summary.csv", model_family_rows)
    write_csv(report_dir / "calibration_buckets.csv", calibration_rows)
    write_csv(report_dir / "equity_curves.csv", equity_rows)
    write_csv(report_dir / "config_search_top.csv", config_search[:200])
    write_csv(report_dir / "execution_group_summary.csv", execution_group_rows)
    write_csv(report_dir / "fast_execution_scan.csv", fast_execution_scan)
    write_csv(report_dir / "fast_execution_pairs.csv", fast_execution_pairs)
    write_csv(report_dir / "fast_execution_group_summary.csv", fast_execution_group_rows)
    write_csv(report_dir / "contribution_summary.csv", contribution_rows)
    write_csv(report_dir / "trade_explanations.csv", trade_explanation)
    write_csv(report_dir / "model_steps.csv", model_steps)
    write_csv(report_dir / "model_mix.csv", model_mix)
    write_csv(report_dir / "feature_blueprint.csv", feature_blueprint)
    write_csv(report_dir / "decision_funnel.csv", decision_funnel)
    write_csv(report_dir / "stress_tests.csv", stress_rows)
    write_csv(report_dir / "stability_regimes.csv", stability_rows)
    write_csv(report_dir / "paper_replay.csv", paper_rows)
    write_csv(report_dir / "paper_replay_summary.csv", paper_summary_rows)
    write_csv(report_dir / "paper_replay_scenarios.csv", paper_scenario_rows)
    write_csv(report_dir / "walk_forward_metrics.csv", walk_forward_metrics)
    write_csv(report_dir / "walk_forward_trades.csv", walk_forward_trades)
    write_csv(report_dir / "walk_forward_probability.csv", walk_forward_probability)
    write_csv(report_dir / "model_search.csv", model_search)
    write_csv(report_dir / "coverage.csv", coverage)
    (report_dir / "manifest.json").write_text(json.dumps(json_ready(manifest), indent=2, ensure_ascii=False), encoding="utf-8")
    (report_dir / "robust_system_report.md").write_text(report_md, encoding="utf-8")
    (report_dir / "index.html").write_text(build_html(report_md, payload), encoding="utf-8")
    return {"report_dir": str(report_dir), "manifest": manifest, "selected_metrics": selected_metrics, "top_config": config_search[:5], "probability_metrics": probability_metrics}


def main() -> None:
    default_source, default_docs, default_report = default_paths()
    parser = argparse.ArgumentParser(description="Build a robust Polymarket 5m trading system from q-calibration, edge, and risk gates.")
    parser.add_argument("--source-root", default=str(default_source))
    parser.add_argument("--docs-dir", default=str(default_docs))
    parser.add_argument("--report-dir", default=str(default_report))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=TRAIN_DAYS_DEFAULT)
    parser.add_argument("--validation-days", type=int, default=VALIDATION_DAYS_DEFAULT)
    parser.add_argument("--test-days", type=int, default=TEST_DAYS_DEFAULT)
    parser.add_argument("--embargo-seconds", type=int, default=EMBARGO_SECONDS_DEFAULT)
    parser.add_argument("--fee", type=float, default=base.FEE_DEFAULT)
    parser.add_argument("--entry-mode", choices=["dense4s", "sparse_minutes"], default=ENTRY_MODE_DEFAULT)
    parser.add_argument("--entry-step-seconds", type=int, default=ENTRY_STEP_SECONDS_DEFAULT)
    parser.add_argument("--entry-start-seconds", type=int, default=ENTRY_START_SECONDS_DEFAULT)
    parser.add_argument("--entry-end-buffer-seconds", type=int, default=ENTRY_END_BUFFER_SECONDS_DEFAULT)
    parser.add_argument("--model-train-sample", type=int, default=MODEL_TRAIN_SAMPLE_DEFAULT)
    parser.add_argument("--model-validation-sample", type=int, default=MODEL_VALIDATION_SAMPLE_DEFAULT)
    parser.add_argument("--full-config-search", action="store_true", help="Run the full execution-parameter search instead of reusing the previously selected robust config.")
    parser.add_argument("--fast-execution-scan", action="store_true", help="Run the lightweight edge/cap/multi-trade execution scan for dashboard diagnostics.")
    parser.add_argument("--skip-fast-execution-scan", action="store_true", help="Skip the lightweight edge/cap/multi-trade execution scan used for dashboard diagnostics.")
    parser.add_argument("--wide-fast-execution-scan", action="store_true", help="Expand the lightweight execution scan from 12 to 36 configs by adding cap 0.005 and edge 0.05.")
    parser.add_argument("--walk-forward-train-days", type=int, default=WALK_FORWARD_TRAIN_DAYS_DEFAULT)
    parser.add_argument("--walk-forward-validation-days", type=int, default=WALK_FORWARD_VALIDATION_DAYS_DEFAULT)
    parser.add_argument("--walk-forward-test-days", type=int, default=WALK_FORWARD_TEST_DAYS_DEFAULT)
    parser.add_argument("--walk-forward-step-days", type=int, default=WALK_FORWARD_STEP_DAYS_DEFAULT)
    parser.add_argument("--walk-forward-max-folds", type=int, default=WALK_FORWARD_MAX_FOLDS_DEFAULT)
    parser.add_argument("--walk-forward-train-sample", type=int, default=WALK_FORWARD_TRAIN_SAMPLE_DEFAULT)
    parser.add_argument("--walk-forward-validation-sample", type=int, default=WALK_FORWARD_VALIDATION_SAMPLE_DEFAULT)
    parser.add_argument("--walk-forward-tune-config", action="store_true", help="Tune a small execution grid inside each walk-forward fold; default keeps execution fixed to reduce fold overfitting.")
    parser.add_argument("--paper-delay-seconds", type=int, default=4)
    parser.add_argument("--paper-max-adverse-slippage", type=float, default=0.01)
    parser.add_argument("--paper-replay-limit", type=int, default=PAPER_REPLAY_LIMIT_DEFAULT)
    args = parser.parse_args()
    output = run(args)
    print(json.dumps(json_ready(output), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
