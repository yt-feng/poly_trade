"""Seal full evidence independently of whether the economic endpoint passes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

import research_vault as vault
import seal_research_outcomes as delivery


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def evaluation_state(config: dict, summary: dict, independent: dict, inputs: dict,
                     evaluation_outcome: str, independent_outcome: str, input_outcome: str,
                     scoped_inputs: dict | None = None) -> dict:
    counters = {}
    for key in ("actual_model_fit_calls", "actual_model_predict_calls", "replayed_cells"):
        value = summary.get(key)
        counters[key] = value if type(value) is int and value >= 0 else (0 if evaluation_outcome in ("", "skipped") else None)
    input_verified = input_outcome == "success" and inputs.get("status") == "complete"
    audited = independent_outcome == "success" and independent.get("passed") is True
    continuation = config.get("evaluation_mode") == "offline_exit_continuation"
    calibration = config.get("evaluation_mode") == "offline_price_calibration"
    margin = config.get("evaluation_mode") == "offline_net_margin"
    if calibration or margin:
        execution_parent = config.get("parent_execution_config")
        parent = execution_parent.get("parent_strategy_config") if isinstance(execution_parent, dict) else None
    else:
        parent = config.get("parent_strategy_config")
    count = ((parent.get("expected_control_candidate_count") if isinstance(parent, dict) else None)
             if continuation or calibration or margin else config.get("expected_control_candidate_count"))
    def pooled(value):
        return (isinstance(value, dict) and value.get("scope") == "pooled_single_ledger"
                and type(value.get("block_index")) is int and value["block_index"] == 0)
    model_counters_valid = counters["actual_model_fit_calls"] == 0 and counters["actual_model_predict_calls"] == 0
    if margin:
        for key in ("net_margin_model_fit_calls", "net_margin_model_predict_calls", "control_model_fit_calls",
                    "control_model_predict_calls", "capacity_model_fit_calls", "capacity_model_predict_calls",
                    "model_scored_rows", "model_forecast_rows", "model_tree_reconstructions", "gate_rows",
                    "producer_replayed_cells", "producer_replay_completions"):
            value = summary.get(key)
            counters[key] = value if type(value) is int and value >= 0 else (0 if evaluation_outcome in ("", "skipped") else None)
        training = config.get("training_binding")
        test = training.get("expected_test_cohort") if isinstance(training, dict) else None
        scored = test.get("scored_rows") if isinstance(test, dict) else None
        max_fit, max_predict = config.get("max_model_fit_calls"), config.get("max_model_predict_calls")
        fits, predicts = counters["actual_model_fit_calls"], counters["actual_model_predict_calls"]
        forecast = counters["model_forecast_rows"]
        model_counters_valid = bool(type(max_fit) is int and type(max_predict) is int
            and 0 <= max_fit <= 79 and max_fit == max_predict
            and type(fits) is int and type(predicts) is int and 0 <= fits <= max_fit and fits == predicts
            and counters["net_margin_model_fit_calls"] == fits and counters["net_margin_model_predict_calls"] == predicts
            and counters["model_tree_reconstructions"] == fits
            and all(type(counters[k]) is int and counters[k] == 0 for k in ("control_model_fit_calls",
                "control_model_predict_calls", "capacity_model_fit_calls", "capacity_model_predict_calls"))
            and type(scored) is int and scored > 0 and counters["model_scored_rows"] == scored
            and type(forecast) is int and 0 <= forecast <= scored and ((fits == 0) == (forecast == 0)))
        input_verified = bool(input_verified and isinstance(scoped_inputs, dict) and scoped_inputs.get("status") == "complete")
        audited = bool(audited and independent.get("format") == "independent-net-margin-reader-v1"
            and type(independent.get("independent_ledger_reconstructions")) is int
            and independent["independent_ledger_reconstructions"] == 2)
        arms = summary.get("arms")
        structure_verified = (summary.get("baseline_control_reproduced") is True
            and summary.get("prefix_control_reproduced") is True and summary.get("control_execution_reproduced") is True
            and isinstance(arms, dict) and set(arms) == {"C_scheduled_retry_fullstream", "C_net_margin_veto"}
            and all(pooled(arm) for arm in arms.values())
            and type(counters["gate_rows"]) is int and counters["gate_rows"] == count
            and counters["producer_replayed_cells"] == 2 and counters["producer_replay_completions"] == 2)
    elif calibration:
        arms = summary.get("arms")
        transforms = summary.get("calibration_forecast_transforms")
        updates = summary.get("reference_label_updates")
        structure_verified = (summary.get("baseline_control_reproduced") is True
                              and summary.get("prefix_control_reproduced") is True
                              and isinstance(arms, dict)
                              and set(arms) == {"C_scheduled_retry_fullstream", "C_price_residual_calibration"}
                              and all(pooled(arm) for arm in arms.values())
                              and type(transforms) is int and transforms == count
                              and type(updates) is int and 0 <= updates <= transforms)
        counters["calibration_forecast_transforms"] = transforms if type(transforms) is int else None
        counters["reference_label_updates"] = updates if type(updates) is int else None
    elif continuation:
        structure_verified = (summary.get("prefix_control_reproduced") is True
                              and pooled(summary.get("prefix_ledger"))
                              and pooled(summary.get("full_ledger")))
    else:
        arms = summary.get("arms")
        structure_verified = (summary.get("control_execution_reproduced") is True
                              and isinstance(arms, dict) and set(arms) == {"C_original", "C_scheduled_retry"}
                              and all(pooled(arm) for arm in arms.values()))
    evaluated = bool(evaluation_outcome == "success" and input_verified and audited
                     and summary.get("strategy_evaluated") is True
                     and summary.get("comparison_valid") is True
                     and structure_verified
                     and summary.get("evaluation_status") == "evaluated"
                     and summary.get("live_orders_enabled") is False
                     and all(type(summary.get(key)) is int and summary[key] == 0
                             for key in ("actual_fills", "verified_canary_fills"))
                     and type(count) is int and count > 0
                     and type(summary.get("candidate_rows")) is int and summary["candidate_rows"] == count
                     and model_counters_valid
                     and counters["replayed_cells"] == (1 if continuation else 2)
                     and (not (continuation or calibration or margin)
                          or type(summary.get("producer_replay_completions")) is int
                          and summary["producer_replay_completions"] == (2 if calibration or margin else 1)))
    return {"strategy_evaluated": evaluated, "comparison_valid": evaluated,
            "input_verified": input_verified, "independent_audit_passed": audited, **counters}


def seal(private: Path, output: Path, recipient: dict, recipient_id: str, env: dict) -> dict:
    private.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The generic bundler retains results/, not arbitrary setup files. Preserve
    # both successful restoration and setup failures before its allowlist runs.
    for name in ("setup-failure.json", "input-receipt.json", "scoped-input-receipt.json"):
        source = private / name
        if source.exists() or source.is_symlink():
            target = private / "results" / ("setup-" + name if name in ("input-receipt.json", "scoped-input-receipt.json") else name)
            target.parent.mkdir(mode=0o700, exist_ok=True)
            if not delivery._copy_small(private, name, target):
                raise ValueError("setup_record_size_limit")
    config = read_object(private / "config.json")
    if config:
        shutil.copyfile(private / "config.json", private / "frozen_input.json")
        (private / "frozen_input.json").chmod(0o600)
    summary = read_object(private / "results/summary.json")
    independent = read_object(private / "diagnostics.txt")
    inputs = read_object(private / "input-receipt.json")
    state = evaluation_state(config, summary, independent, inputs,
                             env.get("EVALUATION_OUTCOME", ""), env.get("INDEPENDENT_OUTCOME", ""),
                             env.get("INPUT_OUTCOME", ""), read_object(private / "scoped-input-receipt.json"))
    diagnostic = private / "diagnostics.txt"
    mode = config.get("evaluation_mode", env.get("EVALUATION_MODE", "offline_exit_retry"))
    format_name = {"offline_exit_retry": "offline-exit-retry-sealed-v1",
                   "offline_exit_continuation": "offline-exit-continuation-sealed-v1",
                   "offline_price_calibration": "offline-price-calibration-sealed-v1",
                   "offline_net_margin": "offline-net-margin-sealed-v1"}
    metadata = {"format": format_name.get(mode, "offline-exit-retry-sealed-v1"), **state,
                "workflow": {key: env.get(key) for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA",
                    "EVALUATION_OUTCOME", "INDEPENDENT_OUTCOME", "INPUT_OUTCOME", "REQUEST_SHA256", "SETUP_OUTCOMES")},
                "independent_audit_helper_sha256": env.get("PRIVATE_AUDIT_SHA"),
                "independent_diagnostic_sha256": hashlib.sha256(diagnostic.read_bytes()).hexdigest() if diagnostic.is_file() else None,
                "model_refit": None if state["actual_model_fit_calls"] is None else state["actual_model_fit_calls"] > 0,
                "new_data_collected": False, "actual_fills": 0, "verified_canary_fills": 0,
                "live_orders_enabled": False,
                "economic_endpoint_scope": "Separate from successful evidence evaluation; may fail in a valid comparison."}
    return delivery.seal_outcomes(private, output, recipient, recipient_id, metadata=metadata,
                                  max_member_bytes=256 * 1024 ** 2)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    recipient = vault.load_json_bytes(Path("research_vault/recipient.json").read_bytes(), 4096)
    rid = os.environ["QUALITY_FIRST_RECIPIENT_ID"]
    vault.validate_recipient(recipient, rid)
    result = seal(args.private, args.output, recipient, rid, dict(os.environ))
    with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
        handle.write("delivery_complete=true\n")
        handle.write("full_evidence=" + str(result["full_evidence_retained"]).lower() + "\n")
        # The full generic status envelope does not expose scientific results.
        # This is an internal workflow gate, separate from economic profitability.
        state = evaluation_state(read_object(args.private / "config.json"), read_object(args.private / "results/summary.json"),
            read_object(args.private / "diagnostics.txt"), read_object(args.private / "input-receipt.json"),
            os.environ.get("EVALUATION_OUTCOME", ""), os.environ.get("INDEPENDENT_OUTCOME", ""),
            os.environ.get("INPUT_OUTCOME", ""), read_object(args.private / "scoped-input-receipt.json"))
        handle.write("comparison_evaluated=" + str(state["strategy_evaluated"] and result["full_evidence_retained"]).lower() + "\n")


if __name__ == "__main__":
    main()
