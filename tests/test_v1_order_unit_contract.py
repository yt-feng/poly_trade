from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS = ROOT / "analysis"


def _has_keyword(call: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in call.keywords)


def test_v1_execution_callers_declare_order_unit():
    """Prevent a legacy notional default from leaking into new replays."""
    missing: list[str] = []
    for path in sorted(ANALYSIS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            is_study_call = (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "simulate"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "study"
            )
            is_local_call = (
                path.name == "v1_execution_walk_forward.py"
                and isinstance(node.func, ast.Name)
                and node.func.id == "simulate"
            )
            if (is_study_call or is_local_call) and not _has_keyword(node, "min_order_mode"):
                missing.append(f"{path}:{node.lineno}")
    assert missing == []


def test_named_share_floor_entrypoint_is_explicitly_share_based():
    path = ANALYSIS / "v1_execution_walk_forward.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "simulate_share_floor"
    )
    calls = [
        node for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "simulate"
    ]
    assert len(calls) == 1
    mode = next(keyword.value for keyword in calls[0].keywords if keyword.arg == "min_order_mode")
    assert isinstance(mode, ast.Constant)
    assert mode.value == "shares"
