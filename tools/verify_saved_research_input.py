"""Verify a scoped saved input in CI without fitting or replaying a strategy.

Configuration, capabilities, restored data and reports are private. The input
capability lives outside the result directory and is never copied into a report.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

import research_vault as vault
import scoped_research_dataset as dataset

FORMAT = "saved-research-input-verification-v1"
MAX_CONFIG = 262144
CONFIG_KEYS = {"version", "format", "expected_code_commit", "descriptor_sha256", "source", "tables", "join_keys"}
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
EPISODE_PATH = re.compile(r"research_vault/episodes/[0-9a-f]{64}/part-0000[.]vault\Z")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def validate_config(config: dict, repository: str, code_commit: str) -> None:
    if (not isinstance(config, dict) or set(config) != CONFIG_KEYS
            or type(config["version"]) is not int or config["version"] != 1
            or config["format"] != FORMAT or not HEX40.fullmatch(code_commit)
            or config["expected_code_commit"] != code_commit
            or not isinstance(config["descriptor_sha256"], str)
            or not HEX64.fullmatch(config["descriptor_sha256"])):
        raise ValueError("invalid saved-input configuration")
    source = config["source"]
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository)
            or repository.rsplit("/", 1)[1] in {".", ".."}
            or not isinstance(source, dict) or set(source) != {"repository", "commit", "path"}
            or source["repository"] != repository
            or not isinstance(source["commit"], str) or not HEX40.fullmatch(source["commit"])
            or not isinstance(source["path"], str) or not EPISODE_PATH.fullmatch(source["path"])):
        raise ValueError("invalid immutable ciphertext source")
    tables = config["tables"]
    if not isinstance(tables, list) or not 1 <= len(tables) <= 4:
        raise ValueError("invalid table contracts")
    names = []
    for table in tables:
        if (not isinstance(table, dict) or set(table) != {"path", "rows", "columns", "ids_sha256"}
                or not isinstance(table["path"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}[.]csv", table["path"])
                or type(table["rows"]) is not int or not 0 < table["rows"] <= 1000000
                or not isinstance(table["columns"], list) or not 1 <= len(table["columns"]) <= 256
                or any(not isinstance(v, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", v) for v in table["columns"])
                or len(set(table["columns"])) != len(table["columns"])
                or "market_id" not in table["columns"]
                or not isinstance(table["ids_sha256"], str) or not HEX64.fullmatch(table["ids_sha256"])):
            raise ValueError("invalid table contract")
        names.append(table["path"])
    joins = config["join_keys"]
    if (len(set(names)) != len(names) or not isinstance(joins, list) or not joins
            or len(joins) != len(set(joins)) or "market_id" not in joins
            or any(not isinstance(k, str) or any(k not in t["columns"] for t in tables) for k in joins)):
        raise ValueError("invalid join contract")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_ciphertext(source: dict, maximum: int, *, token: str | None = None) -> bytes:
    """Only an already validated immutable GitHub contents path is accepted."""
    if type(maximum) is not int or not 0 < maximum <= vault.MAX_ENVELOPE:
        raise ValueError("invalid ciphertext size")
    # Recheck the network target independently of caller-side validation.
    repo = source.get("repository", "")
    if (set(source) != {"repository", "commit", "path"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repo)
            or repo.rsplit("/", 1)[1] in {".", ".."}
            or not isinstance(source["commit"], str) or not HEX40.fullmatch(source["commit"])
            or not isinstance(source["path"], str) or not EPISODE_PATH.fullmatch(source["path"])):
        raise ValueError("invalid ciphertext source")
    url = "https://api.github.com/repos/" + repo + "/contents/" + source["path"] + "?ref=" + source["commit"]
    headers = {"Accept": "application/vnd.github.raw+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "scoped-saved-research-input"}
    if token:
        headers["Authorization"] = "Bearer " + token
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=30) as response:
                if response.status != 200:
                    raise ValueError("unexpected GitHub response")
                data = response.read(maximum + 1)
                if len(data) != maximum:
                    raise ValueError("ciphertext size mismatch")
                return data
        except urllib.error.HTTPError as error:
            if error.code not in {408, 429, 500, 502, 503, 504} or attempt == 2:
                raise ValueError("ciphertext unavailable") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise ValueError("ciphertext unavailable") from None
        time.sleep(attempt + 1)
    raise ValueError("ciphertext unavailable")


def join_value(key: str, value: str) -> str:
    if key.endswith("_ts"):
        fraction = re.search(r"[T ]\d{2}:\d{2}:\d{2}[.,](\d+)", value)
        if fraction and any(c != "0" for c in fraction.group(1)[6:]):
            raise ValueError("unsupported timestamp precision")
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None:
            raise ValueError("timezone required")
        return stamp.astimezone(timezone.utc).isoformat()
    return value


def verify_tables(directory: Path, config: dict) -> list[dict]:
    references = None
    results = []
    for table in config["tables"]:
        identities, joins = [], []
        with (directory / table["path"]).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, strict=True)
            if reader.fieldnames != table["columns"]:
                raise ValueError("table columns mismatch")
            for row in reader:
                if None in row or any(v is None for v in row.values()) or len(identities) >= table["rows"]:
                    raise ValueError("malformed or excessive table rows")
                identity = row["market_id"]
                if not identity:
                    raise ValueError("empty market identity")
                identities.append(identity)
                joins.append(tuple(join_value(k, row[k]) for k in config["join_keys"]))
        digest = sha(vault.canonical(sorted(identities)))
        if len(identities) != table["rows"] or len(set(identities)) != len(identities) or digest != table["ids_sha256"]:
            raise ValueError("table cohort mismatch")
        if references is not None and joins != references:
            raise ValueError("ordered cross-table join mismatch")
        references = joins
        results.append({"path": table["path"], "rows": len(identities), "ids_sha256": digest,
                        "columns": len(table["columns"]), "ordered_join_sha256": sha(vault.canonical(joins))})
    return results


def evaluate(config: dict, capability: dict, restored: Path, *, repository: str, code_commit: str,
             fetch=fetch_ciphertext) -> dict:
    validate_config(config, repository, code_commit)
    # Descriptor verification happens before any remote request or decryption.
    descriptor = dataset.validate_capability(capability, config["descriptor_sha256"])
    data = fetch(config["source"], descriptor["cipher_bytes"], token=os.environ.get("GH_TOKEN"))
    restoration = dataset.restore(data, capability, config["descriptor_sha256"], restored)
    tables = verify_tables(restored, config)
    return {"format": FORMAT, "status": "complete", "input_verified": True,
            "strategy_evaluated": False, "model_refit": False, "actual_model_fit_calls": 0,
            "actual_model_predict_calls": 0, "replayed_cells": 0, "actual_fills": 0,
            "verified_canary_fills": 0, "live_orders_enabled": False, "new_data_collected": False,
            "code_commit": code_commit, "immutable_source": config["source"],
            "descriptor_sha256": config["descriptor_sha256"], "restoration": restoration, "tables": tables,
            "boundary": "Saved input integrity and joins only; no raw-tick re-audit, strategy result, authenticated fill or reusable cash proof."}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--capability", required=True)
    parser.add_argument("--restored", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        config = vault.load_json_bytes(vault.read_bounded(args.config, MAX_CONFIG), MAX_CONFIG)
        capability = vault.load_json_bytes(vault.read_bounded(args.capability, 32768), 32768)
        report = evaluate(config, capability, Path(args.restored), repository=os.environ["GITHUB_REPOSITORY"],
                          code_commit=os.environ["GITHUB_SHA"])
        vault.write_new(output / "summary.json", vault.canonical(report))
        return 0
    except Exception:
        vault.write_new(output / "summary.json", vault.canonical({"format": FORMAT, "status": "failed",
            "input_verified": False, "strategy_evaluated": False, "actual_model_fit_calls": 0,
            "actual_model_predict_calls": 0, "replayed_cells": 0, "live_orders_enabled": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
