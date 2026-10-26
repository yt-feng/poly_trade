from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "h508-h301-forward-adapter.yml"


def test_forward_adapter_uses_private_input_and_never_live_orders():
    text = WORKFLOW.read_text()
    assert "H508_H301_FORWARD_INPUT_B64" in text
    assert "live_orders_enabled': False" in text
    assert "plaintext_published': False" in text
    assert "--source sealed-delivery" in text
    assert "POLYMARKET_PRIVATE_KEY" not in text
    assert "clob-client" not in text.lower()


def test_private_input_is_preflighted_before_runner_setup_and_dependencies():
    text = WORKFLOW.read_text()
    preflight = text.index("- name: Preflight registered private forward bundle")
    setup = text.index("- name: Set up Python")
    install = text.index("python -m pip install", setup)
    decode = text.index("- name: Decode the registered private forward bundle")
    assert preflight < setup < install < decode
    assert "FORWARD_INPUT_REGISTERED" in text
    assert "exit 78" in text[preflight:setup]
