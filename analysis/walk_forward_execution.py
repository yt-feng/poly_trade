"""Leakage-safe event-clustered walk-forward evaluation over v3 books.

This module is an offline research adapter.  It never imports a trading client,
wallet, account API, or network library.  Feature observations and resolution
labels are separate files: labels are joined only after their declared
availability time, and a market/event cluster cannot occur in both train and
test.  The simulator consumes displayed ask/bid levels and depth; midpoint
fills are rejected because they are not executable evidence.

The evaluator is deliberately conservative.  Missing data, unknown fee models,
off-tick prices, stale books, and insufficient folds produce a blocked report
instead of a fabricated score or profitability claim.

The command-line entrypoint also requires the pre-registration gate from
``analysis/preregistered_strategy.py``.  Direct helper calls such as
``evaluate`` remain low-level test adapters; a replay intended for research
must use the CLI with the exact manifest, data hashes, code commit and UTC
evaluation dates.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, InvalidOperation
import json
from pathlib import Path
from typing import Any, Iterable

try:
    from v3_data_contract import load_jsonl, validate_records
    from preregistered_strategy import audit_manifest, file_sha256
except ImportError:  # pragma: no cover - package/import-mode convenience
    from analysis.v3_data_contract import load_jsonl, validate_records
    from analysis.preregistered_strategy import audit_manifest, file_sha256


LABEL_SCHEMA_VERSION = 1
LABEL_KEYS = {
    "label_version", "market_id", "condition_id", "outcome", "resolved_time_ms",
    "label_available_time_ms", "source", "source_sha256",
}
MAX_BOOK_AGE_MS = 1_500
FEE_QUANTUM = Decimal("0.00001")
OFFICIAL_FEES_URL = "https://docs.polymarket.com/trading/fees"
OFFICIAL_ORDER_URL = "https://docs.polymarket.com/trading/place-orders"


class EvaluationBlocked(ValueError):
    """Raised when an input cannot support an honest evaluation."""


@dataclass(frozen=True)
class WalkForwardConfig:
    train_duration_ms: int = 3_600_000
    test_duration_ms: int = 900_000
    purge_ms: int = 5 * 60_000
    embargo_ms: int = 1_000
    latency_ms: int = 1_000
    order_ttl_ms: int = 5_000
    order_size: Decimal = Decimal("5")
    edge_buffer: Decimal = Decimal("0")
    min_training_events: int = 10
    calibration_bins: int = 10

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "WalkForwardConfig":
        raw = raw or {}
        integer_fields = (
            "train_duration_ms", "test_duration_ms", "purge_ms", "embargo_ms",
            "latency_ms", "order_ttl_ms", "min_training_events", "calibration_bins",
        )
        values: dict[str, Any] = {}
        for field in integer_fields:
            value = raw.get(field, getattr(cls, field))
            if type(value) is not int or value < 0:
                raise EvaluationBlocked(f"invalid_config:{field}")
            values[field] = value
        for field in ("order_size", "edge_buffer"):
            try:
                value = Decimal(str(raw.get(field, getattr(cls, field))))
            except (InvalidOperation, ValueError):
                raise EvaluationBlocked(f"invalid_config:{field}") from None
            if not value.is_finite() or value < 0:
                raise EvaluationBlocked(f"invalid_config:{field}")
            values[field] = value
        if values["train_duration_ms"] <= 0 or values["test_duration_ms"] <= 0:
            raise EvaluationBlocked("invalid_config:nonpositive_split_duration")
        if values["order_size"] <= 0 or values["calibration_bins"] <= 0:
            raise EvaluationBlocked("invalid_config:nonpositive_order_or_bins")
        return cls(**values)


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise EvaluationBlocked(f"invalid_decimal:{value!r}") from None
    if not result.is_finite():
        raise EvaluationBlocked(f"nonfinite_decimal:{value!r}")
    return result


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _is_on_tick(price: Decimal, tick: Decimal) -> bool:
    if tick <= 0 or not (Decimal("0") < price < Decimal("1")):
        return False
    quotient = price / tick
    return quotient == quotient.to_integral_value()


def fee_usdc(shares: Decimal, price: Decimal, fee_rate: Decimal, exponent: Decimal) -> Decimal:
    """Official taker fee formula, conservatively rounded to 5 decimals.

    Current Polymarket fee metadata uses exponent=1 for the documented
    `C * feeRate * p * (1-p)` formula.  Unknown exponents fail closed rather
    than being silently generalized.
    """
    if exponent != Decimal("1"):
        raise EvaluationBlocked("unsupported_fee_exponent")
    if shares <= 0 or not Decimal("0") < price < Decimal("1") or not Decimal("0") <= fee_rate <= Decimal("1"):
        raise EvaluationBlocked("invalid_fee_inputs")
    raw = shares * fee_rate * price * (Decimal("1") - price)
    return raw.quantize(FEE_QUANTUM, rounding=ROUND_CEILING)


def _best_levels(record: dict[str, Any], token: str, side: str) -> list[tuple[Decimal, Decimal]]:
    books = record.get("books") or {}
    book = books.get(token) or {}
    levels = book.get("asks" if side == "buy" else "bids") or []
    result = []
    tick = _decimal((record.get("rules") or {}).get("tick_size"))
    for item in levels:
        price, size = _decimal(item.get("price")), _decimal(item.get("size"))
        if size < 0 or not _is_on_tick(price, tick):
            raise EvaluationBlocked("off_tick_or_invalid_book_level")
        if size > 0:
            result.append((price, size))
    if not result:
        raise EvaluationBlocked("missing_executable_book_side")
    return sorted(result, key=lambda level: level[0], reverse=side == "sell")


def _fee_metadata(record: dict[str, Any]) -> tuple[Decimal, Decimal, str]:
    fees = record.get("fees") or {}
    rate = _decimal(fees.get("rate"))
    exponent = _decimal(fees.get("exponent"))
    asset = fees.get("asset")
    if not isinstance(asset, str) or not asset.strip():
        raise EvaluationBlocked("missing_fee_asset")
    if asset.upper() != "USDC":
        raise EvaluationBlocked("unsupported_fee_asset")
    return rate, exponent, asset


def simulate_order(
    decision: dict[str, Any],
    market_records: Iterable[dict[str, Any]],
    *,
    token: str,
    side: str,
    quantity: Decimal | str | int,
    latency_ms: int,
    ttl_ms: int,
    price_source: str = "book_touch",
    limit_price: Decimal | str | None = None,
) -> dict[str, Any]:
    """Simulate one passive/marketable order against later displayed depth.

    Only fresh source events at/after order activation can witness a fill.
    Missing coverage is unknown. Partial fills cancel the remainder in this
    simulation; no queue position or hidden liquidity is invented.
    ``price_source=mid`` is rejected unconditionally.
    """
    if price_source in {"mid", "midpoint", "mid_price"}:
        raise EvaluationBlocked("MID_PRICE_NOT_EXECUTABLE")
    if side not in {"buy", "sell"} or token not in {"up", "down"}:
        raise EvaluationBlocked("invalid_order_side_or_token")
    if type(latency_ms) is not int or latency_ms < 0 or type(ttl_ms) is not int or ttl_ms <= 0:
        raise EvaluationBlocked("invalid_order_timing")
    requested = _decimal(quantity)
    if requested <= 0:
        raise EvaluationBlocked("invalid_order_quantity")
    decision_receive = decision.get("received_time_ms")
    if not _positive_int(decision_receive):
        raise EvaluationBlocked("invalid_decision_receive_time")
    rules = decision.get("rules") or {}
    minimum = _decimal(rules.get("min_order_size"))
    tick = _decimal(rules.get("tick_size"))
    if requested < minimum:
        return {
            "status": "rejected_min_order_size", "requested_shares": str(requested),
            "filled_shares": "0", "remaining_shares": str(requested),
            "latency_ms": latency_ms, "ttl_ms": ttl_ms,
        }
    decision_fee = _fee_metadata(decision)
    active_at = decision_receive + latency_ms
    expires_at = decision_receive + ttl_ms
    def unknown(reason: str) -> dict[str, Any]:
        return {"status": "unknown_execution", "reason": reason,
                "requested_shares": str(requested), "filled_shares": None,
                "remaining_shares": None, "active_at_ms": active_at,
                "expires_at_ms": expires_at}

    decision_event = decision.get("source_event_time_ms")
    if (not _positive_int(decision_event) or decision_event > decision_receive
            or decision_receive - decision_event > MAX_BOOK_AGE_MS):
        return unknown("stale_or_invalid_decision_time")
    candidates = sorted(
        (row for row in market_records
         if _positive_int(row.get("received_time_ms"))
         and active_at <= row["received_time_ms"] <= expires_at),
        key=lambda row: row["received_time_ms"],
    )
    if not candidates:
        return unknown("missing_post_order_feed")
    limit = _decimal(limit_price) if limit_price is not None else None
    if limit is not None and not _is_on_tick(limit, tick):
        raise EvaluationBlocked("limit_price_off_tick")
    last_receive = active_at
    fill_record = None
    for candidate in candidates:
        if (candidate.get("market_id") != decision.get("market_id")
                or candidate.get("condition_id") != decision.get("condition_id")
                or candidate.get("token_ids") != decision.get("token_ids")):
            raise EvaluationBlocked("market_cluster_mismatch")
        event = candidate.get("source_event_time_ms")
        received = candidate["received_time_ms"]
        if not _positive_int(event) or event > received:
            return unknown("invalid_post_order_source_time")
        if received - event > MAX_BOOK_AGE_MS or received - last_receive > MAX_BOOK_AGE_MS:
            return unknown("stale_or_missing_post_order_feed")
        last_receive = received
        # A late-delivered pre-order event cannot witness a post-order fill.
        if event < active_at:
            continue
        if candidate.get("rules") != decision.get("rules"):
            return unknown("rules_changed")
        fill_fee = _fee_metadata(candidate)
        if fill_fee != decision_fee:
            return unknown("fee_metadata_changed")
        try:
            levels = _best_levels(candidate, token, side)
        except EvaluationBlocked as exc:
            if str(exc) == "missing_executable_book_side":
                return unknown("missing_positive_depth")
            raise
        if limit is not None:
            opposite = _best_levels(candidate, token, "sell" if side == "buy" else "buy")
            midpoint = (levels[0][0] + opposite[0][0]) / Decimal("2")
            if limit == midpoint:
                raise EvaluationBlocked("MID_PRICE_NOT_EXECUTABLE")
            levels = [(price, size) for price, size in levels
                      if (price <= limit if side == "buy" else price >= limit)]
        if levels:
            fill_record = candidate
            break
    if fill_record is None:
        # Only a causal, fresh observation at TTL closes the simulated interval.
        # Otherwise absence of a quote is missing evidence, not a zero fill.
        if (candidates[-1]["received_time_ms"] < expires_at
                or candidates[-1]["source_event_time_ms"] < expires_at):
            return unknown("incomplete_feed_through_expiry")
        return {"status": "expired_unfilled", "requested_shares": str(requested),
                "filled_shares": "0", "remaining_shares": str(requested),
                "active_at_ms": active_at, "expires_at_ms": expires_at,
                "expiry_evidence": "continuous_fresh_public_books_simulation_only"}
    remaining, filled, notional = requested, Decimal("0"), Decimal("0")
    fills = []
    for price, size in levels:
        if remaining <= 0:
            break
        amount = min(remaining, size)
        if amount <= 0:
            continue
        fills.append({"price": str(price), "shares": str(amount)})
        filled += amount
        notional += amount * price
        remaining -= amount
    if filled <= 0:
        status = "expired_unfilled"
    elif remaining > 0:
        status = "partial_canceled"
    else:
        status = "filled"
    average = notional / filled if filled else None
    fee = fee_usdc(filled, average, fill_fee[0], fill_fee[1]) if average else Decimal("0")
    return {
        "status": status, "requested_shares": str(requested), "filled_shares": str(filled),
        "remaining_shares": str(remaining), "active_at_ms": active_at,
        "expires_at_ms": expires_at, "fill_received_time_ms": fill_record["received_time_ms"],
        "fill_source_event_time_ms": fill_record["source_event_time_ms"],
        "average_price": str(average) if average is not None else None,
        "notional_usdc": str(notional), "fee_usdc": str(fee), "fills": fills,
        "price_source": price_source, "side": side, "token": token,
    }


def load_labels(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load labels from a separate file; labels are never accepted in v3 rows."""
    if not path.exists():
        return {}, {"input_records": 0, "accepted_records": 0, "quarantined_records": 0,
                    "reason_counts": {"missing_label_file": 1}}
    return validate_labels(load_jsonl(path))


def validate_labels(records: Iterable[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Validate every label; callers must block if any row is quarantined."""
    accepted: dict[str, dict[str, Any]] = {}
    reasons = Counter()
    total = 0
    for record in records:
        total += 1
        if not isinstance(record, dict) or set(record) != LABEL_KEYS:
            reasons["unknown_or_missing_label_fields"] += 1
            continue
        if record.get("label_version") != LABEL_SCHEMA_VERSION:
            reasons["label_schema_version_not_1"] += 1
            continue
        if record.get("outcome") not in {"up", "down"}:
            reasons["invalid_label_outcome"] += 1
            continue
        if not all(_positive_int(record.get(key)) for key in ("resolved_time_ms", "label_available_time_ms")):
            reasons["invalid_label_times"] += 1
            continue
        if record["label_available_time_ms"] < record["resolved_time_ms"]:
            reasons["label_available_before_resolution"] += 1
            continue
        if not all(isinstance(record.get(key), str) and record[key].strip() for key in ("market_id", "condition_id", "source")):
            reasons["invalid_label_identity"] += 1
            continue
        if (not isinstance(record.get("source_sha256"), str)
                or not all(char in "0123456789abcdef" for char in record["source_sha256"])
                or len(record["source_sha256"]) != 64):
            reasons["invalid_label_source_hash"] += 1
            continue
        market = record["market_id"]
        if market in accepted:
            reasons["duplicate_label_market"] += 1
            continue
        accepted[market] = record
    return accepted, {"input_records": total, "accepted_records": len(accepted),
                      "quarantined_records": total - len(accepted), "reason_counts": dict(sorted(reasons.items()))}


def _observation_features(record: dict[str, Any]) -> dict[str, Any]:
    """Extract only as-of book fields; no label or future field is read."""
    if record.get("observation_type") != "book_snapshot":
        raise EvaluationBlocked("only_book_snapshots_are_executable")
    books = record.get("books") or {}
    out: dict[str, Any] = {"market_id": record["market_id"], "condition_id": record["condition_id"],
                           "source_event_time_ms": record["source_event_time_ms"],
                           "received_time_ms": record["received_time_ms"], "rules": record["rules"],
                           "fees": record["fees"], "books": books, "token_ids": record["token_ids"],
                           "observation_id": record["observation_id"]}
    for token in ("up", "down"):
        asks = _best_levels(record, token, "buy")
        bids = _best_levels(record, token, "sell")
        out[f"{token}_ask"] = asks[0][0]
        out[f"{token}_ask_size"] = asks[0][1]
        out[f"{token}_bid"] = bids[0][0]
        out[f"{token}_bid_size"] = bids[0][1]
    return out


def _coerce_feature(record: dict[str, Any]) -> dict[str, Any]:
    """Accept either a raw validated v3 row or an already extracted feature."""
    if all(key in record for key in ("up_ask", "up_bid", "down_ask", "down_bid")):
        return record
    return _observation_features(record)


def feature_key(feature: dict[str, Any]) -> str:
    """Stable causal bucket used by the small reference model."""
    tick = _decimal((feature.get("rules") or {}).get("tick_size"))
    up_ticks = int((feature["up_ask"] / tick).to_integral_value())
    down_ticks = int((feature["down_ask"] / tick).to_integral_value())
    total = feature["up_bid_size"] + feature["up_ask_size"]
    imbalance = (feature["up_bid_size"] - feature["up_ask_size"]) / total if total else Decimal("0")
    bucket = "neg" if imbalance < Decimal("-0.2") else "pos" if imbalance > Decimal("0.2") else "mid"
    return f"{up_ticks}:{down_ticks}:{bucket}"


def fit_model(
    observations: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    *,
    train_market_ids: set[str],
    cutoff_ms: int,
    min_training_events: int,
) -> dict[str, Any]:
    """Fit a smoothed empirical model using only rows and labels before cutoff."""
    if not _positive_int(cutoff_ms):
        raise EvaluationBlocked("invalid_training_cutoff")
    latest: dict[str, dict[str, Any]] = {}
    for record in observations:
        if record["market_id"] not in train_market_ids:
            continue
        if record["received_time_ms"] >= cutoff_ms:
            continue
        label = labels.get(record["market_id"])
        if (not label or label["condition_id"] != record["condition_id"]
                or label["label_available_time_ms"] > cutoff_ms):
            continue
        feature = _coerce_feature(record)
        prior = latest.get(record["market_id"])
        if prior is None or feature["received_time_ms"] > prior["received_time_ms"]:
            latest[record["market_id"]] = feature
    if len(latest) < min_training_events:
        raise EvaluationBlocked("insufficient_pre_cutoff_training_events")
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for market, feature in latest.items():
        counts[feature_key(feature)][0] += 1
        counts[feature_key(feature)][1] += labels[market]["outcome"] == "up"
    total = len(latest)
    wins = sum(pair[1] for pair in counts.values())
    return {
        "counts": {key: [int(value[0]), int(value[1])] for key, value in counts.items()},
        "global_probability_up": (wins + 1) / (total + 2),
        "training_events": total,
        "training_market_ids": sorted(latest),
        "training_observation_ids": sorted(feature["observation_id"] for feature in latest.values()),
        "cutoff_ms": cutoff_ms,
    }


def predict(model: dict[str, Any], feature: dict[str, Any]) -> float:
    key = feature_key(feature)
    count, wins = model["counts"].get(key, [0, 0])
    return (wins + 1) / (count + 2) if count else float(model["global_probability_up"])


def chronological_folds(observations: list[dict[str, Any]], config: WalkForwardConfig) -> list[dict[str, Any]]:
    clusters: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in observations:
        clusters[record["market_id"]].append(record)
    ordered = sorted(
        ((market, min(row["source_event_time_ms"] for row in rows)) for market, rows in clusters.items()),
        key=lambda item: item[1],
    )
    if not ordered:
        return []
    start, end = ordered[0][1], max(event_start for _, event_start in ordered)
    folds = []
    train_end = start + config.train_duration_ms
    while train_end <= end:
        test_start = train_end + config.purge_ms + config.embargo_ms
        test_end = test_start + config.test_duration_ms
        train_ids = {market for market, event_start in ordered if start <= event_start < train_end}
        test_ids = {market for market, event_start in ordered if test_start <= event_start < test_end}
        if train_ids and test_ids and train_ids.isdisjoint(test_ids):
            folds.append({"fold": len(folds) + 1, "train_start_ms": start,
                          "train_end_ms": train_end, "purge_start_ms": train_end,
                          "purge_end_ms": train_end + config.purge_ms,
                          "embargo_start_ms": train_end + config.purge_ms,
                          "embargo_end_ms": test_start, "test_start_ms": test_start,
                          "test_end_ms": test_end, "train_market_ids": sorted(train_ids),
                          "test_market_ids": sorted(test_ids)})
        train_end = test_end
    return folds


def brier_score(predictions: list[dict[str, Any]]) -> float | None:
    if not predictions:
        return None
    return sum((float(row["predicted_prob_up"]) - float(row["actual_up"])) ** 2 for row in predictions) / len(predictions)


def expected_calibration_error(predictions: list[dict[str, Any]], bins: int = 10) -> float | None:
    if not predictions:
        return None
    buckets: list[list[dict[str, Any]]] = [[] for _ in range(bins)]
    for row in predictions:
        probability = min(1.0, max(0.0, float(row["predicted_prob_up"])))
        buckets[min(bins - 1, int(probability * bins))].append(row)
    total = len(predictions)
    error = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = sum(float(row["predicted_prob_up"]) for row in bucket) / len(bucket)
        frequency = sum(float(row["actual_up"]) for row in bucket) / len(bucket)
        error += len(bucket) / total * abs(confidence - frequency)
    return error


def _empty_metrics() -> dict[str, Any]:
    return {
        "predictions": 0, "brier": None, "ece": None, "attempts": 0,
        "filled_attempts": 0, "partial_fills": 0, "expired_orders": 0, "unknown_orders": 0,
        "gross_pnl_usdc": None, "fees_usdc": None, "net_pnl_usdc": None,
        "net_return_on_filled_cost": None,
    }


def evaluate(
    observations: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    config: WalkForwardConfig,
) -> dict[str, Any]:
    """Run blocked-safe walk-forward evaluation and return a public report."""
    # Imported lazily because intake/split reuse the label loader in this module.
    try:
        from walk_forward_intake import audit_record_relationships
        from walk_forward_split_audit import audit_execution_folds
    except ImportError:
        from analysis.walk_forward_intake import audit_record_relationships
        from analysis.walk_forward_split_audit import audit_execution_folds
    validation = validate_records(observations)
    checked_labels, label_validation = validate_labels(labels.values())
    blockers = []
    if validation["quarantined_records"]:
        blockers.append("quarantined_observation_records")
    if label_validation["quarantined_records"]:
        blockers.append("quarantined_label_records")
    if any(key != value.get("market_id") for key, value in labels.items()):
        blockers.append("label_mapping_identity_mismatch")
    if blockers:
        report = blocked_report(blockers, config=config)
        report["input"] = {"observation_records": len(observations), "label_records": len(labels),
                           "v3_validation": {k: v for k, v in validation.items() if k != "accepted"},
                           "labels_validation": label_validation}
        return report
    observations = [row for row in observations if row["observation_type"] == "book_snapshot"]
    relationships = audit_record_relationships(observations, checked_labels)
    if relationships["blocked_reasons"]:
        report = blocked_report(relationships["blocked_reasons"], config=config)
        report["input_audit"] = relationships
        return report
    validation_reasons = Counter()
    accepted: list[dict[str, Any]] = []
    for record in observations:
        try:
            accepted.append(_coerce_feature(record))
        except EvaluationBlocked as exc:
            validation_reasons[str(exc)] += 1
    accepted.sort(key=lambda row: (row["source_event_time_ms"], row["received_time_ms"], row["observation_id"]))
    folds = chronological_folds(accepted, config)
    report: dict[str, Any] = {
        "schema_version": 1, "evaluator": "event_clustered_walk_forward_v1",
        "canary_blocked": True, "canary_allowed": False,
        "source_contract": {"observations": "validated_v3_books", "labels": "separate_v1_available_after_resolution"},
        "official_semantics": {"fees": OFFICIAL_FEES_URL, "orders": OFFICIAL_ORDER_URL,
                                "mid_price_fills": "rejected", "private_execution_evidence": False},
        "input": {"observation_records": len(observations), "accepted_book_records": len(accepted),
                  "label_records": len(labels), "quarantined_or_blocked_records": dict(sorted(validation_reasons.items()))},
        "split_policy": {"train_duration_ms": config.train_duration_ms, "test_duration_ms": config.test_duration_ms,
                          "purge_ms": config.purge_ms, "embargo_ms": config.embargo_ms,
                          "cluster_key": "market_id", "fit_cutoff_rule": "received_time_ms < train_end and label_available_time_ms <= train_end"},
        "folds": [], "metrics": _empty_metrics(),
        "blocked_reasons": [],
        "input_requirements": _input_requirements(None, None, config),
    }
    if validation_reasons:
        report["blocked_reasons"].extend(sorted(validation_reasons))
        report["blocked_reasons"].append("feature_extraction_failed_without_sample_shrink")
        return report
    report["input_audit"] = relationships
    split_audit = audit_execution_folds(observations, labels, folds)
    report["split_audit"] = split_audit
    if split_audit["blocked_reasons"]:
        report["blocked_reasons"].extend(split_audit["blocked_reasons"])
        return report
    if not accepted:
        report["blocked_reasons"].append("no_validated_book_observations")
        return report
    if not labels:
        report["blocked_reasons"].append("no_separate_resolution_labels")
        return report
    if not folds:
        report["blocked_reasons"].append("no_complete_chronological_folds")
        return report
    predictions: list[dict[str, Any]] = []
    execution_results: list[dict[str, Any]] = []
    by_market: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in accepted:
        by_market[row["market_id"]].append(row)
    for fold in folds:
        try:
            model = fit_model(accepted, labels, train_market_ids=set(fold["train_market_ids"]),
                              cutoff_ms=fold["train_end_ms"], min_training_events=config.min_training_events)
        except EvaluationBlocked as exc:
            fold["blocked_reason"] = str(exc)
            report["folds"].append(fold)
            report["blocked_reasons"].append(str(exc))
            return report
        fold["training_events"] = model["training_events"]
        used_test_markets: set[str] = set()
        for market in fold["test_market_ids"]:
            if market in used_test_markets or market not in labels:
                continue
            candidates = [row for row in by_market[market] if fold["test_start_ms"] <= row["source_event_time_ms"] < fold["test_end_ms"]]
            if not candidates:
                continue
            feature = min(candidates, key=lambda row: (row["received_time_ms"], row["observation_id"]))
            label = labels[market]
            if label["condition_id"] != feature["condition_id"]:
                fold.setdefault("blocked_reasons", []).append("label_condition_mismatch")
                continue
            if label["label_available_time_ms"] <= feature["received_time_ms"]:
                fold.setdefault("blocked_reasons", []).append("label_available_before_test_feature")
                continue
            probability = predict(model, feature)
            actual = 1 if label["outcome"] == "up" else 0
            row = {"fold": fold["fold"], "market_id": market, "observation_id": feature["observation_id"],
                   "predicted_prob_up": probability, "actual_up": actual,
                   "feature_received_time_ms": feature["received_time_ms"],
                   "label_available_time_ms": label["label_available_time_ms"]}
            predictions.append(row)
            used_test_markets.add(market)
            # Conservative reference policy: buy only when the predicted edge
            # clears the displayed ask plus a caller-specified buffer.
            up_edge = Decimal(str(probability)) - feature["up_ask"] - config.edge_buffer
            down_edge = Decimal("1") - Decimal(str(probability)) - feature["down_ask"] - config.edge_buffer
            if up_edge <= 0 and down_edge <= 0:
                row["trade"] = "none"
                continue
            token = "up" if up_edge >= down_edge else "down"
            try:
                order = simulate_order(feature, by_market[market], token=token, side="buy",
                                       quantity=config.order_size, latency_ms=config.latency_ms,
                                       ttl_ms=config.order_ttl_ms)
            except EvaluationBlocked as exc:
                report["blocked_reasons"].append(str(exc))
                return report
            row["trade"] = token
            row["order"] = order
            execution_results.append({"prediction": row, "label": label})
        report["folds"].append(fold)
    metrics = _empty_metrics()
    metrics["predictions"] = len(predictions)
    metrics["brier"] = brier_score(predictions)
    metrics["ece"] = expected_calibration_error(predictions, config.calibration_bins)
    metrics["attempts"] = len(execution_results)
    gross = fees = net = cost = Decimal("0")
    for episode in execution_results:
        order = episode["prediction"].get("order") or {}
        status = order.get("status")
        if status == "partial_canceled":
            metrics["partial_fills"] += 1
        if status == "expired_unfilled":
            metrics["expired_orders"] += 1
        if status == "unknown_execution" or str(status).startswith("blocked_"):
            metrics["unknown_orders"] += 1
            continue
        filled = _decimal(order.get("filled_shares", "0"))
        if filled <= 0 or not order.get("average_price"):
            continue
        metrics["filled_attempts"] += 1
        price = _decimal(order["average_price"])
        entry = _decimal(order["notional_usdc"])
        fee = _decimal(order["fee_usdc"])
        settlement = filled if episode["label"]["outcome"] == episode["prediction"]["trade"] else Decimal("0")
        gross += settlement - entry
        fees += fee
        net += settlement - entry - fee
        cost += entry + fee
    if metrics["unknown_orders"]:
        report["blocked_reasons"].append("unknown_execution_outcomes")
    if execution_results and not metrics["unknown_orders"]:
        metrics["gross_pnl_usdc"] = str(gross)
        metrics["fees_usdc"] = str(fees)
        metrics["net_pnl_usdc"] = str(net)
        metrics["net_return_on_filled_cost"] = str(net / cost) if cost else None
    report["metrics"] = metrics
    report["predictions"] = predictions
    if metrics["predictions"] == 0:
        report["blocked_reasons"].append("no_test_predictions")
    if metrics["filled_attempts"] == 0:
        report["blocked_reasons"].append("no_filled_simulated_orders")
    report["blocked_reasons"].append("private_execution_receipts_required_for_canary")
    return report


def _input_requirements(
    observations_path: Path | None,
    labels_path: Path | None,
    config: WalkForwardConfig | None,
) -> dict[str, Any]:
    """Describe the smallest safe input package without publishing local paths."""
    return {
        "observations": {
            "role": "canonical_v3_book_observations",
            "provided_name": observations_path.name if observations_path else None,
            "present": bool(observations_path and observations_path.exists()),
            "format": "JSONL",
            "minimum_record": {
                "observation_type": "book_snapshot",
                "required_fields": [
                    "observation_id", "source_event_time_ms", "received_time_ms",
                    "market_id", "condition_id", "token_ids", "books", "fees",
                    "rules", "provenance",
                ],
                "provenance_fields": ["source", "capture_id", "source_sha256", "retrieved_at_ms"],
                "execution_fields": [
                    "books.<token>.asks", "books.<token>.bids", "rules.tick_size",
                    "rules.min_order_size", "fees.rate", "fees.exponent", "fees.asset",
                ],
            },
            "historical_csv_or_midpoint_only": "insufficient",
        },
        "resolution_labels": {
            "role": "independent_resolution_labels",
            "provided_name": labels_path.name if labels_path else None,
            "present": bool(labels_path and labels_path.exists()),
            "format": "JSONL",
            "minimum_record": {
                "required_fields": sorted(LABEL_KEYS),
                "one_record_per": "market_id",
                "outcome_values": ["up", "down"],
                "availability_rule": "label_available_time_ms >= resolved_time_ms and is after the feature used for prediction",
            },
            "public_quote_or_unresolved_market": "insufficient",
        },
        "evaluation": {
            "complete_chronological_fold": True,
            "event_cluster_key": "market_id",
            "minimum_training_events_per_fold": config.min_training_events if config else None,
            "purge_ms": config.purge_ms if config else None,
            "embargo_ms": config.embargo_ms if config else None,
        },
        "canary_gate_context": {
            "minimum_independent_windows": 300,
            "minimum_independent_utc_dates": 7,
            "minimum_execution_evidence_records": 100,
            "enforced_by_this_public_replay": False,
            "private_receipts_required": True,
        },
        "canary_boundary": "These inputs can produce a public replay only; private order/fill/cancel/fee/settlement/account receipts remain required.",
    }


def blocked_report(
    reason: str | Iterable[str],
    *,
    config: WalkForwardConfig | None = None,
    observations_path: Path | None = None,
    labels_path: Path | None = None,
) -> dict[str, Any]:
    reasons = [reason] if isinstance(reason, str) else list(reason)
    report = {
        "schema_version": 1, "evaluator": "event_clustered_walk_forward_v1",
        "canary_blocked": True, "canary_allowed": False,
        "official_semantics": {"fees": OFFICIAL_FEES_URL, "orders": OFFICIAL_ORDER_URL,
                                "mid_price_fills": "rejected", "private_execution_evidence": False},
        "input": {"observation_records": 0, "accepted_book_records": 0, "label_records": 0},
        "split_policy": {"cluster_key": "market_id", "purge_ms": config.purge_ms if config else None,
                          "embargo_ms": config.embargo_ms if config else None,
                          "fit_cutoff_rule": "received_time_ms < train_end and label_available_time_ms <= train_end"},
        "folds": [], "metrics": _empty_metrics(), "blocked_reasons": reasons,
        "input_requirements": _input_requirements(observations_path, labels_path, config),
    }
    return report


def run_files(observations_path: Path, labels_path: Path, config: WalkForwardConfig) -> dict[str, Any]:
    missing = []
    if not observations_path.exists():
        missing.append("missing_observation_file")
    if not labels_path.exists():
        missing.append("missing_label_file")
    if missing:
        return blocked_report(missing, config=config, observations_path=observations_path, labels_path=labels_path)
    try:
        from walk_forward_intake import build_report as intake_report
    except ImportError:
        from analysis.walk_forward_intake import build_report as intake_report
    intake = intake_report(observations_path, labels_path)
    # Small historical research is allowed; promotion coverage is not waived.
    coverage_only = {"fewer_than_300_independent_windows", "fewer_than_7_independent_utc_dates"}
    blockers = [reason for reason in intake["blocked_reasons"] if reason not in coverage_only]
    if blockers:
        report = blocked_report(blockers, config=config, observations_path=observations_path, labels_path=labels_path)
    else:
        raw = load_jsonl(observations_path)
        labels, _ = load_labels(labels_path)
        report = evaluate(raw, labels, config)
    report["intake"] = intake
    report["input"]["observation_records"] = intake["observations"]["input_records"]
    report["input"]["label_records"] = intake["labels"]["input_records"]
    report["input_requirements"] = _input_requirements(observations_path, labels_path, config)
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--manifest", type=Path,
                        help="required pre-registration manifest; evaluation is blocked without it")
    parser.add_argument("--code-commit", help="commit recorded by the pre-registration manifest")
    parser.add_argument("--evaluation-dates", help="registered UTC dates separated by commas")
    parser.add_argument("--parameters", help="JSON object for the registered candidate; defaults to --config values")
    parser.add_argument("--synthetic", action="store_true",
                        help="exercise the gate only; never produce replay metrics")
    args = parser.parse_args(argv)
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        config = WalkForwardConfig.from_mapping(raw_config)
        if args.manifest is None:
            report = blocked_report("missing_preregistration_manifest", config=config,
                                    observations_path=args.observations, labels_path=args.labels)
            write_report(report, args.output)
            print(json.dumps({"canary_blocked": True, "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
            return 0
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        parameters = json.loads(args.parameters) if args.parameters else {
            "edge_buffer": format(config.edge_buffer, "f"),
            "latency_ms": config.latency_ms,
            "order_size": format(config.order_size, "f"),
            "order_ttl_ms": config.order_ttl_ms,
            "train_duration_ms": config.train_duration_ms,
            "test_duration_ms": config.test_duration_ms,
            "purge_ms": config.purge_ms,
            "embargo_ms": config.embargo_ms,
            "min_training_events": config.min_training_events,
            "calibration_bins": config.calibration_bins,
        }
        preregistration = audit_manifest(
            manifest,
            phase="pre_evaluation",
            observations_sha256=file_sha256(args.observations) if args.observations.is_file() else None,
            labels_sha256=file_sha256(args.labels) if args.labels.is_file() else None,
            code_commit=args.code_commit,
            evaluation_dates_utc=[item for item in (args.evaluation_dates or "").split(",") if item],
            parameters=parameters,
            synthetic=args.synthetic,
        )
        if preregistration["blockers"]:
            report = blocked_report(preregistration["blockers"], config=config,
                                    observations_path=args.observations, labels_path=args.labels)
            report["preregistration"] = preregistration
            write_report(report, args.output)
            print(json.dumps({"canary_blocked": True, "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
            return 0
        if args.synthetic:
            report = blocked_report("synthetic_input_not_evidence", config=config,
                                    observations_path=args.observations, labels_path=args.labels)
            report["preregistration"] = preregistration
            write_report(report, args.output)
            print(json.dumps({"canary_blocked": True, "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
            return 0
        report = run_files(args.observations, args.labels, config)
        report["preregistration"] = preregistration
        write_report(report, args.output)
        print(json.dumps({"canary_blocked": report["canary_blocked"], "blocked_reasons": report["blocked_reasons"],
                          "metrics": report["metrics"]}, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        report = blocked_report(f"input_error:{exc}", observations_path=args.observations, labels_path=args.labels)
        write_report(report, args.output)
        print(json.dumps({"canary_blocked": True, "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
