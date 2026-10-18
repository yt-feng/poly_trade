import base64
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

from quality_first_research import digest, protocol_contract
from research_dispatch_preflight import PreflightError, validate_dispatch


def frozen_payload():
    payload = {
        "version": 1,
        "model": None,
        "threshold": 0.01,
        "source_hash": "source",
        "frozen_at_ms": 1,
        "consumed_through_ms": 0,
        "forward_min_window_start_ms": 300000,
        "initial_cash": 10.0,
        "live_enabled": False,
    }
    payload["protocol_sha256"] = digest(protocol_contract())
    payload["sha256"] = digest(payload)
    return payload


def encode(value):
    return base64.b64encode(json.dumps(value, sort_keys=True).encode()).decode()


class DispatchPreflightTests(unittest.TestCase):
    def test_discovery_does_not_require_frozen_secret(self):
        self.assertFalse(validate_dispatch("discovery", None)["forward_input_checked"])


    def test_forward_requires_a_frozen_secret(self):
        with self.assertRaisesRegex(PreflightError, "FROZEN_INPUT_MISSING"):
            validate_dispatch("forward", None)

    def test_forward_accepts_exact_frozen_protocol_payload(self):
        self.assertTrue(validate_dispatch("forward", encode(frozen_payload()))["forward_input_checked"])

    def test_forward_rejects_tampered_payload_without_exposing_details(self):
        payload = frozen_payload()
        payload["threshold"] = 0.02
        with self.assertRaisesRegex(PreflightError, "FROZEN_MODEL_INTEGRITY_MISMATCH"):
            validate_dispatch("forward", encode(payload))

    def test_forward_rejects_protocol_or_cash_mutation(self):
        payload = frozen_payload()
        payload["initial_cash"] = 11.0
        payload["sha256"] = digest({k: v for k, v in payload.items() if k != "sha256"})
        with self.assertRaisesRegex(PreflightError, "FROZEN_INITIAL_CASH_CHANGED"):
            validate_dispatch("forward", encode(payload))

    def test_forward_rejects_missing_episode_structure(self):
        payload = frozen_payload()
        del payload["forward_min_window_start_ms"]
        payload["sha256"] = digest({k: v for k, v in payload.items() if k != "sha256"})
        with self.assertRaisesRegex(PreflightError, "FROZEN_EPISODE_INVALID"):
            validate_dispatch("forward", encode(payload))

    def test_forward_rejects_unknown_kind(self):
        payload = frozen_payload()
        payload["kind"] = "live"
        payload["sha256"] = digest({k: v for k, v in payload.items() if k != "sha256"})
        with self.assertRaisesRegex(PreflightError, "FROZEN_KIND_INVALID"):
            validate_dispatch("forward", encode(payload))

    def test_unknown_mode_is_rejected(self):
        with self.assertRaisesRegex(PreflightError, "RESEARCH_MODE_INVALID"):
            validate_dispatch("live", None)
