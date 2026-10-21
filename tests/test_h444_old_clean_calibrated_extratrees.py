import numpy as np
import pandas as pd

from analysis.h444_old_clean_calibrated_extratrees import (
    MIN_CALIBRATION_ROWS,
    calibrated_probability,
    fit_platt_calibrator,
)


def _history(rows=MIN_CALIBRATION_ROWS):
    q = np.tile([0.2, 0.8], rows // 2)
    y = np.tile([0, 1], rows // 2)
    return pd.DataFrame({"q_up": q, "outcome_up": y})


def test_calibrator_requires_a_causal_history_budget():
    assert fit_platt_calibrator(_history(MIN_CALIBRATION_ROWS - 1)) is None


def test_calibrator_requires_both_outcome_classes():
    history = pd.DataFrame({"q_up": np.full(MIN_CALIBRATION_ROWS, 0.8), "outcome_up": np.ones(MIN_CALIBRATION_ROWS)})
    assert fit_platt_calibrator(history) is None


def test_calibrated_probability_is_bounded_and_uses_history_only():
    calibrator = fit_platt_calibrator(_history())
    assert calibrator is not None
    values = calibrated_probability(calibrator, [0.01, 0.5, 0.99])
    assert np.all(values > 0.0)
    assert np.all(values < 1.0)
    assert values[0] < values[1] < values[2]
