"""Causal repair of the frozen H549 adaptive cash-boost paper challenger.

H549 is the strongest paper-only cash-boost profile retained from the old-clean
frontier (0.55 until one profitable released exit, then 0.5875).  Its parent
H542 imports the old H432 feature/candidate functions directly, so a normal
module-level patch would not remove the post-decision ``btc_move_4m`` feature.
This module patches the exact bound functions, keeps the H549 parameters and
stress grid frozen, and repairs settlement to market-start + 300 seconds.
It is a separate cloud replay; it never places orders or changes H549 itself.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR.parent))
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from analysis import h542_adaptive_cash_boost as h542

# H542 imported these names from the historical top-level module.  Import that
# exact module object so its estimator globals and the bound candidate function
# are repaired together.
h432 = importlib.import_module("h432_old_clean_extratrees_compound")

REMOVED_FEATURE = "btc_move_4m"
MARKET_DURATION_SECONDS = 300.0
CAUSAL_FEATURES = tuple(feature for feature in h432.FEATURES if feature != REMOVED_FEATURE)
SOURCE_COMMIT = h542.SOURCE_COMMIT
HYPOTHESIS_ID = "H712_CAUSAL_H549_ADAPTIVE_CASH_BOOST_REPAIR"
RESULT_FORMAT = "old-clean-h712-causal-h549-adaptive-cash-boost-repair-v1"
PARENT_HYPOTHESIS_ID = "H549_BOOST_05875_AFTER_ONE_PROFIT"
BASE_FRACTION = 0.55
BOOST_FRACTION = 0.5875
BOOST_AFTER_PROFITS = 1


_LEGACY_CANDIDATE_FRAME = h432._candidate_frame


def _causal_candidate_frame(model, medians, current: pd.DataFrame, slippage: float) -> pd.DataFrame:
    """Keep H549 scoring while replacing non-observable settlement clocks."""
    scored = _LEGACY_CANDIDATE_FRAME(model, medians, current, slippage)
    if scored.empty:
        return scored
    epoch = pd.to_numeric(scored["slug"].astype(str).str.rsplit("-", n=1).str[-1], errors="coerce")
    scored["settlement_ts"] = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce") + pd.Timedelta(
        seconds=MARKET_DURATION_SECONDS
    )
    scored["cash_release_ts"] = scored["settlement_ts"] + pd.Timedelta(seconds=h432.CASH_RELEASE_SECONDS)
    return scored


def _patch_h542() -> tuple[object, object, object]:
    """Install the causal feature/frame contract on H542's bound names."""
    original_features = h432.FEATURES
    original_fit = h542._fit
    original_candidate = h542._candidate_frame
    h432.FEATURES = list(CAUSAL_FEATURES)
    # H542 imported both functions directly; patch those names explicitly.
    h542._fit = h432._fit
    h542._candidate_frame = _causal_candidate_frame
    return original_features, original_fit, original_candidate


def _restore_h542(original: tuple[object, object, object]) -> None:
    original_features, original_fit, original_candidate = original
    h432.FEATURES = original_features
    h542._fit = original_fit
    h542._candidate_frame = original_candidate


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict:
    original = _patch_h542()
    try:
        result = h542.evaluate(
            source_root,
            source_commit=source_commit,
            output=output,
            hypothesis_id=HYPOTHESIS_ID,
            result_format=RESULT_FORMAT,
            base_fraction=BASE_FRACTION,
            boost_fraction=BOOST_FRACTION,
            boost_after_profits=BOOST_AFTER_PROFITS,
        )
    finally:
        _restore_h542(original)

    report_path = output / "summary.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "format": RESULT_FORMAT,
            "hypothesis_id": HYPOTHESIS_ID,
            "parent_hypothesis_id": PARENT_HYPOTHESIS_ID,
            "causal_feature_contract": {
                "removed_feature": REMOVED_FEATURE,
                "features_used": list(CAUSAL_FEATURES),
                "future_feature_used_by_model": False,
                "decision_seconds_after_start": h432.ENTRY_SECONDS_AFTER_START,
            },
            "settlement_clock": "slug_epoch_plus_300_seconds",
            "market_duration_seconds": MARKET_DURATION_SECONDS,
            "cash_release_delay_seconds": h432.CASH_RELEASE_SECONDS,
            "parent_h549_profile": {
                "base_fraction": BASE_FRACTION,
                "boost_fraction": BOOST_FRACTION,
                "boost_after_profitable_released_exits": BOOST_AFTER_PROFITS,
            },
            "parent_h549_result_unchanged": True,
            "new_data_collected": False,
            "live_orders_enabled": False,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "evidence_boundary": "strict-quality pinned old-clean causal H549 paper replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
            "next_action": "promote only if every tested stress cell and independent holdout preserve the ten-fill same-ledger cash floor; otherwise quarantine without holdout retuning",
        }
    )
    report.pop("summary_sha256", None)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["summary_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(
        json.dumps(
            {
                "format": result["format"],
                "hypothesis_id": result["hypothesis_id"],
                "adaptive": result["adaptive"],
                "baseline": result["baseline"],
                "comparison": result["comparison"],
                "causal_feature_contract": result["causal_feature_contract"],
                "settlement_clock": result["settlement_clock"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
