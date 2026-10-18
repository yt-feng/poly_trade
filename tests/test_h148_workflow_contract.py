from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "h148-cross-candidate-controller.yml"
SOURCE_COMMIT = "bbda5f2050685f63153621a54641ebdfab943487"


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_frozen_source_commit_is_job_scoped_for_all_steps():
    text = _workflow_text()
    job_start = text.index("  h148-cross-candidate-controller:")
    steps_start = text.index("    steps:", job_start)
    job_header = text[job_start:steps_start]
    assert f"SOURCE_DATA_COMMIT: {SOURCE_COMMIT}" in job_header

    run_start = text.index("      - name: Run H148 on the frozen old-clean source")
    seal_start = text.index("      - name: Seal H148 results")
    run_block = text[run_start:seal_start]
    seal_block = text[seal_start:]
    assert "SOURCE_DATA_COMMIT:" not in run_block
    assert "SOURCE_DATA_COMMIT:" not in seal_block
    assert "os.environ['SOURCE_DATA_COMMIT']" in seal_block


def test_seal_step_cannot_depend_on_run_step_only_environment():
    text = _workflow_text()
    run_start = text.index("      - name: Run H148 on the frozen old-clean source")
    seal_start = text.index("      - name: Seal H148 results")
    run_block = text[run_start:seal_start]
    seal_block = text[seal_start:]
    assert "SOURCE_DATA_COMMIT:" not in run_block
    assert "SOURCE_DATA_COMMIT:" not in seal_block
    assert "SOURCE_DATA_COMMIT: bbda5f2050685f63153621a54641ebdfab943487" in text
