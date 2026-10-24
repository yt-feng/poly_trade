"""Restore the already-registered H695 inputs and H696 score bundle for H697.

This transport helper only verifies and restores immutable encrypted bytes.  It
does not read a score, fit a model, or perform a strategy replay; the analysis
step owns the bounded aggregate read.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
from pathlib import Path

import prepare_exit_retry_inputs as transport
import research_vault as vault
import scoped_research_dataset as dataset
from restore_quality_capacity_inputs import restore_inputs
from verify_saved_research_input import fetch_ciphertext


def _decode_capability(encoded: str) -> dict:
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 45000:
        raise ValueError("capability_size")
    return vault.load_json_bytes(base64.b64decode(encoded, validate=True), 16384)


def materialize(*, bootstrap: Path, score_envelope: Path, score_descriptor: Path,
                config_sha256: str, score_descriptor_sha256: str) -> dict:
    raw_config = transport.decode_xz(os.environ["NET_MARGIN_CONFIG_B64"], 262144)
    if hashlib.sha256(raw_config).hexdigest() != config_sha256:
        raise ValueError("configuration_digest_mismatch")
    config = vault.load_json_bytes(raw_config, 262144)
    binding = config.get("training_binding")
    if not isinstance(binding, dict) or not isinstance(binding.get("private_inputs"), dict):
        raise ValueError("saved_training_binding_missing")
    # Only the immutable input descriptors and byte hashes are passed to the
    # restore helper.  The old experiment's code commit is deliberately not
    # reused because this cycle performs no model or execution computation.
    view = {
        "expected_code_commit": os.environ["GITHUB_SHA"],
        "cache_bindings": binding["cache_bindings"],
        "private_inputs": binding["private_inputs"],
    }
    bootstrap.mkdir(mode=0o700, parents=True, exist_ok=False)
    capabilities = {
        "dataset": _decode_capability(os.environ["SCOPED_RESEARCH_DATA_CAPABILITY_B64"]),
        "control": _decode_capability(os.environ["SCOPED_RESEARCH_CONTROL_CAPABILITY_B64"]),
    }
    saved_receipt = restore_inputs(
        view, capabilities, bootstrap, repository=os.environ["GITHUB_REPOSITORY"],
        code_commit=os.environ["GITHUB_SHA"], fetch=fetch_ciphertext,
    )
    score_capability = _decode_capability(os.environ["H696_SCORE_BUNDLE_CAPABILITY_B64"])
    descriptor = dataset.validate_capability(score_capability, score_descriptor_sha256)
    registered_descriptor = vault.load_json_bytes(score_descriptor.read_bytes(), 16384)
    if registered_descriptor != descriptor:
        raise ValueError("score_descriptor_capability_mismatch")
    envelope = score_envelope.read_bytes()
    score_root = bootstrap.parent / "score"
    # scoped_research_dataset.restore reserves and creates this directory
    # atomically after all ciphertext checks; do not pre-create it here.
    dataset.restore(envelope, score_capability, score_descriptor_sha256, score_root)
    if not (score_root / "scores.csv").is_file():
        raise ValueError("score_bundle_missing_scores")
    execution_config = {
        "base_protocol": config["parent_execution_config"]["parent_strategy_config"]["base_protocol"],
        "expected_test_cohort": binding["expected_test_cohort"],
    }
    vault.write_new(bootstrap.parent / "execution-config.json", vault.canonical(execution_config))
    receipt = {
        "format": "h697-candidate-coverage-input-receipt-v1",
        "status": "complete",
        "saved_input_restore": saved_receipt,
        "score_descriptor_sha256": dataset.descriptor_sha256(descriptor),
        "score_files_verified": len(descriptor["files"]),
        "model_fit_calls": 0,
        "model_predict_calls": 0,
        "strategy_replays": 0,
        "actual_fills": 0,
        "live_orders_enabled": False,
    }
    vault.write_new(bootstrap.parent / "receipt.json", vault.canonical(receipt))
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--score-envelope", type=Path, required=True)
    parser.add_argument("--score-descriptor", type=Path, required=True)
    parser.add_argument("--config-sha256", required=True)
    parser.add_argument("--score-descriptor-sha256", required=True)
    args = parser.parse_args()
    materialize(
        bootstrap=args.bootstrap,
        score_envelope=args.score_envelope,
        score_descriptor=args.score_descriptor,
        config_sha256=args.config_sha256,
        score_descriptor_sha256=args.score_descriptor_sha256,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
