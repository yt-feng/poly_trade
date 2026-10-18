import importlib.util
import sys
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "analysis"
    / "quantml_candidate_execution_walk_forward.py"
)
SPEC = importlib.util.spec_from_file_location("quantml_controls", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_simulation_controls_are_explicit_and_restorable() -> None:
    old_bankroll, old_min_cost = MODULE.BANKROLL0, MODULE.MIN_COST
    try:
        MODULE.configure_simulation(10.0, 0.10)
        assert MODULE.BANKROLL0 == 10.0
        assert MODULE.MIN_COST == 0.10
        with pytest.raises(ValueError):
            MODULE.configure_simulation(0.0, 0.10)
        with pytest.raises(ValueError):
            MODULE.configure_simulation(10.0, -0.01)
    finally:
        MODULE.configure_simulation(old_bankroll, old_min_cost)
