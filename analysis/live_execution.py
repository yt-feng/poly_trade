#!/usr/bin/env python3
from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Dict, Optional, Union


class LiveExecutionError(RuntimeError):
    pass


def load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    env_path = Path(path)
    if not env_path.exists():
        raise LiveExecutionError(f"env file does not exist: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            os.environ[key] = parsed[0] if parsed else ""
        except ValueError:
            os.environ[key] = value.strip().strip('"').strip("'")


def first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def money_text(value: Union[Decimal, float, str], places: str = "0.01") -> str:
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise LiveExecutionError(f"invalid decimal value: {value!r}") from exc
    if dec <= 0:
        return "0"
    return str(dec.quantize(Decimal(places), rounding=ROUND_DOWN))


def price_text(value: Union[Decimal, float, str]) -> str:
    try:
        dec = Decimal(str(value))
    except InvalidOperation as exc:
        raise LiveExecutionError(f"invalid price value: {value!r}") from exc
    dec = max(Decimal("0.01"), min(Decimal("0.99"), dec))
    return str(dec.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


@dataclass(frozen=True)
class LiveExecutionSettings:
    private_key: str
    wallet: str
    builder_code: str
    max_order_usd: Decimal
    order_type: str

    @classmethod
    def from_args(cls, args: Any) -> "LiveExecutionSettings":
        if str(args.execution_mode).lower() != "live":
            return cls("", "", "", Decimal(str(args.max_live_order_usd)), str(args.live_order_type).upper())
        if not bool(args.allow_live_trading):
            raise LiveExecutionError("live mode requires --allow-live-trading")
        private_key = first_env("POLYMARKET_PRIVATE_KEY")
        wallet = first_env("POLYMARKET_DEPOSIT_WALLET", "POLYMARKET_WALLET", "POLYMARKET_PROXY_WALLET")
        if not private_key:
            raise LiveExecutionError("POLYMARKET_PRIVATE_KEY is required for live execution")
        if not wallet:
            raise LiveExecutionError("POLYMARKET_DEPOSIT_WALLET or POLYMARKET_WALLET is required for live execution")
        max_order = Decimal(str(args.max_live_order_usd))
        if max_order <= 0:
            raise LiveExecutionError("--max-live-order-usd must be greater than zero")
        order_type = str(args.live_order_type).upper()
        if order_type not in {"FAK", "FOK"}:
            raise LiveExecutionError("--live-order-type must be FAK or FOK for market orders")
        return cls(
            private_key=private_key,
            wallet=wallet,
            builder_code=first_env("POLYMARKET_BUILDER_CODE"),
            max_order_usd=max_order,
            order_type=order_type,
        )


@dataclass(frozen=True)
class LiveOrderPlan:
    local_order_id: str
    slug: str
    side: str
    token_id: str
    amount_usd: str
    max_spend_usd: str
    max_price: str
    order_type: str
    estimated_shares: str
    min_order_size: str
    blocked_reason: str = ""

    def event_fields(self) -> Dict[str, Any]:
        return {
            "live_order_local_id": self.local_order_id,
            "token_id": self.token_id,
            "order_type": self.order_type,
            "live_amount_usd": self.amount_usd,
            "live_max_spend_usd": self.max_spend_usd,
            "live_max_price": self.max_price,
            "live_estimated_shares": self.estimated_shares,
            "live_min_order_size": self.min_order_size,
            "reason": self.blocked_reason,
        }


def build_buy_market_plan(
    *,
    local_order_id: str,
    slug: str,
    side: str,
    token_id: str,
    intended_cost: float,
    signal_price: float,
    max_adverse_slippage: float,
    min_order_size: float,
    settings: LiveExecutionSettings,
) -> LiveOrderPlan:
    capped_amount = min(Decimal(str(intended_cost)), settings.max_order_usd)
    amount = Decimal(money_text(capped_amount))
    max_price = Decimal(price_text(Decimal(str(signal_price)) + Decimal(str(max_adverse_slippage))))
    estimated_shares = Decimal("0") if max_price <= 0 else amount / max_price
    min_size = Decimal(str(min_order_size)) if min_order_size and min_order_size > 0 else Decimal("0")
    blocked = ""
    if amount <= 0:
        blocked = "blocked_zero_amount"
    elif min_size > 0 and estimated_shares < min_size:
        blocked = "blocked_min_order_size"
    return LiveOrderPlan(
        local_order_id=local_order_id,
        slug=slug,
        side=side,
        token_id=token_id,
        amount_usd=money_text(amount),
        max_spend_usd=money_text(amount),
        max_price=price_text(max_price),
        order_type=settings.order_type,
        estimated_shares=money_text(estimated_shares),
        min_order_size=money_text(min_size),
        blocked_reason=blocked,
    )


class PolymarketLiveExecutor:
    def __init__(self, settings: LiveExecutionSettings):
        self.settings = settings
        self._client: Any = None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from polymarket import SecureClient
        except ModuleNotFoundError as exc:
            raise LiveExecutionError("polymarket-client is not installed; run `pip install -r requirements.txt`") from exc
        self._client = SecureClient.create(private_key=self.settings.private_key, wallet=self.settings.wallet)
        return self._client

    def place_buy_market_order(self, plan: LiveOrderPlan) -> Dict[str, Any]:
        event: Dict[str, Any] = plan.event_fields()
        if plan.blocked_reason:
            event.update({"live_order_ok": False, "live_order_status": "blocked", "live_order_error": plan.blocked_reason})
            return event
        start = time.perf_counter()
        try:
            kwargs: Dict[str, Any] = {
                "token_id": plan.token_id,
                "side": "BUY",
                "amount": plan.amount_usd,
                "max_spend": plan.max_spend_usd,
                "max_price": plan.max_price,
                "order_type": plan.order_type,
            }
            if self.settings.builder_code:
                kwargs["builder_code"] = self.settings.builder_code
            response = self._get_client().place_market_order(**kwargs)
            event.update(normalize_order_response(response))
        except Exception as exc:
            event.update({"live_order_ok": False, "live_order_status": "exception", "live_order_error": str(exc)})
        event["executor_latency_ms"] = round((time.perf_counter() - start) * 1000.0, 2)
        return event


def normalize_order_response(response: Any) -> Dict[str, Any]:
    ok = bool(getattr(response, "ok", False))
    if ok:
        return {
            "live_order_ok": True,
            "live_order_id": getattr(response, "order_id", ""),
            "live_order_status": getattr(response, "status", ""),
            "live_order_error": "",
            "live_trade_ids": ",".join(str(x) for x in getattr(response, "trade_ids", ()) or ()),
            "live_transactions": ",".join(str(x) for x in getattr(response, "transactions_hashes", ()) or ()),
            "live_making_amount": str(getattr(response, "making_amount", "")),
            "live_taking_amount": str(getattr(response, "taking_amount", "")),
        }
    return {
        "live_order_ok": False,
        "live_order_status": getattr(response, "code", "rejected"),
        "live_order_error": getattr(response, "message", str(response)),
    }
