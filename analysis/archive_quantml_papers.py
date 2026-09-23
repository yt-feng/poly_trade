#!/usr/bin/env python3
"""Archive QuantML paper library snapshots and public paper metadata.

The source AxiomQ pages are local SingleFile HTML exports. This script does not
query that private server. It enriches the local titles through public scholarly
metadata endpoints and downloads only openly available PDFs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import datetime as dt
import difflib
import hashlib
import html
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_DIR = REPO_ROOT / "docs" / "QuantML论文库doc"
DEFAULT_OUT_ROOT = REPO_ROOT / "docs"
USER_AGENT = "poly-trade-quantml-paper-archive/1.0"


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_title(value: str) -> str:
    value = html.unescape(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def slugify(value: str, max_len: int = 90) -> str:
    value = normalize_title(value)
    if not value:
        value = "untitled"
    value = value.replace(" ", "_")
    return value[:max_len].strip("_")


def stable_hash(value: str, n: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:n]


def parse_int(value: str | None) -> int | None:
    if not value or value == "-":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def extract_current_page(text: str, path: Path) -> int | None:
    match = re.search(r"<button class=current>(\d+)</button>", text)
    if match:
        return int(match.group(1))
    match = re.search(r"papers\.html\?page=(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"9：(\d\d)：", path.name)
    if match:
        return int(match.group(1))
    return None


def parse_source_html(source_dir: Path) -> list[dict[str, Any]]:
    files = sorted(source_dir.glob("*.html"))
    records: list[dict[str, Any]] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        page = extract_current_page(text, path)
        for position, block in enumerate(text.split("<div class=paper-item>")[1:], start=1):
            title_match = re.search(r"<div class=pi-title>(?P<title>.*?)</div>", block, re.S)
            meta_match = re.search(r"<div class=pi-meta>(?P<meta>.*?)</div>", block, re.S)
            tags_match = re.search(r"<div class=pi-tags>(?P<tags>.*?)</div>", block, re.S)
            title = clean_text(title_match.group("title") if title_match else "")
            if not title:
                continue
            meta_html = meta_match.group("meta") if meta_match else ""
            tags_html = tags_match.group("tags") if tags_match else ""
            spans = [clean_text(s) for s in re.findall(r"<span>(.*?)</span>", meta_html, re.S)]
            tags = [
                clean_text(t)
                for t in re.findall(r"<span class=tag-chip>(.*?)</span>", tags_html, re.S)
            ]
            records.append(
                {
                    "title": title,
                    "title_key": normalize_title(title),
                    "year": parse_int(spans[0] if len(spans) > 0 else None),
                    "year_raw": spans[0] if len(spans) > 0 else "",
                    "authors_display": spans[1] if len(spans) > 1 else "",
                    "imported_date": spans[2] if len(spans) > 2 else "",
                    "tags_visible": [t for t in tags if t and not t.startswith("+")],
                    "source_page": page,
                    "source_position": position,
                    "source_file": path.name,
                }
            )
    return records


def dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in records:
        key = item["title_key"] or stable_hash(item["title"])
        if key not in grouped:
            base = dict(item)
            base["source_pages"] = []
            base["source_files"] = []
            base["duplicates"] = []
            grouped[key] = base
            order.append(key)
        grouped[key]["source_pages"].append(item.get("source_page"))
        grouped[key]["source_files"].append(item.get("source_file"))
        grouped[key]["duplicates"].append(
            {
                "page": item.get("source_page"),
                "position": item.get("source_position"),
                "file": item.get("source_file"),
            }
        )
        if not grouped[key].get("year") and item.get("year"):
            grouped[key]["year"] = item.get("year")
            grouped[key]["year_raw"] = item.get("year_raw")
        if len(item.get("tags_visible", [])) > len(grouped[key].get("tags_visible", [])):
            grouped[key]["tags_visible"] = item["tags_visible"]

    unique = [grouped[key] for key in order]
    for idx, item in enumerate(unique, start=1):
        item["archive_id"] = f"QML{idx:04d}"
        item["source_pages"] = sorted({p for p in item["source_pages"] if p is not None})
        item["source_files"] = sorted({p for p in item["source_files"] if p})
        item["duplicate_count"] = len(item["duplicates"])
    return unique


def run_curl(url: str, *, timeout: int = 45, output: Path | None = None) -> bytes:
    cmd = [
        "curl",
        "-L",
        "--fail",
        "--connect-timeout",
        "12",
        "--max-time",
        str(timeout),
        "-sS",
        "-A",
        USER_AGENT,
    ]
    if output is not None:
        tmp_output = output.with_suffix(output.suffix + ".part")
        cmd += ["-o", str(tmp_output)]
    cmd.append(url)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(err or f"curl failed with exit {proc.returncode}")
    if output is not None:
        tmp_output.replace(output)
        return b""
    return proc.stdout


def cached_fetch(cache_dir: Path, namespace: str, key: str, url: str, *, suffix: str, timeout: int = 45) -> bytes:
    ns_dir = cache_dir / namespace
    ns_dir.mkdir(parents=True, exist_ok=True)
    cache_path = ns_dir / f"{stable_hash(key)}.{suffix}"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path.read_bytes()
    data = run_curl(url, timeout=timeout)
    cache_path.write_bytes(data)
    return data


def title_similarity(a: str, b: str) -> float:
    na = normalize_title(a)
    nb = normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return min(len(na), len(nb)) / max(len(na), len(nb))
    return difflib.SequenceMatcher(None, na, nb).ratio()


def abstract_from_inverted_index(index: dict[str, list[int]] | None) -> str:
    if not index:
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in index.items():
        for pos in positions:
            positioned.append((int(pos), word))
    positioned.sort(key=lambda x: x[0])
    return " ".join(word for _, word in positioned).strip()


def html_to_plain_abstract(value: str | None) -> str:
    text = clean_text(value)
    text = re.sub(r"^\s*abstract\s*", "", text, flags=re.I)
    return text.strip()


def extract_arxiv_id_from_url(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([A-Za-z0-9._/-]+)", value, re.I)
    if match:
        candidate = match.group(1)
        return candidate.removesuffix(".pdf").strip(" .,/")
    match = re.search(r"10\.48550/arxiv\.([A-Za-z0-9._/-]+)", value, re.I)
    if match:
        return match.group(1).strip(" .,/")
    return ""


def arxiv_pdf_from_id(arxiv_id: str) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}"


def normalize_arxiv_id(arxiv_id: str | None, *, strip_version: bool = False) -> str:
    arxiv_id = extract_arxiv_id_from_url(arxiv_id) or (arxiv_id or "")
    arxiv_id = arxiv_id.strip()
    if strip_version:
        arxiv_id = re.sub(r"v\d+$", "", arxiv_id)
    return arxiv_id


def collect_pdf_candidates(work: dict[str, Any]) -> list[str]:
    candidates: list[str] = []

    def add(value: str | None) -> None:
        if not value:
            return
        value = value.strip()
        if not value or value in candidates:
            return
        arxiv_id = extract_arxiv_id_from_url(value)
        if arxiv_id and "/abs/" in value:
            candidates.append(arxiv_pdf_from_id(arxiv_id))
            return
        candidates.append(value)

    for key in ("best_oa_location", "primary_location"):
        loc = work.get(key) or {}
        add(loc.get("pdf_url"))
        add(loc.get("landing_page_url"))
    oa = work.get("open_access") or {}
    add(oa.get("oa_url"))
    for loc in work.get("locations") or []:
        add((loc or {}).get("pdf_url"))
        add((loc or {}).get("landing_page_url"))
    ids = work.get("ids") or {}
    add(ids.get("doi"))
    add(work.get("doi"))

    normalized: list[str] = []
    for value in candidates:
        arxiv_id = extract_arxiv_id_from_url(value)
        if arxiv_id:
            value = arxiv_pdf_from_id(arxiv_id)
        if value.lower().endswith(".pdf") or "/pdf/" in value.lower() or "arxiv.org/pdf/" in value.lower():
            if value not in normalized:
                normalized.append(value)
    return normalized


def best_openalex_match(record: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    best: dict[str, Any] | None = None
    best_score = 0.0
    wanted_year = record.get("year")
    for work in data.get("results") or []:
        candidate_title = work.get("title") or work.get("display_name") or ""
        score = title_similarity(record["title"], candidate_title)
        if wanted_year and work.get("publication_year"):
            if abs(int(work["publication_year"]) - int(wanted_year)) <= 1:
                score += 0.03
            elif abs(int(work["publication_year"]) - int(wanted_year)) >= 4:
                score -= 0.05
        if score > best_score:
            best = work
            best_score = score
    if best and best_score >= 0.86:
        return best, min(best_score, 1.0)
    return None, best_score


def fetch_openalex(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    params = {
        "filter": f"title.search:{record['title']}",
        "per-page": "5",
    }
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    raw = cached_fetch(cache_dir, "openalex", record["title"], url, suffix="json", timeout=20)
    data = json.loads(raw.decode("utf-8"))
    work, score = best_openalex_match(record, data)
    if not work:
        return {"source": "openalex", "match_score": score, "matched": False}
    abstract = abstract_from_inverted_index(work.get("abstract_inverted_index"))
    pdf_candidates = collect_pdf_candidates(work)
    arxiv_id = ""
    probe_values = list(pdf_candidates)
    for key in ("best_oa_location", "primary_location"):
        loc = work.get(key) or {}
        probe_values.append(loc.get("landing_page_url") or "")
        probe_values.append(loc.get("pdf_url") or "")
    probe_values.append(work.get("doi") or "")
    for value in probe_values:
        arxiv_id = extract_arxiv_id_from_url(value)
        if arxiv_id:
            break
    return {
        "source": "openalex",
        "matched": True,
        "match_score": score,
        "openalex_id": work.get("id") or "",
        "title": work.get("title") or work.get("display_name") or "",
        "year": work.get("publication_year"),
        "doi": work.get("doi") or "",
        "url": ((work.get("primary_location") or {}).get("landing_page_url") or work.get("id") or ""),
        "abstract": abstract,
        "pdf_candidates": pdf_candidates,
        "is_oa": bool((work.get("open_access") or {}).get("is_oa")),
        "oa_status": (work.get("open_access") or {}).get("oa_status") or "",
        "arxiv_id": arxiv_id,
    }


def arxiv_entry_to_candidate(entry: ET.Element) -> dict[str, Any]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    title = clean_text(entry.findtext("atom:title", default="", namespaces=ns))
    entry_id = clean_text(entry.findtext("atom:id", default="", namespaces=ns))
    summary = clean_text(entry.findtext("atom:summary", default="", namespaces=ns))
    published = clean_text(entry.findtext("atom:published", default="", namespaces=ns))
    authors = [
        clean_text(author.findtext("atom:name", default="", namespaces=ns))
        for author in entry.findall("atom:author", ns)
    ]
    pdf_url = ""
    for link in entry.findall("atom:link", ns):
        if link.attrib.get("title") == "pdf" or link.attrib.get("type") == "application/pdf":
            pdf_url = link.attrib.get("href", "")
            break
    return {
        "source": "arxiv",
        "matched": True,
        "match_score": 1.0,
        "title": title,
        "url": entry_id,
        "arxiv_id": extract_arxiv_id_from_url(entry_id),
        "pdf_candidates": [pdf_url] if pdf_url else [],
        "abstract": summary,
        "authors": authors,
        "year": parse_int(published[:4]),
    }


def parse_arxiv_entries(raw: bytes) -> list[dict[str, Any]]:
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    return [arxiv_entry_to_candidate(entry) for entry in root.findall("atom:entry", ns)]


def parse_arxiv_feed(record: dict[str, Any], raw: bytes) -> dict[str, Any]:
    root = ET.fromstring(raw)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    best: dict[str, Any] | None = None
    best_score = 0.0
    for entry in root.findall("atom:entry", ns):
        candidate = arxiv_entry_to_candidate(entry)
        title = candidate.get("title") or ""
        score = title_similarity(record["title"], title)
        if score <= best_score:
            continue
        best = dict(candidate)
        best["match_score"] = score
        best_score = score
    if best and best_score >= 0.88:
        return best
    return {"source": "arxiv", "matched": False, "match_score": best_score}


def fetch_arxiv(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    safe_title = record["title"].replace('"', " ")
    params = {
        "search_query": f'ti:"{safe_title}"',
        "start": "0",
        "max_results": "5",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    raw = cached_fetch(cache_dir, "arxiv", record["title"], url, suffix="xml", timeout=6)
    return parse_arxiv_feed(record, raw)


def fetch_arxiv_id_batch(arxiv_ids: list[str], cache_dir: Path) -> dict[str, dict[str, Any]]:
    cleaned = []
    for arxiv_id in arxiv_ids:
        arxiv_id = normalize_arxiv_id(arxiv_id)
        if arxiv_id and arxiv_id not in cleaned:
            cleaned.append(arxiv_id)
    if not cleaned:
        return {}
    params = {"id_list": ",".join(cleaned), "max_results": str(len(cleaned))}
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    raw = cached_fetch(cache_dir, "arxiv_id_batch", ",".join(cleaned), url, suffix="xml", timeout=30)
    by_id: dict[str, dict[str, Any]] = {}
    for candidate in parse_arxiv_entries(raw):
        arxiv_id = candidate.get("arxiv_id") or ""
        if not arxiv_id:
            continue
        by_id[normalize_arxiv_id(arxiv_id)] = candidate
        by_id[normalize_arxiv_id(arxiv_id, strip_version=True)] = candidate
    return by_id


def best_crossref_match(record: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    items = ((data.get("message") or {}).get("items") or [])
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in items:
        title = " ".join(item.get("title") or [])
        score = title_similarity(record["title"], title)
        if score > best_score:
            best, best_score = item, score
    if best and best_score >= 0.86:
        return best, best_score
    return None, best_score


def fetch_crossref(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    params = {"query.title": record["title"], "rows": "5"}
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    raw = cached_fetch(cache_dir, "crossref", record["title"], url, suffix="json", timeout=10)
    data = json.loads(raw.decode("utf-8"))
    item, score = best_crossref_match(record, data)
    if not item:
        return {"source": "crossref", "matched": False, "match_score": score}
    links = []
    for link in item.get("link") or []:
        url_value = link.get("URL") or ""
        ctype = (link.get("content-type") or "").lower()
        if "pdf" in ctype or url_value.lower().endswith(".pdf"):
            links.append(url_value)
    year = None
    date_parts = ((item.get("issued") or {}).get("date-parts") or [])
    if date_parts and date_parts[0]:
        year = parse_int(str(date_parts[0][0]))
    return {
        "source": "crossref",
        "matched": True,
        "match_score": score,
        "title": " ".join(item.get("title") or []),
        "doi": item.get("DOI") or "",
        "url": item.get("URL") or "",
        "year": year,
        "abstract": html_to_plain_abstract(item.get("abstract")),
        "pdf_candidates": links,
    }


def best_semantic_match(record: dict[str, Any], data: dict[str, Any]) -> tuple[dict[str, Any] | None, float]:
    best: dict[str, Any] | None = None
    best_score = 0.0
    for item in data.get("data") or []:
        score = title_similarity(record["title"], item.get("title") or "")
        if score > best_score:
            best, best_score = item, score
    if best and best_score >= 0.86:
        return best, best_score
    return None, best_score


def fetch_semantic(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    params = {
        "query": record["title"],
        "limit": "5",
        "fields": "title,abstract,year,authors,externalIds,openAccessPdf,url",
    }
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(params)
    raw = cached_fetch(cache_dir, "semantic", record["title"], url, suffix="json", timeout=10)
    data = json.loads(raw.decode("utf-8"))
    item, score = best_semantic_match(record, data)
    if not item:
        return {"source": "semantic_scholar", "matched": False, "match_score": score}
    pdf_url = ((item.get("openAccessPdf") or {}).get("url") or "")
    external = item.get("externalIds") or {}
    return {
        "source": "semantic_scholar",
        "matched": True,
        "match_score": score,
        "title": item.get("title") or "",
        "year": item.get("year"),
        "doi": external.get("DOI") or "",
        "arxiv_id": external.get("ArXiv") or "",
        "url": item.get("url") or "",
        "abstract": item.get("abstract") or "",
        "pdf_candidates": [pdf_url] if pdf_url else [],
    }


def merge_metadata(record: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "public_sources_checked": [],
        "matched_sources": [],
        "pdf_candidates": [],
    }
    for cand in candidates:
        metadata["public_sources_checked"].append(cand.get("source"))
        if not cand.get("matched"):
            continue
        metadata["matched_sources"].append(
            {
                "source": cand.get("source"),
                "match_score": round(float(cand.get("match_score") or 0), 4),
                "title": cand.get("title") or "",
                "url": cand.get("url") or "",
            }
        )
        for key in ("openalex_id", "doi", "arxiv_id", "url", "year", "is_oa", "oa_status"):
            if cand.get(key) and not metadata.get(key):
                metadata[key] = cand.get(key)
        if cand.get("abstract") and not metadata.get("abstract"):
            metadata["abstract"] = cand["abstract"]
            metadata["abstract_source"] = cand.get("source")
        for url in cand.get("pdf_candidates") or []:
            if url and url not in metadata["pdf_candidates"]:
                metadata["pdf_candidates"].append(url)
    if metadata.get("arxiv_id"):
        pdf_url = arxiv_pdf_from_id(str(metadata["arxiv_id"]))
        if pdf_url not in metadata["pdf_candidates"]:
            metadata["pdf_candidates"].insert(0, pdf_url)
    record["metadata"] = metadata
    record["abstract"] = metadata.get("abstract") or ""
    record["abstract_source"] = metadata.get("abstract_source") or ""
    return record


def fetch_openalex_safe(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    try:
        return fetch_openalex(record, cache_dir)
    except Exception as exc:
        return {"source": "openalex", "matched": False, "error": str(exc)}


def arxiv_hints_from_candidates(candidates: list[dict[str, Any]]) -> list[str]:
    hints: list[str] = []
    for candidate in candidates:
        arxiv_id = normalize_arxiv_id(candidate.get("arxiv_id"))
        if arxiv_id and arxiv_id not in hints:
            hints.append(arxiv_id)
        for url in candidate.get("pdf_candidates") or []:
            arxiv_id = normalize_arxiv_id(url)
            if arxiv_id and arxiv_id not in hints:
                hints.append(arxiv_id)
    return hints


def enrich_records(
    records: list[dict[str, Any]],
    out_dir: Path,
    *,
    max_items: int | None = None,
    openalex_delay: float = 0.12,
    arxiv_delay: float = 3.1,
    crossref_delay: float = 0.2,
    semantic_delay: float = 1.0,
    use_crossref: bool = True,
    use_semantic: bool = True,
    openalex_workers: int = 1,
    partial_prefix: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    cache_dir = out_dir / "cache"
    enriched: list[dict[str, Any]] = []
    partial_prefix = partial_prefix or []
    work_records = records[:max_items] if max_items else records
    total = len(work_records)
    openalex_results: dict[int, dict[str, Any]] = {}
    if openalex_workers > 1:
        print(f"[openalex] prefetching {total} titles with {openalex_workers} workers", flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=openalex_workers) as executor:
            futures = {
                executor.submit(fetch_openalex_safe, record, cache_dir): idx
                for idx, record in enumerate(work_records, start=1)
            }
            for done_count, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                idx = futures[future]
                openalex_results[idx] = future.result()
                if done_count % 25 == 0 or done_count == total:
                    print(f"[openalex] {done_count}/{total}", flush=True)

    arxiv_by_id: dict[str, dict[str, Any]] = {}
    if openalex_results:
        arxiv_ids = []
        for result in openalex_results.values():
            if result.get("matched") and not result.get("abstract"):
                for arxiv_id in arxiv_hints_from_candidates([result]):
                    if arxiv_id not in arxiv_ids:
                        arxiv_ids.append(arxiv_id)
        if arxiv_ids:
            print(f"[arxiv-id] batch prefetching {len(arxiv_ids)} ids", flush=True)
        for batch_start in range(0, len(arxiv_ids), 100):
            batch = arxiv_ids[batch_start : batch_start + 100]
            try:
                arxiv_by_id.update(fetch_arxiv_id_batch(batch, cache_dir))
            except Exception as exc:
                print(f"[arxiv-id] batch {batch_start // 100 + 1} failed: {exc}", flush=True)
            print(f"[arxiv-id] {min(batch_start + len(batch), len(arxiv_ids))}/{len(arxiv_ids)}", flush=True)
            if batch_start + len(batch) < len(arxiv_ids):
                time.sleep(arxiv_delay)

    for idx, record in enumerate(work_records, start=1):
        candidates: list[dict[str, Any]] = []
        print(f"[metadata] {idx}/{total} {record['archive_id']} {record['title'][:90]}", flush=True)
        if openalex_workers > 1:
            candidates.append(openalex_results.get(idx) or {"source": "openalex", "matched": False, "error": "missing prefetch result"})
        else:
            candidates.append(fetch_openalex_safe(record, cache_dir))
            time.sleep(openalex_delay)

        matched_any = any(c.get("matched") for c in candidates)
        arxiv_hints = arxiv_hints_from_candidates(candidates)
        has_arxiv_hint = bool(arxiv_hints)
        arxiv_tried = False
        if not any(c.get("matched") and c.get("abstract") for c in candidates) and (not matched_any or has_arxiv_hint):
            batch_candidate = None
            for arxiv_id in arxiv_hints:
                batch_candidate = arxiv_by_id.get(normalize_arxiv_id(arxiv_id)) or arxiv_by_id.get(normalize_arxiv_id(arxiv_id, strip_version=True))
                if batch_candidate:
                    break
            if batch_candidate:
                candidates.append(batch_candidate)
            else:
                try:
                    candidates.append(fetch_arxiv(record, cache_dir))
                except Exception as exc:
                    candidates.append({"source": "arxiv", "matched": False, "error": str(exc)})
                time.sleep(arxiv_delay)
            arxiv_tried = True

        if use_crossref and not any(c.get("matched") and (c.get("doi") or c.get("abstract")) for c in candidates):
            try:
                candidates.append(fetch_crossref(record, cache_dir))
            except Exception as exc:
                candidates.append({"source": "crossref", "matched": False, "error": str(exc)})
            time.sleep(crossref_delay)

        if use_semantic and not any(c.get("matched") and c.get("abstract") for c in candidates):
            try:
                candidates.append(fetch_semantic(record, cache_dir))
            except Exception as exc:
                candidates.append({"source": "semantic_scholar", "matched": False, "error": str(exc)})
            time.sleep(semantic_delay)

        has_abstract = any(c.get("matched") and c.get("abstract") for c in candidates)
        matched_any = any(c.get("matched") for c in candidates)
        arxiv_hints = arxiv_hints_from_candidates(candidates)
        has_arxiv_hint = bool(arxiv_hints)
        if not arxiv_tried and not has_abstract and (not matched_any or has_arxiv_hint):
            batch_candidate = None
            for arxiv_id in arxiv_hints:
                batch_candidate = arxiv_by_id.get(normalize_arxiv_id(arxiv_id)) or arxiv_by_id.get(normalize_arxiv_id(arxiv_id, strip_version=True))
                if batch_candidate:
                    break
            if batch_candidate:
                candidates.append(batch_candidate)
            else:
                try:
                    candidates.append(fetch_arxiv(record, cache_dir))
                except Exception as exc:
                    candidates.append({"source": "arxiv", "matched": False, "error": str(exc)})
                time.sleep(arxiv_delay)

        enriched.append(merge_metadata(dict(record), candidates))
        if idx % 25 == 0 or idx == total:
            write_jsonl(out_dir / "paper_index.partial.jsonl", partial_prefix + enriched)
    if max_items:
        enriched.extend(records[max_items:])
    return enriched


def looks_like_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 1024:
        return False
    with path.open("rb") as fh:
        head = fh.read(8)
    return head.startswith(b"%PDF")


def download_pdfs(
    records: list[dict[str, Any]],
    out_dir: Path,
    *,
    max_items: int | None = None,
    download_delay: float = 0.25,
) -> None:
    pdf_dir = out_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    work_records = records[:max_items] if max_items else records
    total = len(work_records)
    for idx, record in enumerate(work_records, start=1):
        metadata = record.get("metadata") or {}
        candidates = metadata.get("pdf_candidates") or []
        record["pdf"] = {"status": "no_public_pdf", "path": "", "url": "", "error": ""}
        if not candidates:
            continue
        base = f"{record['archive_id']}_{slugify(record['title'])}.pdf"
        pdf_path = pdf_dir / base
        if looks_like_pdf(pdf_path):
            record["pdf"] = {"status": "downloaded", "path": str(pdf_path.relative_to(out_dir)), "url": candidates[0], "error": ""}
            continue
        print(f"[pdf] {idx}/{total} {record['archive_id']} {record['title'][:90]}", flush=True)
        for url in candidates:
            try:
                run_curl(url, timeout=30, output=pdf_path)
                if looks_like_pdf(pdf_path):
                    record["pdf"] = {"status": "downloaded", "path": str(pdf_path.relative_to(out_dir)), "url": url, "error": ""}
                    break
                pdf_path.unlink(missing_ok=True)
            except Exception as exc:
                record["pdf"] = {"status": "download_failed", "path": "", "url": url, "error": str(exc)[:300]}
                pdf_path.unlink(missing_ok=True)
            time.sleep(download_delay)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def extract_text_from_pdf(pdf_path: Path, *, max_pages: int = 3) -> str:
    try:
        import pdfplumber  # type: ignore

        parts: list[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:max_pages]:
                parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        pass

    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        parts = []
        for page in reader.pages[:max_pages]:
            parts.append(page.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""


def extract_abstract_from_text(text: str) -> str:
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"-\n(?=[a-z])", "", normalized)
    normalized = re.sub(r"[ \t]+", " ", normalized)
    marker = re.search(r"(?im)^\s*(?:abstract|summary)\s*[:.\-]?\s*$", normalized)
    start = marker.end() if marker else -1
    if start < 0:
        marker = re.search(r"(?i)\babstract\b\s*[:.\-]\s*", normalized)
        start = marker.end() if marker else -1
    if start < 0:
        return ""

    tail = normalized[start:]
    end_match = re.search(
        r"(?im)^\s*(?:keywords?|key words|jel classification|classification|"
        r"1\.?\s+introduction|i\.?\s+introduction|introduction)\b",
        tail,
    )
    if end_match:
        tail = tail[: end_match.start()]
    abstract = re.sub(r"\s+", " ", tail).strip(" .\n\t")
    if len(abstract) < 80:
        return ""
    return abstract


def fill_missing_abstracts_from_pdfs(rows: list[dict[str, Any]], out_dir: Path) -> int:
    updated = 0
    for row in rows:
        if row.get("abstract"):
            continue
        pdf = row.get("pdf") or {}
        if pdf.get("status") != "downloaded" or not pdf.get("path"):
            continue
        pdf_path = out_dir / pdf["path"]
        if not pdf_path.exists():
            continue
        abstract = extract_abstract_from_text(extract_text_from_pdf(pdf_path))
        if not abstract:
            continue
        row["abstract"] = abstract
        row["abstract_source"] = "pdf_text"
        row.setdefault("metadata", {})["abstract_extracted_from_pdf"] = True
        updated += 1
    return updated


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "archive_id",
        "title",
        "year",
        "authors_display",
        "imported_date",
        "tags_visible",
        "source_pages",
        "duplicate_count",
        "doi",
        "arxiv_id",
        "openalex_id",
        "abstract_source",
        "has_abstract",
        "pdf_status",
        "pdf_path",
        "pdf_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            meta = row.get("metadata") or {}
            pdf = row.get("pdf") or {}
            writer.writerow(
                {
                    "archive_id": row.get("archive_id"),
                    "title": row.get("title"),
                    "year": row.get("year") or meta.get("year") or "",
                    "authors_display": row.get("authors_display") or "",
                    "imported_date": row.get("imported_date") or "",
                    "tags_visible": "; ".join(row.get("tags_visible") or []),
                    "source_pages": "; ".join(str(p) for p in row.get("source_pages") or []),
                    "duplicate_count": row.get("duplicate_count") or 1,
                    "doi": meta.get("doi") or "",
                    "arxiv_id": meta.get("arxiv_id") or "",
                    "openalex_id": meta.get("openalex_id") or "",
                    "abstract_source": row.get("abstract_source") or "",
                    "has_abstract": "yes" if row.get("abstract") else "no",
                    "pdf_status": pdf.get("status") or "",
                    "pdf_path": pdf.get("path") or "",
                    "pdf_url": pdf.get("url") or "",
                }
            )


def write_abstracts(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["# QuantML Paper Abstract Archive", ""]
    for row in rows:
        meta = row.get("metadata") or {}
        pdf = row.get("pdf") or {}
        lines.append(f"## {row['archive_id']} {row['title']}")
        lines.append("")
        lines.append(f"- Year: {row.get('year') or meta.get('year') or '-'}")
        lines.append(f"- Authors: {row.get('authors_display') or '-'}")
        if meta.get("doi"):
            lines.append(f"- DOI: {meta['doi']}")
        if meta.get("arxiv_id"):
            lines.append(f"- arXiv: {meta['arxiv_id']}")
        if meta.get("url"):
            lines.append(f"- Public URL: {meta['url']}")
        if pdf.get("status") == "downloaded":
            lines.append(f"- Local PDF: {pdf.get('path')}")
        else:
            lines.append(f"- Local PDF: {pdf.get('status') or 'not downloaded'}")
        lines.append(f"- Abstract source: {row.get('abstract_source') or '-'}")
        lines.append("")
        lines.append(row.get("abstract") or "No public abstract was found during this run.")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_missing_reports(out_dir: Path, rows: list[dict[str, Any]]) -> None:
    missing_abstract = [r for r in rows if not r.get("abstract")]
    missing_pdf = [r for r in rows if (r.get("pdf") or {}).get("status") != "downloaded"]
    for name, subset, label in [
        ("missing_abstracts.md", missing_abstract, "Missing Public Abstracts"),
        ("missing_pdfs.md", missing_pdf, "Missing Downloaded PDFs"),
    ]:
        lines = [f"# {label}", ""]
        for row in subset:
            pdf = row.get("pdf") or {}
            lines.append(f"- {row['archive_id']} | {row['title']} | PDF: {pdf.get('status') or '-'} | {pdf.get('error') or ''}")
        (out_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(
    out_dir: Path,
    *,
    raw_count: int,
    unique_count: int,
    rows: list[dict[str, Any]],
    source_dir: Path,
    source_files: int,
    skipped_downloads: bool,
    public_sources: list[str],
) -> None:
    downloaded = sum(1 for r in rows if (r.get("pdf") or {}).get("status") == "downloaded")
    abstracts = sum(1 for r in rows if r.get("abstract"))
    matched = sum(1 for r in rows if (r.get("metadata") or {}).get("matched_sources"))
    lines = [
        "# QuantML Paper Archive Report",
        "",
        f"- Created: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- Source directory: {source_dir}",
        f"- Source HTML files copied: {source_files}",
        f"- Raw visible paper rows: {raw_count}",
        f"- Unique normalized titles: {unique_count}",
        "- Source page displayed total: 1034, while local HTML contained 1033 visible rows.",
        f"- Public metadata matches: {matched}",
        f"- Abstracts saved: {abstracts}",
        f"- PDFs downloaded: {downloaded if not skipped_downloads else 'skipped'}",
        "",
        f"Public sources queried: {', '.join(public_sources)}.",
        "The original AxiomQ server returned HTTP 403 to automated access, so this run did not continue querying it.",
        "Sci-Hub or other unauthorized mirrors were not used.",
        "",
        "Key files:",
        "- paper_index.jsonl",
        "- paper_index.csv",
        "- abstracts.md",
        "- missing_abstracts.md",
        "- missing_pdfs.md",
        "- pdfs/",
        "- source_html/",
    ]
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_source_html(source_dir: Path, out_dir: Path) -> int:
    dest = out_dir / "source_html"
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for path in sorted(source_dir.glob("*.html")):
        shutil.copy2(path, dest / path.name)
        count += 1
    return count


def package_archive(out_dir: Path) -> Path:
    zip_base = out_dir.with_suffix("")
    archive_path = shutil.make_archive(str(zip_base), "zip", root_dir=out_dir)
    return Path(archive_path)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--max-items", type=int, default=None, help="Process only the first N unique titles.")
    parser.add_argument("--skip-download", action="store_true", help="Only save metadata and abstracts.")
    parser.add_argument("--no-crossref", action="store_true")
    parser.add_argument("--no-semantic", action="store_true")
    parser.add_argument("--openalex-delay", type=float, default=0.12)
    parser.add_argument("--openalex-workers", type=int, default=1)
    parser.add_argument("--arxiv-delay", type=float, default=3.1)
    parser.add_argument("--crossref-delay", type=float, default=0.2)
    parser.add_argument("--semantic-delay", type=float, default=1.0)
    parser.add_argument("--download-delay", type=float, default=0.25)
    parser.add_argument("--no-zip", action="store_true")
    parser.add_argument("--resume-partial", action="store_true", help="Continue from paper_index.partial.jsonl in out-dir.")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    source_dir = args.source_dir.resolve()
    if not source_dir.exists():
        print(f"Source directory not found: {source_dir}", file=sys.stderr)
        return 2

    out_dir = args.out_dir
    if out_dir is None:
        suffix = f"quantml_paper_archive_{now_stamp()}"
        if args.max_items:
            suffix += f"_sample{args.max_items}"
        out_dir = DEFAULT_OUT_ROOT / suffix
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[archive] output: {out_dir}", flush=True)
    source_files = copy_source_html(source_dir, out_dir)
    raw_records = parse_source_html(source_dir)
    unique_records = dedupe_records(raw_records)
    print(f"[archive] parsed {len(raw_records)} rows, {len(unique_records)} unique titles", flush=True)

    resume_prefix: list[dict[str, Any]] = []
    records_to_process = unique_records
    if args.resume_partial and not args.max_items:
        resume_prefix = read_jsonl(out_dir / "paper_index.partial.jsonl")
        if resume_prefix:
            start = len(resume_prefix)
            records_to_process = unique_records[start:]
            print(f"[archive] resuming after {start} partial records", flush=True)

    records = enrich_records(
        records_to_process,
        out_dir,
        max_items=args.max_items,
        openalex_delay=args.openalex_delay,
        arxiv_delay=args.arxiv_delay,
        crossref_delay=args.crossref_delay,
        semantic_delay=args.semantic_delay,
        use_crossref=not args.no_crossref,
        use_semantic=not args.no_semantic,
        openalex_workers=max(1, args.openalex_workers),
        partial_prefix=resume_prefix,
    )
    if resume_prefix:
        records = resume_prefix + records

    if args.skip_download:
        for record in records[: args.max_items] if args.max_items else records:
            record["pdf"] = {"status": "skipped", "path": "", "url": "", "error": ""}
    else:
        download_pdfs(records, out_dir, max_items=args.max_items, download_delay=args.download_delay)

    filled_from_pdf = fill_missing_abstracts_from_pdfs(records, out_dir)
    if filled_from_pdf:
        print(f"[archive] extracted {filled_from_pdf} abstracts from downloaded PDFs", flush=True)

    write_jsonl(out_dir / "paper_index.jsonl", records)
    partial_path = out_dir / "paper_index.partial.jsonl"
    if partial_path.exists():
        partial_path.unlink()
    write_csv(out_dir / "paper_index.csv", records)
    write_abstracts(out_dir / "abstracts.md", records)
    write_missing_reports(out_dir, records)
    write_report(
        out_dir,
        raw_count=len(raw_records),
        unique_count=len(unique_records),
        rows=records,
        source_dir=source_dir,
        source_files=source_files,
        skipped_downloads=args.skip_download,
        public_sources=[
            "OpenAlex",
            "arXiv",
            *([] if args.no_crossref else ["Crossref"]),
            *([] if args.no_semantic else ["Semantic Scholar"]),
        ],
    )
    if not args.no_zip:
        zip_path = package_archive(out_dir)
        print(f"[archive] zip: {zip_path}", flush=True)
    print("[archive] done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
