"""Synthetic H601 publish-stage tests; no real data, credentials or publication."""
import json
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
MARKER = "SYNTHETIC_PRIVATE_PUBLISH_DIAGNOSTIC"
FAILURES = [
    ("validation", 2), ("permission", 77), ("conflict", 2),
    ("unknown", 37), ("uncertain", 124), ("terminated", 143),
    ("wrong_mode", 2), ("extra_entry", 2),
]


def publication_script():
    source = (ROOT / ".github/workflows/h601-favorite-path-depth.yml").read_text()
    match = re.search(
        r"^      - name: Publish encrypted H601 journal\n        run: \|\n"
        r"(.*?)(?=^      - name: |\Z)", source, re.M | re.S,
    )
    assert match is not None
    return textwrap.dedent(match.group(1))


def run_synthetic_stage(tmp_path, outcome, exit_code):
    for directory in ("bin", "tools", "research_vault", "sealed-delivery", "runner"):
        (tmp_path / directory).mkdir()
    private = tmp_path / "runner/h601-favorite-path-depth"
    private.mkdir(mode=0o700)
    # Deliberately invalid synthetic delivery: a failed validator must never
    # be bypassed by copying these original bytes into a journal tree.
    (tmp_path / "sealed-delivery/part-0000.vault").write_text("SYNTHETIC_NOT_AN_ENVELOPE")
    (tmp_path / "sealed-delivery/status.json").write_text("{}")
    (tmp_path / "research_vault/recipient.json").write_text(
        json.dumps({"recipient_id": "synthetic-public-recipient"}))
    trace = tmp_path / "trace.jsonl"
    (tmp_path / "tools/sitecustomize.py").write_text(textwrap.dedent("""
        import sys
        def no_external_io(event, args):
            if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system'):
                raise AssertionError('Synthetic publication test forbids external I/O')
        sys.addaudithook(no_external_io)
    """))
    (tmp_path / "tools/publish_quality_first_journal.py").write_text(textwrap.dedent(f"""
        import json, os, sys
        from pathlib import Path
        with Path(os.environ['SYNTHETIC_TRACE']).open('a') as stream:
            stream.write(json.dumps({{'kind': 'publisher', 'args': sys.argv[1:]}}) + '\\n')
        print('{MARKER}_stdout')
        print('{MARKER}_stderr', file=sys.stderr)
        code = int(os.environ['SYNTHETIC_EXIT'])
        if code == 0:
            Path('synthetic-success.json').write_text(json.dumps({{'args': sys.argv[1:]}}))
        if os.environ['SYNTHETIC_OUTCOME'] == 'uncertain':
            Path('synthetic-uncertain.json').write_text('first call may have succeeded')
        raise SystemExit(code)
    """))
    (tmp_path / "tools/persist_quality_first_journal.py").write_text(
        "def episode_identity(*args, **kwargs):\n    return 'e' * 64\n")
    python = tmp_path / "bin/python"
    python.write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o700)
    # Every Git operation is an in-memory/filesystem fake in this isolated
    # directory. No actual git executable, remote, credential or API is used.
    fake_git = tmp_path / "bin/git"
    fake_git.write_text("#!" + sys.executable + "\n" + textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        args = sys.argv[1:]
        with Path(os.environ['SYNTHETIC_TRACE']).open('a') as stream:
            stream.write(json.dumps({'kind': 'git', 'args': args}) + '\\n')
        if args[:2] == ['worktree', 'add']:
            root = Path(args[3])
            root.mkdir()
            outcome = os.environ['SYNTHETIC_OUTCOME']
            if outcome in {'wrong_mode', 'extra_entry'}:
                target = root / 'research_vault/episodes' / ('e' * 64)
                target.mkdir(parents=True)
                for name in ('part-0000.vault', 'status.json'):
                    (target / name).write_bytes((Path('sealed-delivery') / name).read_bytes())
                if outcome == 'wrong_mode':
                    (target / 'part-0000.vault').chmod(0o755)
                else:
                    (target / 'unexpected-entry').write_text('synthetic extra tree entry')
        if args[0] == 'ls-remote':
            print('a' * 40 + '\\trefs/heads/sealed-research-journal')
        if 'rev-parse' in args:
            print('a' * 40)
    """))
    fake_git.chmod(0o700)
    for name in ("sleep", "gh", "curl", "wget", "ssh"):
        executable = tmp_path / "bin" / name
        executable.write_text("#!" + sys.executable + "\n" + textwrap.dedent(f"""
            import json, os
            from pathlib import Path
            with Path(os.environ['SYNTHETIC_TRACE']).open('a') as stream:
                stream.write(json.dumps({{'kind': '{name}'}}) + '\\n')
            raise SystemExit({0 if name == 'sleep' else 99})
        """))
        executable.chmod(0o700)
    env = {
        "PATH": str(tmp_path / "bin") + ":/usr/bin:/bin",
        "PYTHONPATH": "tools", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "RUNNER_TEMP": str(tmp_path / "runner"), "GITHUB_RUN_ID": "123456789",
        "GITHUB_RUN_ATTEMPT": "2", "SYNTHETIC_TRACE": str(trace),
        "SYNTHETIC_OUTCOME": outcome, "SYNTHETIC_EXIT": str(exit_code),
    }
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", publication_script()],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=10,
    )
    calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    return result, calls, private


@pytest.mark.parametrize("outcome,exit_code", FAILURES + [("success", 0)])
def test_h601_uses_validated_publisher_once_and_preserves_result(tmp_path, outcome, exit_code):
    result, calls, private = run_synthetic_stage(tmp_path, outcome, exit_code)
    assert result.returncode == exit_code
    assert [call["kind"] for call in calls] == ["publisher"]
    assert calls[0]["args"] == [
        "--source", "sealed-delivery", "--repository", "yt-feng/poly_trade",
        "--run-id", "123456789", "--run-attempt", "2",
        "--recipient-id", "synthetic-public-recipient",
    ]
    assert (tmp_path / "synthetic-success.json").exists() == (exit_code == 0)
    assert (tmp_path / "synthetic-uncertain.json").exists() == (outcome == "uncertain")
    assert not (tmp_path / "runner/h601-sealed-journal-fallback").exists()
    assert MARKER not in result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        "status": "publication_complete" if exit_code == 0 else "publication_failed",
        "plaintext_published": False,
    }
    assert result.stderr == ""
    diagnostic = private / "publish-output.txt"
    assert MARKER + "_stdout" in diagnostic.read_text()
    assert MARKER + "_stderr" in diagnostic.read_text()
    assert stat.S_IMODE(diagnostic.stat().st_mode) == 0o600
    assert (tmp_path / "sealed-delivery/part-0000.vault").read_text() == "SYNTHETIC_NOT_AN_ENVELOPE"
    assert (tmp_path / "sealed-delivery/status.json").read_text() == "{}"
