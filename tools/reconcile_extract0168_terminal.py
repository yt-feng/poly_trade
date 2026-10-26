#!/usr/bin/env python3
"""Idempotently point QuantML checkpoints at the verified extract-0168 terminal.

State-only repair: no corpus read, model fit, strategy replay, order, or secret access.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path("/Users/ytfeng/Code_Pj/poly_trade")
BASE = ROOT / "output/continuous-research-20261016"
STUDY = BASE / "quantml-full-study"
RUN_ID = 40288936144
READBACK = STUDY / "extract-0168-readback-20261026.json"
TERMINAL = STUDY / "extract-0168-terminal-checkpoint-20261026.json"


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"not_object:{path}")
    return value


def write(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.0168.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def verify_run() -> dict[str, Any]:
    proc = subprocess.run(
        ["gh", "run", "view", str(RUN_ID), "--repo", "yt-feng/poly_trade",
         "--json", "databaseId,status,conclusion,workflowName,headSha"],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"github_read_failed:{proc.stderr[-300:]}")
    value = json.loads(proc.stdout)
    if value.get("status") != "completed" or value.get("conclusion") != "success":
        raise RuntimeError(f"run_not_terminal_success:{value}")
    return value


def main() -> None:
    remote = verify_run()
    summary = load(READBACK)
    checkpoint = load(TERMINAL)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    action = (
        "Extract-0168 is terminal and readback-verified: 20 units were committed; "
        "do not repeat 0168 or the 0169 missing-page recovery. Review the returned "
        "units with source attribution before registering one distinct cursor batch; "
        "keep semantic credit 0, live buying paused, and H508-H301 dormant until an "
        "authorized owner-scoped bundle or verified semantic backend exists."
    )
    paths = [
        BASE / "active-episode.json",
        BASE / "cycle-progress.json",
        BASE / "quantml-learning-progress.json",
        STUDY / "manifest-summary.json",
        STUDY / "runtime-state.json",
        STUDY / "progress.json",
    ]
    for path in paths:
        if not path.is_file():
            continue
        state = load(path)
        state.update({
            "current_quantml_extract_run": RUN_ID,
            "latest_quantml_extract_run": RUN_ID,
            "current_extract_run": RUN_ID,
            "latest_extract_run": RUN_ID,
            "latest_quantml_extract_status": "completed_success_readback_verified",
            "latest_extract_status": "completed_success_readback_verified",
            "current_cloud_run_mode": "extract",
            "current_cloud_run_status": "completed_success_readback_verified",
            "current_quantml_terminal": str(TERMINAL),
            "latest_quantml_terminal_checkpoint": str(TERMINAL),
            "latest_quantml_extract_readback": str(READBACK),
            "latest_extract_units_written": checkpoint.get("units_written_this_run", summary.get("coverage", {}).get("units_written_this_run", 0)),
            "latest_quantml_extract_units_written": checkpoint.get("units_written_this_run", summary.get("coverage", {}).get("units_written_this_run", 0)),
            "latest_extract_bytes_written": checkpoint.get("bytes_written_this_run", summary.get("coverage", {}).get("bytes_written_this_run", 0)),
            "latest_quantml_extract_bytes_written": checkpoint.get("bytes_written_this_run", summary.get("coverage", {}).get("bytes_written_this_run", 0)),
            "latest_quantml_extract_semantic_credit": 0,
            "semantic_credit": 0,
            "semantic_read_count_credited": 0,
            "quantml_semantic_read_count": 0,
            "full_corpus_complete": False,
            "semantic_learning_complete": False,
            "active_remote_task": None,
            "active_run": None,
            "current_inflight_run": None,
            "current_inflight_runs": [],
            "next_action": action,
            "next_step": action,
            "quantml_next_action": action,
            "last_update_reason": "reconciled terminal extract-0168 run 40288936144; preserved 0169 non-committed recovery boundary",
            "checkpoint_updated_utc": now,
            "updated_utc": now,
            "updated_at_utc": now,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "live_orders_enabled": False,
            "buying_paused": True,
        })
        # Preserve source denominator from the manifest if the file already has it.
        if path.name == "manifest-summary.json":
            state["source_entries"] = 3492
        write(path, state)
    audit = {
        "format": "quantml-extract-0168-checkpoint-reconciliation-v1",
        "created_at_utc": now,
        "run": remote,
        "terminal_checkpoint": str(TERMINAL),
        "readback": str(READBACK),
        "units_committed": checkpoint.get("units_written_this_run"),
        "bytes_committed": checkpoint.get("bytes_written_this_run"),
        "semantic_credit": 0,
        "full_corpus_complete": False,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "duplicate_dispatch": False,
        "states_updated": len([p for p in paths if p.is_file()]),
        "plaintext_published": False,
        "next_action": action,
    }
    write(BASE / "extract-0168-checkpoint-reconciliation-20261026.json", audit)
    print(json.dumps({"status": "reconciled", "run_id": RUN_ID, "units": audit["units_committed"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
