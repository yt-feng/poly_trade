"""Synthetic timing regressions and compatibility of the seven preparation paths."""
import importlib
from pathlib import Path
import sys
from unittest.mock import patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import all_monthly_clob_systematic_research_v2 as base
import final_v1_live_candidate_search as v1
import monthly_runs_quant_research_framework as qrf
import quantml_candidate_execution_walk_forward as execution


@pytest.mark.parametrize("unit", ["ns", "us"])
@pytest.mark.parametrize("delay", [4, 8])
@pytest.mark.parametrize("signal_second", [110, 120])
@pytest.mark.parametrize("has_eligible_quote", [False, True])
def test_execution_delay_starts_at_decision_deadline(
    unit, delay, signal_second, has_eligible_quote
):
    first = pd.Timestamp("2026-01-01T00:00:00Z")
    seconds = [0, signal_second, 121]
    if has_eligible_quote:
        seconds += [124, 128]
    quotes = pd.DataFrame({
        "market_id": ["m"] * len(seconds),
        "ts_utc": pd.Series(
            [first + pd.Timedelta(seconds=s) for s in seconds],
            dtype=f"datetime64[{unit}, UTC]",
        ),
        "buy_up_cents": [50 + i for i in range(len(seconds))],
        "buy_down_cents": [50 - i for i in range(len(seconds))],
    })
    # The existing API needs neither freshness policy nor settlement metadata.
    out = execution.attach_execution_snapshots(
        pd.DataFrame({"market_id": ["m"]}), quotes, (delay,), (2,)
    ).iloc[0]
    for side in ("buy_up", "buy_down"):
        prefix = f"exec_{side}_2m_d{delay}"
        assert out[f"{prefix}_signal_ts"] == first + pd.Timedelta(seconds=signal_second)
        if has_eligible_quote:
            fill_second = 120 + delay
            assert out[f"{prefix}_quote_ts"] == first + pd.Timedelta(seconds=fill_second)
            expected_price = quotes.loc[seconds.index(fill_second), f"{side}_cents"] / 100
            assert out[f"{prefix}_quote_price"] == expected_price
            assert out[f"{prefix}_adverse_slippage"] == pytest.approx(
                expected_price - quotes.loc[1, f"{side}_cents"] / 100
            )
        else:
            assert pd.isna(out[f"{prefix}_quote_ts"])
            assert pd.isna(out[f"{prefix}_quote_price"])
            assert pd.isna(out[f"{prefix}_adverse_slippage"])


EXECUTION_CALLERS = [
    ("quantml_candidate_execution_walk_forward", "prepare_complete_data"),
    ("v1_execution_walk_forward", "prepare_data"),
    ("registry_small_cash_holdout", "prepare_registry_data"),
    ("classic_min_order_chain", "prepare"),
    ("causal_entry_gate_prefix_selection", "prepare_once"),
    ("h104_old_clean_compound_variants", "prepare_fast"),
    ("h60_fast_focused_registry_verification", "prepare_fast_data"),
]


@pytest.mark.parametrize("module_name,function", EXECUTION_CALLERS)
def test_existing_execution_callers_return_without_new_inputs(module_name, function, tmp_path):
    module = importlib.import_module(module_name)
    start = pd.Timestamp("2026-01-01T00:00:00Z")
    # Some callers require exactly 83 run names. These are synthetic names;
    # their count is not evidence of historical completeness or strategy quality.
    runs = [f"synthetic-{i:03}" for i in range(83)]
    seconds = (0, 50, 61, 64, 68, 110, 121, 124, 128, 230, 241, 244)
    raw = pd.DataFrame([{
        "ts_iso": start + pd.Timedelta(seconds=300 * i + s),
        "slug": f"synthetic-{1767225600 + 300 * i}", "run_name": run,
        "source_file": "synthetic.csv", "target_price": 100., "final_price": 99.,
        "buy_up_cents": 51., "buy_down_cents": 49.,
        "sell_up_cents": 50., "sell_down_cents": 48.,
        "buy_up_size": 200., "buy_down_size": 200.,
        "spread_up_cents": 1., "spread_down_cents": 1.,
    } for i, run in enumerate(runs) for s in seconds])
    coverage = pd.DataFrame({"run_name": runs, "file_count": [1] * len(runs)})
    quotes = base.prepare_quotes(raw)
    features = quotes.groupby("market_id", as_index=False).agg(
        first_quote_ts=("ts_utc", "min"), run_name=("run_name", "first")
    )
    features["outcome_up"] = 0.
    attach = execution.attach_execution_snapshots
    source = tmp_path / "unused-input"
    # Exercise the real preparation and execution lookup. Replace only source
    # loading and unrelated feature/model/signal work; no fitting or reports.
    with patch.object(base, "read_all_monthly_runs", return_value=(raw, coverage)) as loader, \
         patch.object(base, "build_features", return_value=features), \
         patch.object(v1, "add_session_labels", side_effect=lambda frame: frame), \
         patch.object(v1, "add_quality_features", side_effect=lambda frame: frame), \
         patch.object(qrf, "add_research_features", side_effect=lambda frame: frame), \
         patch.object(qrf, "rolling_logit_safe", return_value=float("nan")), \
         patch.object(qrf, "choose_trade", return_value=("skip", -1, 0., "synthetic")), \
         patch.object(v1, "choose_trade", return_value=("skip", -1, 0., "synthetic")), \
         patch.object(execution, "add_candidate_factors", side_effect=lambda frame: frame), \
         patch.object(module, "attach_execution_snapshots", side_effect=attach) as boundary, \
         patch.object(pd.DataFrame, "to_csv", side_effect=AssertionError("report write forbidden")), \
         patch("sklearn.linear_model.LogisticRegression.fit", side_effect=AssertionError("fit forbidden")):
        args = (source, ["micro_book_consensus_up"]) if function == "prepare" else (source,)
        result = getattr(module, function)(*args)
        loader.assert_called_once_with(source)
        boundary.assert_called_once()

    out = result[0]
    assert result[-1] == runs
    assert len(out) == len(runs)
    assert not source.exists()
    assert set(boundary.call_args.kwargs) == {"delays", "minutes"}
    for minute in boundary.call_args.kwargs["minutes"]:
        for delay in boundary.call_args.kwargs["delays"]:
            for side in ("buy_up", "buy_down"):
                prefix = f"exec_{side}_{minute}m_d{delay}"
                expected_signal = out.first_quote_ts + pd.Timedelta(seconds=60 * minute - 10)
                expected_fill = out.first_quote_ts + pd.Timedelta(seconds=60 * minute + delay)
                assert (out[f"{prefix}_signal_ts"] == expected_signal).all()
                assert (out[f"{prefix}_quote_ts"] == expected_fill).all()
