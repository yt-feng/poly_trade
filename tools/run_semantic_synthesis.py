#!/usr/bin/env python3
"""Run one bounded cloud readback and DeepSeek semantic synthesis.

The corpus readback and model response stay inside the encrypted journal.  No
semantic credit is granted by this controller; a later readback verifier must
validate the response and citations first.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import research_vault as vault
import run_corpus_extract as corpus

PROJECT = Path.cwd().resolve()
ROOT = PROJECT.parent
RECIPIENT_ID = "5d68eb70893219fd4281958caf0072d145babc30123714f070a3ba058956e7a9"
MAX_INPUT_CHARS = 24_000
MAX_OUTPUT_CHARS = 32_000


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def api_call(source_texts: list[dict]) -> dict:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {"status": "missing_secret", "semantic_worker_credit": 0}
    joined = "\n\n".join(
        f"SOURCE {i + 1}\nPATH: {item['path']}\nTEXT:\n{item['text']}"
        for i, item in enumerate(source_texts)
    )[:MAX_INPUT_CHARS]
    prompt = (
        "You are doing a bounded, evidence-first review of QuantML material for a "
        "Polymarket BTC five-minute binary market with one evolving $10 cash ledger. "
        "Do not invent results or claim profitability. Extract only methods, formulas, "
        "data requirements, causal/time-leakage risks, and transfer limits. Return JSON "
        "with keys: source_cards, method_claims, formulas, data_quality_gaps, "
        "btc5m_transfer_limits, falsifiable_next_tests. Every claim must include a "
        "short source_locator. If the material has no executable edge, say so.\n\n"
        + joined
    )
    body = {
        "model": "deepseek-flash",
        "messages": [
            {"role": "system", "content": "Return strict JSON only; preserve uncertainty."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
    }
    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        "https://api.deepseek.com/chat/completions",
        data=raw_body,
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            status = response.status
            response_raw = response.read(MAX_OUTPUT_CHARS + 1)
    except Exception as exc:
        return {"status": "api_error", "error_class": type(exc).__name__, "semantic_worker_credit": 0}
    if len(response_raw) > MAX_OUTPUT_CHARS:
        return {"status": "response_too_large", "http_status": status, "semantic_worker_credit": 0}
    digest = hashlib.sha256(response_raw).hexdigest()
    try:
        envelope = json.loads(response_raw)
        content = envelope["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            raise ValueError("response_not_object")
    except Exception as exc:
        return {
            "status": "invalid_json_response",
            "http_status": status,
            "response_sha256": digest,
            "error_class": type(exc).__name__,
            "semantic_worker_credit": 0,
        }
    return {
        "status": "complete_pending_readback_verification",
        "http_status": status,
        "model": "deepseek-flash",
        "response_sha256": digest,
        "response": parsed,
        "usage": envelope.get("usage"),
        "semantic_worker_credit": 0,
    }


def seal_and_publish(record: dict) -> dict:
    base = PROJECT / "output/continuous-research-20261016"
    base.mkdir(parents=True, exist_ok=True)
    delivery = base / "semantic-synthesis-20261028-delivery"
    delivery.mkdir(mode=0o700, exist_ok=True)
    encoded_recipient = os.environ.get("QUALITY_FIRST_RECIPIENT_JSON_B64", "")
    if not encoded_recipient:
        raise RuntimeError("recipient_public_key_missing")
    try:
        recipient_raw = base64.b64decode(encoded_recipient, validate=True)
    except Exception as exc:
        raise RuntimeError("recipient_public_key_invalid") from exc
    recipient = vault.load_json_bytes(recipient_raw, 4096)
    vault.validate_recipient(recipient, RECIPIENT_ID)
    vault.write_new(delivery / "part-0000.vault", vault.seal(vault.canonical(record), recipient, RECIPIENT_ID), 0o600)
    vault.write_new(
        delivery / "status.json",
        vault.canonical({
            "version": 1,
            "status": "complete",
            "stage": "sealed_delivery",
            "strategy_evaluated": False,
            "live_orders_enabled": False,
            "plaintext_published": False,
            "semantic_credit": 0,
            "sealed_parts": 1,
        }) + b"\n",
        0o644,
    )
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    pub = subprocess.run(
        [
            sys.executable,
            str(PROJECT / "tools/publish_quality_first_journal.py"),
            "--source", str(delivery),
            "--repository", "yt-feng/poly_trade",
            "--run-id", run_id,
            "--run-attempt", attempt,
            "--recipient-id", RECIPIENT_ID,
            "--cell-id", "semantic-synthesis-20261028",
        ],
        cwd=PROJECT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if pub.returncode:
        raise RuntimeError("journal_publish_failed")
    result = json.loads(pub.stdout)
    receipt = {
        "format": "quantml-semantic-synthesis-publication-v1",
        "created_utc": record["created_utc"],
        "status": result.get("status"),
        "remote_branch": result.get("branch"),
        "remote_episode_id": result.get("episode_id"),
        "plaintext_published": False,
        "semantic_credit": 0,
        "sealed_parts": result.get("sealed_parts"),
        "validation_scope": result.get("validation_scope"),
    }
    temp = base / ".semantic-synthesis-20261028-publication.json.tmp"
    temp.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, base / "semantic-synthesis-20261028-publication.json")
    return receipt


def main() -> int:
    os.umask(0o077)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("CLOUD_ONLY", file=sys.stderr)
        return 2
    record = corpus.extract()
    created = now_utc()
    extracted = record.get("extraction") if isinstance(record, dict) else None
    texts: list[dict] = []
    if isinstance(extracted, dict):
        for item in extracted.get("unit_texts", []):
            if not isinstance(item, dict) or not item.get("artifact_complete"):
                continue
            text = item.get("artifact_utf8")
            unit = item.get("unit") if isinstance(item.get("unit"), dict) else {}
            if isinstance(text, str) and text:
                texts.append({"path": unit.get("relative_path", "unknown"), "text": text})
            if len(texts) >= 2:
                break
    synthesis = api_call(texts) if texts else {"status": "no_bounded_text_readback", "semantic_worker_credit": 0}
    out = {
        "format": "quantml-semantic-synthesis-v1",
        "created_utc": created,
        "corpus_record_status": record.get("status") if isinstance(record, dict) else "unknown",
        "source_scope": "one explicit source-cursor readback; not full corpus",
        "source_text_count_sent": len(texts),
        "source_paths_sent": [item["path"] for item in texts],
        "synthesis": synthesis,
        "semantic_credit": 0,
        "strategy_evaluations": 0,
        "actual_fills": 0,
        "verified_canary_fills": 0,
        "live_orders_enabled": False,
        "next_action": "Read back and independently verify source locators and claims; only then consider a semantic-learning card. Do not change the frozen BTC model or live gate.",
    }
    receipt = seal_and_publish(out)
    print(json.dumps({"status": "sealed_and_published", "semantic_credit": 0, "remote_episode_id": receipt.get("remote_episode_id")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
