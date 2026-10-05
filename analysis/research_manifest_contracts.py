"""Validate both registered experiment formats without evaluating a strategy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from analysis.preregistered_strategy import validate_manifest


def validate_experiment_manifest(manifest: Any) -> list[str]:
    """Dispatch explicit strategy manifests; retain the original experiment gate."""
    if not isinstance(manifest, dict):
        return ["experiment_manifest_not_object"]

    kind = manifest.get("manifest_type")
    if kind == "pre_registered_strategy_hypothesis":
        return validate_manifest(manifest)
    if "manifest_type" in manifest:
        return ["unsupported_experiment_manifest_type"]
    # A damaged typed manifest must not pass as a legacy experiment merely
    # because legacy identity fields were added to it.
    if any(field in manifest for field in ("schema_version", "strategy_id", "manifest_sha256")):
        return ["missing_experiment_manifest_type"]

    errors = []
    identity = manifest.get("experiment_id")
    if not isinstance(identity, str) or not identity.strip():
        errors.append("missing_experiment_id")
    if manifest.get("scope") != "strategy_research":
        errors.append("invalid_experiment_scope")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict) or protocol.get("public_quotes_are_not_fills") is not True:
        errors.append("experiment_public_quote_boundary_missing")
    return errors


def validate_experiment_directory(directory: Path) -> list[Path]:
    """Validate every JSON record, with its filename in any failure diagnostic."""
    paths = sorted(directory.glob("*.json"))
    if not paths:
        raise ValueError(f"{directory}: no experiment manifests")
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise ValueError(f"{path}: unreadable experiment JSON") from error
        try:
            errors = validate_experiment_manifest(manifest)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{path}: invalid experiment field types") from error
        if errors:
            raise ValueError(f"{path}: {', '.join(errors)}")
    return paths
