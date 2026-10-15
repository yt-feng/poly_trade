"""Quality-first, causal research inputs. This module never places orders.

Contiguous gold segment selection is for fitting only. All normalized decisions remain
in the dataset, including decisions followed by a gap. Evaluation code must use
the decision-time ``eligible`` flag, never a future window's gold classification.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
from pathlib import Path
import re


WINDOW_MS = 300_000
SLUG_RE = re.compile(r"btc-updown-5m-([0-9]+)\Z")
DEFAULT_FEATURES = ("spot_return_5s_bps", "up_mid_change_5s")
KNOWN_FEE_MODELS = {"explicit_fee_free", "C_rate_p_one_minus_p_2026-09-23"}


@dataclass(frozen=True)
class QualityPolicy:
    max_quote_age_ms: int = 1500
    max_quote_skew_ms: int = 250
    max_metadata_age_ms: int = 60_000
    max_source_age_ms: int = 5000
    max_sampling_lag_ms: int = 100
    max_sample_gap_ms: int = 2500
    max_lookback_jitter_ms: int = 1500
    gold_min_contiguous_seconds: int = 90

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or value < 0:
                raise ValueError(name + " must be a nonnegative integer")
        if not 1 <= self.gold_min_contiguous_seconds <= 300:
            raise ValueError("gold_min_contiguous_seconds must be between 1 and 300")


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value):
    result = number(value)
    return int(result) if result is not None and result.is_integer() else None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _fresh(obj, now, age, require_event=False):
    obj = _dict(obj)
    received = number(obj.get("received_ms"))
    if received is None or not 0 <= now - received <= age:
        return False
    event = number(obj.get("event_ms"))
    if event is not None and event > now:
        return False
    return not require_event or (event is not None and 0 <= now - event <= age)


def _layer(reasons):
    reasons = sorted(set(reasons))
    return {"accepted": not reasons, "reasons": reasons}


def _verified_rows(path):
    side = path.with_name(path.name + ".sha256")
    if not side.is_file():
        raise ValueError("Missing checksum sidecar: " + path.name)
    tokens = side.read_text(encoding="utf-8").split()
    if not tokens or not re.fullmatch(r"[a-fA-F0-9]{64}", tokens[0]):
        raise ValueError("Invalid checksum sidecar: " + path.name)
    with path.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != tokens[0].lower():
        raise ValueError("Checksum mismatch: " + path.name)
    def rows():
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Non-object archive record: " + path.name)
                    yield row
    return rows(), digest


def compact_snapshot(row):
    """A declared projection after hashing; source archives remain immutable.

    No observation is sampled out. Large unrelated ladders, diagnostics and
    cross-market payloads are discarded only from the in-memory research view.
    The complete original row digest is still used to detect conflicting copies.
    """
    top_keys = ("schema_version", "asset", "slug", "sample_ms", "window_start_ms",
                "up_token_id", "down_token_id", "poly_valid", "binance_valid",
                "chainlink_valid", "sampler_lag_ms")
    result = {key: row[key] for key in top_keys if key in row}
    quote_keys = ("bid", "ask", "bid_size", "ask_size", "received_ms", "event_ms", "price", "token_id")
    for name in ("up", "down", "binance", "chainlink"):
        value = _dict(row.get(name))
        result[name] = {key: value[key] for key in quote_keys if key in value}
    micro = _dict(row.get("microstructure"))
    compact = {key: micro[key] for key in ("schema_version", "asof_ms", "rules", "valid") if key in micro}
    for name in ("poly_up", "poly_down"):
        value = _dict(micro.get(name))
        compact[name] = {key: value[key] for key in ("received_ms", "event_ms", "min_order_size", "tick_size", "hash") if key in value}
    for name in ("coinbase_ticker", "twap60"):
        value = _dict(micro.get(name))
        compact[name] = {key: value[key] for key in ("received_ms", "event_ms", "price") if key in value}
        if isinstance(value.get("payload"), dict):
            compact[name]["payload"] = {"price": value["payload"].get("price")}
    for name in ("spot_flow", "perp_flow", "coinbase_flow"):
        value = _dict(micro.get(name))
        compact[name] = {key: value[key] for key in ("connected", "last_trade_received_ms") if key in value}
        for horizon in ("5s", "30s"):
            bucket = _dict(value.get(horizon))
            compact[name][horizon] = {key: bucket[key] for key in ("warmed", "recent_id_gap_or_overflow", "imbalance") if key in bucket}
    result["microstructure"] = compact
    return result


def load_verified_snapshots(root, max_rows=None):
    """Load BTC archive rows; conflicting duplicates fail, identical ones dedupe."""
    root = Path(root)
    unique, row_digests, files = {}, {}, []
    duplicates = ignored_assets = 0
    for path in sorted(root.rglob("snapshots-*.jsonl.gz")):
        archive, digest = _verified_rows(path)
        file_record = {"path": str(path.relative_to(root)), "sha256": digest,
                       "bytes": path.stat().st_size, "rows": 0,
                       "first_sample_ms": None, "last_sample_ms": None}
        files.append(file_record)
        for row in archive:
            file_record["rows"] += 1
            archive_ms = integer(row.get("sample_ms"))
            if archive_ms is not None and archive_ms >= 0:
                first = file_record["first_sample_ms"]
                last = file_record["last_sample_ms"]
                file_record["first_sample_ms"] = archive_ms if first is None else min(first, archive_ms)
                file_record["last_sample_ms"] = archive_ms if last is None else max(last, archive_ms)
            if row.get("asset") != "btc":
                ignored_assets += 1
                continue
            ms = integer(row.get("sample_ms"))
            if ms is None or not isinstance(row.get("slug"), str):
                raise ValueError("BTC archive row lacks a valid time/market identity")
            key = (row["slug"], ms)
            row_digest = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).digest()
            if key in unique:
                duplicates += 1
                if row_digests[key] != row_digest:
                    raise ValueError("Conflicting duplicate BTC observation")
            else:
                unique[key] = compact_snapshot(row)
                row_digests[key] = row_digest
                if max_rows is not None and len(unique) > max_rows:
                    raise ValueError("ROW_BUDGET_EXCEEDED: choose a smaller, date-stratified archive selection; no rows were silently truncated")
    rows = sorted(unique.values(), key=lambda row: (row["sample_ms"], row["slug"]))
    if not files:
        raise ValueError("No verified snapshot archives")
    provenance = {"schema_version": 1, "files": files, "rows": len(rows),
                  "duplicate_rows": duplicates, "ignored_other_assets": ignored_assets,
                  "full_history_complete": False, "memory_projection_version": 1,
                  "projection_note": "No row sampling; unused raw fields omitted after full-archive verification and full-row conflict hashing"}
    provenance["selection_sha256"] = hashlib.sha256(json.dumps(
        files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return rows, provenance


def mark_evaluation_scope(dataset, provenance, max_frame_gap_ms=2500):
    """Mark full markets inside preselected whole-file sampling frames.

    A source file is one frame from its first through last sampled second,
    irrespective of internal missing/invalid observations. Overlapping or
    adjacent files form one frame. Partial boundary markets are outside the
    evaluation sampling plan; internal gaps cannot remove a market or trade.
    These are sampled-second bounds, not a claim of complete event capture.
    Training gold segments are unaffected.
    """
    if type(max_frame_gap_ms) is not int or max_frame_gap_ms < 0:
        raise ValueError("max_frame_gap_ms must be a nonnegative integer")
    bounds = []
    for source in provenance.get("files", []):
        first, last = integer(source.get("first_sample_ms")), integer(source.get("last_sample_ms"))
        if first is None and last is None and source.get("rows") == 0:
            continue
        if first is None or last is None or first < 0 or last < first:
            raise ValueError("Source archive sampling bounds missing or invalid")
        bounds.append({"first_sample_ms": first, "last_sample_ms": last,
                       "source_files": [source.get("path")]})
    if not bounds:
        raise ValueError("No source archive sampling frames")
    merged = []
    for source in sorted(bounds, key=lambda item: (item["first_sample_ms"], item["last_sample_ms"])):
        if merged and source["first_sample_ms"] - merged[-1]["last_sample_ms"] <= max_frame_gap_ms:
            merged[-1]["last_sample_ms"] = max(merged[-1]["last_sample_ms"], source["last_sample_ms"])
            merged[-1]["source_files"].extend(source["source_files"])
        else:
            merged.append(source)
    for frame in merged:
        frame["start_ms"] = frame["first_sample_ms"] // 1000 * 1000
        frame["end_ms_exclusive"] = (frame["last_sample_ms"] // 1000 + 1) * 1000
        frame["source_file_count"] = len(frame["source_files"])
    assignments = {}
    for window in dataset["windows"]:
        start, end = window["window_start_ms"], window["window_end_ms"]
        included = start is not None and end is not None and any(
            frame["start_ms"] <= start and end <= frame["end_ms_exclusive"] for frame in merged)
        assignments[window["slug"]] = bool(included)
        window["evaluation_scope_included"] = bool(included)
    for decision in dataset["decisions"]:
        decision["evaluation_scope_included"] = assignments[decision["slug"]]
    included_count = sum(assignments.values())
    dataset["evaluation_scope"] = {
        "version": 1,
        "basis": "whole_preselected_archive_bounds_not_quality_or_outcomes",
        "sampled_second_bounds": True,
        "max_frame_gap_ms": max_frame_gap_ms,
        "frames": merged,
        "included_market_windows": included_count,
        "excluded_boundary_market_windows": len(assignments) - included_count,
        "included_decisions": sum(decision["evaluation_scope_included"] for decision in dataset["decisions"]),
        "internal_gaps_split_frames": False,
        "training_selection_unchanged": True,
        "complete_exchange_event_capture": False,
    }
    return dataset


def normalize_snapshot(raw, policy=None):
    """Separate intrinsic row identity, executable quote quality and unknown rules."""
    policy = policy or QualityPolicy()
    raw = _dict(raw)
    micro = _dict(raw.get("microstructure"))
    rules = _dict(micro.get("rules"))
    flags = _dict(micro.get("valid"))
    fees = _dict(rules.get("fees"))
    ms = integer(raw.get("sample_ms"))
    slug = raw.get("slug")
    match = SLUG_RE.fullmatch(slug) if isinstance(slug, str) else None
    start = int(match.group(1)) * 1000 if match else None
    base, execution, unverified = [], [], []
    if raw.get("asset") != "btc":
        base.append("wrong_asset")
    if ms is None or ms < 0:
        base.append("sample_time_invalid")
    if start is None or start % WINDOW_MS:
        base.append("market_identity_invalid")
    if ms is not None and start is not None and not start <= ms < start + WINDOW_MS:
        base.append("sample_outside_market")
    supplied_start = integer(raw.get("window_start_ms"))
    if supplied_start is not None and supplied_start != start:
        base.append("market_start_conflict")
    if micro.get("schema_version") != 3:
        base.append("microstructure_schema_not_v3")
    asof = number(micro.get("asof_ms"))
    if ms is None or asof is None or not 0 <= ms - asof <= policy.max_source_age_ms:
        base.append("microstructure_time_invalid")
    if rules.get("slug") is not None and rules.get("slug") != slug:
        base.append("rules_market_conflict")
    condition_id = rules.get("condition_id")
    up_token, down_token = raw.get("up_token_id"), raw.get("down_token_id")
    if not condition_id or not up_token or not down_token or up_token == down_token:
        base.append("token_or_condition_identity_missing")

    now = ms if ms is not None else -1
    lag = number(raw.get("sampler_lag_ms"))
    if lag is None or not 0 <= lag <= policy.max_sampling_lag_ms:
        execution.append("sampling_lag")
    metadata_ms = number(rules.get("metadata_received_ms"))
    if not flags.get("rules") or metadata_ms is None or not 0 <= now - metadata_ms <= policy.max_metadata_age_ms:
        execution.append("rules_stale_or_unknown")
    if rules.get("active") is not True or rules.get("closed") is not False or rules.get("accepting_orders") is not True:
        execution.append("market_not_open")
    rate, exponent = number(fees.get("rate")), number(fees.get("exponent"))
    model = fees.get("model")
    if fees.get("known") is not True or rate is None or not 0 <= rate <= 1 or exponent != 1:
        execution.append("fee_unknown_or_unsupported")
    if model not in KNOWN_FEE_MODELS:
        execution.append("fee_model_identity_unverified")
    if model == "explicit_fee_free" and rate != 0:
        execution.append("fee_model_conflict")
    minimum, tick = number(rules.get("min_order_size")), number(rules.get("tick_size"))
    if minimum is None or minimum <= 0:
        execution.append("minimum_order_size_unknown")
    if tick is None or not 0 < tick < 1:
        execution.append("tick_size_unknown")
    if not rules.get("rules_sha256"):
        unverified.append("rules_source_hash_missing")
    min_notional = number(rules.get("min_order_notional"))
    size_step = number(rules.get("order_size_step"))
    if min_notional is None or min_notional < 0:
        min_notional = None
        unverified.append("minimum_notional_unknown")
    if size_step is None or size_step <= 0:
        size_step = None
        unverified.append("order_size_step_unknown")
    # Capture currently labels its fee amount as a cash-equivalent assumption.
    if fees.get("asset") not in ("USDC", "USD", "pUSD"):
        unverified.append("fee_debit_asset_unverified")
    books = {}
    for side in ("up", "down"):
        book = _dict(raw.get(side))
        detail = _dict(micro.get("poly_" + side))
        bid, ask = number(book.get("bid")), number(book.get("ask"))
        bid_size, ask_size = number(book.get("bid_size")), number(book.get("ask_size"))
        if raw.get("poly_valid") is not True or not _fresh(book, now, policy.max_quote_age_ms):
            execution.append(side + "_book_stale")
        if bid is None or ask is None or not 0 < bid <= ask < 1:
            execution.append(side + "_book_price_invalid")
        if bid_size is None or ask_size is None or bid_size < 0 or ask_size < 0:
            execution.append(side + "_book_depth_unknown")
        book_min, book_tick = number(detail.get("min_order_size")), number(detail.get("tick_size"))
        detail_fresh = _fresh(detail, now, policy.max_quote_age_ms)
        confirmed_min = detail_fresh and book_min is not None and book_min > 0
        if detail_fresh:
            if book_min is not None and minimum is not None and book_min != minimum:
                # Gamma orderMinSize has an unverified unit; do not compare it
                # as if it were the CLOB book's explicit share quantity.
                unverified.append(side + "_book_market_minimum_numeric_difference")
            if book_tick is not None and tick is not None and not math.isclose(book_tick, tick, abs_tol=1e-12):
                execution.append(side + "_tick_size_conflict")
        else:
            unverified.append(side + "_book_rule_metadata_unverified")
        if not confirmed_min:
            unverified.append(side + "_minimum_order_unit_unknown")
        books[side] = {"bid": bid, "ask": ask, "bid_size": bid_size, "ask_size": ask_size,
                       "received_ms": integer(book.get("received_ms")),
                       "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
                       "min_order_size": book_min if confirmed_min else minimum, "tick_size": tick,
                       "min_order_size_unit": "shares" if confirmed_min else None,
                       "min_order_size_unit_source": "producer_clob_book_field" if confirmed_min else "market_orderMinSize_unit_unverified"}
    receipts = [books[side]["received_ms"] for side in ("up", "down")]
    if None in receipts or abs(receipts[0] - receipts[1]) > policy.max_quote_skew_ms:
        execution.append("pair_quote_skew")
    both_minimums_verified = all(books[side]["min_order_size_unit"] == "shares" for side in ("up", "down"))
    return {"raw": raw, "sample_ms": ms, "slug": slug, "window_start_ms": start,
            "window_end_ms": start + WINDOW_MS if start is not None else None,
            "condition_id": condition_id, "up_token_id": up_token, "down_token_id": down_token,
            "up": books["up"], "down": books["down"], "books": books,
            "rules": {"tick_size": tick, "min_order_size": minimum,
                      "min_order_notional": min_notional, "order_size_step": size_step,
                      "metadata_received_ms": metadata_ms, "reference_kind": rules.get("reference_kind")},
            "fees": {"rate": rate, "exponent": exponent, "model": model,
                     "asset": fees.get("asset"), "known": fees.get("known") is True},
            "min_order_size": max(books[side]["min_order_size"] for side in ("up", "down")) if both_minimums_verified else minimum,
            "min_order_size_unit": "shares" if both_minimums_verified else None,
            "min_order_size_unit_source": "producer_clob_book_field" if both_minimums_verified else "market_orderMinSize_unit_unverified",
            "quality": {"base_integrity": _layer(base), "execution_quality": _layer(execution)},
            "execution_metadata_verified": not unverified and not execution and not base,
            "execution_metadata_unverified": sorted(set(unverified)),
            "public_quote_proxy_only": True}


class CausalQualityEngine:
    """Compute features using only current/past received observations, never labels."""

    def __init__(self, policy=None, required_features=DEFAULT_FEATURES):
        self.policy = policy or QualityPolicy()
        self.required_features = tuple(required_features)
        self.history = deque()
        self.last_ms = None

    def update(self, raw):
        result = normalize_snapshot(raw, self.policy)
        ms = result["sample_ms"]
        if ms is None:
            raise ValueError("Causal engine requires a valid sample time")
        if self.last_ms is not None and ms <= self.last_ms:
            raise ValueError("Causal engine requires strictly increasing sample times")
        if self.last_ms is not None and (ms - self.last_ms > self.policy.max_sample_gap_ms
                                        or ms // 1000 - self.last_ms // 1000 > 1):
            self.history.clear()
        self.last_ms = ms
        micro = _dict(raw.get("microstructure"))
        flags = _dict(micro.get("valid"))
        micro_ok = result["quality"]["base_integrity"]["accepted"]
        source = {}
        for name, obj, valid, event in (
            ("spot", raw.get("binance"), raw.get("binance_valid"), True),
            ("chainlink", raw.get("chainlink"), raw.get("chainlink_valid"), True),
            ("coinbase", micro.get("coinbase_ticker"), flags.get("coinbase"), True),
            ("twap", micro.get("twap60"), flags.get("twap60"), True),
        ):
            obj = _dict(obj)
            value = number(obj.get("price"))
            if value is None:
                value = number(_dict(obj.get("payload")).get("price"))
            source[name] = value if (micro_ok and valid is True and value is not None and value > 0
                and _fresh(obj, ms, self.policy.max_source_age_ms, event)) else None
        quote_ok = result["quality"]["execution_quality"]["accepted"] and micro_ok
        current = {"ms": ms, "slug": result["slug"], **source,
                   "up_mid": result["up"]["mid"] if quote_ok else None}
        features = {}
        for horizon in (5, 30, 60):
            target = ms - horizon * 1000
            old = next((row for row in reversed(self.history) if row["ms"] <= target), None)
            if old is not None and target - old["ms"] > self.policy.max_lookback_jitter_ms:
                old = None
            segment = [row for row in self.history if old is not None and row["ms"] >= old["ms"]] + [current]
            for name in ("spot", "chainlink", "coinbase", "twap"):
                good = old is not None and segment and all(row[name] is not None for row in segment)
                features[f"{name}_return_{horizon}s_bps"] = (
                    10_000 * (current[name] / old[name] - 1) if good else None)
            same_market = old is not None and all(row["slug"] == current["slug"] for row in segment)
            good_mid = same_market and all(row["up_mid"] is not None for row in segment)
            features[f"up_mid_change_{horizon}s"] = current["up_mid"] - old["up_mid"] if good_mid else None
        features["spot_price"] = source["spot"]
        features["coinbase_price"] = source["coinbase"]
        features["up_mid"] = current["up_mid"]
        features["up_spread"] = (result["up"]["ask"] - result["up"]["bid"]) if quote_ok else None
        features["down_spread"] = (result["down"]["ask"] - result["down"]["bid"]) if quote_ok else None
        for name in ("spot", "perp", "coinbase"):
            flow = _dict(micro.get(name + "_flow"))
            received = number(flow.get("last_trade_received_ms"))
            for horizon in (5, 30):
                value = _dict(flow.get(str(horizon) + "s"))
                imbalance = number(value.get("imbalance"))
                okay = (micro_ok and flow.get("connected") is True and value.get("warmed") is True
                        and value.get("recent_id_gap_or_overflow") is False and received is not None
                        and 0 <= ms - received <= self.policy.max_source_age_ms
                        and imbalance is not None and -1 <= imbalance <= 1)
                features[f"{name}_flow_{horizon}s"] = imbalance if okay else None
        # Only fields with an independently checked source clock are exposed.
        missing = ["feature_unavailable:" + name for name in self.required_features if features.get(name) is None]
        result["quality"]["candidate_feature_quality"] = _layer(missing)
        result["features"] = features
        result["signal_cutoff_ms"] = ms
        result["eligible"] = all(layer["accepted"] for layer in result["quality"].values())
        result["decision_reasons"] = sorted({reason for layer in result["quality"].values() for reason in layer["reasons"]})
        result["quality"]["decision_eligible"] = result["eligible"]
        result["decision_uses_future_quality"] = False
        self.history.append(current)
        while self.history and self.history[0]["ms"] < ms - 62_000:
            self.history.popleft()
        return result


def build_quality_dataset(rows, policy=None, required_features=DEFAULT_FEATURES):
    policy = policy or QualityPolicy()
    engine = CausalQualityEngine(policy, required_features)
    decisions = [engine.update(row) for row in rows]
    by_window = defaultdict(list)
    counters = {name: Counter() for name in ("base_integrity", "execution_quality", "candidate_feature_quality")}
    accepted = Counter()
    unverified = Counter()
    for decision in decisions:
        by_window[decision["slug"]].append(decision)
        for name in counters:
            layer = decision["quality"][name]
            counters[name].update(layer["reasons"])
            accepted[name] += int(layer["accepted"])
        unverified.update(decision["execution_metadata_unverified"])
    windows, gold_segments = [], []
    lookback_ms = max([0] + [int(match.group(1)) * 1000 for name in required_features
                            for match in [re.search(r"_(\d+)s(?:_|$)", name)] if match])
    for slug, group in sorted(by_window.items(), key=lambda item: item[1][0]["sample_ms"]):
        start = group[0]["window_start_ms"]
        seconds, bad_seconds, candidate_bad_seconds = set(), set(), set()
        reasons = []
        for row in group:
            second = row["sample_ms"] // 1000
            if start is not None and start <= row["sample_ms"] < start + WINDOW_MS:
                seconds.add(second)
                if not row["quality"]["base_integrity"]["accepted"] or not row["quality"]["execution_quality"]["accepted"]:
                    bad_seconds.add(second)
                if not row["quality"]["candidate_feature_quality"]["accepted"]:
                    candidate_bad_seconds.add(second)
        good_fraction = len(seconds - bad_seconds) / len(seconds) if seconds else 0.0
        mature = [row for row in group if start is not None and row["sample_ms"] >= start + lookback_ms]
        candidate_fraction = (sum(row["quality"]["candidate_feature_quality"]["accepted"] for row in mature)
                              / len(mature)) if mature else 0.0
        gaps = [b["sample_ms"] - a["sample_ms"] for a, b in zip(group, group[1:])]
        good_seconds = sorted(seconds - bad_seconds - candidate_bad_seconds)
        runs = []
        for second in good_seconds:
            if not runs or second != runs[-1][-1] + 1:
                runs.append([second])
            else:
                runs[-1].append(second)
        segments = []
        for run in runs:
            if len(run) >= policy.gold_min_contiguous_seconds:
                segment = {"segment_id": f"{slug}:{run[0]}:{run[-1] + 1}", "slug": slug,
                           "start_ms": run[0] * 1000, "end_ms_exclusive": (run[-1] + 1) * 1000,
                           "observed_seconds": len(run), "all_seconds_jointly_valid": True}
                segments.append(segment)
                gold_segments.append(segment)
        if not segments:
            reasons.append("no_gold_contiguous_segment")
        for row in group:
            segment = next((item for item in segments if item["start_ms"] <= row["sample_ms"] < item["end_ms_exclusive"]), None)
            row["training_gold"] = segment is not None and row["eligible"]
            row["gold_segment_id"] = segment["segment_id"] if segment else None
        windows.append({"slug": slug, "window_start_ms": start,
                        "window_end_ms": start + WINDOW_MS if start is not None else None,
                        "observed_seconds": len(seconds), "joint_good_seconds": len(seconds - bad_seconds),
                        "joint_good_fraction": good_fraction, "decision_ready_rows": sum(row["eligible"] for row in group),
                        "candidate_good_fraction_after_warmup": candidate_fraction,
                        "complete_300_seconds": len(seconds) == 300,
                        "longest_sample_interval_ms": max(gaps, default=0),
                        "gold_segment_count": len(segments),
                        "gold_training_seconds": sum(segment["observed_seconds"] for segment in segments),
                        "gold_for_training": not reasons, "gold_reasons": reasons})
    gold = [row["slug"] for row in windows if row["gold_for_training"]]
    return {"schema_version": 1, "policy": asdict(policy), "required_features": list(required_features),
            "decisions": decisions, "windows": windows, "gold_window_ids": gold, "gold_segments": gold_segments,
            "funnel": {"input_rows": len(decisions), "input_windows": len(windows),
                       "gold_training_windows": len(gold), "decision_eligible_rows": sum(row["eligible"] for row in decisions),
                       "gold_training_segments": len(gold_segments),
                       "gold_training_rows": sum(row["training_gold"] for row in decisions),
                       "incomplete_windows_with_gold_segments": sum(not row["complete_300_seconds"] and row["gold_for_training"] for row in windows),
                       "layer_accepted_rows": dict(accepted),
                       "layer_rejections": {name: dict(count) for name, count in counters.items()},
                       "execution_metadata_unverified": dict(unverified),
                       "window_exclusions": dict(Counter(reason for row in windows for reason in row["gold_reasons"]))},
            "selection_rule": "gold_segments_for_training_only; evaluation_keeps_all_decisions"}


def gold_training_rows(dataset, cutoff_ms, labels=None):
    """Training rows from past gold segments; optional labels must be known at fit."""
    gold = set(dataset["gold_window_ids"])
    if labels is not None:
        if not isinstance(labels, dict):
            raise TypeError("labels must map slug to an independently verified label")
        eligible_labels = set()
        for slug, label in labels.items():
            available = integer(label.get("label_available_at_ms", label.get("label_available_time_ms")))
            if available is not None and available <= cutoff_ms:
                eligible_labels.add(slug)
        gold &= eligible_labels
    return [row for row in dataset["decisions"] if row["slug"] in gold and row["training_gold"]
            and row["window_end_ms"] <= cutoff_ms and row["sample_ms"] < cutoff_ms]


def chronological_split(dataset, train_end_ms, validation_end_ms, test_end_ms=None, purge_ms=60_000):
    """Whole-window chronological split, with an embargo after each boundary.

No test/validation window is removed because later data is missing. Feature
lookbacks must not exceed ``purge_ms``; training label availability still needs
the explicit check in ``gold_training_rows`` or the caller's target builder.
"""
    if not 0 <= train_end_ms < validation_end_ms or purge_ms < 0:
        raise ValueError("Invalid chronological split boundaries")
    if test_end_ms is not None and test_end_ms <= validation_end_ms:
        raise ValueError("test_end_ms must follow validation_end_ms")
    split = {"train": [], "validation": [], "test": [], "purged": []}
    gold = set(dataset["gold_window_ids"])
    assignments = {}
    for window in dataset["windows"]:
        start, end = window["window_start_ms"], window["window_end_ms"]
        target = "purged"
        if start is not None and end <= train_end_ms and window["slug"] in gold:
            target = "train"
        elif start is not None and start >= train_end_ms + purge_ms and end <= validation_end_ms:
            target = "validation"
        elif start is not None and start >= validation_end_ms + purge_ms and (test_end_ms is None or end <= test_end_ms):
            target = "test"
        assignments[window["slug"]] = target
    for row in dataset["decisions"]:
        target = assignments[row["slug"]]
        if target == "train" and not row["training_gold"]:
            target = "purged"
        split[target].append(row)
    split["boundaries"] = {"train_end_ms": train_end_ms, "validation_end_ms": validation_end_ms,
                            "test_end_ms": test_end_ms, "purge_ms": purge_ms}
    split["window_assignments"] = assignments
    return split
