import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))

import h467_pmdata_queue_latency_replay as h467


def test_contract_is_fixed_to_one_evolving_ledger():
    assert h467.INITIAL_CASH == 10.0
    assert h467.MINIMUM_SHARES == 5.0
    assert h467.REQUIRED_FILLS == 10
    assert h467.CASH_RELEASE_DELAY_SECONDS == 600.0
    assert h467.DATASET_REVISION == "acb8cecf4234677823ddd8ee1275de1db48aa995"
    assert h467.DATASET_SOURCE_REPO_COMMIT == "b7f29614a695740c2102760a4dacfb85b8d6e623"


def test_winning_outcome_normalization():
    assert h467._winning_outcome("yes") == "yes"
    assert h467._winning_outcome("NO") == "no"
    assert h467._winning_outcome("unknown") is None


def test_pmdata_market_book_has_complementary_token_mapping():
    assert h467._invert_winning_outcome("yes") == "no"
    assert h467._invert_winning_outcome("NO") == "yes"
    assert h467._invert_winning_outcome(None) is None
