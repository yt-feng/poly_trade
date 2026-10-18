import hashlib
import unittest

from analysis.polybolt_twap_adapter import (
    PolyBoltContractError,
    build_continuity_manifest,
    normalize_polybolt_event,
    require_continuity,
)


BASE = {
    "topic": "prices.crypto.twap",
    "type": "update",
    "timestamp": 1_800_000_000_100,
    "seq": 1,
    "dropped": 0,
    "authenticated": True,
    "received_ms": 1_800_000_000_200,
    "connection_id": "conn-a",
    "sequence_scope": "capture-a/conn-a",
    "capture_id": "capture-a",
    "raw_payload_sha256": hashlib.sha256(b"event-1").hexdigest(),
    "payload": {
        "symbol": "btcusd",
        "timestamp": 1_800_000_000_100,
        "value": "100.000000000000000001",
        "windowSeconds": 60,
    },
}


def event(seq=1, value=None):
    row = dict(BASE)
    row["seq"] = seq
    row["timestamp"] = BASE["timestamp"] + (seq - 1) * 1000
    row["received_ms"] = BASE["received_ms"] + (seq - 1) * 1000
    row["payload"] = dict(BASE["payload"])
    row["payload"]["timestamp"] = row["timestamp"]
    if value is not None:
        row["payload"]["value"] = value
    row["raw_payload_sha256"] = hashlib.sha256(f"event-{seq}".encode()).hexdigest()
    return row


class PolyBoltAdapterTests(unittest.TestCase):
    def test_update_preserves_decimal_without_e18_scaling(self):
        out = normalize_polybolt_event(event())
        self.assertEqual(out["protocol"], "polybolt_crypto_twap_60")
        self.assertEqual(out["value"], "100.000000000000000001")
        self.assertEqual(out["window_seconds"], 60)
        self.assertEqual(out["seq"], 1)
        self.assertEqual(out["dropped"], 0)

    def test_camel_and_snake_window_aliases_must_agree(self):
        row = event()
        row["payload"]["window_seconds"] = 60
        self.assertEqual(normalize_polybolt_event(row)["window_seconds"], 60)
        row["payload"]["window_seconds"] = 30
        with self.assertRaises(PolyBoltContractError):
            normalize_polybolt_event(row)

    def test_snapshot_is_explicitly_bounded_history(self):
        row = event()
        row["type"] = "subscribe"
        row["payload"] = {
            "symbol": "btcusd",
            "data": [
                {"timestamp": 1_800_000_000_000, "value": "99.9"},
                {"timestamp": 1_800_000_000_100, "value": "100.0"},
            ],
            "windowSeconds": 60,
        }
        out = normalize_polybolt_event(row)
        self.assertEqual(out["kind"], "snapshot")
        self.assertTrue(out["history_snapshot_boundary"])
        self.assertEqual(len(out["points"]), 2)

    def test_rejects_legacy_symbol_old_window_float_and_missing_drop(self):
        for mutate in (
            lambda r: r["payload"].update(symbol="btc/usd"),
            lambda r: r["payload"].update(window_s=60),
            lambda r: r["payload"].update(value=100.0),
            lambda r: r.pop("dropped"),
            lambda r: r.update(authenticated=False),
        ):
            row = event()
            mutate(row)
            with self.subTest(row=row), self.assertRaises(PolyBoltContractError):
                normalize_polybolt_event(row)

    def test_rejects_future_or_mismatched_timestamps(self):
        row = event()
        row["payload"]["timestamp"] = row["received_ms"] + 1
        with self.assertRaises(PolyBoltContractError):
            normalize_polybolt_event(row)
        row = event()
        row["timestamp"] = row["received_ms"] + 1
        with self.assertRaises(PolyBoltContractError):
            normalize_polybolt_event(row)
        row = event()
        row["payload"]["timestamp"] = row["timestamp"]
        row["payload"]["value"] = "NaN"
        with self.assertRaises(PolyBoltContractError):
            normalize_polybolt_event(row)

    def test_continuity_manifest_rejects_gap_duplicate_and_reorder(self):
        good = [normalize_polybolt_event(event(1)), normalize_polybolt_event(event(2)), normalize_polybolt_event(event(3))]
        manifest = build_continuity_manifest(good)
        self.assertTrue(manifest["continuity_ok"])
        self.assertEqual(manifest["gap_count"], 0)
        self.assertEqual(require_continuity(good)["last_seq"], 3)

        gap = [normalize_polybolt_event(event(1)), normalize_polybolt_event(event(3))]
        self.assertFalse(build_continuity_manifest(gap)["continuity_ok"])
        with self.assertRaises(PolyBoltContractError):
            require_continuity(gap)
        duplicate = [normalize_polybolt_event(event(1)), normalize_polybolt_event(event(1))]
        self.assertFalse(build_continuity_manifest(duplicate)["continuity_ok"])
        reordered = [normalize_polybolt_event(event(2)), normalize_polybolt_event(event(1))]
        self.assertFalse(build_continuity_manifest(reordered)["continuity_ok"])

    def test_reconnect_scope_is_not_silently_joined(self):
        first = normalize_polybolt_event(event(1))
        second = normalize_polybolt_event(event(1))
        second["connection_id"] = "conn-b"
        second["sequence_scope"] = "capture-a/conn-b"
        second["reconnect_boundary"] = True
        manifest = build_continuity_manifest([first, second])
        self.assertFalse(manifest["continuity_ok"])
        self.assertEqual(manifest["reconnect_segments"], 2)


if __name__ == "__main__":
    unittest.main()
