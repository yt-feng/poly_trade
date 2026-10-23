"""Restore two separately scoped saved inputs; never unlock a journal identity."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import re

import research_vault as vault
import scoped_research_dataset as dataset
from verify_saved_research_input import fetch_ciphertext, EPISODE_PATH

INPUT_FILES = {
    "dataset": {"features.csv": "features_csv_sha256", "quote-cash-domains.csv": "domains_csv_sha256"},
    "control": {"control-predictions.csv": "control_predictions_csv_sha256"},
}


def restore_inputs(config: dict, capabilities: dict, output: Path, *, repository: str, code_commit: str,
                   fetch=fetch_ciphertext) -> dict:
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
            or repository.rsplit("/", 1)[1] in {".", ".."}
            or config.get("expected_code_commit") != code_commit or not re.fullmatch(r"[0-9a-f]{40}", code_commit)
            or set(config.get("private_inputs", {})) != set(INPUT_FILES)
            or set(capabilities) != set(INPUT_FILES)):
        raise ValueError("invalid saved input/code binding")
    descriptors = {}
    for kind, files in INPUT_FILES.items():
        spec = config["private_inputs"][kind]
        if not isinstance(spec, dict) or set(spec) != {"descriptor_sha256", "source"}:
            raise ValueError("invalid saved input specification")
        source = spec["source"]
        if (not isinstance(source, dict) or set(source) != {"repository", "commit", "path"}
                or source["repository"] != repository
                or not isinstance(source["commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", source["commit"])
                or not isinstance(source["path"], str) or not EPISODE_PATH.fullmatch(source["path"])):
            raise ValueError("invalid immutable saved input source")
        descriptor = dataset.validate_capability(capabilities[kind], spec["descriptor_sha256"])
        members = {v["path"]: v for v in descriptor["files"]}
        if set(members) != set(files) | {"provenance.json", "dataset-manifest.json"}:
            raise ValueError("unexpected saved input members")
        if any(members[name]["sha256"] != config["cache_bindings"][key] for name, key in files.items()):
            raise ValueError("saved CSV binding mismatch")
        descriptors[kind] = descriptor
    output = Path(output)
    if output.is_symlink() or not output.is_dir():
        raise ValueError("private bootstrap directory required")
    if any((output / kind).exists() or (output / kind).is_symlink() for kind in INPUT_FILES):
        raise ValueError("refusing existing restored input")
    receipt = {"format": "quality-capacity-saved-inputs-v1", "status": "complete", "inputs": {},
               "strategy_evaluated": False, "model_fit_calls": 0, "model_predict_calls": 0,
               "policy_replays": 0, "global_identity_unlocks": 0, "live_orders_enabled": False}
    for kind in INPUT_FILES:
        spec = config["private_inputs"][kind]
        envelope = fetch(spec["source"], descriptors[kind]["cipher_bytes"], token=os.environ.get("GH_TOKEN"))
        restored = dataset.restore(envelope, capabilities[kind], spec["descriptor_sha256"], output / kind)
        receipt["inputs"][kind] = {"immutable_source": spec["source"],
            "descriptor_sha256": spec["descriptor_sha256"], "cipher_sha256": hashlib.sha256(envelope).hexdigest(),
            "files": descriptors[kind]["files"], "restoration": restored}
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "dataset-capability", "control-capability", "bootstrap", "receipt"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        config = vault.load_json_bytes(vault.read_bounded(args.config, 262144), 262144)
        capabilities = {kind: vault.load_json_bytes(vault.read_bounded(path, 16384), 16384)
                        for kind, path in (("dataset", args.dataset_capability), ("control", args.control_capability))}
        receipt = restore_inputs(config, capabilities, Path(args.bootstrap),
            repository=os.environ["GITHUB_REPOSITORY"], code_commit=os.environ["GITHUB_SHA"])
        vault.write_new(args.receipt, vault.canonical(receipt))
        return 0
    except Exception:
        vault.write_new(args.receipt, vault.canonical({"format": "quality-capacity-saved-inputs-v1", "status": "failed",
            "strategy_evaluated": False, "model_fit_calls": 0, "model_predict_calls": 0,
            "policy_replays": 0, "global_identity_unlocks": 0, "live_orders_enabled": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
