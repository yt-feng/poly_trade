import json
from pathlib import Path
import unittest

from analysis.execution_realism_contract import (
    OUTPUT_FIELDS,
    audit_execution_contract,
    expand_stress_cells,
    matrix_digest,
    validate_execution_contract,
)


ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "research" / "strategy" / "experiments" / "EXP-0002-btc5m-preregistered-reference.json"


def contract() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["execution_realism"]


class ExecutionRealismContractTests(unittest.TestCase):
    def test_registered_matrix_has_all_dimensions_and_null_metric_slots(self):
        value = contract()
        self.assertEqual(validate_execution_contract(value), [])
        cells = expand_stress_cells(value)
        self.assertEqual(len(cells), 64)
        self.assertEqual(value["matrix_sha256"], matrix_digest(value["stress_matrix"]))
        self.assertEqual(set(cells[0]["metrics"]), set(OUTPUT_FIELDS))
        self.assertTrue(all(metric is None for cell in cells for metric in cell["metrics"].values()))

    def test_missing_cost_edge_or_sensitivity_is_fail_closed(self):
        value = contract()
        value["minimum_edge"] = {}
        value["stress_matrix"].pop("slippage_ticks")
        errors = validate_execution_contract(value)
        self.assertIn("missing_minimum_edge_values", errors)
        self.assertIn("incomplete_stress_sensitivity", errors)
        report = audit_execution_contract(value, synthetic=True)
        self.assertTrue(any(reason.startswith("contract:") for reason in report["blockers"]))
        self.assertIn("synthetic_input_not_evidence", report["blockers"])

    def test_midpoint_and_unregistered_cell_are_blockers(self):
        value = contract()
        value["midpoint_allowed"] = True
        cell = expand_stress_cells(contract())[0]
        cell["stress"]["ttl_ms"] = 123
        report = audit_execution_contract(value, selected_cell=cell)
        self.assertIn("contract:midpoint_execution_forbidden", report["blockers"])
        self.assertIn("stress_cell_not_registered", report["blockers"])

    def test_incomplete_coverage_and_missing_output_field_are_blockers(self):
        value = contract()
        cells = expand_stress_cells(value)
        report = audit_execution_contract(
            value,
            covered_cell_ids=[cells[0]["cell_id"]],
            observed_outputs=[{"cell_id": cells[0]["cell_id"], "metrics": {"brier": None}}],
        )
        self.assertIn("incomplete_stress_sensitivity", report["blockers"])
        self.assertIn("missing_stress_output_field:net_pnl_usdc", report["blockers"])

    def test_observed_cell_set_must_be_complete_and_known(self):
        value = contract()
        cells = expand_stress_cells(value)
        outputs = [{"cell_id": cell["cell_id"], "metrics": {field: None for field in OUTPUT_FIELDS}} for cell in cells]
        outputs.append({"cell_id": "unknown", "metrics": {field: None for field in OUTPUT_FIELDS}})
        report = audit_execution_contract(value, observed_outputs=outputs)
        self.assertIn("unknown_stress_cell", report["blockers"])

    def test_duplicate_cell_records_are_blocked(self):
        value = contract()
        cell = expand_stress_cells(value)[0]
        output = {"cell_id": cell["cell_id"], "metrics": {field: 1 for field in OUTPUT_FIELDS}}
        report = audit_execution_contract(value, covered_cell_ids=[cell["cell_id"], cell["cell_id"]], observed_outputs=[output, output])
        self.assertIn("duplicate_stress_cell", report["blockers"])


if __name__ == "__main__":
    unittest.main()
