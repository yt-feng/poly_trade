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
except ImportError:  # pragma: no cover - package/import-mode convenience
    from analysis.v3_data_contract import load_jsonl, validate_records


LABEL_SCHEMA_VERSION = 1
LABEL_KEYS = {
    "label_version", "market_id", "condition_id", "outcome", "resolved_time_ms",
    "label_available_time_ms", "source", "source_sha256",
}
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
        result.append((price, size))
    if not result:
        raise EvaluationBlocked("missing_executable_book_side")
    return result


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

    The first book available after latency is used.  Any remaining quantity is
    cancelled at expiry; no queue position or hidden liquidity is invented.
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
    candidates = sorted(
        (row for row in market_records
         if row.get("received_time_ms", 0) >= active_at
         and row.get("received_time_ms", 0) <= expires_at),
        key=lambda row: row["received_time_ms"],
    )
    if not candidates:
        return {
            "status": "expired_unfilled", "requested_shares": str(requested),
            "filled_shares": "0", "remaining_shares": str(requested),
            "active_at_ms": active_at, "expires_at_ms": expires_at,
        }
    fill_record = candidates[0]
    if fill_record.get("market_id") != decision.get("market_id"):
        raise EvaluationBlocked("market_cluster_mismatch")
    if fill_record.get("rules") != decision.get("rules"):
        return {
            "status": "blocked_rules_changed", "requested_shares": str(requested),
            "filled_shares": "0", "remaining_shares": str(requested),
        }
    fill_fee = _fee_metadata(fill_record)
    if fill_fee != decision_fee:
        return {
            "status": "blocked_fee_metadata_changed", "requested_shares": str(requested),
            "filled_shares": "0", "remaining_shares": str(requested),
        }
    levels = _best_levels(fill_record, token, side)
    if limit_price is not None:
        limit = _decimal(limit_price)
        if not _is_on_tick(limit, tick):
            raise EvaluationBlocked("limit_price_off_tick")
        opposite = _best_levels(fill_record, token, "sell" if side == "buy" else "buy")
        midpoint = (levels[0][0] + opposite[0][0]) / Decimal("2")
        if limit == midpoint:
            raise EvaluationBlocked("MID_PRICE_NOT_EXECUTABLE")
        # A limit order may only consume prices no worse than its limit.  A
        # midpoint supplied as a limit still cannot pass unless it is displayed
        # liquidity at a book level.
        levels = [(price, size) for price, size in levels
                  if (price <= limit if side == "buy" else price >= limit)]
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
        "average_price": str(average) if average is not None else None,
        "notional_usdc": str(notional), "fee_usdc": str(fee), "fills": fills,
        "price_source": price_source, "side": side, "token": token,
    }


def load_labels(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Load labels from a separate file; labels are never accepted in v3 rows."""
    if not path.exists():
        return {}, {"input_records": 0, "accepted_records": 0, "quarantined_records": 0,
                    "reason_counts": {"missing_label_file": 1}}
    accepted: dict[str, dict[str, Any]] = {}
    reasons = Counter()
    total = 0
    for record in load_jsonl(path):
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
                           "fees": record["fees"], "books": books, "observation_id": record["observation_id"]}
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
        "filled_attempts": 0, "partial_fills": 0, "expired_orders": 0,
        "gross_pnl_usdc": None, "fees_usdc": None, "net_pnl_usdc": None,
        "net_return_on_filled_cost": None,
    }


def evaluate(
    observations: list[dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    config: WalkForwardConfig,
) -> dict[str, Any]:
    """Run blocked-safe walk-forward evaluation and return a public report."""
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
    }
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
            continue
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
            order = simulate_order(feature, by_market[market], token=token, side="buy",
                                   quantity=config.order_size, latency_ms=config.latency_ms,
                                   ttl_ms=config.order_ttl_ms)
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
    if execution_results:
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


def blocked_report(reason: str, *, config: WalkForwardConfig | None = None) -> dict[str, Any]:
    report = {
        "schema_version": 1, "evaluator": "event_clustered_walk_forward_v1",
        "canary_blocked": True, "canary_allowed": False,
        "official_semantics": {"fees": OFFICIAL_FEES_URL, "orders": OFFICIAL_ORDER_URL,
                                "mid_price_fills": "rejected", "private_execution_evidence": False},
        "input": {"observation_records": 0, "accepted_book_records": 0, "label_records": 0},
        "split_policy": {"cluster_key": "market_id", "purge_ms": config.purge_ms if config else None,
                          "embargo_ms": config.embargo_ms if config else None,
                          "fit_cutoff_rule": "received_time_ms < train_end and label_available_time_ms <= train_end"},
        "folds": [], "metrics": _empty_metrics(), "blocked_reasons": [reason],
    }
    return report


def run_files(observations_path: Path, labels_path: Path, config: WalkForwardConfig) -> dict[str, Any]:
    if not observations_path.exists():
        return blocked_report("missing_observation_file", config=config)
    raw = load_jsonl(observations_path)
    validation = validate_records(raw)
    observations = [record for record in validation["accepted"] if record.get("observation_type") == "book_snapshot"]
    labels, label_report = load_labels(labels_path)
    report = evaluate(observations, labels, config)
    report["input"]["v3_validation"] = {key: value for key, value in validation.items() if key != "accepted"}
    report["input"]["labels_validation"] = label_report
    return report


def write_report(report: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        raw_config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
        config = WalkForwardConfig.from_mapping(raw_config)
        report = run_files(args.observations, args.labels, config)
        write_report(report, args.output)
        print(json.dumps({"canary_blocked": report["canary_blocked"], "blocked_reasons": report["blocked_reasons"],
                          "metrics": report["metrics"]}, sort_keys=True))
        return 0
    except (OSError, json.JSONDecodeError, EvaluationBlocked) as exc:
        report = blocked_report(f"input_error:{exc}")
        write_report(report, args.output)
        print(json.dumps({"canary_blocked": True, "blocked_reasons": report["blocked_reasons"]}, sort_keys=True))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
