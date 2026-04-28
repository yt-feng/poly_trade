from __future__ import annotations

PROVIDERS = {
    "chainlink": {
        "priority": "P0",
        "purpose": "reference label alignment",
        "required_env": ["CHAINLINK_API_KEY", "CHAINLINK_BASE_URL", "CHAINLINK_STREAM_ID_BTC_USD"],
        "expected_outputs": [
            "reference_start_price",
            "reference_end_price",
            "reference_return_5m",
            "reference_bid",
            "reference_ask",
            "reference_volatility",
            "reference_liquidity",
        ],
        "notes": "Most important but may be hardest to backfill historically.",
    },
    "polymarket": {
        "priority": "P0",
        "purpose": "market microstructure and price-in detection",
        "required_env": ["POLYMARKET_API_BASE"],
        "optional_env": ["POLYMARKET_API_KEY", "POLYMARKET_AUTH_TOKEN", "POLYMARKET_WS_URL"],
        "expected_outputs": [
            "best_bid_yes",
            "best_ask_yes",
            "best_bid_no",
            "best_ask_no",
            "spread_yes",
            "spread_no",
            "book_imbalance",
            "microprice",
            "trade_sign_imbalance",
            "depth_top5",
        ],
        "notes": "Highest practical value because execution happens on Polymarket.",
    },
    "binance_futures": {
        "priority": "P0",
        "purpose": "perp microstructure, OI, taker flow, liquidation state",
        "required_env": ["BINANCE_FUTURES_BASE_URL", "BINANCE_SYMBOL"],
        "optional_env": ["BINANCE_API_KEY", "BINANCE_API_SECRET", "BINANCE_SPOT_BASE_URL"],
        "expected_outputs": [
            "open_interest",
            "d_open_interest_30s",
            "d_open_interest_60s",
            "taker_buy_sell_ratio",
            "liquidation_long_short_imbalance",
            "depth_imbalance",
            "perp_spot_basis",
            "mark_index_basis",
        ],
        "notes": "Usually more informative than simple OHLCV for 5-minute tasks.",
    },
    "deribit": {
        "priority": "P1",
        "purpose": "options and derivative risk-state filter",
        "required_env": ["DERIBIT_BASE_URL", "DERIBIT_CURRENCY"],
        "optional_env": ["DERIBIT_CLIENT_ID", "DERIBIT_CLIENT_SECRET"],
        "expected_outputs": [
            "near_atm_iv",
            "rr_25d",
            "deribit_basis",
            "deribit_oi",
            "deribit_bid_ask_spread",
        ],
        "notes": "Better as a regime / tail-risk layer than a stand-alone short-horizon alpha source.",
    },
    "macro_calendar": {
        "priority": "P1",
        "purpose": "event-time regime gating",
        "required_env": ["MACRO_CALENDAR_API_KEY", "MACRO_CALENDAR_BASE_URL"],
        "expected_outputs": [
            "has_major_macro_within_15m",
            "had_major_macro_last_15m",
            "macro_event_type",
            "macro_event_importance",
        ],
        "notes": "Useful to separate normal periods from CPI/FOMC/NFP-like releases.",
    },
    "deepseek": {
        "priority": "P2",
        "purpose": "semantic scoring / narrative regime tags",
        "required_env": ["DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL"],
        "expected_outputs": [
            "event_sentiment_score",
            "risk_on_off_score",
            "btc_directional_text_score",
            "macro_surprise_text_score",
        ],
        "notes": "Should augment, not replace, market microstructure or reference labels.",
    },
}


def list_provider_names() -> list[str]:
    return sorted(PROVIDERS.keys())


def get_provider(name: str) -> dict:
    return PROVIDERS[name]
