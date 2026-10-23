"""Causal, full-position retry selection with caller-frozen timing parameters.

Only an already selected legal-price quote with known insufficient side depth
can trigger another request. Each request selects price before checking depth.
This module performs no accounting, model work, data acquisition or orders.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real
from typing import Any, Protocol

import numpy as np
import pandas as pd


def _number(value: Any) -> float:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError("invalid_retry_numeric_parameter")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("invalid_retry_numeric_parameter")
    return number


def _time(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("invalid_retry_utc_clock")
    return timestamp.tz_convert("UTC")


def _seconds(value: float) -> pd.Timedelta:
    try:
        return pd.Timedelta(seconds=value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("invalid_retry_duration") from exc


@dataclass(frozen=True)
class RetryPolicy:
    reaction_delay_seconds: float
    order_latency_seconds: float
    max_quote_delay_seconds: float
    deadline_after_original_target_seconds: float
    max_extra_attempts: int

    def __post_init__(self):
        names = ("reaction_delay_seconds", "order_latency_seconds",
                 "max_quote_delay_seconds", "deadline_after_original_target_seconds")
        for name in names:
            value = _number(getattr(self, name))
            if value < 0:
                raise ValueError("negative_retry_duration")
            _seconds(value)
            object.__setattr__(self, name, value)
        if (self.deadline_after_original_target_seconds <= 0
                or self.reaction_delay_seconds + self.order_latency_seconds <= 0
                or type(self.max_extra_attempts) is not int
                or not 1 <= self.max_extra_attempts <= 1000):
            raise ValueError("invalid_retry_budget_or_progress")


@dataclass(frozen=True)
class ExitRequest:
    market_id: str
    side: str
    original_exit_target_ts: Any
    market_close_ts: Any
    held_shares: float
    slippage: float
    depth_haircut: float
    max_quote_delay_seconds: float
    order_latency_seconds: float

    def __post_init__(self):
        if not isinstance(self.market_id, str) or not self.market_id or self.side not in ("up", "down"):
            raise ValueError("invalid_retry_identity")
        for name in ("original_exit_target_ts", "market_close_ts"):
            object.__setattr__(self, name, _time(getattr(self, name)))
        for name in ("held_shares", "slippage", "depth_haircut",
                     "max_quote_delay_seconds", "order_latency_seconds"):
            object.__setattr__(self, name, _number(getattr(self, name)))
        if (self.held_shares <= 0 or not 0 <= self.slippage < 1
                or not 0 < self.depth_haircut <= 1
                or min(self.max_quote_delay_seconds, self.order_latency_seconds) < 0):
            raise ValueError("invalid_retry_request")


class QuoteSource(Protocol):
    def select(self, market_id: str, target_ts: Any, close_ts: Any, *, side: str,
               kind: str, max_quote_delay_seconds: float) -> tuple[pd.Series | None, str]: ...


@dataclass(frozen=True)
class ExitResolution:
    quote: pd.Series | None
    reason: str
    resolved_by_retry: bool
    selected_attempt_arrival_ts: pd.Timestamp
    terminal_reason: str
    deadline_ts: pd.Timestamp
    trace: tuple[dict[str, Any], ...]

    def to_evidence(self) -> dict[str, Any]:
        delay = None if self.quote is None else (
            _time(self.quote["ts_utc"]).value - self.selected_attempt_arrival_ts.value) / 1e9
        return {"resolved_by_retry": self.resolved_by_retry,
                "selected_attempt_arrival_ts": self.selected_attempt_arrival_ts.isoformat(),
                "selected_attempt_quote_delay_seconds": delay,
                "terminal_reason": self.terminal_reason, "deadline_ts": self.deadline_ts.isoformat(),
                "trace": [dict(row) for row in self.trace]}


def _quote_number(value: Any) -> float | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _observation(request: ExitRequest, quote: pd.Series | None, reason: str,
                 arrival: pd.Timestamp, limit: pd.Timestamp) -> dict[str, Any]:
    result = dict(selected_quote_ts=None, bid_price=None, fill_price=None,
                  available_shares=None, effective_available_shares=None)
    if not isinstance(reason, str) or not reason or (quote is None) == (reason == "selected"):
        raise ValueError("invalid_exit_selection_result")
    if quote is None:
        return {**result, "status": reason}
    stamp = _time(quote["ts_utc"])
    if not arrival <= stamp <= arrival + _seconds(request.max_quote_delay_seconds) or stamp >= limit:
        raise ValueError("selected_exit_quote_outside_request")
    conflict = quote.get("quote_conflict", False)
    if not isinstance(conflict, (bool, np.bool_)) or bool(conflict):
        raise ValueError("selected_exit_quote_must_not_be_conflicting")
    result["selected_quote_ts"] = stamp.isoformat()
    raw_price = _quote_number(quote.get(f"sell_{request.side}_cents"))
    size = _quote_number(quote.get(f"sell_{request.side}_size"))
    if raw_price is not None:
        result["bid_price"] = raw_price / 100
        result["fill_price"] = raw_price / 100 - request.slippage
    result["available_shares"] = size
    if size is not None:
        result["effective_available_shares"] = size * request.depth_haircut
    if raw_price is None or not 0 < raw_price < 100 or not 0 < result["fill_price"] < 1:
        status = "invalid_quote_price"
    elif size is None or size < 0:
        status = "invalid_quote_size"
    else:
        status = "thin" if size * request.depth_haircut < request.held_shares else "full_size"
    return {**result, "status": status}


def _trace(request: ExitRequest, observation: dict[str, Any], reason: str, index: int,
           decision: pd.Timestamp, arrival: pd.Timestamp, limit: pd.Timestamp,
           failure_observed: pd.Timestamp | None) -> dict[str, Any]:
    natural_end = arrival + _seconds(request.max_quote_delay_seconds)
    return {"attempt_index": index, "is_retry": index > 0,
            "failure_observed_ts": None if failure_observed is None else failure_observed.isoformat(),
            "decision_ts": decision.isoformat(), "arrival_ts": arrival.isoformat(),
            "window_end_ts": min(natural_end, limit).isoformat(),
            "window_end_inclusive": natural_end < limit, "quote_status": reason,
            "held_shares": request.held_shares, "depth_haircut": request.depth_haircut,
            **observation}


class ScheduledExitResolver:
    def __init__(self, policy: RetryPolicy):
        if not isinstance(policy, RetryPolicy):
            raise ValueError("retry_policy_required")
        self.policy = policy

    def __call__(self, request: ExitRequest, original_quote: pd.Series | None,
                 original_reason: str, quote_source: QuoteSource) -> ExitResolution:
        policy = self.policy
        if (request.order_latency_seconds != policy.order_latency_seconds
                or request.max_quote_delay_seconds != policy.max_quote_delay_seconds):
            raise ValueError("retry_and_ledger_clock_mismatch")
        target, close = request.original_exit_target_ts, request.market_close_ts
        deadline = min(target + _seconds(policy.deadline_after_original_target_seconds), close)
        observation = _observation(request, original_quote, original_reason, target, close)
        if observation["status"] != "thin":
            return ExitResolution(original_quote, original_reason, False, target,
                                  "original_" + observation["status"], deadline, ())
        trace = [_trace(request, observation, original_reason, 0,
                        target - _seconds(policy.order_latency_seconds), target, close, None)]
        failed_at = _time(original_quote["ts_utc"])
        terminal = "retry_attempt_budget_exhausted"
        for index in range(1, policy.max_extra_attempts + 1):
            decision = failed_at + _seconds(policy.reaction_delay_seconds)
            arrival = decision + _seconds(policy.order_latency_seconds)
            if arrival >= deadline:
                terminal = "retry_deadline_reached"
                break
            quote, reason = quote_source.select(request.market_id, arrival, deadline, side=request.side,
                kind="exit", max_quote_delay_seconds=policy.max_quote_delay_seconds)
            observation = _observation(request, quote, reason, arrival, deadline)
            trace.append(_trace(request, observation, reason, index, decision, arrival, deadline, failed_at))
            if observation["status"] == "full_size":
                return ExitResolution(quote, reason, True, arrival, "retry_full_size", deadline, tuple(trace))
            if observation["status"] != "thin":
                terminal = "retry_" + observation["status"]
                break
            failed_at = _time(quote["ts_utc"])
        # Returning the original thin quote preserves the caller's lock arithmetic.
        return ExitResolution(original_quote, original_reason, False, target, terminal, deadline, tuple(trace))
