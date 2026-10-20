"""Execute only synthetic H373 shell stages; no source data, credentials or API calls."""
import base64
import importlib.util
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import textwrap

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = (
    "h373-maker-touch-persistence-repair.yml",
    "h373-maker-touch-sensitivity-matrix.yml",
    "h373-maker-touch-sensitivity.yml",
)
SPEC = importlib.util.spec_from_file_location("h373_test_vault", ROOT / "tools/research_vault.py")
VAULT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VAULT)
MARKER = "SYNTHETIC_PRIVATE_candidate_alpha_wallet_beta"


def shell_steps(filename):
    """Read these fixed-format workflow run blocks without another test dependency."""
    source = (ROOT / ".github/workflows" / filename).read_text()
    steps = []
    for match in re.finditer(r"^      - name: ([^\n]+)\n(.*?)(?=^      - name: |\Z)", source, re.M | re.S):
        name, body = match.groups()
        run = re.search(r"^        run: (\|\n.*|[^\n]+)", body, re.M | re.S)
        if run:
            value = run.group(1)
            code = textwrap.dedent(value[2:]) if value.startswith("|\n") else value + "\n"
            steps.append((name, code, "        if: always()" in body))
    return steps


def stage(name):
    if name.startswith(("Recompute", "Run one H373", "Run H373")):
        return "analysis"
    if name.startswith("Seal"):
        return "seal"
    if name.startswith("Publish encrypted"):
        return "publish"
    if "summary" in name.lower():
        return "summary"
    if name.startswith("Remove runner-local"):
        return "cleanup"
    return None


def synthetic_workspace(tmp_path, failure):
    for name in ("analysis", "tools", "bin", "runner"):
        (tmp_path / name).mkdir()
    python = tmp_path / "bin/python"
    python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o700)
    # The original cleanup commands execute only against this temporary sandbox.
    remove = tmp_path / "bin/rm"
    remove.write_text("#!/bin/sh\nif [ \"$FAIL_STAGE\" = cleanup ]; then\n"
                      f"  echo {MARKER}_cleanup >&2\n  exit 23\nfi\n"
                      'exec /bin/rm "$@"\n')
    remove.chmod(0o700)
    (tmp_path / "tools/sitecustomize.py").write_text(textwrap.dedent(f"""
        import os, shutil, sys
        def no_network(event, args):
            if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
                raise AssertionError('Synthetic validation forbids external I/O')
        sys.addaudithook(no_network)
        if os.environ.get('FAIL_STAGE') == 'cleanup':
            def failed_cleanup(*args, **kwargs):
                print('{MARKER}_cleanup', file=sys.stderr)
                raise SystemExit(23)
            shutil.rmtree = failed_cleanup
    """))
    (tmp_path / "analysis/h373_maker_touch_sensitivity_old_clean.py").write_text(textwrap.dedent(f"""
        import argparse, json, os, sys
        from pathlib import Path
        parser = argparse.ArgumentParser()
        parser.add_argument('--source-root')
        parser.add_argument('--source-commit')
        parser.add_argument('--crossing-cents')
        parser.add_argument('--output', type=Path)
        args = parser.parse_args()
        # Never read source-root: deliberately emit identifying synthetic output.
        print('{MARKER}_stdout', flush=True)
        print('{MARKER}_stderr', file=sys.stderr, flush=True)
        args.output.mkdir()
        (args.output / 'report.json').write_text(json.dumps({{'finding': '{MARKER}'}}))
        (args.output / 'metrics.csv').write_text('finding\\n{MARKER}\\n')
        if os.environ['FAIL_STAGE'] == 'analysis':
            raise SystemExit(17)
    """))
    # Real envelope encryption with an ephemeral synthetic recipient. Faults are
    # injected around seal/write_new without touching production tools or keys.
    (tmp_path / "tools/research_vault.py").write_text(textwrap.dedent(f"""
        import os, runpy, sys
        globals().update({{k: v for k, v in runpy.run_path({str(ROOT / 'tools/research_vault.py')!r}).items()
                          if not k.startswith('__')}})
        original_seal, original_write = seal, write_new
        def seal(*args, **kwargs):
            print('{MARKER}_seal_stdout')
            print('{MARKER}_seal_stderr', file=sys.stderr)
            if os.environ['FAIL_STAGE'] == 'seal':
                raise ValueError('{MARKER}_seal_exception')
            return original_seal(*args, **kwargs)
        def write_new(path, *args, **kwargs):
            if os.environ['FAIL_STAGE'] == 'partial_seal' and path.name == 'status.json':
                raise ValueError('{MARKER}_partial_seal_exception')
            return original_write(path, *args, **kwargs)
    """))
    (tmp_path / "tools/publish_quality_first_journal.py").write_text(textwrap.dedent(f"""
        import argparse, json, os, sys
        from pathlib import Path
        sys.path.insert(0, {str(ROOT / 'tools')!r})
        from persist_quality_first_journal import validate_delivery
        parser = argparse.ArgumentParser()
        for flag in ('source', 'repository', 'run-id', 'run-attempt', 'recipient-id'):
            parser.add_argument('--' + flag, required=True)
        args = parser.parse_args()
        Path('publisher-called.json').write_text(json.dumps(vars(args)))
        validate_delivery(args.source, args.recipient_id)
        print('{MARKER}_publish_stdout')
        print('{MARKER}_publish_stderr', file=sys.stderr)
        if os.environ['FAIL_STAGE'] == 'publish':
            raise SystemExit(37)
        # Fake delivery receipt only; no GitHub client or network invocation.
        Path('synthetic-published.json').write_text(json.dumps(vars(args)))
    """))
    private_key = X25519PrivateKey.generate()
    recipient = VAULT.recipient_for(private_key)
    env = {
        "PATH": str(tmp_path / "bin") + ":/usr/bin:/bin",
        "PYTHONPATH": "tools", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_RUN_ID": "123456789", "GITHUB_RUN_ATTEMPT": "2", "GITHUB_REF": "refs/heads/main",
        "SOURCE_DATA_COMMIT": "a" * 40,
        "QUALITY_FIRST_RECIPIENT_JSON_B64": base64.b64encode(VAULT.canonical(recipient)).decode(),
        "QUALITY_FIRST_RECIPIENT_ID": recipient["recipient_id"],
        "RESEARCH_RECIPIENT_ID": recipient["recipient_id"], "FAIL_STAGE": failure,
    }
    return env, private_key


@pytest.mark.parametrize("filename", WORKFLOWS)
@pytest.mark.parametrize("failure", ["none", "analysis", "seal", "partial_seal", "publish",
                                     "cleanup", "stale_private", "stale_delivery"])
def test_private_output_and_fail_closed_delivery(filename, failure, tmp_path):
    env, key = synthetic_workspace(tmp_path, failure)
    directory = ("h373-repair-0.25" if "repair" in filename else
                 "h373-0.25" if "matrix" in filename else "h373")
    private = tmp_path / "runner" / directory
    delivery = tmp_path / "sealed-delivery"
    if failure == "stale_private":
        private.mkdir()
        (private / "analysis-output.txt").write_text(MARKER + "_stale")
    if failure in {"stale_private", "stale_delivery"}:
        delivery.mkdir()
        (delivery / "part-0000.vault").write_text(MARKER + "_unsealed_stale")

    public = []
    results = []
    private_logs = {}
    ciphertext = None
    failed = False
    for name, script, always in shell_steps(filename):
        kind = stage(name)
        if kind is None:
            continue
        if kind in {"analysis", "seal", "publish"}:
            assert not always
        if kind in {"summary", "cleanup"}:
            assert always
        if failed and not always:
            continue  # GitHub's default success() gate.
        if kind == "cleanup":
            private_logs = {p.name: p.read_text() for p in private.glob("*-output.txt")}
            if failure != "stale_private":
                assert stat.S_IMODE(private.stat().st_mode) == 0o700
                assert all(stat.S_IMODE(p.stat().st_mode) == 0o600
                           for p in private.glob("*-output.txt"))
            if (delivery / "status.json").exists():
                assert MARKER not in (delivery / "status.json").read_text()
                assert {p.name for p in delivery.iterdir()} == {"part-0000.vault", "status.json"}
                ciphertext = (delivery / "part-0000.vault").read_bytes()
        rendered = (script.replace("${{ matrix.crossing_cents }}", "0.25")
                    .replace("${{ inputs.source_commit }}", "a" * 40)
                    .replace("${{ job.status }}", "failure" if failed else "success"))
        assert "${{" not in rendered
        result = subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", rendered],
                                cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
        results.append((kind, result.returncode))
        public.extend([result.stdout, result.stderr])
        failed = failed or result.returncode != 0
    summary = (tmp_path / "summary.md").read_text()
    public.append(summary)
    assert MARKER not in "".join(public)
    assert "crossing_cents" not in summary and "0.25" not in summary
    assert ("failure" if failed else "success") in summary
    assert results[-1][0] == "summary", "Summarize after cleanup so its failure is visible"
    if failure in {"none", "cleanup"}:
        assert (tmp_path / "synthetic-published.json").exists()
        assert MARKER.encode() not in ciphertext
        payload = json.loads(VAULT.unseal(ciphertext, key))
        assert payload["summary"]["finding"] == MARKER
        assert MARKER + "_stdout" in payload["analysis_output"]
        assert MARKER + "_stderr" in payload["analysis_output"]
        assert payload["workflow"]["run_id"] == env["GITHUB_RUN_ID"]
        assert payload["workflow"]["run_attempt"] == env["GITHUB_RUN_ATTEMPT"]
        assert payload["workflow"]["source_commit"] == env["SOURCE_DATA_COMMIT"]
        if "matrix" in filename or "repair" in filename:
            assert payload["workflow"]["crossing_cents"] == "0.25"
        receipt = json.loads((tmp_path / "synthetic-published.json").read_text())
        assert receipt["repository"] == "yt-feng/poly_trade"
        assert receipt["run_id"] == env["GITHUB_RUN_ID"]
        assert receipt["run_attempt"] == env["GITHUB_RUN_ATTEMPT"]
    else:
        assert not (tmp_path / "synthetic-published.json").exists()
    if failure in {"analysis", "seal", "partial_seal", "stale_private", "stale_delivery"}:
        assert not (tmp_path / "publisher-called.json").exists()
    if failure in {"analysis", "seal", "partial_seal", "publish", "cleanup"}:
        expected_code = {"analysis": 17, "seal": 1, "partial_seal": 1, "publish": 37, "cleanup": 23}[failure]
        assert expected_code in [code for _, code in results]
    if failure not in {"stale_private", "cleanup"}:
        assert MARKER + "_stdout" in private_logs["analysis-output.txt"]
        assert MARKER + "_stderr" in private_logs["analysis-output.txt"]
    if failure in {"seal", "partial_seal", "publish"}:
        log = "publish-output.txt" if failure == "publish" else "seal-output.txt"
        assert MARKER in private_logs[log]
    if failure != "cleanup":
        assert not private.exists() and not delivery.exists()


@pytest.mark.parametrize("filename", WORKFLOWS)
def test_summary_reports_cancellation_without_research_details(filename, tmp_path):
    _, script, always = next(s for s in shell_steps(filename) if stage(s[0]) == "summary")
    assert always
    script = script.replace("${{ job.status }}", "cancelled").replace("${{ matrix.crossing_cents }}", "0.25")
    result = subprocess.run(["bash", "-e", "-c", script], cwd=tmp_path,
                            env={"GITHUB_STEP_SUMMARY": str(tmp_path / "summary")},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    text = (tmp_path / "summary").read_text()
    assert "cancelled" in text and "completed" not in text
    assert "crossing_cents" not in text and "0.25" not in text
