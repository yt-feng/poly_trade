"""Fail-closed validation before a quality-first research dispatch.

This check only validates the dispatch envelope.  It does not acquire public
data, train a model, inspect an account, or place an order.  In particular, a
forward run must carry the exact frozen model/protocol payload that the
research runner will accept; otherwise the expensive research job is never
started.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import sys
from typing import Any

# The workflow invokes this file as ``python tools/...`` from repository root;
# keep imports deterministic in both that form and direct test imports.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analysis"))

from quality_first_research import (
    VERSION,
    digest,
    protocol_contract,
    validate_research_challenger,
)


MODES = frozenset({"discovery", "challenger", "forward"})
MAX_FROZEN_BYTES = 4 * 1024 * 1024


class PreflightError(ValueError):
    """A dispatch envelope cannot be admitted."""


def _decode_frozen(encoded: str) -> dict[str, Any]:
    if not isinstance(encoded, str) or not encoded.strip():
        raise PreflightError("FROZEN_INPUT_MISSING")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise PreflightError("FROZEN_INPUT_NOT_BASE64") from exc
    if not raw or len(raw) > MAX_FROZEN_BYTES:
        raise PreflightError("FROZEN_INPUT_SIZE_INVALID")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError("FROZEN_INPUT_NOT_JSON") from exc
    if not isinstance(value, dict):
        raise PreflightError("FROZEN_INPUT_NOT_OBJECT")
    return value


def validate_dispatch(mode: str, frozen_input_b64: str | None, initial_cash: float = 10.0) -> dict[str, Any]:
    """Validate one dispatch without returning secret or model contents."""
    if mode not in MODES:
        raise PreflightError("RESEARCH_MODE_INVALID")
    if type(initial_cash) not in (int, float) or initial_cash != 10.0:
        raise PreflightError("INITIAL_CASH_NOT_FROZEN")
    if mode != "forward":
        return {"mode": mode, "forward_input_checked": False}

    frozen = _decode_frozen(frozen_input_b64 or "")
    expected = frozen.get("sha256")
    payload = dict(frozen)
    payload.pop("sha256", None)
    if expected != digest(payload) or frozen.get("version") != VERSION:
        raise PreflightError("FROZEN_MODEL_INTEGRITY_MISMATCH")
    if frozen.get("protocol_sha256") != digest(protocol_contract()):
        raise PreflightError("FROZEN_MODEL_PROTOCOL_CHANGED")
    if frozen.get("initial_cash") != initial_cash:
        raise PreflightError("FROZEN_INITIAL_CASH_CHANGED")
    if frozen.get("kind") == "research_challenger":
        try:
            validate_research_challenger(frozen)
        except (TypeError, ValueError) as exc:
            raise PreflightError("FROZEN_EPISODE_INVALID") from exc
    elif frozen.get("kind") not in (None, ""):
        raise PreflightError("FROZEN_KIND_INVALID")
    return {"mode": mode, "forward_input_checked": True}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="discovery")
    parser.add_argument("--initial-cash", type=float, default=10.0)
    args = parser.parse_args()
    try:
        validate_dispatch(args.mode, os.environ.get("FROZEN_INPUT_B64"), args.initial_cash)
    except PreflightError:
        print("RESEARCH_DISPATCH_PREFLIGHT_FAILED", flush=True)
        return 2
    print("RESEARCH_DISPATCH_PREFLIGHT_OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
