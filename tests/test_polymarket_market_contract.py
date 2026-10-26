import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
from polymarket_market_contract import (  # noqa: E402
    ContractError,
    parse_market_constraints,
    required_order_notional_usd,
    validate_order_contract,
)


def _metadata(**updates):
    value = {
        "min_order_size": 5.0,
        "tick_size": 0.01,
        "itode": False,
        "observed_ts": "2026-01-01T00:00:00Z",
    }
    value.update(updates)
    return value


def test_official_five_is_usd_notional_not_five_shares():
    constraints = parse_market_constraints(_metadata())
    assert constraints.minimum_order_size_usd == 5.0
    assert required_order_notional_usd(constraints, 0.20, strategy_minimum_shares=5.0) == 5.0


def test_larger_market_notional_is_valid_and_not_compared_to_shares():
    constraints = parse_market_constraints(_metadata(min_order_size=10.0))
    assert required_order_notional_usd(constraints, 0.20, strategy_minimum_shares=5.0) == 10.0


def test_share_floor_is_converted_at_price_before_comparison():
    constraints = parse_market_constraints(_metadata(min_order_size=3.0))
    assert required_order_notional_usd(constraints, 0.80, strategy_minimum_shares=5.0) == 4.0


def test_aliases_must_agree():
    with pytest.raises(ContractError, match="minimum_order_size_alias_conflict"):
        parse_market_constraints(_metadata(orderMinSize=6.0))


@pytest.mark.parametrize("value", [True, False, 0, -1, "nan"])
def test_invalid_notional_rejected(value):
    with pytest.raises(ContractError, match="minimum_order_size_usd_invalid"):
        parse_market_constraints(_metadata(min_order_size=value))


def test_tick_and_optional_delay_gate_are_explicit():
    result = validate_order_contract(_metadata(), price=0.50, strategy_minimum_shares=5.0)
    assert result["required_order_notional_usd"] == 5.0
    assert result["unit_semantics"] == {"market_minimum": "usdc_notional", "strategy_minimum": "shares_optional"}
    with pytest.raises(ContractError, match="itode_not_supported_by_protocol"):
        validate_order_contract(_metadata(itode=True), price=0.50, require_no_itode=True)


def test_off_tick_price_rejected():
    with pytest.raises(ContractError, match="order_price_off_tick"):
        validate_order_contract(_metadata(), price=0.505)
