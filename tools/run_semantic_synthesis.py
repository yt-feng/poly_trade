#!/usr/bin/env python3
"""Run one bounded cloud readback and DeepSeek semantic synthesis.

The corpus readback and model response stay inside the encrypted journal.  No
semantic credit is granted by this controller; a later readback verifier must
validate the response and citations first.
"""
from __future__ import annotations

import base64
import ast
import datetime as dt
import hashlib
import json
import os
import re
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
        "short source_locator. Keep the JSON concise (under 700 output tokens). If the "
        "material has no executable edge, say so.\n\n"
        + joined
    )
    body = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "Return strict JSON only; preserve uncertainty."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 2400,
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
    def parse_json_content(content: object) -> dict:
        if isinstance(content, dict):
            return content
        if not isinstance(content, str):
            raise ValueError("response_content_not_text")
        candidate = content.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].lstrip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        candidates = [candidate]
        start, end = candidate.find("{"), candidate.rfind("}")
        if start >= 0 and end > start:
            candidates.append(candidate[start:end + 1])
        last_error: Exception | None = None
        parsed = None
        for item in candidates:
            try:
                parsed = json.loads(item)
                break
            except json.JSONDecodeError as exc:
                last_error = exc
                try:
                    parsed = ast.literal_eval(item)
                    break
                except (SyntaxError, ValueError, TypeError) as exc2:
                    last_error = exc2
                    repaired = re.sub(r",\s*([}\]])", r"\1", item)
                    try:
                        parsed = json.loads(repaired)
                        break
                    except json.JSONDecodeError as exc3:
                        last_error = exc3
        if parsed is None:
            if last_error is not None:
                raise last_error
            raise ValueError("response_not_json")
        if not isinstance(parsed, dict):
            raise ValueError("response_not_object")
        return parsed

    envelope = None
    content = None
    try:
        envelope = json.loads(response_raw)
        content = envelope["choices"][0]["message"]["content"]
        parsed = parse_json_content(content)
    except Exception as exc:
        return {
            "status": "invalid_json_response",
            "http_status": status,
            "model": "deepseek-v4-pro",
            "response_sha256": digest,
            "response_text": content if isinstance(content, str) else None,
            "response_len": len(content) if isinstance(content, str) else 0,
            "finish_reason": (envelope.get("choices") or [{}])[0].get("finish_reason") if isinstance(envelope, dict) else None,
            "usage": envelope.get("usage") if isinstance(envelope, dict) else None,
            "error_class": type(exc).__name__,
            "semantic_worker_credit": 0,
        }
    return {
        "status": "complete_pending_readback_verification",
        "http_status": status,
        "model": "deepseek-v4-pro",
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
    source_path_by_id: dict[str, str] = {}
    if isinstance(extracted, dict):
        for entry in extracted.get("selected_manifest", []):
            if isinstance(entry, dict) and isinstance(entry.get("source_id"), str) and isinstance(entry.get("relative_path"), str):
                source_path_by_id[entry["source_id"]] = entry["relative_path"]
        for entry in extracted.get("source_receipts", []):
            if isinstance(entry, dict) and isinstance(entry.get("source_id"), str) and isinstance(entry.get("relative_path"), str):
                source_path_by_id.setdefault(entry["source_id"], entry["relative_path"])
    if isinstance(extracted, dict):
        for item in extracted.get("unit_texts", []):
            if not isinstance(item, dict) or not item.get("artifact_complete"):
                continue
            text = item.get("artifact_utf8")
            unit = item.get("unit") if isinstance(item.get("unit"), dict) else {}
            if isinstance(text, str) and text:
                source_id = unit.get("source_id")
                path = unit.get("relative_path") if isinstance(unit.get("relative_path"), str) else source_path_by_id.get(source_id)
                if not isinstance(path, str) or not path:
                    continue
                texts.append({"path": path, "source_id": source_id, "text": text})
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
        "source_ids_sent": [item.get("source_id") for item in texts],
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
