#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import pickle
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_execution
import robust_trading_system_tool as robust
import systematic_intraday_strategy_tool as base


GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"
SEED_MARKET_URL = "https://polymarket.com/zh/event/btc-updown-5m-1776752100"
BJ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc
MODEL_CACHE_VERSION = 1
DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[1] / "reports" / "live_paper_trading"

SNAPSHOT_FIELDS = [
    "ts_iso",
    "slug",
    "market_url",
    "window_text",
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
    "level_count_bid_up",
    "level_count_ask_up",
    "level_count_bid_down",
    "level_count_ask_down",
    "min_order_size_up",
    "min_order_size_down",
    "tick_size_up",
    "tick_size_down",
    "target_price",
    "final_price",
    "trade_count_1s",
    "trade_volume_1s",
]

EVENT_FIELDS = [
    "event_ts",
    "event_type",
    "order_id",
    "slug",
    "window_text",
    "side",
    "status",
    "entry_second",
    "q_model",
    "edge",
    "signal_price",
    "paper_price",
    "price_slippage",
    "stake_fraction",
    "intended_cost",
    "fill_cost",
    "shares",
    "fill_ratio",
    "pnl_usd",
    "bankroll_after",
    "target_price",
    "final_price",
    "delay_seconds",
    "max_adverse_slippage",
    "reason",
    "market_url",
    "execution_mode",
    "token_id",
    "order_type",
    "live_order_local_id",
    "live_order_ok",
    "live_order_id",
    "live_order_status",
    "live_order_error",
    "live_trade_ids",
    "live_transactions",
    "live_amount_usd",
    "live_max_spend_usd",
    "live_max_price",
    "live_estimated_shares",
    "live_min_order_size",
    "live_making_amount",
    "live_taking_amount",
    "executor_latency_ms",
]


class LivePaperError(RuntimeError):
    pass


@dataclass
class MarketInfo:
    slug: str
    market_url: str
    up_token_id: str
    down_token_id: str
    window_text: str
    target_price: float
    asset_key: str


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not math.isnan(float(value)) and not math.isinf(float(value))


def fnum(value: object, default: float = math.nan) -> float:
    try:
        x = float(value)
    except Exception:
        return default
    return x if math.isfinite(x) else default


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


def slug_from_market_url(url: str) -> str:
    match = re.search(r"/event/([^/?#]+)", normalize_url(url))
    if not match:
        raise LivePaperError(f"Cannot parse market slug from URL: {url}")
    return match.group(1)


def series_prefix_from_slug(slug: str) -> str:
    match = re.match(r"(.+)-\d{10}$", slug)
    if not match:
        raise LivePaperError(f"Slug does not end with epoch seconds: {slug}")
    return match.group(1)


def infer_asset_key(series_prefix: str) -> str:
    return series_prefix.split("-", 1)[0].lower()


def build_market_url(template_url: str, slug: str) -> str:
    return re.sub(r"/event/[^/?#]+", f"/event/{slug}", normalize_url(template_url))


def floor_to_5m(dt: datetime) -> datetime:
    floored = dt.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=floored.minute % 5)


def current_window_slug(series_prefix: str, now_bj: Optional[datetime] = None) -> Tuple[str, datetime, datetime]:
    now_bj = now_bj or datetime.now(BJ)
    start_bj = floor_to_5m(now_bj)
    end_bj = start_bj + timedelta(minutes=5)
    return f"{series_prefix}-{int(start_bj.astimezone(UTC).timestamp())}", start_bj, end_bj


def request_json(url: str, timeout: float, retries: int = 2) -> Any:
    if requests is None:
        raise LivePaperError("requests is not installed")
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    last_error: Optional[Exception] = None
    for attempt in range(max(1, retries + 1)):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    if last_error is not None:
        raise last_error
    raise LivePaperError(f"request failed: {url}")


def try_json_loads(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if (text.startswith("[") and text.endswith("]")) or (text.startswith("{") and text.endswith("}")):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return value
    return value


def normalize_price(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    text = str(value).strip().replace(",", "")
    if not text:
        return ""
    try:
        return f"{float(text):.2f}"
    except Exception:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
        return f"{float(match.group(1)):.2f}" if match else ""


def fetch_live_reference_price(asset_key: str, timeout: float) -> str:
    if asset_key != "btc":
        return ""
    urls = [
        ("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", "price"),
        ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", "price"),
    ]
    for url, key in urls:
        try:
            price = normalize_price(request_json(url, timeout).get(key))
            if price:
                return price
        except Exception:
            continue
    return ""


def round_half(value: float) -> float:
    return round(value * 2.0) / 2.0


def fetch_window_start_price(asset_key: str, start_bj: datetime, timeout: float) -> str:
    if asset_key != "btc":
        return ""
    start_ms = int(start_bj.astimezone(UTC).timestamp() * 1000)
    end_ms = start_ms + 60_000
    try:
        payload = request_json(
            f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={start_ms}&endTime={end_ms}&limit=1",
            timeout,
        )
        if isinstance(payload, list) and payload:
            price = normalize_price(payload[0][1])
            return f"{round_half(float(price)):.2f}" if price else ""
    except Exception:
        return ""
    return ""


def iter_nodes(obj: Any) -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key), value
            yield from iter_nodes(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_nodes(item)


def extract_target_from_text(raw: Dict[str, Any]) -> str:
    patterns = [
        r"above\s+or\s+below[^0-9$]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"高于或低于[^0-9$¥￥]*[$¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"above[^0-9$]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
        r"below[^0-9$]*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    ]
    for key, value in iter_nodes(raw):
        if key.lower().replace("-", "_") not in {"question", "title", "description", "subtitle", "resolutioncriteria", "resolution_criteria", "rules"}:
            continue
        if not isinstance(value, str):
            continue
        for pattern in patterns:
            match = re.search(pattern, value, flags=re.IGNORECASE)
            if match:
                return normalize_price(match.group(1))
    return ""


def fetch_market_info(slug: str, template_url: str, asset_key: str, timeout: float) -> MarketInfo:
    errors: List[str] = []
    market: Any = None
    for url in (f"{GAMMA_BASE}/markets?slug={slug}", f"{GAMMA_BASE}/markets/slug/{slug}"):
        try:
            payload = request_json(url, timeout)
            market = payload[0] if isinstance(payload, list) and payload else payload
            if isinstance(market, dict):
                break
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    if not isinstance(market, dict):
        raise LivePaperError(f"No market found for slug {slug}; errors={errors}")
    outcomes = try_json_loads(market.get("outcomes")) or []
    token_ids = try_json_loads(market.get("clobTokenIds")) or []
    mapping: Dict[str, str] = {}
    for outcome, token_id in zip(outcomes, token_ids):
        if outcome is not None and token_id is not None:
            mapping[str(outcome).strip().lower()] = str(token_id)
    up_token = mapping.get("up") or mapping.get("yes")
    down_token = mapping.get("down") or mapping.get("no")
    if not up_token or not down_token:
        raise LivePaperError(f"Could not map Up/Down token IDs for {slug}: {outcomes!r}")
    match = re.search(r"-(\d{10})$", slug)
    if not match:
        raise LivePaperError(f"Cannot parse epoch from slug {slug}")
    start_bj = datetime.fromtimestamp(int(match.group(1)), UTC).astimezone(BJ)
    target_text = fetch_window_start_price(asset_key, start_bj, timeout) or extract_target_from_text(market)
    if not target_text:
        live_price = fetch_live_reference_price(asset_key, timeout)
        target_text = live_price
    if not target_text:
        raise LivePaperError(f"Could not infer target price for {slug}")
    return MarketInfo(
        slug=slug,
        market_url=build_market_url(template_url, slug),
        up_token_id=up_token,
        down_token_id=down_token,
        window_text=f"{start_bj:%H:%M}-{start_bj + timedelta(minutes=5):%H:%M}",
        target_price=float(target_text),
        asset_key=asset_key,
    )


def parse_book_levels(book_side: Any) -> List[Tuple[float, float]]:
    levels: List[Tuple[float, float]] = []
    for level in book_side or []:
        if isinstance(level, dict):
            price = level.get("price")
            size = level.get("size") or level.get("amount") or level.get("quantity") or 0.0
        elif isinstance(level, (list, tuple)) and level:
            price = level[0]
            size = level[1] if len(level) > 1 else 0.0
        else:
            continue
        try:
            levels.append((float(price), float(size)))
        except Exception:
            continue
    return levels


def fetch_book(token_id: str, timeout: float) -> Dict[str, Any]:
    return request_json(f"{CLOB_BASE}/book?token_id={token_id}", timeout)


def best_bid_ask(book: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    bids = parse_book_levels(book.get("bids"))
    asks = parse_book_levels(book.get("asks"))
    return max((price for price, _ in bids), default=None), min((price for price, _ in asks), default=None)


def top_size(book_side: Any, best: str) -> str:
    levels = parse_book_levels(book_side)
    if not levels:
        return ""
    chosen = max(levels, key=lambda x: x[0]) if best == "bid" else min(levels, key=lambda x: x[0])
    return f"{chosen[1]:.2f}"


def depth_sum(book_side: Any, top_n: int = 5) -> str:
    levels = parse_book_levels(book_side)[:top_n]
    return f"{sum(size for _, size in levels):.2f}" if levels else ""


def level_count(book_side: Any) -> str:
    return str(len(parse_book_levels(book_side)))


def cents(value: Optional[float]) -> str:
    return "" if value is None else f"{value * 100:.2f}"


def mid_cents(bid: Optional[float], ask: Optional[float]) -> str:
    return "" if bid is None or ask is None else f"{((bid + ask) / 2.0) * 100:.2f}"


def spread_cents(bid: Optional[float], ask: Optional[float]) -> str:
    return "" if bid is None or ask is None else f"{(ask - bid) * 100:.2f}"


def snapshot_row(info: MarketInfo, timeout: float) -> Dict[str, str]:
    up_book = fetch_book(info.up_token_id, timeout)
    down_book = fetch_book(info.down_token_id, timeout)
    up_bid, up_ask = best_bid_ask(up_book)
    down_bid, down_ask = best_bid_ask(down_book)
    now = datetime.now(BJ)
    return {
        "ts_iso": now.isoformat(timespec="seconds"),
        "slug": info.slug,
        "market_url": info.market_url,
        "window_text": info.window_text,
        "buy_up_cents": cents(up_ask),
        "buy_down_cents": cents(down_ask),
        "sell_up_cents": cents(up_bid),
        "sell_down_cents": cents(down_bid),
        "buy_up_size": top_size(up_book.get("asks"), "ask"),
        "buy_down_size": top_size(down_book.get("asks"), "ask"),
        "sell_up_size": top_size(up_book.get("bids"), "bid"),
        "sell_down_size": top_size(down_book.get("bids"), "bid"),
        "mid_up_cents": mid_cents(up_bid, up_ask),
        "mid_down_cents": mid_cents(down_bid, down_ask),
        "spread_up_cents": spread_cents(up_bid, up_ask),
        "spread_down_cents": spread_cents(down_bid, down_ask),
        "bid_depth_up_5": depth_sum(up_book.get("bids")),
        "ask_depth_up_5": depth_sum(up_book.get("asks")),
        "bid_depth_down_5": depth_sum(down_book.get("bids")),
        "ask_depth_down_5": depth_sum(down_book.get("asks")),
        "level_count_bid_up": level_count(up_book.get("bids")),
        "level_count_ask_up": level_count(up_book.get("asks")),
        "level_count_bid_down": level_count(down_book.get("bids")),
        "level_count_ask_down": level_count(down_book.get("asks")),
        "min_order_size_up": str(up_book.get("min_order_size", "")),
        "min_order_size_down": str(down_book.get("min_order_size", "")),
        "tick_size_up": str(up_book.get("tick_size", "")),
        "tick_size_down": str(down_book.get("tick_size", "")),
        "target_price": f"{info.target_price:.2f}",
        "final_price": fetch_live_reference_price(info.asset_key, timeout),
        "trade_count_1s": "",
        "trade_volume_1s": "",
    }


def ensure_csv(path: Path, fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with path.open("w", newline="", encoding="utf-8") as handle:
            csv.DictWriter(handle, fieldnames=fields).writeheader()
        return
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        current = reader.fieldnames or []
        if current == fields:
            return
        rows = list(reader)
    merged = fields + [field for field in current if field not in fields]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=merged)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in merged})


def append_csv(path: Path, fields: List[str], row: Dict[str, Any]) -> None:
    ensure_csv(path, fields)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writerow({field: clean_csv(row.get(field, "")) for field in fields})


def clean_csv(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float) and math.isnan(value):
        return ""
    return value


def read_csv_tail(path: Path, limit: int) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[-limit:]


def fixed_live_model_feature_sets() -> List[Tuple[str, List[str]]]:
    return [
        ("prior_plus_btc", robust.feature_set("market_prior", "btc")),
        ("prior_btc_liquidity", robust.feature_set("market_prior", "btc", "interactions", "liquidity_micro", "boundary")),
    ]


def train_fixed_live_model(args: argparse.Namespace) -> Dict[str, Any]:
    source_root = Path(args.source_root)
    log(f"training live paper model from {source_root}")
    grouped, _, raw_stats = base.read_monthly_runs(source_root)
    markets_all = base.build_markets(grouped)
    window_run_level, window_start, window_end = base.select_window(markets_all, args.lookback_days)
    markets, duplicate_slugs, max_runs_per_slug = base.dedupe_by_slug(window_run_level)
    split = robust.make_split(markets, args.train_days, args.validation_days, args.test_days, args.embargo_seconds)
    candidates = robust.build_candidates(
        markets,
        split,
        args.fee,
        "dense4s",
        args.entry_step_seconds,
        args.entry_start_seconds,
        args.entry_end_buffer_seconds,
    )
    train_rows = [row for row in candidates if row["split"] == "train"]
    validation_rows = [row for row in candidates if row["split"] == "validation"]
    model_train_rows = robust.evenly_sample(train_rows, args.model_train_sample)
    model_validation_rows = robust.evenly_sample(validation_rows, args.model_validation_sample)
    member_models = []
    for feature_set_name, feature_names in fixed_live_model_feature_sets():
        model, _ = robust.train_logistic(feature_set_name, model_train_rows, feature_names, 0.01, epochs=args.model_epochs)
        member_models.append(model)
    labels = [int(row["label"]) for row in model_validation_rows]
    blend = robust.train_logit_blend("mixed_top2_logit_blend", member_models, model_validation_rows, labels, 0.01, epochs=args.model_epochs)
    logits = [robust.predict_logit(blend, row) for row in model_validation_rows]
    cal_intercept, cal_slope = robust.fit_platt(logits, labels)
    model = robust.ModelBundle(
        blend.feature_set_name,
        blend.feature_names,
        blend.means,
        blend.stds,
        blend.weights,
        blend.l2,
        cal_intercept,
        cal_slope,
        blend.blend_members,
        blend.blend_l2,
    )
    return {
        "version": MODEL_CACHE_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "model": model,
        "config": robust.selected_fixed_decision_config(),
        "metadata": {
            "source_root": str(source_root),
            "lookback_days": args.lookback_days,
            "window_start_utc": window_start.isoformat(),
            "window_end_utc": window_end.isoformat(),
            "markets": len(markets),
            "duplicate_slugs": duplicate_slugs,
            "max_runs_per_slug": max_runs_per_slug,
            "raw_runs": raw_stats.get("runs"),
            "raw_rows": raw_stats.get("raw_rows"),
            "candidates": len(candidates),
            "train_rows": len(train_rows),
            "validation_rows": len(validation_rows),
            "model_train_rows": len(model_train_rows),
            "model_validation_rows": len(model_validation_rows),
            "model_epochs": args.model_epochs,
            "members": [name for name, _, _ in model.blend_members],
            "calibration_intercept": cal_intercept,
            "calibration_slope": cal_slope,
        },
    }


def load_model_bundle(args: argparse.Namespace) -> Dict[str, Any]:
    cache_path = Path(args.model_cache)
    if cache_path.exists() and not args.retrain_model:
        with cache_path.open("rb") as handle:
            payload = pickle.load(handle)
        if payload.get("version") == MODEL_CACHE_VERSION:
            return payload
    payload = train_fixed_live_model(args)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as handle:
        pickle.dump(payload, handle)
    return payload


def make_live_market(slug: str, rows: List[Dict[str, object]], info: MarketInfo) -> Optional[Dict[str, object]]:
    if not rows:
        return None
    rows = sorted(rows, key=lambda row: row["ts_utc"])  # type: ignore[index]
    start_ts = base.parse_slug_start(slug) or rows[0].get("ts_utc")
    if not isinstance(start_ts, datetime):
        return None
    close_ts = start_ts + timedelta(minutes=5)
    latest = rows[-1]
    target = info.target_price
    final_now = fnum(latest.get("final_price"), math.nan)
    outcome_up = 1.0 if finite(final_now) and final_now > target else 0.0
    available_first2 = [row for row in rows if isinstance(row.get("ts_utc"), datetime) and row["ts_utc"] <= start_ts + timedelta(minutes=2)]
    basis = available_first2 or rows
    row1 = base.row_after_time(rows, start_ts, 1) or latest
    row2 = base.row_after_time(rows, start_ts, 2) or latest
    row4 = base.row_after_time(rows, start_ts, 4) or latest

    path = [float(row["final_price"]) for row in basis if finite(row.get("final_price"))]
    diffs = [path[i] - path[i - 1] for i in range(1, len(path))]
    total_abs = sum(abs(x) for x in diffs)
    path_efficiency = abs(path[-1] - path[0]) / total_abs if len(path) >= 2 and total_abs > 0 else 1.0
    realized_vol = robust.stdev(diffs) if len(diffs) >= 2 else 0.0

    def move(row: Optional[Dict[str, object]]) -> float:
        if row is None or not finite(row.get("final_price")):
            return math.nan
        return float(row["final_price"]) - target

    buy_up_size_2m = base.size_from_row(row2, "buy_up_size")
    buy_down_size_2m = base.size_from_row(row2, "buy_down_size")
    bid_depth_up_2m = base.size_from_row(row2, "bid_depth_up_5")
    bid_depth_down_2m = base.size_from_row(row2, "bid_depth_down_5")
    ask_depth_up_2m = base.size_from_row(row2, "ask_depth_up_5")
    ask_depth_down_2m = base.size_from_row(row2, "ask_depth_down_5")
    mid_open = basis[0].get("mid_up_prob", math.nan)
    mid_now = latest.get("mid_up_prob", math.nan)
    spread_up = base.median(float(row.get("spread_up_cents", math.nan)) for row in basis) / 100.0
    spread_down = base.median(float(row.get("spread_down_cents", math.nan)) for row in basis) / 100.0
    overround = base.median(float(row.get("mid_overround_cents", math.nan)) for row in basis) / 100.0

    return {
        "run_name": "live",
        "slug": slug,
        "market_id": f"live::{slug}",
        "first_quote_ts": start_ts,
        "market_start_ts": start_ts,
        "market_close_ts": close_ts,
        "outcome_up": outcome_up,
        "session_et": base.session_et(start_ts),
        "btc_move_1m": move(row1),
        "btc_move_2m": move(row2),
        "btc_move_4m": move(row4),
        "mid_up_prob_open": float(mid_open) if finite(mid_open) else math.nan,
        "mid_up_prob_2m": float(mid_now) if finite(mid_now) else math.nan,
        "mid_up_prob_change_2m": float(mid_now) - float(mid_open) if finite(mid_now) and finite(mid_open) else math.nan,
        "size_imbalance_updown_2m": base.imbalance(buy_up_size_2m, buy_down_size_2m),
        "book_pressure_up_2m": base.imbalance(bid_depth_up_2m, ask_depth_up_2m),
        "book_pressure_down_2m": base.imbalance(bid_depth_down_2m, ask_depth_down_2m),
        "spread_up_median_first2m": spread_up,
        "spread_down_median_first2m": spread_down,
        "overround_median_first2m": overround,
        "realized_vol_first2m": realized_vol,
        "path_efficiency_first2m": path_efficiency,
        "rows": rows,
    }


def broad_live_split(now: datetime) -> robust.SplitSpec:
    start = now - timedelta(days=3650)
    end = now + timedelta(days=3650)
    return robust.SplitSpec(start, end, end + timedelta(seconds=1), end + timedelta(seconds=2), end + timedelta(seconds=3), end + timedelta(seconds=4), 0)


def side_price_size(row: Dict[str, object], side: str) -> Tuple[float, float]:
    if side == "buy_up":
        return base.price_from_row(row, "buy_up_cents"), base.size_from_row(row, "buy_up_size")
    return base.price_from_row(row, "buy_down_cents"), base.size_from_row(row, "buy_down_size")


def side_token_id(info: MarketInfo, side: str) -> str:
    return info.up_token_id if side == "buy_up" else info.down_token_id


def side_min_order_size(row: Dict[str, object], side: str) -> float:
    return base.size_from_row(row, "min_order_size_up" if side == "buy_up" else "min_order_size_down")


def state_default(args: argparse.Namespace, model_payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": "",
        "bankroll": args.starting_bankroll,
        "starting_bankroll": args.starting_bankroll,
        "realized_pnl": 0.0,
        "signals": 0,
        "fills": 0,
        "settlements": 0,
        "skips": 0,
        "current_slug": "",
        "feed_status": "initialized",
        "last_error": "",
        "last_error_at": "",
        "last_snapshot_at": "",
        "operator_note": "Dashboard initialized. Run analysis/live_paper_trader.py to start live paper trading.",
        "pending_orders": [],
        "open_positions": [],
        "traded_slugs": [],
        "last_final_by_slug": {},
        "target_by_slug": {},
        "execution_mode": args.execution_mode,
        "live_orders": [],
        "live_order_count_by_day": {},
        "live_notional_by_day": {},
        "model": model_payload.get("metadata", {}),
    }


def load_state(path: Path, args: argparse.Namespace, model_payload: Dict[str, Any]) -> Dict[str, Any]:
    if path.exists() and not args.reset_state:
        return json.loads(path.read_text(encoding="utf-8"))
    return state_default(args, model_payload)


def save_state(path: Path, state: Dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(UTC).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def read_iso(text: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(text)
    except Exception:
        return None


def event_now() -> str:
    return datetime.now(BJ).isoformat(timespec="seconds")


def build_dashboard(report_dir: Path, state: Dict[str, Any]) -> None:
    events = read_csv_tail(report_dir / "paper_events.csv", 300)
    snapshots = read_csv_tail(report_dir / "snapshots.csv", 180)
    payload = {"state": state, "events": events, "snapshots": snapshots}
    payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html_text = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Live Paper Trading</title>
  <style>
    body {{ margin:0; background:#f4f5f2; color:#202124; font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    header {{ padding:16px 22px; border-bottom:1px solid #d8dcd2; background:#fafaf7; position:sticky; top:0; }}
    h1 {{ margin:0 0 6px; font-size:20px; }}
    .sub {{ color:#667085; }}
    .status {{ margin:14px 0; padding:12px 14px; border:1px solid #d8dcd2; border-radius:8px; background:#fff; }}
    .status b {{ display:block; margin-bottom:4px; }}
    .status code {{ background:#f2f4f7; padding:2px 5px; border-radius:4px; }}
    .status.error {{ border-color:#f5b5ae; background:#fff7f5; }}
    .status.live {{ border-color:#abefc6; background:#f6fef9; }}
    main {{ padding:18px 22px 34px; max-width:1400px; margin:0 auto; }}
    .cards {{ display:grid; grid-template-columns:repeat(6,minmax(140px,1fr)); gap:10px; margin-bottom:14px; }}
    .card,.panel {{ background:white; border:1px solid #d8dcd2; border-radius:8px; padding:12px; }}
    .label {{ color:#667085; font-size:12px; }}
    .value {{ font-size:22px; font-weight:720; margin-top:4px; overflow-wrap:anywhere; }}
    .grid {{ display:grid; grid-template-columns:1.1fr 1fr; gap:12px; }}
    table {{ width:100%; border-collapse:collapse; min-width:900px; }}
    th,td {{ border-bottom:1px solid #e4e7ec; padding:7px 8px; text-align:left; white-space:nowrap; }}
    th {{ background:#eff1ec; color:#344054; font-size:12px; position:sticky; top:0; }}
    .scroll {{ overflow:auto; max-height:420px; border:1px solid #d8dcd2; border-radius:8px; background:white; }}
    canvas {{ width:100%; height:260px; display:block; }}
    .good {{ color:#137333; font-weight:650; }} .bad {{ color:#b42318; font-weight:650; }}
    @media(max-width:1000px) {{ .cards {{ grid-template-columns:repeat(2,1fr); }} .grid {{ grid-template-columns:1fr; }} }}
  </style>
</head>
<body>
<header>
  <h1>Polymarket 5min Live Paper Trading</h1>
  <div class="sub">实时盘口、策略信号、纸盘/订单计划/实盘执行事件。页面自动写入最新状态，浏览器刷新即可看更新。</div>
</header>
<main>
  <section class="status" id="statusBox">
    <b>Live feed status</b>
    <div>正在读取状态...</div>
  </section>
  <section class="cards" id="cards"></section>
  <section class="grid">
    <div class="panel"><h2>Bankroll / Event Timeline</h2><canvas id="curve" width="900" height="300"></canvas></div>
    <div class="panel"><h2>实时解释</h2><p>signal 表示策略看到 q-edge 机会；fill 表示延迟后仍能按容忍滑点模拟成交；settle 表示按窗口结束时 BTC 是否高于目标价结算 paper PnL。</p><div id="explain"></div></div>
  </section>
  <h2>Paper Events</h2><div class="scroll"><table id="events"></table></div>
  <h2>Latest Snapshots</h2><div class="scroll"><table id="snapshots"></table></div>
</main>
<script id="payload" type="application/json">{payload_json}</script>
<script>
const data = JSON.parse(document.getElementById('payload').textContent);
const fmtMoney = v => v == null || v === '' || Number.isNaN(Number(v)) ? '' : '$' + Number(v).toFixed(2);
const fmtNum = v => v == null || v === '' || Number.isNaN(Number(v)) ? '' : Number(v).toFixed(4);
const fmtPct = v => v == null || v === '' || Number.isNaN(Number(v)) ? '' : (Number(v)*100).toFixed(1) + '%';
function table(id, rows, cols) {{
  document.getElementById(id).innerHTML = '<thead><tr>' + cols.map(c=>'<th>'+c[0]+'</th>').join('') + '</tr></thead><tbody>' +
    rows.slice().reverse().map(r=>'<tr>' + cols.map(c=>'<td>'+c[1](r)+'</td>').join('') + '</tr>').join('') + '</tbody>';
}}
const s = data.state || {{}};
document.getElementById('cards').innerHTML = [
  ['Bankroll', fmtMoney(s.bankroll), 'start ' + fmtMoney(s.starting_bankroll)],
  ['Mode', s.execution_mode || 'paper', 'paper / plan / live'],
  ['Realized PnL', fmtMoney(s.realized_pnl), 'paper settlement'],
  ['Signals', s.signals || 0, 'q-edge 触发'],
  ['Fills', s.fills || 0, '延迟后成交'],
  ['Open', (s.open_positions||[]).length, '未结算持仓'],
  ['Pending', (s.pending_orders||[]).length, '等待延迟检查']
].map(c=>'<div class="card"><div class="label">'+c[0]+'</div><div class="value">'+c[1]+'</div><div class="label">'+c[2]+'</div></div>').join('');
const statusText = s.feed_status === 'live'
  ? '实时行情已连接，最近快照：' + (s.last_snapshot_at || '')
  : s.feed_status === 'error'
    ? '行情连接失败：' + (s.last_error || '')
    : '尚未连接实时行情。这个页面已经初始化，但 live paper 进程还没有成功写入快照。';
document.getElementById('statusBox').innerHTML =
  '<b>Live feed status: ' + (s.feed_status || 'unknown') + '</b>' +
  '<div>' + statusText + '</div>' +
  '<div class="label">这个 HTML 不会自己拉行情；必须让 Python 进程持续运行，它才会写入 snapshots/events。</div>' +
  '<div class="label">启动命令：<code>cd /Users/ytfeng/Code_Pj/poly_trade && python3 analysis/live_paper_trader.py</code></div>' +
  (s.last_error_at ? '<div class="label">last_error_at: ' + s.last_error_at + '</div>' : '');
document.getElementById('statusBox').classList.add(s.feed_status === 'live' ? 'live' : s.feed_status === 'error' ? 'error' : 'idle');
document.getElementById('explain').innerHTML =
  '<p><b>当前 market:</b> ' + (s.current_slug || '未连接实时行情') + '</p>' +
  '<p><b>执行模式:</b> ' + (s.execution_mode || 'paper') + '</p>' +
  '<p><b>模型:</b> mixed_top2_logit_blend，执行配置 ' + (s.model?.members || []).join(' + ') + '</p>' +
  '<p><b>状态更新时间:</b> ' + (s.updated_at || '') + '</p>';
table('events', data.events || [], [
  ['time', r=>r.event_ts], ['type', r=>r.event_type], ['status', r=>r.status], ['slug', r=>r.slug],
  ['side', r=>r.side], ['q', r=>fmtNum(r.q_model)], ['edge', r=>fmtNum(r.edge)], ['signal', r=>fmtNum(r.signal_price)],
  ['paper', r=>fmtNum(r.paper_price)], ['live id', r=>r.live_order_id || r.live_order_local_id || ''], ['live ok', r=>r.live_order_ok || ''],
  ['amount', r=>fmtMoney(r.live_amount_usd || r.fill_cost)], ['PnL', r=>fmtMoney(r.pnl_usd)], ['bankroll', r=>fmtMoney(r.bankroll_after)]
]);
table('snapshots', data.snapshots || [], [
  ['time', r=>r.ts_iso], ['slug', r=>r.slug], ['window', r=>r.window_text], ['up ask', r=>r.buy_up_cents],
  ['down ask', r=>r.buy_down_cents], ['up size', r=>r.buy_up_size], ['down size', r=>r.buy_down_size],
  ['min up', r=>r.min_order_size_up], ['min down', r=>r.min_order_size_down], ['target', r=>r.target_price], ['BTC', r=>r.final_price]
]);
const canvas = document.getElementById('curve'), ctx = canvas.getContext('2d');
const bankrollEvents = (data.events || []).filter(r=>r.bankroll_after);
if (bankrollEvents.length) {{
  const vals = bankrollEvents.map(r=>Number(r.bankroll_after)).filter(Number.isFinite);
  const w=canvas.width,h=canvas.height,p=34,min=Math.min(...vals),max=Math.max(...vals),span=Math.max(1e-9,max-min);
  ctx.clearRect(0,0,w,h); ctx.strokeStyle='#d8dcd2'; ctx.beginPath(); ctx.moveTo(p,h-p); ctx.lineTo(w-p,h-p); ctx.lineTo(w-p,p); ctx.stroke();
  ctx.strokeStyle='#0f766e'; ctx.lineWidth=2; ctx.beginPath();
  vals.forEach((v,i)=>{{ const x=p+i/Math.max(1,vals.length-1)*(w-2*p), y=h-p-(v-min)/span*(h-2*p); if(i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y); }});
  ctx.stroke(); ctx.fillStyle='#344054'; ctx.fillText('latest ' + fmtMoney(vals[vals.length-1]), p, 20);
}}
setTimeout(() => location.reload(), 10000);
</script>
</body>
</html>"""
    (report_dir / "index.html").write_text(html_text, encoding="utf-8")


class LivePaperEngine:
    def __init__(self, args: argparse.Namespace, model_payload: Dict[str, Any]):
        self.args = args
        self.report_dir = Path(args.report_dir)
        self.model = model_payload["model"]
        self.cfg = model_payload["config"]
        self.execution_mode = str(args.execution_mode).lower()
        self.live_settings = live_execution.LiveExecutionSettings.from_args(args)
        self.live_executor = (
            live_execution.PolymarketLiveExecutor(self.live_settings)
            if self.execution_mode == "live"
            else None
        )
        self.state_path = self.report_dir / "state.json"
        self.state = load_state(self.state_path, args, model_payload)
        self.state["execution_mode"] = self.execution_mode
        self.template_url = normalize_url(args.market_url)
        seed_slug = slug_from_market_url(self.template_url)
        self.series_prefix = series_prefix_from_slug(seed_slug)
        self.asset_key = infer_asset_key(self.series_prefix)
        self.cached_info: Optional[MarketInfo] = None
        self.rows_by_slug: Dict[str, List[Dict[str, object]]] = {}

    def current_info(self) -> MarketInfo:
        slug, _, _ = current_window_slug(self.series_prefix)
        if self.cached_info is None or self.cached_info.slug != slug:
            self.cached_info = fetch_market_info(slug, self.template_url, self.asset_key, self.args.timeout)
            self.state["current_slug"] = slug
            log(f"locked live window {self.cached_info.window_text} {slug}")
        return self.cached_info

    def append_event(self, row: Dict[str, Any]) -> None:
        append_csv(self.report_dir / "paper_events.csv", EVENT_FIELDS, row)

    def mark_slug_traded(self, slug: str) -> None:
        traded = set(self.state.get("traded_slugs", []))
        traded.add(slug)
        self.state["traded_slugs"] = sorted(traded)

    def build_live_order_plan(self, order: Dict[str, Any], snap: Dict[str, object], info: MarketInfo) -> live_execution.LiveOrderPlan:
        return live_execution.build_buy_market_plan(
            local_order_id=str(order["order_id"]),
            slug=info.slug,
            side=str(order["side"]),
            token_id=side_token_id(info, str(order["side"])),
            intended_cost=float(order["intended_cost"]),
            signal_price=float(order["signal_price"]),
            max_adverse_slippage=float(order["max_adverse_slippage"]),
            min_order_size=side_min_order_size(snap, str(order["side"])),
            settings=self.live_settings,
        )

    def live_quota_block_reason(self, plan: live_execution.LiveOrderPlan, now: datetime) -> str:
        day = now.astimezone(BJ).date().isoformat()
        count_by_day = self.state.setdefault("live_order_count_by_day", {})
        notional_by_day = self.state.setdefault("live_notional_by_day", {})
        count = int(count_by_day.get(day, 0))
        notional = float(notional_by_day.get(day, 0.0))
        amount = fnum(plan.amount_usd, 0.0)
        if count >= int(self.args.max_live_orders_per_day):
            return "blocked_daily_order_cap"
        if notional + amount > float(self.args.max_live_notional_usd_per_day):
            return "blocked_daily_notional_cap"
        return ""

    def record_live_acceptance(self, plan: live_execution.LiveOrderPlan, now: datetime, result: Dict[str, Any]) -> None:
        if not result.get("live_order_ok"):
            return
        day = now.astimezone(BJ).date().isoformat()
        count_by_day = self.state.setdefault("live_order_count_by_day", {})
        notional_by_day = self.state.setdefault("live_notional_by_day", {})
        count_by_day[day] = int(count_by_day.get(day, 0)) + 1
        notional_by_day[day] = float(notional_by_day.get(day, 0.0)) + fnum(plan.amount_usd, 0.0)

    def record_signal(self, candidate: Dict[str, object], snap: Dict[str, object], info: MarketInfo, now: datetime) -> None:
        order_id = f"{info.slug}:{candidate['side']}:{int(float(candidate['entry_second']))}:{len(self.state.get('pending_orders', [])) + self.state.get('signals', 0) + 1}"
        stake_fraction = robust.stake_fraction(candidate, self.cfg)
        signal_price = float(candidate["entry_price"])
        intended_cost = min(float(self.state["bankroll"]) * stake_fraction, float(self.state["bankroll"]))
        order = {
            "order_id": order_id,
            "slug": info.slug,
            "window_text": info.window_text,
            "market_url": info.market_url,
            "side": candidate["side"],
            "status": "pending_delay" if self.execution_mode == "paper" else "signal",
            "signal_ts": now.isoformat(),
            "due_ts": (now + timedelta(seconds=self.args.paper_delay_seconds)).isoformat(),
            "close_ts": (base.parse_slug_start(info.slug) + timedelta(minutes=5)).isoformat() if base.parse_slug_start(info.slug) else "",
            "entry_second": candidate["entry_second"],
            "q_model": candidate["q_model"],
            "edge": candidate["edge"],
            "signal_price": signal_price,
            "stake_fraction": stake_fraction,
            "intended_cost": intended_cost,
            "target_price": info.target_price,
            "delay_seconds": self.args.paper_delay_seconds,
            "max_adverse_slippage": self.args.paper_max_adverse_slippage,
            "execution_mode": self.execution_mode,
        }
        self.state["signals"] = int(self.state.get("signals", 0)) + 1
        event = dict(order)
        event.update({"event_ts": event_now(), "event_type": "signal", "bankroll_after": self.state["bankroll"]})
        self.append_event(event)
        if self.execution_mode == "paper":
            self.state["pending_orders"].append(order)
            self.mark_slug_traded(info.slug)
            return

        plan = self.build_live_order_plan(order, snap, info)
        execution_event = dict(order)
        execution_event.update(plan.event_fields())
        execution_event.update(
            {
                "event_ts": event_now(),
                "event_type": "order_plan" if self.execution_mode == "plan" else "live_order",
                "status": "planned" if not plan.blocked_reason else plan.blocked_reason,
                "bankroll_after": self.state["bankroll"],
            }
        )
        if self.execution_mode == "live":
            quota_reason = self.live_quota_block_reason(plan, now)
            if quota_reason:
                execution_event.update({"status": quota_reason, "live_order_ok": False, "live_order_status": "blocked", "live_order_error": quota_reason})
            elif self.live_executor is None:
                execution_event.update({"status": "executor_missing", "live_order_ok": False, "live_order_error": "live executor is not initialized"})
            else:
                result = self.live_executor.place_buy_market_order(plan)
                execution_event.update(result)
                execution_event["status"] = str(result.get("live_order_status") or execution_event["status"])
                self.record_live_acceptance(plan, now, result)
        self.append_event(execution_event)
        self.state.setdefault("live_orders", []).append(
            {key: execution_event.get(key, "") for key in ("event_ts", "event_type", "status", "slug", "side", "live_order_local_id", "live_order_id", "live_order_ok", "live_amount_usd", "live_max_price", "live_order_error")}
        )
        self.state["live_orders"] = self.state["live_orders"][-100:]
        self.mark_slug_traded(info.slug)

    def maybe_signal(self, market: Dict[str, object], snap: Dict[str, object], info: MarketInfo) -> None:
        if info.slug in set(self.state.get("traded_slugs", [])):
            return
        entry_ts = snap.get("ts_utc")
        start_ts = market.get("market_start_ts")
        close_ts = market.get("market_close_ts")
        if not isinstance(entry_ts, datetime) or not isinstance(start_ts, datetime) or not isinstance(close_ts, datetime):
            return
        if entry_ts < start_ts + timedelta(seconds=self.args.entry_start_seconds):
            return
        if entry_ts > close_ts - timedelta(seconds=self.args.entry_end_buffer_seconds):
            return
        entry_second = (entry_ts - start_ts).total_seconds()
        split = broad_live_split(entry_ts)
        candidates: List[Dict[str, object]] = []
        for side in ("buy_up", "buy_down"):
            candidate = robust.candidate_for_snapshot(market, snap, entry_second, side, 0, split, self.args.fee)
            if candidate is not None:
                candidate["split"] = "live"
                candidates.append(candidate)
        robust.attach_scores(candidates, self.model, self.args.fee)
        options = [row for row in candidates if robust.candidate_passes(row, self.cfg)]
        if not options:
            return
        chosen = max(options, key=lambda row: float(row.get("edge", -999.0)))
        self.record_signal(chosen, snap, info, entry_ts)

    def process_pending(self, snap: Dict[str, object]) -> None:
        now = snap.get("ts_utc")
        if not isinstance(now, datetime):
            return
        remaining: List[Dict[str, Any]] = []
        for order in self.state.get("pending_orders", []):
            due = read_iso(str(order.get("due_ts", "")))
            if due is None or now < due:
                remaining.append(order)
                continue
            side = str(order["side"])
            paper_price, paper_size = side_price_size(snap, side)
            status = "no_quote"
            fill_cost = 0.0
            shares = 0.0
            fill_ratio = 0.0
            reason = ""
            if finite(paper_price) and finite(paper_size) and paper_size > 0:
                if paper_price <= float(order["signal_price"]) + self.args.paper_max_adverse_slippage:
                    fill_cost = min(float(order["intended_cost"]), float(self.state["bankroll"]), paper_size * paper_price)
                    if fill_cost > 0:
                        shares = fill_cost / paper_price
                        fill_ratio = fill_cost / float(order["intended_cost"]) if float(order["intended_cost"]) > 0 else 1.0
                        status = "full_fill" if fill_ratio >= 0.99 else "partial_fill"
                        position = dict(order)
                        position.update(
                            {
                                "status": "open",
                                "fill_ts": now.isoformat(),
                                "paper_price": paper_price,
                                "fill_cost": fill_cost,
                                "shares": shares,
                                "fill_ratio": fill_ratio,
                            }
                        )
                        self.state["open_positions"].append(position)
                        self.state["fills"] = int(self.state.get("fills", 0)) + 1
                    else:
                        status = "zero_size"
                else:
                    status = "price_moved"
                    reason = "paper price exceeded signal price plus slippage limit"
            event = dict(order)
            event.update(
                {
                    "event_ts": event_now(),
                    "event_type": "fill_check",
                    "status": status,
                    "paper_price": paper_price,
                    "price_slippage": paper_price - float(order["signal_price"]) if finite(paper_price) else math.nan,
                    "fill_cost": fill_cost,
                    "shares": shares,
                    "fill_ratio": fill_ratio,
                    "bankroll_after": self.state["bankroll"],
                    "reason": reason,
                }
            )
            self.append_event(event)
        self.state["pending_orders"] = remaining

    def settle_positions(self) -> None:
        now = datetime.now(UTC)
        remaining: List[Dict[str, Any]] = []
        for position in self.state.get("open_positions", []):
            close_ts = read_iso(str(position.get("close_ts", "")))
            if close_ts is None or now < close_ts + timedelta(seconds=self.args.settle_buffer_seconds):
                remaining.append(position)
                continue
            slug = str(position["slug"])
            final_price = fnum(self.state.get("last_final_by_slug", {}).get(slug), math.nan)
            target_price = fnum(position.get("target_price"), math.nan)
            if not finite(final_price):
                final_price = fnum(fetch_live_reference_price(self.asset_key, self.args.timeout), math.nan)
            if not finite(final_price) or not finite(target_price):
                remaining.append(position)
                continue
            side = str(position["side"])
            won = final_price > target_price
            payout = 1.0 if (side == "buy_up" and won) or (side == "buy_down" and not won) else 0.0
            pnl = float(position["shares"]) * (payout - float(position["paper_price"]) - self.args.fee)
            self.state["bankroll"] = float(self.state["bankroll"]) + pnl
            self.state["realized_pnl"] = float(self.state.get("realized_pnl", 0.0)) + pnl
            self.state["settlements"] = int(self.state.get("settlements", 0)) + 1
            event = dict(position)
            event.update(
                {
                    "event_ts": event_now(),
                    "event_type": "settle",
                    "status": "settled_win" if payout > 0 else "settled_loss",
                    "pnl_usd": pnl,
                    "bankroll_after": self.state["bankroll"],
                    "final_price": final_price,
                    "target_price": target_price,
                }
            )
            self.append_event(event)
        self.state["open_positions"] = remaining

    def step(self) -> Dict[str, str]:
        info = self.current_info()
        raw = snapshot_row(info, self.args.timeout)
        append_csv(self.report_dir / "snapshots.csv", SNAPSHOT_FIELDS, raw)
        clean = base.clean_row(raw, "live")
        if clean is None:
            raise LivePaperError("live snapshot could not be parsed")
        self.rows_by_slug.setdefault(info.slug, []).append(clean)
        self.rows_by_slug[info.slug] = self.rows_by_slug[info.slug][-360:]
        if finite(clean.get("final_price")):
            self.state.setdefault("last_final_by_slug", {})[info.slug] = float(clean["final_price"])
        self.state.setdefault("target_by_slug", {})[info.slug] = info.target_price
        self.state["feed_status"] = "live"
        self.state["last_error"] = ""
        self.state["last_snapshot_at"] = raw.get("ts_iso", "")
        market = make_live_market(info.slug, self.rows_by_slug[info.slug], info)
        if market is not None:
            self.maybe_signal(market, clean, info)
        self.process_pending(clean)
        self.settle_positions()
        save_state(self.state_path, self.state)
        build_dashboard(self.report_dir, self.state)
        return raw


def default_paths() -> Tuple[Path, Path]:
    source_root, _, _ = robust.default_paths()
    return source_root, DEFAULT_REPORT_DIR


def parse_args() -> argparse.Namespace:
    source_root, report_dir = default_paths()
    parser = argparse.ArgumentParser(description="Run live paper trading for Polymarket BTC 5min Up/Down. This never places real orders.")
    parser.add_argument("--market-url", default=SEED_MARKET_URL)
    parser.add_argument("--env-file", default="", help="Optional local env file for live trading credentials; existing shell env wins.")
    parser.add_argument("--source-root", default=str(source_root))
    parser.add_argument("--report-dir", default=str(report_dir))
    parser.add_argument("--model-cache", default=str(report_dir / "live_model_cache.pkl"))
    parser.add_argument("--retrain-model", action="store_true")
    parser.add_argument("--reset-state", action="store_true")
    parser.add_argument("--init-dashboard", action="store_true", help="Create an empty state/dashboard without touching live market APIs.")
    parser.add_argument("--prepare-model-cache", action="store_true", help="Train/cache the live model and exit.")
    parser.add_argument("--once", action="store_true", help="Run exactly one live snapshot/evaluation step.")
    parser.add_argument("--duration-minutes", type=float, default=0.0, help="Run for N minutes; 0 means run until Ctrl-C.")
    parser.add_argument("--sample-seconds", type=float, default=4.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--fee", type=float, default=base.FEE_DEFAULT)
    parser.add_argument("--starting-bankroll", type=float, default=100.0)
    parser.add_argument("--execution-mode", choices=["paper", "plan", "live"], default="paper", help="paper simulates fills; plan logs live order params; live submits orders through the Polymarket SDK.")
    parser.add_argument("--allow-live-trading", action="store_true", help="Required with --execution-mode live.")
    parser.add_argument("--max-live-order-usd", type=float, default=1.0, help="Hard cap for each live BUY market order.")
    parser.add_argument("--max-live-orders-per-day", type=int, default=5, help="Hard cap for accepted live orders per local day.")
    parser.add_argument("--max-live-notional-usd-per-day", type=float, default=5.0, help="Hard cap for accepted live BUY notional per local day.")
    parser.add_argument("--live-order-type", choices=["FAK", "FOK"], default="FAK", help="Market order type used in live execution.")
    parser.add_argument("--paper-delay-seconds", type=int, default=4)
    parser.add_argument("--paper-max-adverse-slippage", type=float, default=0.01)
    parser.add_argument("--settle-buffer-seconds", type=int, default=2)
    parser.add_argument("--entry-start-seconds", type=int, default=robust.ENTRY_START_SECONDS_DEFAULT)
    parser.add_argument("--entry-end-buffer-seconds", type=int, default=robust.ENTRY_END_BUFFER_SECONDS_DEFAULT)
    parser.add_argument("--entry-step-seconds", type=int, default=robust.ENTRY_STEP_SECONDS_DEFAULT)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--train-days", type=int, default=robust.TRAIN_DAYS_DEFAULT)
    parser.add_argument("--validation-days", type=int, default=robust.VALIDATION_DAYS_DEFAULT)
    parser.add_argument("--test-days", type=int, default=robust.TEST_DAYS_DEFAULT)
    parser.add_argument("--embargo-seconds", type=int, default=robust.EMBARGO_SECONDS_DEFAULT)
    parser.add_argument("--model-train-sample", type=int, default=60_000)
    parser.add_argument("--model-validation-sample", type=int, default=30_000)
    parser.add_argument("--model-epochs", type=int, default=350)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        live_execution.load_env_file(args.env_file)
    except live_execution.LiveExecutionError as exc:
        log(f"configuration error: {exc}")
        return 2
    if args.init_dashboard:
        report_dir = Path(args.report_dir)
        payload = {
            "metadata": {
                "status": "initialized_without_live_feed",
                "members": ["prior_plus_btc@l2=0.01", "prior_btc_liquidity@l2=0.01"],
            }
        }
        state = state_default(args, payload)
        save_state(report_dir / "state.json", state)
        ensure_csv(report_dir / "snapshots.csv", SNAPSHOT_FIELDS)
        ensure_csv(report_dir / "paper_events.csv", EVENT_FIELDS)
        build_dashboard(report_dir, state)
        print(json.dumps({"dashboard": str(report_dir / "index.html"), "state": str(report_dir / "state.json")}, indent=2, ensure_ascii=False))
        return 0
    if args.execution_mode == "live":
        try:
            live_execution.LiveExecutionSettings.from_args(args)
        except live_execution.LiveExecutionError as exc:
            log(f"configuration error: {exc}")
            return 2
    model_payload = load_model_bundle(args)
    if args.prepare_model_cache:
        report_dir = Path(args.report_dir)
        state = load_state(report_dir / "state.json", args, model_payload)
        save_state(report_dir / "state.json", state)
        ensure_csv(report_dir / "snapshots.csv", SNAPSHOT_FIELDS)
        ensure_csv(report_dir / "paper_events.csv", EVENT_FIELDS)
        build_dashboard(report_dir, state)
        print(json.dumps({"model_cache": args.model_cache, "metadata": model_payload.get("metadata", {})}, indent=2, ensure_ascii=False))
        return 0
    engine = LivePaperEngine(args, model_payload)
    end_time = datetime.now(UTC) + timedelta(minutes=args.duration_minutes) if args.duration_minutes > 0 else None
    try:
        while True:
            row = engine.step()
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if args.once:
                return 0
            if end_time is not None and datetime.now(UTC) >= end_time:
                return 0
            time.sleep(max(0.5, args.sample_seconds))
    except KeyboardInterrupt:
        log("stopped by user")
        return 0
    except Exception as exc:
        engine.state["feed_status"] = "error"
        engine.state["last_error"] = str(exc)
        engine.state["last_error_at"] = datetime.now(UTC).isoformat()
        save_state(engine.state_path, engine.state)
        build_dashboard(engine.report_dir, engine.state)
        log(f"fatal: {exc}")
        return 1
    finally:
        if getattr(engine, "live_executor", None) is not None:
            engine.live_executor.close()


if __name__ == "__main__":
    raise SystemExit(main())
