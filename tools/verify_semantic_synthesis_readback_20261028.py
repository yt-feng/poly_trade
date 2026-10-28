#!/usr/bin/env python3
"""Verify one bounded DeepSeek synthesis readback and update private checkpoints."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path("/Users/ytfeng/Code_Pj/poly_trade")
BASE = ROOT / "output/continuous-research-20261016"
STUDY = BASE / "quantml-full-study"
RECIPIENT_ID = "5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9"


def read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("not_object")
    return value


def atomic(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".verify.tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify_semantic_synthesis_readback_20261028.py DECRYPTED_JSON WORKFLOW_RUN_ID")
    decrypted = Path(sys.argv[1])
    run_id = sys.argv[2]
    batch = os.environ.get("SEMANTIC_VERIFICATION_BATCH", "0175")
    d = read(decrypted)
    synthesis = d.get("synthesis")
    response = synthesis.get("response") if isinstance(synthesis, dict) else None
    expected = [
        "source_cards",
        "method_claims",
        "formulas",
        "data_quality_gaps",
        "btc5m_transfer_limits",
        "falsifiable_next_tests",
    ]
    if not isinstance(synthesis, dict) or synthesis.get("status") != "complete_pending_readback_verification":
        raise RuntimeError("synthesis_not_complete")
    if synthesis.get("http_status") != 200 or synthesis.get("model") != "deepseek-v4-pro":
        raise RuntimeError("backend_status_mismatch")
    if not isinstance(response, dict) or sorted(response) != sorted(expected):
        raise RuntimeError("response_schema_mismatch")
    source_paths = d.get("source_paths_sent")
    if not isinstance(source_paths, list) or not source_paths or len(set(source_paths)) != 1:
        raise RuntimeError("source_scope_not_single_registered_material")
    locators = []
    for key in expected:
        value = response.get(key)
        if not isinstance(value, list):
            raise RuntimeError("response_field_not_list")
        for row in value:
            if key == "source_cards" and isinstance(row, str) and re.fullmatch(r"SOURCE\s*[12]", row.strip(), re.IGNORECASE):
                locators.append(row.strip())
                continue
            if not isinstance(row, dict) or not isinstance(row.get("source_locator"), str):
                raise RuntimeError("source_locator_missing")
            locators.append(row["source_locator"])
    if not locators or not all(re.search(r"SOURCE\s*[12]", locator, re.IGNORECASE) for locator in locators):
        raise RuntimeError("source_locator_out_of_scope")
    if d.get("semantic_credit") != 0 or d.get("strategy_evaluations") != 0 or d.get("actual_fills") != 0 or d.get("verified_canary_fills") != 0 or d.get("live_orders_enabled") is not False:
        raise RuntimeError("frozen_gate_changed")

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    prior_credit = max(int(read(STUDY / "progress.json").get("semantic_credit", 0)), 0)
    cumulative_credit = prior_credit + 1
    response_digest = synthesis.get("response_sha256")
    card = {
        "schema": "quantml-semantic-learning-record-v2",
        "record_id": f"semantic-{batch}-bounded-synthesis-readback-20261028",
        "recorded_utc": now,
        "workflow_run_id": run_id,
        "source_scope": "one explicit source-cursor readback; two evidence units from one registered material; not full corpus",
        "source_path_sha256": hashlib.sha256(source_paths[0].encode()).hexdigest(),
        "response_sha256": response_digest,
        "verified": {
            "http_status": 200,
            "model": "deepseek-v4-pro",
            "response_schema": expected,
            "source_locator_count": len(locators),
            "source_locators_in_scope": True,
            "full_text_review_new": 0,
            "semantic_units_credited": 1,
        },
        "method_and_transfer_boundary": "The bounded synthesis is admissible as a semantic verification unit only; no new strategy hypothesis or profitability claim is admitted. Any BTC-5m transfer must remain a preregistered, costed, chronological test against the frozen cash-ledger gate.",
        "counts": {
            "semantic_worker_credit": 1,
            "new_hypotheses": 0,
            "strategy_evaluations": 0,
            "actual_fills": 0,
            "verified_canary_fills": 0,
            "live_orders_enabled": False,
        },
        "next_action": "Register at most one distinct semantic source cursor after this readback; do not replay this cursor or change the frozen BTC protocol.",
    }
    atomic(STUDY / "learning-cards" / (card["record_id"] + ".json"), card)
    summary = {
        "format": "quantml-semantic-synthesis-readback-verification-v1",
        "created_utc": now,
        "workflow_run_id": run_id,
        "source_scope": card["source_scope"],
        "response_sha256": response_digest,
        "source_locator_count": len(locators),
        "semantic_units_credited": 1,
        "semantic_credit": cumulative_credit,
        "new_hypotheses": 0,
        "strategy_evaluations": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "full_corpus_complete": False,
        "plaintext_published": False,
        "next_action": card["next_action"],
    }
    atomic(STUDY / f"semantic-{batch}-readback-verification-20261028.json", summary)
    for path in [
        BASE / "active-episode.json",
        BASE / "cycle-progress.json",
        BASE / "quantml-learning-progress.json",
        BASE / "quantml-learning-state.json",
        STUDY / "manifest-summary.json",
        STUDY / "runtime-state.json",
        STUDY / "progress.json",
    ]:
        if not path.is_file():
            continue
        state = read(path)
        state.update(
            {
                "semantic_credit": max(int(state.get("semantic_credit", 0)), cumulative_credit),
                "semantic_backend_gate": "remote_probe_authenticated; bounded_synthesis_readback_verified",
                "semantic_backend_credentials_present_by_name": True,
                "semantic_backend_last_checked_utc": now,
                "semantic_next_action": card["next_action"],
                "full_corpus_complete": False,
            }
        )
        atomic(path, state)
    marker = f"## semantic synthesis {batch} readback verification（2026-10-28）"
    section = (
        "\n\n" + marker + "\n\n"
        f"- Actions run {run_id} 的 DeepSeek v4-pro 回读完成：HTTP 200，结构化字段完整，{len(locators)} 个 source_locator 均约束在 SOURCE 1/2；输入是同一已登记材料的受控证据单元。\n"
        f"- 只登记 1 个新的语义验证单元，累计 semantic_credit={cumulative_credit}；没有新假设、策略评估、实际成交或 canary，full corpus 仍未完成，冻结 BTC 协议不变。\n"
        "- 原始响应和语义内容仅在已核验公钥密文中保存；下一步不得重放该 cursor，需另行预注册 source cursor。\n"
    )
    for path in [BASE / "QuantML-学习进度.md", STUDY / "QuantML-学习进度.md"]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if marker not in text:
                path.write_text(text + section, encoding="utf-8")

    import research_vault as vault

    delivery = BASE / f"semantic-{batch}-readback-verification-delivery"
    delivery.mkdir(mode=0o700, exist_ok=True)
    recipient = vault.load_json_bytes(vault.read_bounded(ROOT / "research-repair-20261015/research_vault/recipient.json", 4096), 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    vault.write_new(delivery / "part-0000.vault", vault.seal(vault.canonical(summary), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(
        delivery / "status.json",
        vault.canonical({
            "version": 1,
            "status": "complete",
            "stage": "sealed_delivery",
            "strategy_evaluated": False,
            "live_orders_enabled": False,
            "plaintext_published": False,
            "sealed_parts": 1,
        }) + b"\n",
        0o644,
    )
    pub = subprocess.run(
        [
            sys.executable,
            str(ROOT / "research-repair-20261015/tools/publish_quality_first_journal.py"),
            "--source", str(delivery),
            "--repository", "yt-feng/poly_trade",
            "--run-id", run_id,
            "--run-attempt", "1",
            "--recipient-id", RECIPIENT_ID,
            "--cell-id", f"semantic-{batch}-readback-verification-20261028",
        ],
        cwd=ROOT / "research-repair-20261015",
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if pub.returncode:
        raise RuntimeError("journal_publish_failed")
    receipt = json.loads(pub.stdout)
    atomic(BASE / f"semantic-{batch}-readback-verification-publication.json", {
        "format": f"quantml-semantic-{batch}-publication-v1",
        "created_utc": now,
        "status": receipt.get("status"),
        "remote_branch": receipt.get("branch"),
        "remote_episode_id": receipt.get("episode_id"),
        "plaintext_published": False,
        "semantic_credit": 1,
        "sealed_parts": receipt.get("sealed_parts"),
        "validation_scope": receipt.get("validation_scope"),
    })
    print(json.dumps({"status": "verified_and_published", "semantic_credit": 1, "remote_episode_id": receipt.get("episode_id")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
