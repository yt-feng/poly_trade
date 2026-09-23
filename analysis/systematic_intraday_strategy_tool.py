from __future__ import annotations

import argparse
import csv
import html
import json
import math
import re
import statistics
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from xml.etree import ElementTree as ET


FEE_DEFAULT = 0.01
STARTING_BANKROLL = 100.0
WINDOW_12H = 48
WINDOW_36H = 144
WINDOW_72H = 864
ET_TZ = "America/New_York"
SH_TZ = "Asia/Shanghai"
PRIOR_FINAL_MIX = {"v1_active_fill_mix"}
PRIOR_MIXES = {"v1_active_fill_mix", "v1_balanced_mix", "v1_adaptive_mix", "v1_conservative_mix"}
PRIOR_VALIDATION_SELECTED = {"classic_breakout_up"}
PRIOR_SELECTED = PRIOR_FINAL_MIX | PRIOR_MIXES | PRIOR_VALIDATION_SELECTED

NUMERIC_COLUMNS = {
    "buy_up_cents",
    "buy_down_cents",
    "sell_up_cents",
    "sell_down_cents",
    "buy_up_size",
    "buy_down_size",
    "sell_up_size",
    "sell_down_size",
    "mid_up_cents",
    "mid_down_cents",
    "spread_up_cents",
    "spread_down_cents",
    "bid_depth_up_5",
    "ask_depth_up_5",
    "bid_depth_down_5",
    "ask_depth_down_5",
    "min_order_size_up",
    "min_order_size_down",
    "tick_size_up",
    "tick_size_down",
    "target_price",
    "final_price",
    "trade_count_1s",
    "trade_volume_1s",
}


try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python 3.9+ on the target machine has zoneinfo.
    ZoneInfo = None  # type: ignore


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(float(value))


def num(value: object) -> float:
    if value is None:
        return math.nan
    text = str(value).strip()
    if not text:
        return math.nan
    try:
        return float(text)
    except Exception:
        return math.nan


def val(value: object, default: float = 0.0) -> float:
    return float(value) if finite(value) else default


def first_finite(values: Iterable[float]) -> float:
    for value in values:
        if finite(value):
            return float(value)
    return math.nan


def last_finite(values: Iterable[float]) -> float:
    seen = math.nan
    for value in values:
        if finite(value):
            seen = float(value)
    return seen


def median(values: Iterable[float]) -> float:
    xs = [float(v) for v in values if finite(v)]
    if not xs:
        return math.nan
    return statistics.median(xs)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_ts(text: object) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text)).astimezone(timezone.utc)
    except Exception:
        return None


def parse_slug_start(slug: str) -> Optional[datetime]:
    match = re.search(r"(\d{10})$", str(slug))
    if not match:
        return None
    return datetime.fromtimestamp(int(match.group(1)), timezone.utc)


def to_tz_text(ts: Optional[datetime], tz_name: str) -> str:
    if ts is None:
        return ""
    if ZoneInfo is None:
        return ts.isoformat()
    return ts.astimezone(ZoneInfo(tz_name)).isoformat()


def session_et(ts: datetime) -> str:
    if ZoneInfo is None:
        local = ts
    else:
        local = ts.astimezone(ZoneInfo(ET_TZ))
    minutes = local.hour * 60 + local.minute
    weekday = local.weekday()
    if minutes >= 1140 or minutes < 120:
        return "asia"
    if 120 <= minutes < 570:
        return "london"
    if weekday < 5 and 570 <= minutes < 720:
        return "us_open"
    if weekday < 5 and 720 <= minutes < 960:
        return "us_afternoon"
    return "other"


def imbalance(a: float, b: float) -> float:
    if not finite(a) or not finite(b) or a + b <= 0:
        return math.nan
    return float((a - b) / (a + b))


def price_from_row(row: Optional[Dict[str, object]], column: str) -> float:
    if row is None:
        return math.nan
    value = row.get(column, math.nan)
    return float(value) / 100.0 if finite(value) else math.nan


def size_from_row(row: Optional[Dict[str, object]], column: str) -> float:
    if row is None:
        return math.nan
    value = row.get(column, math.nan)
    return float(value) if finite(value) else math.nan


def snapshot_row(rows: List[Dict[str, object]], first_ts: datetime, minute: int) -> Optional[Dict[str, object]]:
    cutoff = first_ts + timedelta(minutes=minute)
    selected = None
    for row in rows:
        ts = row["ts_utc"]
        if isinstance(ts, datetime) and ts <= cutoff:
            selected = row
        else:
            break
    return selected


def row_after_time(rows: List[Dict[str, object]], first_ts: datetime, minute: int) -> Optional[Dict[str, object]]:
    cutoff = first_ts + timedelta(minutes=minute)
    selected = None
    for row in rows:
        ts = row["ts_utc"]
        if isinstance(ts, datetime) and ts <= cutoff:
            selected = row
        else:
            break
    return selected


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    mode: str
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    trail_start: Optional[float] = None
    trail_gap: Optional[float] = None
    hold_buffer_seconds: int = 5
    close_buffer_seconds: int = 5
    require_exit_liquidity: bool = True


@dataclass(frozen=True)
class StrategySpec:
    name: str
    family: str
    entry_name: str
    mode: str
    exit_policy: ExitPolicy


SETTLE_POLICY = ExitPolicy("settle", "settle")
EXIT_POLICIES = [
    SETTLE_POLICY,
    ExitPolicy("tp04_sl02_buf3s", "intraday", take_profit=0.04, stop_loss=0.02, hold_buffer_seconds=3, close_buffer_seconds=3),
    ExitPolicy("tp05_sl03_buf5s", "intraday", take_profit=0.05, stop_loss=0.03),
    ExitPolicy("tp06_sl03_buf3s", "intraday", take_profit=0.06, stop_loss=0.03, hold_buffer_seconds=3, close_buffer_seconds=3),
    ExitPolicy("tp08_sl04_buf5s", "intraday", take_profit=0.08, stop_loss=0.04),
    ExitPolicy("tp12_sl06_buf5s", "intraday", take_profit=0.12, stop_loss=0.06),
    ExitPolicy("trail08_gap03_buf5s", "intraday", trail_start=0.08, trail_gap=0.03, stop_loss=0.05),
]


def qfrac(quality: float, low: float, high: float) -> float:
    return low + (high - low) * clamp(val(quality), 0.0, 1.0)


def breakout(row: Dict[str, object]) -> bool:
    return finite(row.get("btc_move_4m")) and 30 < float(row["btc_move_4m"]) <= 50 and val(row.get("buy_up_price_4m"), 1.0) <= 0.90


def breakout_with_liquidity(row: Dict[str, object]) -> bool:
    return breakout(row) and val(row.get("buy_up_size_4m")) >= 100


def milddrop(row: Dict[str, object]) -> bool:
    return (
        finite(row.get("btc_move_2m"))
        and -30 < float(row["btc_move_2m"]) <= -10
        and val(row.get("size_imbalance_updown_2m"), 1.0) <= 0
        and val(row.get("book_pressure_down_2m"), -1.0) >= -0.2
    )


def early_drop(row: Dict[str, object]) -> bool:
    return finite(row.get("btc_move_1m")) and float(row["btc_move_1m"]) <= -20


def early_drop_with_liquidity(row: Dict[str, object]) -> bool:
    return early_drop(row) and val(row.get("buy_down_size_1m")) >= 80


def sharpdrop_reversal(row: Dict[str, object]) -> bool:
    return (
        finite(row.get("btc_move_2m"))
        and -50 < float(row["btc_move_2m"]) <= -30
        and val(row.get("buy_up_price_2m"), 1.0) <= 0.45
        and val(row.get("buy_up_size_2m")) >= 120
    )


def extreme_up_fade(row: Dict[str, object]) -> bool:
    return finite(row.get("btc_move_2m")) and float(row["btc_move_2m"]) >= 50 and val(row.get("buy_down_price_2m"), 1.0) <= 0.30


def book_consensus_down(row: Dict[str, object]) -> bool:
    return (
        milddrop(row)
        and val(row.get("size_imbalance_updown_2m"), 1.0) <= -0.10
        and val(row.get("book_pressure_down_2m"), -1.0) >= 0.0
        and val(row.get("trade_count_sum_first2m")) >= 20
    )


def book_consensus_up(row: Dict[str, object]) -> bool:
    return (
        finite(row.get("btc_move_2m"))
        and float(row["btc_move_2m"]) >= 10
        and val(row.get("size_imbalance_updown_2m"), -1.0) >= 0.10
        and val(row.get("book_pressure_up_2m"), -1.0) >= 0.0
        and val(row.get("buy_up_price_2m"), 1.0) <= 0.75
    )


def ts_momentum_up(row: Dict[str, object]) -> bool:
    return (
        finite(row.get("btc_move_2m"))
        and float(row["btc_move_2m"]) >= 20
        and val(row.get("path_efficiency_first2m")) >= 0.55
        and val(row.get("mid_up_prob_change_2m"), -1.0) >= 0.02
        and val(row.get("buy_up_price_2m"), 1.0) <= 0.75
    )


def ts_momentum_down(row: Dict[str, object]) -> bool:
    return (
        finite(row.get("btc_move_2m"))
        and float(row["btc_move_2m"]) <= -15
        and val(row.get("path_efficiency_first2m")) >= 0.50
        and val(row.get("mid_up_prob_change_2m"), 0.0) <= -0.02
        and val(row.get("buy_down_price_2m"), 1.0) <= 0.75
    )


def pm_overshoot_fade_up(row: Dict[str, object]) -> bool:
    return (
        bool(row.get("pm_up_crash_without_btc_crash"))
        and val(row.get("spread_up_median_first2m"), 1.0) <= 0.05
        and val(row.get("buy_up_size_2m")) >= 120
    )


def pm_squeeze_fade_down(row: Dict[str, object]) -> bool:
    return (
        bool(row.get("pm_up_squeeze_without_btc_squeeze"))
        and val(row.get("spread_down_median_first2m"), 1.0) <= 0.05
        and val(row.get("buy_down_size_2m")) >= 120
    )


def fixed_fraction_from_name(name: str, default: float = 0.05) -> float:
    match = re.search(r"fixed_(\d+)pct", name)
    if not match:
        return default
    return float(match.group(1)) / 100.0


def adaptive_fraction(quality: float) -> float:
    return clamp(0.04 + 0.08 * clamp(val(quality), 0.0, 1.0), 0.04, 0.12)


def choose_trade(row: Dict[str, object], entry_name: str) -> Tuple[str, int, float, str]:
    sess = str(row.get("session_et", "other"))

    if entry_name == "classic_breakout_core":
        if sess in {"asia", "us_open"} and breakout_with_liquidity(row) and row.get("gate_up"):
            return "buy_up", 4, 0.05, "classic_breakout_core"
    elif entry_name == "classic_breakout_up":
        if breakout(row) and row.get("gate_up"):
            return "buy_up", 4, 0.05, "breakout_up"
    elif entry_name == "classic_milddrop_core":
        if sess in {"london", "us_afternoon"} and milddrop(row) and row.get("gate_down"):
            return "buy_down", 2, 0.05, "classic_milddrop_core"
    elif entry_name == "classic_milddrop_down":
        if milddrop(row) and row.get("gate_down"):
            return "buy_down", 2, 0.05, "milddrop_down"
    elif entry_name == "classic_early_drop_down":
        if sess in {"london", "us_afternoon"} and early_drop_with_liquidity(row) and val(row.get("spread_down_median_first2m"), 1.0) <= 0.05:
            return "buy_down", 1, 0.04, "classic_early_drop_down"
    elif entry_name == "classic_early_drop_down_wf":
        if early_drop(row) and val(row.get("spread_down_median_first2m"), 1.0) <= 0.06:
            return "buy_down", 1, 0.04, "classic_early_drop_down_wf"
    elif entry_name == "classic_sharpdrop_reversal":
        if sharpdrop_reversal(row) and val(row.get("spread_up_median_first2m"), 1.0) <= 0.06:
            return "buy_up", 2, 0.04, "classic_sharpdrop_reversal"
    elif entry_name == "classic_sharpdrop_reversal_up":
        if sharpdrop_reversal(row) and val(row.get("spread_up_median_first2m"), 1.0) <= 0.06:
            return "buy_up", 2, 0.04, "sharpdrop_reversal_up"
    elif entry_name == "classic_extremeup_fade_down":
        if extreme_up_fade(row) and val(row.get("spread_down_median_first2m"), 1.0) <= 0.06:
            return "buy_down", 2, 0.04, "classic_extremeup_fade_down"
    elif entry_name == "micro_book_consensus_down":
        if sess in {"london", "us_afternoon", "other"} and book_consensus_down(row) and row.get("gate_down"):
            return "buy_down", 2, 0.05, "micro_book_consensus_down"
    elif entry_name == "micro_book_consensus_up":
        if sess in {"asia", "us_open", "other"} and book_consensus_up(row) and row.get("gate_up"):
            return "buy_up", 2, 0.04, "micro_book_consensus_up"
    elif entry_name == "game_pm_overshoot_fade_up":
        if pm_overshoot_fade_up(row):
            return "buy_up", 2, 0.035, "game_pm_overshoot_fade_up"
    elif entry_name == "game_pm_squeeze_fade_down":
        if pm_squeeze_fade_down(row):
            return "buy_down", 2, 0.035, "game_pm_squeeze_fade_down"
    elif entry_name == "timeseries_momentum_up":
        if ts_momentum_up(row) and row.get("gate_up"):
            return "buy_up", 2, 0.04, "timeseries_momentum_up"
    elif entry_name == "timeseries_momentum_down":
        if ts_momentum_down(row) and row.get("gate_down"):
            return "buy_down", 2, 0.04, "timeseries_momentum_down"
    elif entry_name == "timeseries_reversion_down":
        if extreme_up_fade(row):
            return "buy_down", 2, 0.035, "timeseries_reversion_down"
    elif entry_name == "v1_conservative_mix":
        if sess == "asia" and breakout(row) and row.get("triple_gate_up"):
            return "buy_up", 4, 0.08, "asia_breakout"
        if sess == "london" and milddrop(row) and row.get("triple_gate_down") and val(row.get("quality_milddrop")) >= 0.60:
            return "buy_down", 2, 0.06, "london_milddrop"
        if sess == "us_afternoon" and milddrop(row) and val(row.get("quality_milddrop")) >= 0.58:
            return "buy_down", 2, 0.08, "us_afternoon_milddrop"
    elif entry_name == "v1_balanced_mix":
        if sess == "asia" and breakout(row) and row.get("triple_gate_up"):
            return "buy_up", 4, 0.10, "asia_breakout"
        if sess == "london" and milddrop(row) and row.get("triple_gate_down"):
            return "buy_down", 2, 0.08, "london_milddrop"
        if sess == "us_afternoon" and milddrop(row) and val(row.get("quality_milddrop")) >= 0.55:
            return "buy_down", 2, 0.10, "us_afternoon_milddrop"
        if sess in {"asia", "us_open"} and breakout(row) and row.get("triple_gate_up") and val(row.get("quality_breakout")) >= 0.60:
            return "buy_up", 4, 0.06, "breakout_filler"
    elif entry_name == "v1_adaptive_mix":
        if sess == "asia" and breakout(row) and row.get("triple_gate_up"):
            return "buy_up", 4, qfrac(row.get("quality_breakout", math.nan), 0.05, 0.10), "asia_breakout"
        if sess == "london" and milddrop(row) and row.get("triple_gate_down"):
            return "buy_down", 2, qfrac(row.get("quality_milddrop", math.nan), 0.04, 0.09), "london_milddrop"
        if sess == "us_afternoon" and milddrop(row) and val(row.get("quality_milddrop")) >= 0.55:
            return "buy_down", 2, qfrac(row.get("quality_milddrop", math.nan), 0.05, 0.10), "us_afternoon_milddrop"
    elif entry_name == "v1_active_fill_mix":
        if sess == "asia" and breakout(row):
            return "buy_up", 4, 0.08 if row.get("triple_gate_up") else 0.05, "asia_breakout"
        if sess == "london" and milddrop(row):
            return "buy_down", 2, 0.06 if row.get("triple_gate_down") else 0.04, "london_milddrop"
        if sess == "us_afternoon" and milddrop(row):
            return "buy_down", 2, 0.08 if val(row.get("quality_milddrop")) >= 0.55 else 0.05, "us_afternoon_milddrop"
        if sess == "us_open" and breakout(row) and val(row.get("quality_breakout")) >= 0.58:
            return "buy_up", 4, 0.04, "us_open_breakout"
    elif entry_name == "v2_strict_session_mix":
        if sess == "asia" and breakout_with_liquidity(row) and row.get("gate_up_strict"):
            return "buy_up", 4, qfrac(row.get("quality_breakout", math.nan), 0.03, 0.07), "asia_breakout_strict"
        if sess == "london" and book_consensus_down(row) and row.get("gate_down_strict"):
            return "buy_down", 2, qfrac(row.get("quality_milddrop", math.nan), 0.03, 0.07), "london_book_down_strict"
        if sess == "us_afternoon" and milddrop(row) and val(row.get("quality_milddrop")) >= 0.62:
            return "buy_down", 2, 0.05, "us_afternoon_milddrop_strict"
    elif entry_name == "v2_micro_game_mix":
        if sess in {"london", "us_afternoon"} and book_consensus_down(row):
            return "buy_down", 2, 0.05, "book_consensus"
        if pm_overshoot_fade_up(row):
            return "buy_up", 2, 0.035, "pm_overshoot_fade"
        if sess == "asia" and breakout_with_liquidity(row) and row.get("gate_up"):
            return "buy_up", 4, 0.05, "asia_breakout"
    elif entry_name == "v2_time_series_mix":
        if ts_momentum_up(row) and row.get("gate_up"):
            return "buy_up", 2, 0.04, "ts_momentum"
        if extreme_up_fade(row):
            return "buy_down", 2, 0.04, "ts_reversion"
        if sess in {"london", "us_afternoon"} and milddrop(row) and val(row.get("quality_milddrop")) >= 0.60:
            return "buy_down", 2, 0.04, "milddrop_quality"
    elif entry_name == "portfolio_micro_ts_mix":
        if book_consensus_down(row) and row.get("gate_down"):
            return "buy_down", 2, 0.045, "book_consensus_down"
        if ts_momentum_up(row) and row.get("gate_up"):
            return "buy_up", 2, 0.035, "ts_momentum_up"
        if bool(row.get("pm_up_crash_without_btc_crash")) and val(row.get("spread_up_median_first2m"), 1.0) <= 0.05:
            return "buy_up", 2, 0.03, "pm_overshoot_fade_up"
    elif entry_name == "portfolio_conservative_v2":
        if sess == "asia" and breakout(row) and row.get("gate_up_strict"):
            return "buy_up", 4, 0.04, "asia_breakout_strict"
        if sess == "london" and book_consensus_down(row) and row.get("gate_down_strict"):
            return "buy_down", 2, 0.04, "london_book_down_strict"
        if sess == "us_afternoon" and milddrop(row) and val(row.get("quality_milddrop")) >= 0.62:
            return "buy_down", 2, 0.045, "us_afternoon_milddrop"
    elif entry_name.startswith("session_breakout_"):
        target_session = entry_name.split("session_breakout_")[1].split("_fixed_")[0]
        if sess == target_session and breakout(row):
            return "buy_up", 4, fixed_fraction_from_name(entry_name), f"{target_session}_breakout"
    elif entry_name.startswith("session_milddrop_"):
        target_session = entry_name.split("session_milddrop_")[1].split("_fixed_")[0]
        if sess == target_session and milddrop(row):
            return "buy_down", 2, fixed_fraction_from_name(entry_name), f"{target_session}_milddrop"
    elif entry_name.startswith("gate_breakout_fixed_"):
        if breakout(row) and row.get("triple_gate_up"):
            return "buy_up", 4, fixed_fraction_from_name(entry_name), "gate_breakout"
    elif entry_name.startswith("gate_milddrop_fixed_"):
        if milddrop(row) and row.get("triple_gate_down"):
            return "buy_down", 2, fixed_fraction_from_name(entry_name), "gate_milddrop"
    elif entry_name.startswith("combo_session_mix_fixed_") or entry_name == "combo_session_mix_adaptive":
        if sess == "us_open" and breakout(row) and row.get("triple_gate_up"):
            frac = adaptive_fraction(row.get("quality_breakout", math.nan)) if entry_name.endswith("_adaptive") else fixed_fraction_from_name(entry_name)
            return "buy_up", 4, frac, "combo_us_open_breakout"
        if sess in {"london", "asia"} and milddrop(row) and row.get("triple_gate_down") and val(row.get("quality_milddrop")) >= 0.55:
            frac = adaptive_fraction(row.get("quality_milddrop", math.nan)) if entry_name.endswith("_adaptive") else fixed_fraction_from_name(entry_name)
            return "buy_down", 2, frac, "combo_milddrop"
    elif entry_name.startswith("combo_guarded_fixed_") or entry_name == "combo_guarded_adaptive":
        if sess in {"us_open", "us_afternoon"} and breakout(row) and row.get("triple_gate_up"):
            frac = adaptive_fraction(row.get("quality_breakout", math.nan)) if entry_name.endswith("_adaptive") else fixed_fraction_from_name(entry_name)
            return "buy_up", 4, frac, "combo_breakout"
        if sess in {"asia", "london"} and milddrop(row) and row.get("triple_gate_down"):
            frac = adaptive_fraction(row.get("quality_milddrop", math.nan)) if entry_name.endswith("_adaptive") else fixed_fraction_from_name(entry_name)
            return "buy_down", 2, frac, "combo_milddrop"

    return "skip", -1, math.nan, "none"


def strategy_mode(entry_name: str) -> str:
    if entry_name.startswith("combo_") or entry_name.startswith("session_") or entry_name.startswith("gate_"):
        return "robust"
    if entry_name.startswith("portfolio_"):
        return "qrf"
    return "full_refresh"


def strategy_family(entry_name: str) -> str:
    if entry_name.startswith("v1_"):
        return "previous_v1_mix"
    if entry_name.startswith("v2_"):
        return "refreshed_v2_mix"
    if entry_name.startswith("portfolio_"):
        return "portfolio_mix"
    if entry_name.startswith("combo_"):
        return "combo_session"
    if entry_name.startswith("session_"):
        return "session_single"
    if entry_name.startswith("gate_"):
        return "gate_single"
    if entry_name.startswith("classic_"):
        return "classic_quant"
    if entry_name.startswith("micro_"):
        return "microstructure"
    if entry_name.startswith("game_"):
        return "game_theory"
    if entry_name.startswith("timeseries_"):
        return "time_series"
    return "other"


def prior_label(entry_name: str) -> str:
    labels: List[str] = []
    if entry_name in PRIOR_FINAL_MIX:
        labels.append("prior_final_v1_mix")
    if entry_name in PRIOR_MIXES:
        labels.append("prior_mixed_candidate")
    if entry_name in PRIOR_VALIDATION_SELECTED:
        labels.append("prior_validation_selected")
    return ";".join(labels)


def build_entry_names() -> List[str]:
    names = [
        "classic_breakout_core",
        "classic_breakout_up",
        "classic_milddrop_core",
        "classic_milddrop_down",
        "classic_early_drop_down",
        "classic_early_drop_down_wf",
        "classic_sharpdrop_reversal",
        "classic_sharpdrop_reversal_up",
        "classic_extremeup_fade_down",
        "micro_book_consensus_down",
        "micro_book_consensus_up",
        "game_pm_overshoot_fade_up",
        "game_pm_squeeze_fade_down",
        "timeseries_momentum_up",
        "timeseries_momentum_down",
        "timeseries_reversion_down",
        "v1_conservative_mix",
        "v1_balanced_mix",
        "v1_adaptive_mix",
        "v1_active_fill_mix",
        "v2_strict_session_mix",
        "v2_micro_game_mix",
        "v2_time_series_mix",
        "portfolio_micro_ts_mix",
        "portfolio_conservative_v2",
    ]
    for sess in ["asia", "london", "us_open", "us_afternoon"]:
        for pct in [4, 6, 8, 10, 12]:
            names.append(f"session_breakout_{sess}_fixed_{pct}pct")
            names.append(f"session_milddrop_{sess}_fixed_{pct}pct")
    for pct in [4, 6, 8, 10, 12]:
        names.extend(
            [
                f"gate_breakout_fixed_{pct}pct",
                f"gate_milddrop_fixed_{pct}pct",
                f"combo_session_mix_fixed_{pct}pct",
                f"combo_guarded_fixed_{pct}pct",
            ]
        )
    names.extend(["combo_session_mix_adaptive", "combo_guarded_adaptive"])
    return names


def build_strategy_specs() -> List[StrategySpec]:
    specs: List[StrategySpec] = []
    for entry_name in build_entry_names():
        mode = strategy_mode(entry_name)
        family = strategy_family(entry_name)
        for policy in EXIT_POLICIES:
            strategy_name = f"{entry_name}__{policy.name}"
            specs.append(StrategySpec(strategy_name, family, entry_name, mode, policy))
    return specs


def clean_row(row: Dict[str, str], source_file: str) -> Optional[Dict[str, object]]:
    ts = parse_ts(row.get("ts_iso"))
    slug = row.get("slug", "")
    if ts is None or not slug:
        return None
    out: Dict[str, object] = {"ts_iso": row.get("ts_iso", ""), "ts_utc": ts, "source_file": source_file}
    for key, value in row.items():
        if key in NUMERIC_COLUMNS:
            out[key] = num(value)
        else:
            out[key] = value
    if not finite(out.get("mid_up_cents")) and finite(out.get("buy_up_cents")) and finite(out.get("sell_up_cents")):
        out["mid_up_cents"] = (float(out["buy_up_cents"]) + float(out["sell_up_cents"])) / 2.0
    if not finite(out.get("mid_down_cents")) and finite(out.get("buy_down_cents")) and finite(out.get("sell_down_cents")):
        out["mid_down_cents"] = (float(out["buy_down_cents"]) + float(out["sell_down_cents"])) / 2.0
    out["mid_up_prob"] = float(out["mid_up_cents"]) / 100.0 if finite(out.get("mid_up_cents")) else math.nan
    out["mid_down_prob"] = float(out["mid_down_cents"]) / 100.0 if finite(out.get("mid_down_cents")) else math.nan
    out["mid_overround_cents"] = (
        float(out["mid_up_cents"]) + float(out["mid_down_cents"]) - 100.0
        if finite(out.get("mid_up_cents")) and finite(out.get("mid_down_cents"))
        else math.nan
    )
    return out


def read_monthly_runs(source_root: Path) -> Tuple[Dict[Tuple[str, str], List[Dict[str, object]]], List[Dict[str, object]], Dict[str, int]]:
    grouped: Dict[Tuple[str, str], List[Dict[str, object]]] = defaultdict(list)
    coverage: List[Dict[str, object]] = []
    stats = {"raw_rows": 0, "files": 0, "runs": 0}
    for run_dir in sorted(path for path in source_root.iterdir() if path.is_dir()):
        files = sorted(run_dir.glob("*btc-updown-5m_quotes.csv"))
        if not files:
            continue
        stats["runs"] += 1
        stats["files"] += len(files)
        run_rows = 0
        for file_path in files:
            with file_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    stats["raw_rows"] += 1
                    run_rows += 1
                    clean = clean_row(row, file_path.name)
                    if clean is None:
                        continue
                    slug = str(clean.get("slug", ""))
                    grouped[(run_dir.name, slug)].append(clean)
        coverage.append({"run_name": run_dir.name, "file_count": len(files), "raw_row_count": run_rows})
    return grouped, coverage, stats


def build_market(run_name: str, slug: str, rows: List[Dict[str, object]]) -> Optional[Dict[str, object]]:
    dedup: Dict[str, Dict[str, object]] = {}
    for row in sorted(rows, key=lambda x: (x["ts_utc"], str(x.get("source_file", "")))):  # type: ignore[index]
        dedup[str(row.get("ts_iso", ""))] = row
    rows = sorted(dedup.values(), key=lambda x: (x["ts_utc"], str(x.get("source_file", ""))))  # type: ignore[index]
    if not rows:
        return None
    first_ts = rows[0]["ts_utc"]
    if not isinstance(first_ts, datetime):
        return None
    market_start = parse_slug_start(slug) or first_ts
    market_close = market_start + timedelta(minutes=5)

    target = first_finite(float(row.get("target_price", math.nan)) for row in rows)
    final = last_finite(float(row.get("final_price", math.nan)) for row in rows)
    if not finite(target) or not finite(final):
        return None
    outcome_up = 1.0 if final > target else 0.0

    row1 = snapshot_row(rows, first_ts, 1)
    row2 = snapshot_row(rows, first_ts, 2)
    row4 = snapshot_row(rows, first_ts, 4)
    if row2 is None:
        return None
    first2 = [row for row in rows if isinstance(row["ts_utc"], datetime) and row["ts_utc"] <= first_ts + timedelta(minutes=2)]
    if not first2:
        return None

    path = [float(row["final_price"]) for row in first2 if finite(row.get("final_price"))]
    diffs = [path[i] - path[i - 1] for i in range(1, len(path))]
    total_abs = sum(abs(x) for x in diffs)
    path_efficiency = abs(path[-1] - path[0]) / total_abs if len(path) >= 2 and total_abs > 0 else 1.0
    realized_vol = statistics.stdev(diffs) if len(diffs) >= 2 else 0.0
    move_series = [float(row["final_price"]) - target for row in first2 if finite(row.get("final_price"))]
    max_drawdown_first2m = abs(min(move_series)) if move_series else math.nan
    max_rebound_first2m = max(move_series) if move_series else math.nan

    def move(row: Optional[Dict[str, object]]) -> float:
        if row is None or not finite(row.get("final_price")):
            return math.nan
        return float(row["final_price"]) - target

    buy_up_size_2m = size_from_row(row2, "buy_up_size")
    buy_down_size_2m = size_from_row(row2, "buy_down_size")
    sell_up_size_2m = size_from_row(row2, "sell_up_size")
    sell_down_size_2m = size_from_row(row2, "sell_down_size")
    bid_depth_up_2m = size_from_row(row2, "bid_depth_up_5")
    bid_depth_down_2m = size_from_row(row2, "bid_depth_down_5")
    ask_depth_up_2m = size_from_row(row2, "ask_depth_up_5")
    ask_depth_down_2m = size_from_row(row2, "ask_depth_down_5")
    size_imb = imbalance(buy_up_size_2m, buy_down_size_2m)
    sell_size_imb = imbalance(sell_up_size_2m, sell_down_size_2m)
    bid_depth_imb = imbalance(bid_depth_up_2m, bid_depth_down_2m)
    book_pressure_up = imbalance(bid_depth_up_2m, ask_depth_up_2m)
    book_pressure_down = imbalance(bid_depth_down_2m, ask_depth_down_2m)

    mid_open = first2[0].get("mid_up_prob", math.nan)
    mid_2m = row2.get("mid_up_prob", math.nan)
    spread_up = median(float(row.get("spread_up_cents", math.nan)) for row in first2) / 100.0
    spread_down = median(float(row.get("spread_down_cents", math.nan)) for row in first2) / 100.0
    overround = median(float(row.get("mid_overround_cents", math.nan)) for row in first2) / 100.0
    buy_up_size_4m = size_from_row(row4, "buy_up_size")
    buy_down_size_4m = size_from_row(row4, "buy_down_size")

    feature: Dict[str, object] = {
        "run_name": run_name,
        "slug": slug,
        "market_id": f"{run_name}::{slug}",
        "first_quote_ts": first_ts,
        "market_start_ts": market_start,
        "market_close_ts": market_close,
        "outcome_up": outcome_up,
        "quote_count": len(rows),
        "quote_count_first2m": len(first2),
        "session_et": session_et(first_ts),
        "btc_move_1m": move(row1),
        "btc_move_2m": move(row2),
        "btc_move_4m": move(row4),
        "mid_up_prob_open": float(mid_open) if finite(mid_open) else math.nan,
        "mid_up_prob_2m": float(mid_2m) if finite(mid_2m) else math.nan,
        "mid_up_prob_change_2m": float(mid_2m) - float(mid_open) if finite(mid_2m) and finite(mid_open) else math.nan,
        "buy_up_price_1m": price_from_row(row1, "buy_up_cents"),
        "buy_down_price_1m": price_from_row(row1, "buy_down_cents"),
        "buy_up_price_2m": price_from_row(row2, "buy_up_cents"),
        "buy_down_price_2m": price_from_row(row2, "buy_down_cents"),
        "buy_up_price_4m": price_from_row(row4, "buy_up_cents"),
        "buy_down_price_4m": price_from_row(row4, "buy_down_cents"),
        "buy_up_size_1m": size_from_row(row1, "buy_up_size"),
        "buy_down_size_1m": size_from_row(row1, "buy_down_size"),
        "buy_up_size_2m": buy_up_size_2m,
        "buy_down_size_2m": buy_down_size_2m,
        "buy_up_size_4m": buy_up_size_4m,
        "buy_down_size_4m": buy_down_size_4m,
        "sell_up_size_2m": sell_up_size_2m,
        "sell_down_size_2m": sell_down_size_2m,
        "size_imbalance_updown_2m": size_imb,
        "sell_size_imbalance_updown_2m": sell_size_imb,
        "bid_depth_imbalance_updown_2m": bid_depth_imb,
        "book_pressure_up_2m": book_pressure_up,
        "book_pressure_down_2m": book_pressure_down,
        "spread_up_median_first2m": spread_up,
        "spread_down_median_first2m": spread_down,
        "overround_median_first2m": overround,
        "trade_count_sum_first2m": sum(val(row.get("trade_count_1s")) for row in first2),
        "trade_volume_sum_first2m": sum(val(row.get("trade_volume_1s")) for row in first2),
        "realized_vol_first2m": realized_vol,
        "path_efficiency_first2m": path_efficiency,
        "max_drawdown_first2m": max_drawdown_first2m,
        "max_rebound_first2m": max_rebound_first2m,
        "rows": rows,
    }

    spread_up_f = val(spread_up, 1.0)
    spread_down_f = val(spread_down, 1.0)
    overround_f = val(overround, 1.0)
    buy_up_liq_2 = val(buy_up_size_2m)
    buy_up_liq_4 = val(buy_up_size_4m)
    buy_down_liq_2 = val(buy_down_size_2m)
    feature["gate_up"] = spread_up_f <= 0.05 and overround_f <= 0.04 and buy_up_liq_2 >= 120
    feature["gate_down"] = spread_down_f <= 0.05 and overround_f <= 0.04 and buy_down_liq_2 >= 150
    feature["gate_up_strict"] = spread_up_f <= 0.04 and overround_f <= 0.03 and buy_up_liq_2 >= 180
    feature["gate_down_strict"] = spread_down_f <= 0.04 and overround_f <= 0.03 and buy_down_liq_2 >= 180
    feature["triple_gate_up"] = spread_up_f <= 0.05 and overround_f <= 0.04 and buy_up_liq_4 >= 120
    feature["triple_gate_down"] = bool(feature["gate_down"])

    breakout_liq = clamp(buy_up_liq_4 / 250.0, 0.0, 1.0)
    breakout_spread = clamp(1 - spread_up_f / 0.08, 0.0, 1.0)
    breakout_overround = clamp(1 - overround_f / 0.06, 0.0, 1.0)
    breakout_path = clamp(1 - abs(val(feature.get("btc_move_4m")) - 40) / 20.0, 0.0, 1.0)
    feature["quality_breakout"] = (breakout_liq + breakout_spread + breakout_overround + breakout_path) / 4.0

    mild_liq = clamp(buy_down_liq_2 / 250.0, 0.0, 1.0)
    mild_spread = clamp(1 - spread_down_f / 0.08, 0.0, 1.0)
    mild_overround = clamp(1 - overround_f / 0.06, 0.0, 1.0)
    mild_book = clamp((-val(size_imb, 1.0) + (val(book_pressure_down, -1.0) + 1) / 2.0) / 2.0, 0.0, 1.0)
    feature["quality_milddrop"] = (mild_liq + mild_spread + mild_overround + mild_book) / 4.0
    feature["pm_up_crash_without_btc_crash"] = val(feature.get("mid_up_prob_change_2m")) <= -0.10 and val(feature.get("btc_move_2m"), -999) > -25
    feature["pm_up_squeeze_without_btc_squeeze"] = val(feature.get("mid_up_prob_change_2m")) >= 0.10 and val(feature.get("btc_move_2m"), 999) < 25
    return feature


def build_markets(grouped: Dict[Tuple[str, str], List[Dict[str, object]]]) -> List[Dict[str, object]]:
    markets: List[Dict[str, object]] = []
    for (run_name, slug), rows in grouped.items():
        market = build_market(run_name, slug, rows)
        if market is not None:
            markets.append(market)
    markets.sort(key=lambda row: row["first_quote_ts"])  # type: ignore[index]
    return markets


def dedupe_by_slug(markets: List[Dict[str, object]]) -> Tuple[List[Dict[str, object]], int, int]:
    by_slug: Dict[str, Dict[str, object]] = {}
    slug_runs: Dict[str, set] = defaultdict(set)
    for market in markets:
        slug = str(market["slug"])
        slug_runs[slug].add(str(market["run_name"]))
        old = by_slug.get(slug)
        if old is None or market["first_quote_ts"] < old["first_quote_ts"]:  # type: ignore[operator]
            by_slug[slug] = market
    duplicate_slugs = sum(1 for runs in slug_runs.values() if len(runs) > 1)
    max_runs_per_slug = max((len(runs) for runs in slug_runs.values()), default=0)
    return sorted(by_slug.values(), key=lambda row: row["first_quote_ts"]), duplicate_slugs, max_runs_per_slug  # type: ignore[index]


def select_window(markets: List[Dict[str, object]], lookback_days: int) -> Tuple[List[Dict[str, object]], datetime, datetime]:
    if not markets:
        raise RuntimeError("No usable markets")
    end = max(row["first_quote_ts"] for row in markets if isinstance(row.get("first_quote_ts"), datetime))
    start = end - timedelta(days=lookback_days)
    window = [row for row in markets if isinstance(row.get("first_quote_ts"), datetime) and start <= row["first_quote_ts"] <= end]
    return window, start, end


def entry_snapshot(row: Dict[str, object], side: str, minute: int) -> Tuple[float, float, Optional[datetime]]:
    price_key = "buy_up_price" if side == "buy_up" else "buy_down_price"
    size_key = "buy_up_size" if side == "buy_up" else "buy_down_size"
    price = row.get(f"{price_key}_{minute}m", math.nan)
    size = row.get(f"{size_key}_{minute}m", math.nan)
    first_ts = row.get("first_quote_ts")
    rows = row.get("rows")
    entry_ts = None
    if isinstance(first_ts, datetime) and isinstance(rows, list):
        snap = row_after_time(rows, first_ts, minute)
        if snap is not None and isinstance(snap.get("ts_utc"), datetime):
            entry_ts = snap["ts_utc"]  # type: ignore[assignment]
    return float(price) if finite(price) else math.nan, float(size) if finite(size) else math.nan, entry_ts


def settlement_pnl_per_share(row: Dict[str, object], side: str, entry_price: float, fee: float) -> float:
    if side == "buy_up":
        return float(row["outcome_up"]) - entry_price - fee
    return (1.0 - float(row["outcome_up"])) - entry_price - fee


def sell_price_size(path_row: Dict[str, object], side: str) -> Tuple[float, float]:
    if side == "buy_up":
        price = path_row.get("sell_up_cents", math.nan)
        size = path_row.get("sell_up_size", math.nan)
    else:
        price = path_row.get("sell_down_cents", math.nan)
        size = path_row.get("sell_down_size", math.nan)
    return (float(price) / 100.0 if finite(price) else math.nan, float(size) if finite(size) else math.nan)


def simulate_exit(
    row: Dict[str, object],
    side: str,
    shares: float,
    entry_price: float,
    entry_ts: Optional[datetime],
    policy: ExitPolicy,
    fee: float,
) -> Tuple[str, float, Optional[datetime], float, int]:
    if policy.mode == "settle" or entry_ts is None:
        return "settle", settlement_pnl_per_share(row, side, entry_price, fee), None, math.nan, 0

    rows = row.get("rows", [])
    if not isinstance(rows, list):
        return "settle", settlement_pnl_per_share(row, side, entry_price, fee), None, math.nan, 0
    close_ts = row.get("market_close_ts")
    if not isinstance(close_ts, datetime):
        close_ts = entry_ts + timedelta(minutes=5)
    earliest = entry_ts + timedelta(seconds=policy.hold_buffer_seconds)
    latest = close_ts - timedelta(seconds=policy.close_buffer_seconds)
    best_gain = -999.0
    skipped_for_liquidity = 0

    for path_row in rows:
        ts = path_row.get("ts_utc")
        if not isinstance(ts, datetime) or ts < earliest or ts > latest:
            continue
        sell_price, sell_size = sell_price_size(path_row, side)
        if not finite(sell_price) or sell_price <= 0:
            continue
        if policy.require_exit_liquidity and (not finite(sell_size) or sell_size < shares):
            skipped_for_liquidity += 1
            continue
        gain = sell_price - entry_price
        best_gain = max(best_gain, gain)
        if policy.stop_loss is not None and gain <= -policy.stop_loss:
            return "stop", sell_price - entry_price - fee, ts, sell_price, skipped_for_liquidity
        if policy.take_profit is not None and gain >= policy.take_profit:
            return "take_profit", sell_price - entry_price - fee, ts, sell_price, skipped_for_liquidity
        if policy.trail_start is not None and policy.trail_gap is not None and best_gain >= policy.trail_start and gain <= best_gain - policy.trail_gap:
            return "trail", sell_price - entry_price - fee, ts, sell_price, skipped_for_liquidity

    return "settle", settlement_pnl_per_share(row, side, entry_price, fee), None, math.nan, skipped_for_liquidity


def quota_config(spec: StrategySpec) -> Tuple[int, Optional[int], int, int]:
    if spec.mode == "robust":
        max_12h = 6 if spec.entry_name.startswith("combo_guarded") else 8
        return max_12h, None, 8, WINDOW_12H
    if spec.mode == "qrf":
        max_36h = 14 if spec.entry_name == "v1_active_fill_mix" else 10
        cooldown_len = 6 if spec.entry_name == "v1_active_fill_mix" else 8
        return max_36h, None, cooldown_len, WINDOW_36H
    max_36h = 14 if spec.entry_name == "v1_active_fill_mix" else 10
    max_72h = 28 if spec.entry_name == "v1_active_fill_mix" else 18
    cooldown_len = 6 if spec.entry_name == "v1_active_fill_mix" else 8
    return max_36h, max_72h, cooldown_len, WINDOW_36H


def rolling_window_metrics(event_returns: List[float], trade_flags: List[int], window: int) -> Dict[str, object]:
    if len(event_returns) < window:
        return {
            f"worst_{window}_return": math.nan,
            f"median_{window}_return": math.nan,
            f"positive_{window}_rate": math.nan,
            f"active_{window}_rate": math.nan,
            f"num_{window}_windows": 0,
        }
    returns: List[float] = []
    active: List[int] = []
    for start in range(0, len(event_returns) - window + 1):
        wealth = 1.0
        for event_ret in event_returns[start : start + window]:
            wealth *= 1.0 + event_ret
        returns.append(wealth - 1.0)
        active.append(1 if sum(trade_flags[start : start + window]) > 0 else 0)
    return {
        f"worst_{window}_return": min(returns),
        f"median_{window}_return": statistics.median(returns),
        f"positive_{window}_rate": sum(1 for x in returns if x > 0) / len(returns),
        f"active_{window}_rate": sum(active) / len(active),
        f"num_{window}_windows": len(returns),
    }


def simulate_strategy(markets: List[Dict[str, object]], spec: StrategySpec, fee: float) -> Tuple[Dict[str, object], List[Dict[str, object]], List[float]]:
    bankroll = STARTING_BANKROLL
    peak = STARTING_BANKROLL
    max_dd = 0.0
    loss_streak = 0
    cooldown = 0
    recent_primary: List[int] = []
    recent_72: List[int] = []
    logs: List[Dict[str, object]] = []
    curve: List[float] = []
    event_returns = [0.0 for _ in markets]
    trade_flags = [0 for _ in markets]
    max_primary, max_72, cooldown_len, primary_window = quota_config(spec)

    for idx, row in enumerate(markets):
        recent_primary = [x for x in recent_primary if x > idx - primary_window]
        recent_72 = [x for x in recent_72 if x > idx - WINDOW_72H]
        if cooldown > 0:
            cooldown -= 1
            curve.append(bankroll)
            continue
        if len(recent_primary) >= max_primary or (max_72 is not None and len(recent_72) >= max_72):
            curve.append(bankroll)
            continue

        side, minute, fraction, component = choose_trade(row, spec.entry_name)
        if side == "skip":
            curve.append(bankroll)
            continue
        entry_price, size_avail, entry_ts = entry_snapshot(row, side, minute)
        if not finite(entry_price) or not finite(size_avail) or entry_price <= 0:
            curve.append(bankroll)
            continue

        bankroll_before = bankroll
        target_cost = min(bankroll * fraction, bankroll, size_avail * entry_price)
        if target_cost <= 0:
            curve.append(bankroll)
            continue
        shares = target_cost / entry_price
        exit_reason, pnl_per_share, exit_ts, exit_price, skipped_exit_liq = simulate_exit(row, side, shares, entry_price, entry_ts, spec.exit_policy, fee)
        pnl = shares * pnl_per_share
        bankroll += pnl
        event_ret = pnl / bankroll_before if bankroll_before > 0 else 0.0
        event_returns[idx] = event_ret
        trade_flags[idx] = 1
        recent_primary.append(idx)
        recent_72.append(idx)
        peak = max(peak, bankroll)
        max_dd = max(max_dd, 0.0 if peak <= 0 else (peak - bankroll) / peak)
        if pnl < 0:
            loss_streak += 1
            if loss_streak >= 2:
                cooldown = cooldown_len
                loss_streak = 0
        else:
            loss_streak = 0
        curve.append(bankroll)
        logs.append(
            {
                "strategy": spec.name,
                "entry_name": spec.entry_name,
                "family": spec.family,
                "exit_policy": spec.exit_policy.name,
                "first_quote_ts": row["first_quote_ts"].isoformat() if isinstance(row.get("first_quote_ts"), datetime) else "",
                "first_quote_cst": to_tz_text(row.get("first_quote_ts") if isinstance(row.get("first_quote_ts"), datetime) else None, SH_TZ),
                "run_name": row["run_name"],
                "slug": row["slug"],
                "market_id": row["market_id"],
                "session_et": row.get("session_et", ""),
                "component": component,
                "side": side,
                "entry_minute": minute,
                "entry_ts": entry_ts.isoformat() if entry_ts else "",
                "exit_ts": exit_ts.isoformat() if exit_ts else "",
                "exit_reason": exit_reason,
                "fraction": fraction,
                "target_cost": target_cost,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_usd": pnl,
                "event_ret": event_ret,
                "bankroll_after": bankroll,
                "skipped_exit_liquidity_quotes": skipped_exit_liq,
            }
        )

    pnl_values = [float(log["pnl_usd"]) for log in logs]
    wins = sum(x for x in pnl_values if x > 0)
    losses = sum(x for x in pnl_values if x < 0)
    cost_values = [float(log["target_cost"]) for log in logs if finite(log.get("target_cost")) and float(log["target_cost"]) > 0]
    ending = bankroll
    metrics: Dict[str, object] = {
        "strategy": spec.name,
        "entry_name": spec.entry_name,
        "family": spec.family,
        "prior_label": prior_label(spec.entry_name),
        "mode": spec.mode,
        "exit_policy": spec.exit_policy.name,
        "exit_mode": spec.exit_policy.mode,
        "trades": len(logs),
        "ending_bankroll": ending,
        "total_return": ending / STARTING_BANKROLL - 1.0,
        "win_rate": sum(1 for x in pnl_values if x > 0) / len(pnl_values) if pnl_values else math.nan,
        "profit_factor": wins / abs(losses) if losses < 0 else math.nan,
        "max_drawdown": max_dd,
        "avg_cost": statistics.mean(cost_values) if cost_values else math.nan,
        "first_trade_ts": logs[0]["first_quote_ts"] if logs else "",
        "last_trade_ts": logs[-1]["first_quote_ts"] if logs else "",
        "take_profit_exits": sum(1 for log in logs if log["exit_reason"] == "take_profit"),
        "stop_exits": sum(1 for log in logs if log["exit_reason"] == "stop"),
        "trail_exits": sum(1 for log in logs if log["exit_reason"] == "trail"),
        "settle_exits": sum(1 for log in logs if log["exit_reason"] == "settle"),
    }
    metrics.update(rolling_window_metrics(event_returns, trade_flags, WINDOW_12H))
    metrics.update(rolling_window_metrics(event_returns, trade_flags, WINDOW_36H))
    metrics.update(rolling_window_metrics(event_returns, trade_flags, WINDOW_72H))
    return metrics, logs, curve


def score_results(results: List[Dict[str, object]]) -> None:
    def rank_percent(values: List[Tuple[int, float]], reverse: bool = False) -> Dict[int, float]:
        values = [(idx, value) for idx, value in values if finite(value)]
        if not values:
            return {}
        values.sort(key=lambda item: item[1], reverse=reverse)
        n = len(values)
        return {idx: (rank + 1) / n for rank, (idx, _) in enumerate(values)}

    end_rank = rank_percent([(i, float(row["ending_bankroll"])) for i, row in enumerate(results)], reverse=False)
    dd_rank = rank_percent([(i, -float(row["max_drawdown"])) for i, row in enumerate(results)], reverse=False)
    pf_rank = rank_percent([(i, float(row["profit_factor"])) for i, row in enumerate(results) if finite(row.get("profit_factor"))], reverse=False)
    w36_rank = rank_percent([(i, float(row.get(f"worst_{WINDOW_36H}_return", math.nan))) for i, row in enumerate(results)], reverse=False)
    active_rank = rank_percent([(i, float(row.get(f"active_{WINDOW_36H}_rate", math.nan))) for i, row in enumerate(results)], reverse=False)
    for idx, row in enumerate(results):
        trades = int(row.get("trades", 0))
        score_trades = 1.0 if trades >= 10 else (0.3 if trades > 0 else 0.0)
        row["research_score"] = (
            0.30 * end_rank.get(idx, 0.0)
            + 0.20 * dd_rank.get(idx, 0.0)
            + 0.18 * w36_rank.get(idx, 0.0)
            + 0.14 * pf_rank.get(idx, 0.0)
            + 0.10 * active_rank.get(idx, 0.0)
            + 0.08 * score_trades
        )


def xml_text(elem: ET.Element) -> str:
    return "".join(elem.itertext())


def read_xlsx_inventory(path: Path, preview_rows: int = 5) -> Dict[str, object]:
    inventory: Dict[str, object] = {"file": str(path), "name": path.name, "size_bytes": path.stat().st_size, "sheets": []}
    try:
        with zipfile.ZipFile(path) as zf:
            ns = {
                "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
                "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
            }
            shared: List[str] = []
            if "xl/sharedStrings.xml" in zf.namelist():
                root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                for si in root.findall(".//main:si", ns):
                    shared.append(xml_text(si))
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            rel_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rels = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rel_root.findall(".//pkgrel:Relationship", ns)}
            for sheet in wb.findall(".//main:sheet", ns):
                name = sheet.attrib.get("name", "")
                rid = sheet.attrib.get(f"{{{ns['rel']}}}id", "")
                target = rels.get(rid, "")
                sheet_path = "xl/" + target.lstrip("/")
                if sheet_path not in zf.namelist():
                    continue
                sheet_root = ET.fromstring(zf.read(sheet_path))
                rows_preview: List[List[str]] = []
                row_count = 0
                nonempty_cells = 0
                for row in sheet_root.findall(".//main:row", ns):
                    row_count += 1
                    values: List[str] = []
                    for cell in row.findall("main:c", ns):
                        text = ""
                        cell_type = cell.attrib.get("t", "")
                        if cell_type == "s":
                            v = cell.find("main:v", ns)
                            if v is not None and v.text and v.text.isdigit():
                                idx = int(v.text)
                                text = shared[idx] if idx < len(shared) else ""
                        elif cell_type == "inlineStr":
                            is_elem = cell.find("main:is", ns)
                            text = xml_text(is_elem) if is_elem is not None else ""
                        else:
                            v = cell.find("main:v", ns)
                            text = v.text if v is not None and v.text else ""
                        if text:
                            nonempty_cells += 1
                        values.append(text)
                    if len(rows_preview) < preview_rows and any(v for v in values):
                        rows_preview.append(values[:10])
                inventory["sheets"].append({"name": name, "row_count": row_count, "nonempty_cells": nonempty_cells, "preview": rows_preview})  # type: ignore[index]
    except Exception as exc:
        inventory["error"] = str(exc)
    return inventory


def build_docs_inventory(docs_dir: Path) -> List[Dict[str, object]]:
    inventory: List[Dict[str, object]] = []
    for path in sorted(docs_dir.glob("*.xlsx")):
        inventory.append(read_xlsx_inventory(path))
    for path in sorted(docs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        headings = [line.strip("# ").strip() for line in text.splitlines() if line.startswith("#")]
        inventory.append({"file": str(path), "name": path.name, "size_bytes": path.stat().st_size, "type": "markdown", "headings": headings[:20]})
    return inventory


def csv_safe(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


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
            writer.writerow({key: csv_safe(row.get(key, "")) for key in fieldnames})


def json_ready(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, dict):
        return {key: json_ready(val_) for key, val_ in value.items() if key != "rows"}
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    return value


def format_money(value: object) -> str:
    return "" if not finite(value) else f"${float(value):,.2f}"


def format_pct(value: object) -> str:
    return "" if not finite(value) else f"{float(value):.2%}"


def make_html(report: Dict[str, object], results: List[Dict[str, object]], trade_samples: List[Dict[str, object]], docs_inventory: List[Dict[str, object]]) -> str:
    top = sorted(results, key=lambda row: float(row.get("ending_bankroll", 0.0)), reverse=True)[:20]
    prior = sorted(
        [row for row in results if row.get("prior_label")],
        key=lambda row: (str(row.get("entry_name", "")), float(row.get("ending_bankroll", 0.0))),
        reverse=True,
    )
    payload = {
        "report": report,
        "results": results,
        "tradeSamples": trade_samples,
        "docs": docs_inventory,
    }
    payload_json = json.dumps(json_ready(payload), ensure_ascii=False)
    top_rows = "\n".join(
        f"<tr><td>{html.escape(str(row['strategy']))}</td><td>{html.escape(str(row['family']))}</td><td>{html.escape(str(row['exit_policy']))}</td><td>{int(row['trades'])}</td><td>{format_money(row['ending_bankroll'])}</td><td>{float(row['total_return']):.2%}</td><td>{float(row['max_drawdown']):.2%}</td></tr>"
        for row in top
    )
    prior_rows = "\n".join(
        f"<tr><td>{html.escape(str(row['entry_name']))}</td><td>{html.escape(str(row['prior_label']))}</td><td>{html.escape(str(row['exit_policy']))}</td><td>{int(row['trades'])}</td><td>{format_money(row['ending_bankroll'])}</td><td>{format_pct(row['total_return'])}</td><td>{format_pct(row['max_drawdown'])}</td><td>{format_pct(row['win_rate'])}</td><td>{format_pct(row.get(f'worst_{WINDOW_36H}_return'))}</td></tr>"
        for row in prior
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Polymarket 5m Strategy Lab</title>
  <style>
    :root {{
      --bg: #f7f7f4;
      --panel: #ffffff;
      --ink: #202124;
      --muted: #666b73;
      --line: #d9d7cf;
      --accent: #0f766e;
      --bad: #b42318;
      --good: #127a3a;
      --chip: #eef3f1;
    }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    header {{ padding: 18px 22px 10px; border-bottom: 1px solid var(--line); background: #fbfbf8; position: sticky; top: 0; z-index: 5; }}
    h1 {{ margin: 0 0 8px; font-size: 20px; letter-spacing: 0; }}
    .meta {{ display: flex; gap: 10px; flex-wrap: wrap; color: var(--muted); }}
    .chip {{ background: var(--chip); border: 1px solid var(--line); border-radius: 6px; padding: 4px 8px; }}
    main {{ padding: 18px 22px 28px; }}
    .grid {{ display: grid; grid-template-columns: repeat(4, minmax(160px, 1fr)); gap: 10px; margin-bottom: 14px; }}
    .metric {{ background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; min-height: 72px; }}
    .metric .label {{ color: var(--muted); font-size: 12px; }}
    .metric .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
    .controls {{ display: grid; grid-template-columns: 1.4fr repeat(6, minmax(120px, 1fr)); gap: 8px; margin: 14px 0; }}
    input, select {{ width: 100%; border: 1px solid var(--line); border-radius: 6px; padding: 8px 9px; background: #fff; color: var(--ink); }}
    table {{ width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--line); }}
    th, td {{ padding: 8px 9px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; white-space: nowrap; }}
    th {{ position: sticky; top: 78px; z-index: 3; background: #efeee8; cursor: pointer; font-size: 12px; color: #3f4348; }}
    tr:hover td {{ background: #f5faf8; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .good {{ color: var(--good); font-weight: 650; }}
    .bad {{ color: var(--bad); font-weight: 650; }}
    .bar {{ height: 7px; background: #e7e3d8; border-radius: 4px; overflow: hidden; min-width: 70px; }}
    .bar > span {{ display: block; height: 100%; background: var(--accent); }}
    details {{ margin: 16px 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; }}
    summary {{ cursor: pointer; font-weight: 700; }}
    .two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }}
    .small {{ color: var(--muted); font-size: 12px; }}
    .scroll {{ overflow-x: auto; }}
    @media (max-width: 1000px) {{
      .grid {{ grid-template-columns: repeat(2, minmax(150px, 1fr)); }}
      .controls {{ grid-template-columns: 1fr 1fr; }}
      .two {{ grid-template-columns: 1fr; }}
      th {{ top: 118px; }}
    }}
  </style>
</head>
<body>
<header>
  <h1>Polymarket 5m Strategy Lab</h1>
  <div class="meta">
    <span class="chip">窗口：{html.escape(str(report.get("window_start_cst", "")))} - {html.escape(str(report.get("window_end_cst", "")))}</span>
    <span class="chip">事件：{report.get("window_events", "")}</span>
    <span class="chip">策略变体：{len(results)}</span>
    <span class="chip">起始本金：$100</span>
  </div>
</header>
<main>
  <section class="grid" id="cards"></section>
  <section class="controls">
    <input id="q" placeholder="搜索策略 / family / exit policy">
    <select id="family"><option value="">全部 family</option></select>
    <select id="exit"><option value="">全部 exit</option></select>
    <select id="minTrades"><option value="0">不限交易数</option><option value="10">至少 10 笔</option><option value="30">至少 30 笔</option><option value="100">至少 100 笔</option></select>
    <select id="maxDd"><option value="1">不限回撤</option><option value="0.3">回撤 <= 30%</option><option value="0.5">回撤 <= 50%</option></select>
    <select id="tag"><option value="">全部标签</option><option value="prior">历史候选</option></select>
    <select id="sort"><option value="ending_bankroll">按最终本金</option><option value="research_score">按综合分</option><option value="max_drawdown">按低回撤</option><option value="profit_factor">按 profit factor</option><option value="trades">按交易数</option><option value="total_return">按收益</option><option value="win_rate">按胜率</option><option value="worst_{WINDOW_36H}_return">按最差36h</option></select>
  </section>

  <div class="scroll"><table id="resultTable">
    <thead><tr>
      <th data-key="strategy">策略</th><th data-key="family">family</th><th data-key="exit_policy">exit</th>
      <th class="num" data-key="trades">交易</th><th class="num" data-key="ending_bankroll">最终本金</th>
      <th class="num" data-key="total_return">收益</th><th class="num" data-key="max_drawdown">回撤</th>
      <th class="num" data-key="win_rate">胜率</th><th class="num" data-key="profit_factor">PF</th><th class="num" data-key="research_score">分数</th>
    </tr></thead>
    <tbody></tbody>
  </table></div>

  <details open>
    <summary>Top 20 快照</summary>
    <div class="scroll"><table><thead><tr><th>策略</th><th>family</th><th>exit</th><th>交易</th><th>最终本金</th><th>收益</th><th>最大回撤</th></tr></thead><tbody>{top_rows}</tbody></table></div>
  </details>

  <details open>
    <summary>历史 mixed / validation 候选复测</summary>
    <div class="small">这些是之前报告里真正出现过的候选：v1_active_fill_mix、v1_balanced/adaptive/conservative_mix，以及 validation 选出的 classic_breakout_up。每条同时比较 settle 和 3-5 秒 buffer 的日内退出版本。</div>
    <div class="scroll"><table><thead><tr><th>entry</th><th>标签</th><th>exit</th><th>交易</th><th>最终本金</th><th>收益</th><th>回撤</th><th>胜率</th><th>最差36h</th></tr></thead><tbody>{prior_rows}</tbody></table></div>
  </details>

  <section class="two">
    <details>
      <summary>交易样本</summary>
      <div class="small">显示当前筛选结果里排名最高策略的最近交易样本。</div>
      <table id="tradeTable"><thead><tr><th>策略</th><th>时间(CST)</th><th>session</th><th>方向</th><th>退出</th><th class="num">入场</th><th class="num">出场</th><th class="num">PnL</th><th class="num">本金</th></tr></thead><tbody></tbody></table>
    </details>
    <details>
      <summary>docs 策略框架索引</summary>
      <div id="docsBox" class="small"></div>
    </details>
  </section>
</main>
<script id="payload" type="application/json">{payload_json}</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const results = data.results;
let sortKey = 'ending_bankroll';
let sortDir = -1;
const fmtMoney = v => v == null ? '' : '$' + Number(v).toFixed(2);
const fmtPct = v => v == null ? '' : (Number(v) * 100).toFixed(2) + '%';
const fmtNum = v => v == null ? '' : Number(v).toFixed(3);
function fillSelect(id, values) {{
  const el = document.getElementById(id);
  values.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; el.appendChild(o); }});
}}
fillSelect('family', [...new Set(results.map(r => r.family))].sort());
fillSelect('exit', [...new Set(results.map(r => r.exit_policy))].sort());
function filtered() {{
  const q = document.getElementById('q').value.toLowerCase();
  const fam = document.getElementById('family').value;
  const ex = document.getElementById('exit').value;
  const minT = Number(document.getElementById('minTrades').value);
  const maxDd = Number(document.getElementById('maxDd').value);
  const tag = document.getElementById('tag').value;
  return results.filter(r => {{
    const hay = (r.strategy + ' ' + r.family + ' ' + r.exit_policy).toLowerCase();
    return (!q || hay.includes(q)) && (!fam || r.family === fam) && (!ex || r.exit_policy === ex) && (!tag || r.prior_label) && r.trades >= minT && r.max_drawdown <= maxDd;
  }}).sort((a,b) => {{
    const rawA = a[sortKey], rawB = b[sortKey];
    const av = Number(rawA), bv = Number(rawB);
    if (Number.isFinite(av) && Number.isFinite(bv)) {{
      if (sortKey === 'max_drawdown') return (av - bv) * (sortDir === -1 ? 1 : -1);
      return (av - bv) * sortDir;
    }}
    return String(rawA || '').localeCompare(String(rawB || '')) * sortDir;
  }});
}}
function render(readSortControl = true) {{
  if (readSortControl) sortKey = document.getElementById('sort').value || sortKey;
  const rows = filtered();
  const tbody = document.querySelector('#resultTable tbody');
  tbody.innerHTML = rows.slice(0, 300).map(r => {{
    const retClass = r.total_return >= 0 ? 'good' : 'bad';
    const ddw = Math.min(100, Math.round(Number(r.max_drawdown || 0) * 100));
    return `<tr><td>${{r.strategy}}</td><td>${{r.family}}</td><td>${{r.exit_policy}}</td><td class="num">${{r.trades}}</td><td class="num ${{retClass}}">${{fmtMoney(r.ending_bankroll)}}</td><td class="num ${{retClass}}">${{fmtPct(r.total_return)}}</td><td class="num"><div class="bar"><span style="width:${{ddw}}%"></span></div>${{fmtPct(r.max_drawdown)}}</td><td class="num">${{fmtPct(r.win_rate)}}</td><td class="num">${{fmtNum(r.profit_factor)}}</td><td class="num">${{fmtNum(r.research_score)}}</td></tr>`;
  }}).join('');
  const best = rows[0];
  const cards = [
    ['筛选后策略数', rows.length],
    ['最佳最终本金', best ? fmtMoney(best.ending_bankroll) : ''],
    ['最佳策略收益', best ? fmtPct(best.total_return) : ''],
    ['最佳策略回撤', best ? fmtPct(best.max_drawdown) : '']
  ];
  document.getElementById('cards').innerHTML = cards.map(c => `<div class="metric"><div class="label">${{c[0]}}</div><div class="value">${{c[1]}}</div></div>`).join('');
  renderTrades(best ? best.strategy : '');
}}
function renderTrades(strategy) {{
  const rows = data.tradeSamples.filter(t => t.strategy === strategy).slice(-80).reverse();
  document.querySelector('#tradeTable tbody').innerHTML = rows.map(t => `<tr><td>${{t.entry_name}}</td><td>${{t.first_quote_cst || t.first_quote_ts}}</td><td>${{t.session_et}}</td><td>${{t.side}}</td><td>${{t.exit_reason}}</td><td class="num">${{fmtNum(t.entry_price)}}</td><td class="num">${{fmtNum(t.exit_price)}}</td><td class="num ${{Number(t.pnl_usd) >= 0 ? 'good':'bad'}}">${{Number(t.pnl_usd).toFixed(2)}}</td><td class="num">${{fmtMoney(t.bankroll_after)}}</td></tr>`).join('');
}}
function renderDocs() {{
  document.getElementById('docsBox').innerHTML = data.docs.map(d => {{
    const sheets = d.sheets ? d.sheets.map(s => `<li>${{s.name}}: ${{s.row_count}} rows, preview ${{(s.preview || []).map(r => r.join(' | ')).slice(0,2).join(' / ')}}</li>`).join('') : '';
    const headings = d.headings ? d.headings.join(' / ') : '';
    return `<p><b>${{d.name}}</b><br>${{headings}}<ul>${{sheets}}</ul></p>`;
  }}).join('');
}}
document.querySelectorAll('input,select').forEach(el => el.addEventListener('input', () => render(true)));
document.querySelectorAll('th[data-key]').forEach(th => th.addEventListener('click', () => {{
  sortKey = th.dataset.key;
  sortDir *= -1;
  const sort = document.getElementById('sort');
  if ([...sort.options].some(o => o.value === sortKey)) sort.value = sortKey;
  render(false);
}}));
renderDocs();
render();
</script>
</body>
</html>"""


def compact_result(row: Dict[str, object]) -> str:
    pf = "" if not finite(row.get("profit_factor")) else f"{float(row['profit_factor']):.3f}"
    return (
        f"`{row.get('strategy', '')}`: {format_money(row.get('ending_bankroll'))}, "
        f"return {format_pct(row.get('total_return'))}, trades {int(row.get('trades', 0))}, "
        f"drawdown {format_pct(row.get('max_drawdown'))}, PF {pf}"
    )


def build_strategy_brief(report: Dict[str, object], results: List[Dict[str, object]], prior_rows: List[Dict[str, object]]) -> str:
    top_score = results[0] if results else {}
    top_ending = max(results, key=lambda row: float(row.get("ending_bankroll", 0.0))) if results else {}
    intraday = [row for row in results if row.get("exit_mode") == "intraday"]
    top_intraday = max(intraday, key=lambda row: float(row.get("ending_bankroll", 0.0))) if intraday else {}
    top_intraday_score = max(intraday, key=lambda row: float(row.get("research_score", 0.0))) if intraday else {}

    best_prior_by_entry: List[Dict[str, object]] = []
    for entry in sorted({str(row.get("entry_name", "")) for row in prior_rows}):
        rows = [row for row in prior_rows if row.get("entry_name") == entry]
        if rows:
            best_prior_by_entry.append(max(rows, key=lambda row: float(row.get("ending_bankroll", 0.0))))

    lines = [
        "# Polymarket 5m systematic intraday strategy brief",
        "",
        f"- Data window: {report.get('window_start_cst')} to {report.get('window_end_cst')}",
        f"- Events: {report.get('window_events')} usable 5m markets from {report.get('raw_files')} CSV files",
        f"- Strategy variants: {report.get('strategy_variants')}",
        f"- Starting bankroll: {format_money(report.get('starting_bankroll'))}",
        f"- Fee model: ${float(report.get('fee', 0.0)):.2f} per share round-trip adjustment in this simulation",
        "",
        "## Current winners",
        "",
        f"- Best research score: {compact_result(top_score)}" if top_score else "- Best research score: n/a",
        f"- Highest ending bankroll: {compact_result(top_ending)}" if top_ending else "- Highest ending bankroll: n/a",
        f"- Highest intraday ending bankroll: {compact_result(top_intraday)}" if top_intraday else "- Highest intraday ending bankroll: n/a",
        f"- Best intraday risk-adjusted score: {compact_result(top_intraday_score)}" if top_intraday_score else "- Best intraday risk-adjusted score: n/a",
        "",
        "## Prior selected strategy rerun",
        "",
    ]
    for row in best_prior_by_entry:
        lines.append(f"- {compact_result(row)}; prior label `{row.get('prior_label', '')}`")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- The old mixed candidates are no longer competitive on the latest 30-day window under this execution model.",
            "- Intraday sell exits with 3-5 second buffers reduce drawdown on many setups, but they do not automatically improve every old mixed strategy.",
            "- Current best candidates are session-specific London breakout variants; the raw-return winner still has materially higher drawdown.",
            "",
            "## Execution caveats",
            "",
            "- This is still a replay backtest, not a live order-placement simulator.",
            "- Entry and exit use top-of-book price and size checks from captured quotes.",
            "- The 3-5 second buffers avoid end-of-window micro-timing, but failed orders, queue position, partial fills, and API latency are not fully modeled.",
        ]
    )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> Dict[str, object]:
    source_root = Path(args.source_root).expanduser().resolve()
    docs_dir = Path(args.docs_dir).expanduser().resolve()
    report_dir = Path(args.report_dir).expanduser().resolve()
    report_dir.mkdir(parents=True, exist_ok=True)

    grouped, coverage, raw_stats = read_monthly_runs(source_root)
    markets_all = build_markets(grouped)
    window_run_level, start, end = select_window(markets_all, args.lookback_days)
    markets, duplicate_slugs, max_runs_per_slug = dedupe_by_slug(window_run_level)
    docs_inventory = build_docs_inventory(docs_dir)
    specs = build_strategy_specs()

    results: List[Dict[str, object]] = []
    logs_by_strategy: Dict[str, List[Dict[str, object]]] = {}
    curves_by_strategy: Dict[str, List[float]] = {}
    for spec in specs:
        metrics, logs, curve = simulate_strategy(markets, spec, args.fee)
        results.append(metrics)
        logs_by_strategy[spec.name] = logs
        curves_by_strategy[spec.name] = curve[-300:]
    score_results(results)
    results.sort(key=lambda row: (float(row.get("research_score", 0.0)), float(row.get("ending_bankroll", 0.0))), reverse=True)

    top_by_ending = sorted(results, key=lambda row: float(row.get("ending_bankroll", 0.0)), reverse=True)
    prior_rows = sorted(
        [row for row in results if row.get("prior_label")],
        key=lambda row: (str(row.get("entry_name", "")), float(row.get("ending_bankroll", 0.0))),
        reverse=True,
    )
    keep_strategies = {row["strategy"] for row in top_by_ending[:25]} | {row["strategy"] for row in results[:25]} | {row["strategy"] for row in prior_rows}
    trade_samples: List[Dict[str, object]] = []
    for strategy in keep_strategies:
        trade_samples.extend(logs_by_strategy.get(str(strategy), [])[-120:])

    report = {
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
        "window_start_cst": to_tz_text(start, SH_TZ),
        "window_end_cst": to_tz_text(end, SH_TZ),
        "strategy_variants": len(results),
        "fee": args.fee,
        "starting_bankroll": STARTING_BANKROLL,
    }

    write_csv(report_dir / "strategy_results.csv", results)
    write_csv(report_dir / "trade_samples_top.csv", trade_samples)
    write_csv(report_dir / "prior_selected_comparison.csv", prior_rows)
    write_csv(report_dir / "coverage.csv", coverage)
    (report_dir / "docs_inventory.json").write_text(json.dumps(json_ready(docs_inventory), indent=2, ensure_ascii=False), encoding="utf-8")
    (report_dir / "manifest.json").write_text(json.dumps(json_ready(report), indent=2, ensure_ascii=False), encoding="utf-8")
    (report_dir / "strategy_brief.md").write_text(build_strategy_brief(report, results, prior_rows), encoding="utf-8")
    (report_dir / "index.html").write_text(make_html(report, results, trade_samples, docs_inventory), encoding="utf-8")
    return {"report": report, "top_by_score": results[:10], "top_by_ending": top_by_ending[:10], "report_dir": str(report_dir)}


def default_paths() -> Tuple[Path, Path, Path]:
    repo = Path(__file__).resolve().parents[1]
    code_root = repo.parent
    return code_root / "poly" / "data" / "monthly_runs", repo / "docs", repo / "reports" / "systematic_intraday_tool"


def main() -> None:
    default_source, default_docs, default_report = default_paths()
    parser = argparse.ArgumentParser(description="Build a systematic Polymarket 5m strategy HTML tool with intraday exit policies.")
    parser.add_argument("--source-root", default=str(default_source))
    parser.add_argument("--docs-dir", default=str(default_docs))
    parser.add_argument("--report-dir", default=str(default_report))
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--fee", type=float, default=FEE_DEFAULT)
    args = parser.parse_args()
    output = run(args)
    print(json.dumps(json_ready(output), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
