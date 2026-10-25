"""Causal repair of the frozen H600 paper challenger.

H600 inherits the H434/ H432 ExtraTrees feature contract.  The temporal audit
showed that ``btc_move_4m`` is not observable at H600's 124-second decision
point, so a paper pass that uses it cannot be promoted.  H711 keeps H600's
favorite-side rule, depth map, fee, latency and one-ledger protocol fixed while
removing that feature and anchoring settlement to market-start + 300 seconds.

This module is a separate cloud experiment.  It never places orders and does
not alter the frozen H600 manifest or its historical result.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR.parent))

from analysis import h432_old_clean_extratrees_compound as h432
from analysis import h561_online_depth_proxy as h561
from analysis import h600_favorite_side_depth as h600


REMOVED_FEATURE = "btc_move_4m"
MARKET_DURATION_SECONDS = 300.0
CAUSAL_FEATURES = tuple(feature for feature in h432.FEATURES if feature != REMOVED_FEATURE)
SOURCE_COMMIT = h600.SOURCE_COMMIT
HYPOTHESIS_ID = "H711_CAUSAL_H600_FAVORITE_SIDE_DEPTH_REPAIR"
RESULT_FORMAT = "old-clean-h711-causal-h600-favorite-side-depth-repair-v1"


def _causal_candidate_frame(model, medians, current: pd.DataFrame, slippage: float) -> pd.DataFrame:
    """Keep the frozen H600 candidate policy but repair its clocks."""
    scored = _LEGACY_CANDIDATE_FRAME(model, medians, current, slippage)
    if scored.empty:
        return scored
    epoch = pd.to_numeric(scored["slug"].astype(str).str.rsplit("-", n=1).str[-1], errors="coerce")
    scored["settlement_ts"] = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce") + pd.Timedelta(
        seconds=MARKET_DURATION_SECONDS
    )
    scored["cash_release_ts"] = scored["settlement_ts"] + pd.Timedelta(seconds=h432.CASH_RELEASE_SECONDS)
    return scored


_LEGACY_CANDIDATE_FRAME = h432._candidate_frame


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict:
    """Run H600 once with only the causal feature and settlement-clock repairs."""
    original_features = h432.FEATURES
    original_h432_candidate = h432._candidate_frame
    original_h561_candidate = h561._candidate_frame
    try:
        h432.FEATURES = list(CAUSAL_FEATURES)
        h432._candidate_frame = _causal_candidate_frame
        h561._candidate_frame = _causal_candidate_frame
        result = h600.evaluate(source_root, source_commit=source_commit, output=output)
    finally:
        h432.FEATURES = original_features
        h432._candidate_frame = original_h432_candidate
        h561._candidate_frame = original_h561_candidate

    report_path = output / "summary.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(
        {
            "format": RESULT_FORMAT,
            "hypothesis_id": HYPOTHESIS_ID,
            "parent_hypothesis_id": h600.HYPOTHESIS_ID,
            "causal_feature_contract": {
                "removed_feature": REMOVED_FEATURE,
                "features_used": list(CAUSAL_FEATURES),
                "future_feature_used_by_model": False,
                "decision_seconds_after_start": h432.ENTRY_SECONDS_AFTER_START,
            },
            "settlement_clock": "slug_epoch_plus_300_seconds",
            "market_duration_seconds": MARKET_DURATION_SECONDS,
            "cash_release_delay_seconds": h432.CASH_RELEASE_SECONDS,
            "parent_h600_result_unchanged": True,
            "new_data_collected": False,
            "live_orders_enabled": False,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "evidence_boundary": "strict-quality pinned old-clean causal H600 paper replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
            "next_action": "promote only if every tested stress cell has independent holdout ten-fill same-ledger passage; otherwise quarantine and do not retune on holdout",
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
                "feature_rows": result["feature_rows"],
                "variants": result["variants"],
                "causal_feature_contract": result["causal_feature_contract"],
                "settlement_clock": result["settlement_clock"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
