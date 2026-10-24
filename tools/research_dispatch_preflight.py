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
import math
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
    try:
        integrity_ok = expected == digest(payload)
        protocol_ok = frozen.get("protocol_sha256") == digest(protocol_contract())
    except Exception as exc:
        raise PreflightError("FROZEN_MODEL_INTEGRITY_MISMATCH") from exc
    if not integrity_ok or frozen.get("version") != VERSION:
        raise PreflightError("FROZEN_MODEL_INTEGRITY_MISMATCH")
    if not protocol_ok:
        raise PreflightError("FROZEN_MODEL_PROTOCOL_CHANGED")
    if frozen.get("initial_cash") != initial_cash:
        raise PreflightError("FROZEN_INITIAL_CASH_CHANGED")
    if "model" not in frozen or "threshold" not in frozen:
        raise PreflightError("FROZEN_EPISODE_INVALID")
    begin = frozen.get("forward_min_window_start_ms")
    if type(begin) is not int or begin < 0:
        raise PreflightError("FROZEN_EPISODE_INVALID")
    threshold = frozen.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
        raise PreflightError("FROZEN_EPISODE_INVALID")
    if payload.get("kind") == "research_challenger":
        try:
            # The research runner validates the signed payload after removing
            # its outer sha256 envelope field. Keep the dispatch preflight in
            # lockstep so a valid challenger is not rejected merely because
            # the envelope signature is still attached during episode checks.
            validate_research_challenger(payload)
        except Exception as exc:
            raise PreflightError("FROZEN_EPISODE_INVALID") from exc
    elif payload.get("kind") is not None:
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
