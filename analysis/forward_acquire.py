"""Acquire production snapshot/label archives for registered forward canary research.

Read-only. This module never imports trading code, reads wallet credentials, or
places orders. It explicitly paginates release assets because long-running
capture releases can exceed GitHub's embedded asset list.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

API = "https://api.github.com/repos/yt-feng/poly"
DEFAULT_MAX_BYTES = 450 * 1024 * 1024
RELEASE_RE = re.compile(r"capture-v2-\d+-\d+")


def request(url: str) -> bytes:
    headers = {"User-Agent": "poly-trade-forward-readonly", "Accept": "application/vnd.github+json"}
    token = os.getenv("GH_TOKEN", "")
    if url.startswith("https://api.github.com/") and token:
        headers["Authorization"] = "Bearer " + token
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (401, 403, 418, 451):
                raise
            if exc.code < 500 and exc.code != 429:
                raise
        except (TimeoutError, OSError) as exc:
            last = exc
        time.sleep(2 ** attempt)
    raise RuntimeError(f"request failed after retries: {last}")


def get_json(url: str):
    return json.loads(request(url))


def list_capture_releases(max_releases: int) -> list[dict]:
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
        batch = get_json(f"{API}/releases/{release_id}/assets?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("asset API did not return a list")
        assets.extend(batch)
        if len(batch) < 100:
            break
    return assets


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def acquire(out: Path, max_releases: int = 16, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_repository": "yt-feng/poly",
        "read_only": True,
        "asset_pagination": True,
        "max_releases": max_releases,
        "max_bytes": max_bytes,
        "downloaded_bytes": 0,
        "releases": [],
        "files": [],
        "errors": [],
    }
    releases = list_capture_releases(max_releases)
    if not releases:
        raise RuntimeError("no capture releases found")
    used = 0
    for release in releases:
        rid = int(release["id"])
        tag = str(release["tag_name"])
        assets = list_release_assets(rid)
        by_name = {str(a.get("name")): a for a in assets}
        eligible = [
            a for a in assets
            if str(a.get("name", "")).startswith(("snapshots-", "labels-"))
            and str(a.get("name", "")).endswith(".jsonl.gz")
        ]
        manifest["releases"].append({
            "id": rid,
            "tag": tag,
            "published_at": release.get("published_at"),
            "asset_count": len(assets),
            "eligible_snapshot_label_archives": len(eligible),
        })
        for asset in sorted(eligible, key=lambda a: str(a.get("name", ""))):
            name = str(asset["name"])
            if Path(name).name != name:
                raise ValueError("unsafe asset name")
            side = by_name.get(name + ".sha256")
            if side is None:
                manifest["errors"].append({"tag": tag, "name": name, "error": "missing checksum sidecar"})
                continue
            size = int(asset.get("size") or 0)
            if used + size > max_bytes:
                manifest["errors"].append({"tag": tag, "name": name, "error": "download byte budget"})
                continue
            try:
                body = request(str(asset["browser_download_url"]))
                checksum_body = request(str(side["browser_download_url"]))
                expected = checksum_body.decode("utf-8").strip().split()[0]
                actual = hashlib.sha256(body).hexdigest()
                if actual != expected:
                    raise ValueError("checksum mismatch")
                dest = out / "captured" / tag / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(body)
                dest.with_name(name + ".sha256").write_bytes(checksum_body)
                used += len(body)
                manifest["files"].append({
                    "release": tag,
                    "name": name,
                    "path": str(dest.relative_to(out)),
                    "bytes": len(body),
                    "sha256": actual,
                    "created_at": asset.get("created_at"),
                    "url": asset.get("browser_download_url"),
                })
            except Exception as exc:  # retain a complete acquisition audit instead of silently skipping
                manifest["errors"].append({"tag": tag, "name": name, "error": str(exc)[:500]})
            manifest["downloaded_bytes"] = used
            save_json(out / "acquisition_manifest.json", manifest)
    save_json(out / "acquisition_manifest.json", manifest)
    if not any(f["name"].startswith("snapshots-") for f in manifest["files"]):
        raise RuntimeError("no checksum-verified snapshot archive acquired")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="forward_inputs")
    parser.add_argument("--max-releases", type=int, default=16)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    result = acquire(Path(args.output), args.max_releases, args.max_bytes)
    print(json.dumps({
        "files": len(result["files"]),
        "releases": len(result["releases"]),
        "bytes": result["downloaded_bytes"],
        "errors": result["errors"][:20],
    }, indent=2))


if __name__ == "__main__":
    main()
