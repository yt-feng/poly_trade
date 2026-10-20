from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h414-expanded-hf-maker-replay.yml"


def test_h414_is_fixed_contiguous_small_cash_and_sealed():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'default: "24"' in text
    assert 'default: "0"' in text
    assert "window_count must remain the pre-registered 24" in text
    assert "start_offset must remain the pre-registered 0" in text
    assert "QUEUE_VALUES: 0,5,10" in text
    assert "--queue \"$QUEUE_VALUES\"" in text
    assert "split_size_usd" not in text  # the fixed $5 split is enforced by the adapter
    assert "no-concatenation" in text.lower() or "no_concatenation" in text.lower()
    assert "h414-expanded-hf-maker-replay-sealed-v1" in text
    assert "live_orders_enabled': False" in text


def test_h414_pins_both_public_inputs():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "DATASET_REVISION: f3bfb79f19611ea3115ad29c18720166ee6dd0b5" in text
    assert "SOURCE_COMMIT: accd1511a08748302c8116ab65575930e1240068" in text
    assert "DATASET_BASE:" in text
    assert "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-${start}" in text
