#!/usr/bin/env python3
"""Seal the bounded semantic controller failures without publishing details."""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/ytfeng/Code_Pj/poly_trade")
BASE = ROOT / "output/continuous-research-20261016"
RECIPIENT_ID = "5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9"


def atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".failure.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    summary = {
        "format": "quantml-semantic-synthesis-failure-summary-v1",
        "created_utc": now,
        "failed_runs": [
            {"run_id": "40619655847", "class": "missing_private_recipient_file_in_actions_checkout", "stage": "sealed_delivery"},
            {"run_id": "40619794112", "class": "publisher_workdir_path_mismatch", "stage": "sealed_delivery"},
            {"run_id": "40619923962", "class": "sealed_status_schema_rejected_extra_field", "stage": "sealed_delivery"},
            {"run_id": "40620068537", "class": "deepseek_http_200_truncated_non_json_response", "stage": "semantic_readback", "semantic_credit": 0},
        ],
        "root_causes_repaired": [
            "Use the verified public recipient supplied by the repository variable.",
            "Use the Actions checkout root as publisher cwd.",
            "Keep the sealed status schema allowlisted.",
            "Bound output and parse documented DeepSeek v4-pro structured responses.",
        ],
        "strategy_evaluations": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "plaintext_published": False,
        "next_action": "No more replay of these runs; use the verified semantic-0175 readback and register one distinct cursor only if it adds information.",
    }
    atomic(BASE / "semantic-synthesis-failure-summary-20261028.json", summary)
    sys.path.insert(0, str(ROOT / "research-repair-20261015/tools"))
    import research_vault as vault

    recipient = vault.load_json_bytes(vault.read_bounded(ROOT / "research-repair-20261015/research_vault/recipient.json", 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    delivery = BASE / "semantic-synthesis-failure-summary-20261028-delivery"
    delivery.mkdir(mode=0o700, exist_ok=True)
    vault.write_new(delivery / "part-0000.vault", vault.seal(vault.canonical(summary), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(
        delivery / "status.json",
        vault.canonical({"version": 1, "status": "complete", "stage": "sealed_delivery", "strategy_evaluated": False, "live_orders_enabled": False, "plaintext_published": False, "sealed_parts": 1}) + b"\n",
        0o644,
    )
    pub = subprocess.run(
        [sys.executable, str(ROOT / "research-repair-20261015/tools/publish_quality_first_journal.py"), "--source", str(delivery), "--repository", "yt-feng/poly_trade", "--run-id", "40620641373", "--run-attempt", "1", "--recipient-id", RECIPIENT_ID, "--cell-id", "semantic-synthesis-failure-summary-20261028"],
        cwd=ROOT / "research-repair-20261015",
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if pub.returncode:
        raise RuntimeError("journal_publish_failed")
    result = json.loads(pub.stdout)
    receipt = {"format": "quantml-semantic-synthesis-failure-publication-v1", "created_utc": now, "status": result.get("status"), "remote_branch": result.get("branch"), "remote_episode_id": result.get("episode_id"), "plaintext_published": False, "sealed_parts": result.get("sealed_parts"), "validation_scope": result.get("validation_scope")}
    atomic(BASE / "semantic-synthesis-failure-summary-20261028-publication.json", receipt)
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
