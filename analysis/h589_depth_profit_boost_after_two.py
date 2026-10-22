"""H589: H578 depth sizing with a delayed released-profit boost.

H581 showed that boosting after one released profit breaks four selection stress
rows.  H589 keeps H578's retained 3/5/7 depth map and raises the low-depth
fraction from 0.55 to 0.5875 only after two consecutive profitable releases.
The experiment is one predeclared paper policy on the pinned old-clean data.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import h567_depth_profit_synergy as base

HYPOTHESIS_ID = "H589_H578_DEPTH_PROFIT_BOOST_AFTER_TWO"
RESULT_FORMAT = "old-clean-h589-depth-profit-boost-after-two-v1"
SOURCE_COMMIT = base.SOURCE_COMMIT
THRESHOLDS = (3.0, 5.0, 7.0)
BOOST_AFTER_PROFITS = 2
BASE_FRACTION = base.BASE_FRACTION
BOOST_FRACTION = base.BOOST_FRACTION


def combined_fraction(row: Any, cash: float, consecutive_released_profits: int):
    return base.combined_fraction(
        row,
        cash,
        consecutive_released_profits,
        thresholds=THRESHOLDS,
        base_fraction=BASE_FRACTION,
        boost_fraction=BOOST_FRACTION,
        boost_after_profits=BOOST_AFTER_PROFITS,
    )


def evaluate(source_root: Path, *, source_commit: str = SOURCE_COMMIT, output: Path) -> dict[str, Any]:
    # Delegate the already-tested replay and report machinery, changing only
    # the pre-registered policy constants.  Restore globals even on failure.
    saved = (base.HYPOTHESIS_ID, base.RESULT_FORMAT, base.THRESHOLDS, base.BOOST_AFTER_PROFITS)
    base.HYPOTHESIS_ID = HYPOTHESIS_ID
    base.RESULT_FORMAT = RESULT_FORMAT
    base.THRESHOLDS = THRESHOLDS
    base.BOOST_AFTER_PROFITS = BOOST_AFTER_PROFITS
    try:
        return base.evaluate(source_root, source_commit=source_commit, output=output)
    finally:
        base.HYPOTHESIS_ID, base.RESULT_FORMAT, base.THRESHOLDS, base.BOOST_AFTER_PROFITS = saved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", default=SOURCE_COMMIT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.source_root, source_commit=args.source_commit, output=args.output)
    print(result["selection"], result["holdout"])


if __name__ == "__main__":
    main()
