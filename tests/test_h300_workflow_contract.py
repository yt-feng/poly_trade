from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "h300-owner-scoped-payout-transfer-capture.yml"


def test_h300_defaults_to_the_verified_read_only_receipt_endpoint():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "default: https://polygon.drpc.org" in text
