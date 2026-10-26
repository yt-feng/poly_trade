from pathlib import Path

import pandas as pd

from analysis.h534_time_regime_gate import overnight_utc_gate


def test_overnight_gate_is_fixed_utc_window_and_excludes_boundary():
    frame = pd.DataFrame({
        "entry_ts": [
            "2026-01-01T00:00:00Z",
            "2026-01-01T01:59:59Z",
            "2026-01-01T02:00:00Z",
            "2026-01-01T23:59:59Z",
        ]
    })
    assert overnight_utc_gate(frame).tolist() == [True, True, False, False]


def test_gate_module_declares_paper_only_boundaries():
    source = Path(__file__).resolve().parents[1] / "analysis" / "h534_time_regime_gate.py"
    text = source.read_text()
    assert "H534_UTC_OVERNIGHT_REGIME_GATE" in text
    assert "model_weights_changed" in text
    assert "verified_canary_fills" in text
