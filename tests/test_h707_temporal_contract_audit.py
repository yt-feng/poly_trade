import unittest

import pandas as pd

from analysis import h707_temporal_contract_audit as audit


class H707TemporalContractTests(unittest.TestCase):
    def _frame(self, age4=0.0):
        return pd.DataFrame({
            "market_id": ["m"], "run_name": ["r"], "slug": ["btc-updown-5m-1700000300"],
            "first_quote_ts": [pd.Timestamp("2023-11-14T22:10:00Z")],
            "snapshot_age_seconds_1m": [0.0], "snapshot_age_seconds_2m": [0.0],
            "snapshot_age_seconds_4m": [age4], "outcome_up": [1.0],
        })

    def test_four_minute_feature_is_rejected_at_124_second_entry(self):
        frame = self._frame(0.0)
        decision = pd.to_datetime(frame["first_quote_ts"], utc=True) + pd.to_timedelta(124, unit="s")
        observed = audit._feature_timestamp(frame, "btc_move_4m")
        self.assertGreater(observed.iloc[0], decision.iloc[0])

    def test_four_minute_snapshot_can_be_causal_only_if_observed_early(self):
        frame = self._frame(120.0)
        decision = pd.to_datetime(frame["first_quote_ts"], utc=True) + pd.to_timedelta(124, unit="s")
        observed = audit._feature_timestamp(frame, "btc_move_4m")
        self.assertLessEqual(observed.iloc[0], decision.iloc[0])

    def test_unmapped_feature_is_explicit(self):
        self.assertNotIn("not_a_feature", audit.FEATURE_CUTOFF_SECONDS)


if __name__ == "__main__":
    unittest.main()
