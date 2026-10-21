"""H491: triage the newest public BTC/Polymarket research before replaying it.

This is a source-bound synthesis, not a market-data download or a strategy
replay.  It records why the OpenMarket release is useful for execution
realism but is not an admissible new alpha signal for the frozen small-cash
Canary protocol.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


SOURCES = {
    "paper": "https://arxiv.org/abs/2607.26245",
    "repository": "https://github.com/gregyoung14/openmarket",
    "dataset": "https://huggingface.co/datasets/gregyoung14/openmarket-btc-polymarket",
}


def synthesize() -> dict[str, Any]:
    checks = {
        "source_is_primary_paper": True,
        "source_code_and_dataset_are_versioned": True,
        "out_of_sample_beats_mid_prior": False,
        "simulated_economics_are_positive_after_cost": False,
        "source_proves_owner_scoped_fills": False,
        "source_proves_settlement_cash_release": False,
    }
    return {
        "format": "h491-openmarket-source-synthesis-v1",
        "hypothesis_id": "H491_OPENMARKET_BINANCE_LEAD_LAG_ALPHA",
        "sources": SOURCES,
        "source_version": "OpenMarket v0.5.2 / unified v0.4.3",
        "reported_scale": {
            "unified_rows": 727098247,
            "archival_snapshots": 202,
            "explicit_lead_lag_pairs": 2936031,
            "features": 43,
            "walk_forward_windows": 559,
        },
        "reported_results": {
            "model_oos_beats_mid_prior": False,
            "simulated_pnl_per_attempted_trade": -0.116,
            "collector_clock_quote_response_median_ms": 347,
            "one_tick_spread_share": 0.919,
        },
        "checks": checks,
        # The negative results are the point of this triage.  A successful
        # audit therefore means the source is primary/versioned and that the
        # proposed alpha/evidence claims are correctly rejected.
        "all_checks_passed": (
            checks["source_is_primary_paper"]
            and checks["source_code_and_dataset_are_versioned"]
            and not checks["out_of_sample_beats_mid_prior"]
            and not checks["simulated_economics_are_positive_after_cost"]
            and not checks["source_proves_owner_scoped_fills"]
            and not checks["source_proves_settlement_cash_release"]
        ),
        "decision": "reject_binance_lead_lag_as_standalone_alpha_and_do_not_replay_duplicate_signal",
        "implication": "Use the source for execution-realism assumptions and data-schema comparison only. Keep H453/H434 frozen as the old-clean paper reference; require owner-scoped order/trade/settlement/released-cash/balance evidence before any Canary promotion.",
        "new_market_data_collected": False,
        "model_refit": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "same_ledger_compounding_rows": 0,
        "root_cause": "A high-resolution public feed and a plausible cross-venue lag do not establish a positive post-cost edge or owner-scoped cash continuity; execution and settlement evidence remain the limiting boundary.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = synthesize()
    if not result["all_checks_passed"]:
        raise SystemExit("h491_source_synthesis_failed")
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": result["format"], "all_checks_passed": True, "decision": result["decision"]}, sort_keys=True))


if __name__ == "__main__":
    main()

