"""Causal repair of the H534R ExtraTrees paper frontier.

H707 proved that the frozen H432/H534 model contract used ``btc_move_4m``
after the declared 124-second entry.  H709 keeps the model parameters,
selection thresholds, fee/depth rules and one-position cash ledger fixed, but
removes that post-decision feature and uses market-start + 300 seconds as the
settlement boundary before the 900-second cash-release delay.

This module wraps the existing H432 replay so its old result remains
unchanged; only this separately registered causal candidate is evaluated.
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

from analysis import h432_old_clean_extratrees_compound as legacy


REMOVED_FEATURE = "btc_move_4m"
MARKET_DURATION_SECONDS = 300.0
CAUSAL_FEATURES = tuple(feature for feature in legacy.FEATURES if feature != REMOVED_FEATURE)
SOURCE_COMMIT = legacy.SOURCE_COMMIT


def _causal_candidate_frame(model, medians, current: pd.DataFrame, slippage: float) -> pd.DataFrame:
    scored = _LEGACY_CANDIDATE_FRAME(model, medians, current, slippage)
    if scored.empty:
        return scored
    epoch = pd.to_numeric(scored["slug"].astype(str).str.rsplit("-", n=1).str[-1], errors="coerce")
    scored["settlement_ts"] = pd.to_datetime(epoch, unit="s", utc=True, errors="coerce") + pd.Timedelta(seconds=MARKET_DURATION_SECONDS)
    scored["cash_release_ts"] = scored["settlement_ts"] + pd.Timedelta(seconds=legacy.CASH_RELEASE_SECONDS)
    return scored


_LEGACY_CANDIDATE_FRAME = legacy._candidate_frame


def evaluate(source_root: Path, output: Path, source_commit: str = SOURCE_COMMIT) -> dict:
    original_features = legacy.FEATURES
    original_frame = legacy._candidate_frame
    try:
        legacy.FEATURES = list(CAUSAL_FEATURES)
        legacy._candidate_frame = _causal_candidate_frame
        report = legacy.evaluate(source_root, output, source_commit)
    finally:
        legacy.FEATURES = original_features
        legacy._candidate_frame = original_frame

    report_path = output / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update({
        "format": "old-clean-h709-causal-h534-repair-v1",
        "hypothesis_id": "H709_CAUSAL_H534_EXTRATREES_REPAIR",
        "parent_hypothesis_id": "H432_PREQUENTIAL_EXTRATREES_STRICT_COMPOUND",
        "causal_feature_contract": {
            "removed_feature": REMOVED_FEATURE,
            "features_used": list(CAUSAL_FEATURES),
            "future_feature_used_by_model": False,
            "decision_seconds_after_start": legacy.ENTRY_SECONDS_AFTER_START,
        },
        "settlement_clock": "slug_epoch_plus_300_seconds",
        "market_duration_seconds": MARKET_DURATION_SECONDS,
        "cash_release_delay_seconds": legacy.CASH_RELEASE_SECONDS,
        "parent_h432_result_unchanged": True,
        "new_data_collected": False,
        "live_orders_enabled": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "evidence_boundary": "strict-quality pinned old-clean causal paper replay; no authenticated order, fill, settlement, released-cash, or balance receipt",
        "next_action": "promote only if every tested slippage has independent holdout ten-fill same-ledger passage; otherwise quarantine and do not retune on holdout",
    })
    report.pop("report_content_sha256", None)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    report["report_content_sha256"] = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    return report


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, args.output, args.source_commit)
    print(json.dumps({
        "format": result["format"],
        "hypothesis_id": result["hypothesis_id"],
        "feature_rows": result["feature_rows"],
        "slippages": result["slippages"],
        "causal_feature_contract": result["causal_feature_contract"],
        "settlement_clock": result["settlement_clock"],
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
