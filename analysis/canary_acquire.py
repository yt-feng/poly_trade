"""Download public, checksummed readiness inputs. No accounts or orders.

Snapshot/label research coverage has its own byte budget. Raw event evidence uses
an independent smaller budget so large raw archives cannot starve the causal
snapshot sample. Long production releases use explicit asset pagination.
Mutable health/manifest transport failures are retained as warnings, never used
to excuse failed archives, checksums, source downloads or access restrictions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError

API = "https://api.github.com/repos/yt-feng/poly"
RELEASE_RE = re.compile(r"capture-v2-\d+-\d+")
DEFAULT_RESEARCH_BYTES = 220 * 1024 * 1024
DEFAULT_RAW_BYTES = 32 * 1024 * 1024
MUTABLE_METADATA_LIMIT = 8 * 1024 * 1024
OPTIONAL_STATUS_METADATA = frozenset(("health.json", "manifest.json"))
OPTIONAL_METADATA_HTTP_FAILURES = frozenset((404, 409, 500, 502, 503, 504))


def request(url: str, limit: int = 100 * 1024 * 1024) -> bytes:
    if limit <= 0:
        raise ValueError("request limit must be positive")
    headers = {"User-Agent": "poly-canary-research", "Accept": "application/json"}
    if url.startswith("https://api.github.com/") and os.getenv("GH_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["GH_TOKEN"]
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=90) as response:
                data = response.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("Response exceeds download budget")
                return data
        except HTTPError as exc:
            if exc.code < 500 or attempt == 2:
                raise
        except (TimeoutError, OSError):
            if attempt == 2:
                raise
        time.sleep(2 ** attempt)
    raise RuntimeError("Retries exhausted")


def get_json(url: str):
    return json.loads(request(url, 20 * 1024 * 1024))


def list_capture_releases(max_releases: int = 4) -> list[dict]:
    if max_releases <= 0:
        raise ValueError("max_releases must be positive")
    releases: list[dict] = []
    for page in range(1, 6):
        batch = get_json(f"{API}/releases?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("release API did not return a list")
        releases.extend(r for r in batch if RELEASE_RE.fullmatch(str(r.get("tag_name", ""))))
        if len(batch) < 100 or len(releases) >= max_releases:
            break
    releases.sort(key=lambda r: str(r.get("published_at", "")), reverse=True)
    return releases[:max_releases]


def list_release_assets(release_id: int) -> list[dict]:
    assets: list[dict] = []
    for page in range(1, 20):
        batch = get_json(f"{API}/releases/{int(release_id)}/assets?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("asset API did not return a list")
        assets.extend(batch)
        if len(batch) < 100:
            break
    return assets


def _safe_name(name: str) -> str:
    if Path(name).name != name:
        raise ValueError("Unsafe filename")
    return name


def _record_download_failure(release, name, bucket, exc, errors, metadata_warnings=None):
    record = {
        "release": release.get("tag_name"), "name": name, "kind": bucket,
        "error": str(exc)[:200], "exception_type": type(exc).__name__,
        "http_status": exc.code if isinstance(exc, HTTPError) else None,
    }
    optional = (name in OPTIONAL_STATUS_METADATA and isinstance(exc, HTTPError)
                and exc.code in OPTIONAL_METADATA_HTTP_FAILURES)
    if optional and metadata_warnings is not None:
        record.update(severity="optional_status_metadata_unavailable",
                      used_for_signals_or_labels=False,
                      note="No health or full-manifest completeness claim; immutable archives still require their SHA sidecars.")
        metadata_warnings.append(record)
    else:
        record["severity"] = "fatal_transport_or_integrity_error"
        errors.append(record)


def _download(
    *, output: Path, release: dict, assets: dict[str, dict], name: str,
    bucket: str, records: list[dict], errors: list[dict], metadata_warnings=None,
) -> int:
    name = _safe_name(name)
    item = assets[name]
    size = int(item.get("size") or 0)
    if size < 0:
        raise ValueError("negative asset size")
    try:
        # Only closed gzip archives use the listed size as a hard response bound.
        limit = max(1, size) if name.endswith(".gz") else MUTABLE_METADATA_LIMIT
        if not name.endswith(".gz") and size > MUTABLE_METADATA_LIMIT:
            raise ValueError("mutable metadata exceeds bounded download limit")
        data = request(str(item["browser_download_url"]), limit)
        digest = hashlib.sha256(data).hexdigest()
        check = None
        if name.endswith(".gz"):
            side = assets.get(name + ".sha256")
            if not side:
                raise ValueError("missing checksum sidecar")
            check = request(str(side["browser_download_url"]), 4096)
            if digest != check.decode().split()[0]:
                raise ValueError("SHA256 mismatch")
        dest = output / "captured" / str(release["tag_name"]) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        if check:
            dest.with_name(name + ".sha256").write_bytes(check)
        records.append({
            "path": str(dest.relative_to(output)), "release": release["tag_name"],
            "kind": bucket, "sha256": digest, "bytes": len(data),
            "url": item["browser_download_url"],
        })
        return len(data)
    except Exception as exc:
        _record_download_failure(release, name, bucket, exc, errors, metadata_warnings)
        return 0


def acquire(
    output: Path, *, max_bytes: int = DEFAULT_RESEARCH_BYTES,
    max_raw_bytes: int = DEFAULT_RAW_BYTES, max_releases: int = 4, raw_files: int = 2,
) -> dict:
    if max_bytes <= 0 or max_raw_bytes < 0 or max_releases <= 0 or raw_files < 0:
        raise ValueError("invalid acquisition limits")
    output.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    errors: list[dict] = []
    skipped: list[dict] = []
    metadata_warnings: list[dict] = []
    research_used = 0
    raw_used = 0
    releases = list_capture_releases(max_releases)
    (output / "releases.json").write_text(json.dumps(releases, indent=2), encoding="utf-8")
    release_assets: list[tuple[dict, dict[str, dict]]] = []
    for release in releases:
        assets = {str(x["name"]): x for x in list_release_assets(int(release["id"]))}
        release_assets.append((release, assets))
        labels = sorted(n for n in assets if n.startswith("labels-") and n.endswith(".jsonl.gz"))
        snapshots = sorted(n for n in assets if n.startswith("snapshots-") and n.endswith(".jsonl.gz"))
        metadata = [n for n in ("health.json", "manifest.json") if n in assets]
        for name in [*labels, *snapshots, *metadata]:
            item = assets[name]
            size = int(item.get("size") or 0)
            budget_size = size if name.endswith(".gz") else max(size, MUTABLE_METADATA_LIMIT)
            if research_used + budget_size > max_bytes:
                skipped.append({"release": release["tag_name"], "name": name,
                                "kind": "research", "bytes": size, "reason": "research byte budget"})
                continue
            research_used += _download(output=output, release=release, assets=assets, name=name,
                bucket="research", records=records, errors=errors, metadata_warnings=metadata_warnings)
            if research_used > max_bytes:
                errors.append({"release": release["tag_name"], "name": name, "kind": "research",
                               "error": "actual downloaded bytes exceeded research budget"})
    if release_assets and raw_files and max_raw_bytes:
        release, assets = release_assets[0]
        raw_names = sorted(n for n in assets if n.startswith("raw-") and n.endswith(".jsonl.gz")
                           and n + ".sha256" in assets)[-raw_files:]
        for name in raw_names:
            size = int(assets[name].get("size") or 0)
            if raw_used + size > max_raw_bytes:
                skipped.append({"release": release["tag_name"], "name": name, "kind": "raw_event_evidence",
                                "bytes": size, "reason": "raw byte budget"})
                continue
            raw_used += _download(output=output, release=release, assets=assets, name=name,
                bucket="raw_event_evidence", records=records, errors=errors, metadata_warnings=metadata_warnings)
    for name in ("capture_v3.py", "microstructure_v3.py", "microstructure_math_v3.py"):
        try:
            data = request("https://raw.githubusercontent.com/yt-feng/poly/main/" + name, 4 * 1024 * 1024)
            dest = output / "collector_source" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except Exception as exc:
            errors.append({"name": name, "kind": "collector_source", "error": str(exc)[:200]})
    summary = {
        "retrieved_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_repository": "yt-feng/poly", "asset_pagination": True,
        "selected_releases": [r.get("tag_name") for r in releases],
        "files": records, "errors": errors, "skipped": skipped,
        "optional_metadata_warnings": metadata_warnings,
        "optional_metadata_transport_complete": not metadata_warnings,
        "research_downloaded_bytes": research_used, "max_research_bytes": max_bytes,
        "raw_downloaded_bytes": raw_used, "max_raw_bytes": max_raw_bytes,
        "full_history_complete": False, "read_only": True, "orders_enabled": False,
        "note": "Budget skips and optional health/manifest HTTP failures are explicit. "
                "Every snapshot/label archive still requires its immutable SHA sidecar. "
                "Missing status metadata prevents a full health/manifest claim, not verified-archive research. "
                "Authorization/rate restrictions, checksum, archive and source failures remain fatal.",
    }
    (output / "manifest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(records), "research_bytes": research_used,
                      "raw_bytes": raw_used, "skipped": len(skipped), "errors": errors,
                      "optional_metadata_warnings": metadata_warnings, "releases": len(releases)}, indent=2))
    if errors:
        raise RuntimeError("Read-only acquisition had transport/integrity errors; inspect manifest.json")
    if not any("/snapshots-" in f["path"] for f in records):
        raise RuntimeError("No verified snapshots")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("canary_inputs"))
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_RESEARCH_BYTES)
    parser.add_argument("--max-raw-bytes", type=int, default=DEFAULT_RAW_BYTES)
    parser.add_argument("--max-releases", type=int, default=4)
    parser.add_argument("--raw-files", type=int, default=2)
    args = parser.parse_args()
    acquire(args.output, max_bytes=args.max_bytes, max_raw_bytes=args.max_raw_bytes,
            max_releases=args.max_releases, raw_files=args.raw_files)


if __name__ == "__main__":
    main()
