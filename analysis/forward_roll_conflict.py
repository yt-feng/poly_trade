"""Forward-only validation for the preregistered roll_conflict/taker/30s candidate.

This is research/shadow code. It cannot submit, sign, cancel, or reconcile orders.
The registry fixes the candidate and forward window boundary before evaluation.
"""
from __future__ import annotations
import argparse
import csv
from decimal import Decimal, ROUND_HALF_UP
import json
import random
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from canary_core import CausalSignals
from canary_readiness import load, replay

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "config" / "canary_forward_v1.json"


def slug_start_ms(slug: str) -> int:
    return int(str(slug).rsplit("-", 1)[1]) * 1000


def fee_round_5(value: float) -> float:
    """Protocol docs state fee USDC is rounded to five decimal places."""
    return float(Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def enrich_fee_precision(attempt: dict) -> dict:
    a = dict(attempt)
    shares = int(a.get("shares") or 0)
    if shares <= 0:
        return a
    a["public_fee_currency"] = "USDC"
    a["public_net_shares_after_fee"] = shares
    a["private_net_share_reconciled"] = False
    if a.get("status") != "valued":
        return a
    entry_fee_ps = float(a.get("entry_fee_per_share") or 0.0)
    total_fee_ps = float(a.get("fees_cents_per_share") or 0.0) / 100.0
    exit_fee_ps = max(0.0, total_fee_ps - entry_fee_ps)
    rounded_entry_total = fee_round_5(entry_fee_ps * shares)
    rounded_exit_total = fee_round_5(exit_fee_ps * shares)
    rounded_fee_ps = (rounded_entry_total + rounded_exit_total) / shares
    rounded_net_cents = float(a["gross_cents_per_share"]) - rounded_fee_ps * 100.0
    a["fee_rounded_entry_usdc"] = rounded_entry_total
    a["fee_rounded_exit_usdc"] = rounded_exit_total
    a["fee_precision_net_cents_per_share"] = rounded_net_cents
    a["conservative_net_cents_per_share"] = min(float(a["net_cents_per_share"]), rounded_net_cents)
    return a


def exposure_value_cents(a: dict) -> float | None:
    """Per-share result used for clustered lower bounds, including unknown exits."""
    if "entry_ms" not in a:
        return None
    shares = float(a.get("shares") or 0)
    if shares <= 0:
        return None
    if a.get("status") == "valued":
        return float(a.get("conservative_net_cents_per_share", a["net_cents_per_share"]))
    return float(a.get("pnl_lower_bound_usd") or 0.0) / shares * 100.0


def block_records(attempts: list[dict], hours: int) -> tuple[list[dict], list[dict]]:
    days: dict[str, list[float]] = defaultdict(list)
    blocks: dict[int, list[float]] = defaultdict(list)
    block_ms = hours * 3600 * 1000
    for a in attempts:
        value = exposure_value_cents(a)
        ms = a.get("entry_ms")
        if value is None or ms is None:
            continue
        day = datetime.fromtimestamp(ms / 1000, timezone.utc).date().isoformat()
        days[day].append(value)
        blocks[int(ms) // block_ms].append(value)
    day_rows = [{"kind": "utc_day", "block": k, "n": len(v), "mean_cents_per_share": statistics.mean(v), "sum_cents_per_share": sum(v)} for k, v in sorted(days.items())]
    time_rows = []
    for k, v in sorted(blocks.items()):
        start_ms = k * block_ms
        time_rows.append({
            "kind": f"utc_{hours}h",
            "block": datetime.fromtimestamp(start_ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "n": len(v),
            "mean_cents_per_share": statistics.mean(v),
            "sum_cents_per_share": sum(v),
        })
    return day_rows, time_rows


def clustered_bootstrap_lower(block_rows: list[dict], alpha: float, resamples: int, seed: int, minimum_blocks: int) -> float | None:
    if len(block_rows) < minimum_blocks:
        return None
    rng = random.Random(seed)
    stats = []
    nblocks = len(block_rows)
    for _ in range(resamples):
        selected = [block_rows[rng.randrange(nblocks)] for _ in range(nblocks)]
        count = sum(int(b["n"]) for b in selected)
        if count:
            stats.append(sum(float(b["sum_cents_per_share"]) for b in selected) / count)
    if not stats:
        return None
    stats.sort()
    index = max(0, min(len(stats) - 1, int(alpha * len(stats))))
    return stats[index]


def summarize_latency(attempts: list[dict], registry: dict, delay: int) -> tuple[dict, list[dict]]:
    attempts = [enrich_fee_precision(a) for a in attempts]
    entered = [a for a in attempts if "entry_ms" in a]
    valued = [a for a in attempts if a.get("status") == "valued"]
    values = [float(a["conservative_net_cents_per_share"]) for a in valued]
    stress_values = [float(a["stress_net_cents_per_share"]) for a in valued]
    conf = registry["confidence"]
    day_rows, time_rows = block_records(attempts, int(conf["time_block_hours"]))
    lower_day = clustered_bootstrap_lower(
        day_rows, float(conf["one_sided_alpha"]), int(conf["bootstrap_resamples"]),
        int(conf["bootstrap_seed"]) + delay, int(conf["minimum_utc_day_blocks"]),
    )
    lower_time = clustered_bootstrap_lower(
        time_rows, float(conf["one_sided_alpha"]), int(conf["bootstrap_resamples"]),
        int(conf["bootstrap_seed"]) + 100 + delay, int(conf["minimum_time_blocks"]),
    )
    summary = {
        "latency_seconds": delay,
        "attempts": len(attempts),
        "entered_attempts": len(entered),
        "valued_attempts": len(valued),
        "valued_exit_fraction": len(valued) / len(entered) if entered else 0.0,
        "attempt_windows": len({a["slug"] for a in attempts}),
        "mean_net_cents_per_share": statistics.mean(values) if values else None,
        "median_net_cents_per_share": statistics.median(values) if values else None,
        "wins": sum(v > 0 for v in values),
        "mean_extra_exit_tick_net_cents_per_share": statistics.mean(stress_values) if stress_values else None,
        "total_valued_pnl_usd": sum(float(a.get("pnl_usd") or 0.0) for a in valued),
        "unknown_exit_worst_case_total_usd": sum(float(a.get("pnl_lower_bound_usd") or 0.0) for a in attempts),
        "utc_day_blocks_with_exposure": len(day_rows),
        "time_blocks_with_exposure": len(time_rows),
        "utc_day_cluster_lower95_cents_per_share": lower_day,
        "time_block_cluster_lower95_cents_per_share": lower_time,
        "fee_precision_view": "Each entry/exit USDC fee total rounded to 5 decimals; conservative net is the worse of continuous and rounded fee views.",
        "public_net_share_view": "Current public fee documentation states fees in USDC, so replay keeps purchased shares unchanged; actual matched fills/net position still require private reconciliation.",
    }
    blocks = [dict(r, latency_seconds=delay) for r in day_rows + time_rows]
    return summary, blocks


def gate(summary1: dict, summary3: dict, coverage: dict, registry: dict) -> dict:
    f = registry["fixed_checkpoints"]
    reasons = []
    checks = {
        "coverage_windows": coverage["forward_distinct_windows"] >= int(f["minimum_final_distinct_windows"]),
        "coverage_utc_days": coverage["forward_utc_days"] >= int(f["minimum_final_utc_days"]),
        "valued_attempts": summary1["valued_attempts"] >= int(f["minimum_final_valued_attempts"]),
        "valued_exit_fraction": summary1["valued_exit_fraction"] >= float(f["minimum_valued_exit_fraction"]),
        "primary_mean_positive": summary1["mean_net_cents_per_share"] is not None and summary1["mean_net_cents_per_share"] > 0,
        "primary_day_lower_positive": summary1["utc_day_cluster_lower95_cents_per_share"] is not None and summary1["utc_day_cluster_lower95_cents_per_share"] > 0,
        "primary_time_lower_positive": summary1["time_block_cluster_lower95_cents_per_share"] is not None and summary1["time_block_cluster_lower95_cents_per_share"] > 0,
        "stress_3s_mean_positive": summary3["mean_net_cents_per_share"] is not None and summary3["mean_net_cents_per_share"] > 0,
        "stress_3s_day_lower_positive": summary3["utc_day_cluster_lower95_cents_per_share"] is not None and summary3["utc_day_cluster_lower95_cents_per_share"] > 0,
        "extra_exit_tick_positive": summary1["mean_extra_exit_tick_net_cents_per_share"] is not None and summary1["mean_extra_exit_tick_net_cents_per_share"] > 0,
        "worst_case_exit_ledger_positive": summary1["unknown_exit_worst_case_total_usd"] > 0,
    }
    for name, ok in checks.items():
        if not ok:
            reasons.append(name)
    evidence_ready = all(checks.values())
    return {
        "checks": checks,
        "human_review_evidence_ready": evidence_ready,
        "live_canary_eligible": False,
        "live_canary_blockers": [
            "separate human approval is mandatory",
            "private order/fill/partial-fill/cancel/fee/net-position reconciliation is mandatory",
            "this research module has no live order capability",
        ],
        "failed_evidence_checks": reasons,
    }


def checkpoint_status(summary: dict, coverage: dict, registry: dict) -> dict:
    f = registry["fixed_checkpoints"]
    valued = int(summary["valued_attempts"])
    windows = int(coverage["forward_distinct_windows"])
    return {
        "valued_attempts_reached": [x for x in f["valued_attempts"] if valued >= int(x)],
        "distinct_windows_reached": [x for x in f["distinct_windows"] if windows >= int(x)],
        "monitoring_only": bool(f["checkpoint_results_are_monitoring_only"]),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for k, v in row.items()})


def run(input_root: Path, output_root: Path, registry_path: Path = DEFAULT_REGISTRY) -> dict:
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if registry["candidate"]["hypothesis"] != "roll_conflict" or registry["candidate"]["execution_model"] != "taker" or int(registry["candidate"]["hold_seconds"]) != 30:
        raise ValueError("forward v1 candidate identity changed; create a new registry version instead")
    rows, source_coverage = load(input_root)
    boundary = int(registry["registration"]["forward_min_window_start_ms"])
    forward_rows = [r for r in rows if slug_start_ms(r["slug"]) >= boundary]
    coverage = {
        "registry": registry["registry_name"],
        "forward_min_window_start_ms": boundary,
        "forward_min_window_start_utc": registry["registration"]["forward_min_window_start_utc"],
        "source_v3_rows": source_coverage["v3_rows"],
        "source_v3_windows": source_coverage["v3_windows"],
        "forward_rows": len(forward_rows),
        "forward_distinct_windows": len({r["slug"] for r in forward_rows}),
        "forward_utc_days": len({datetime.fromtimestamp(slug_start_ms(r["slug"]) / 1000, timezone.utc).date() for r in forward_rows}),
        "forward_first_window_start_ms": min((slug_start_ms(r["slug"]) for r in forward_rows), default=None),
        "forward_last_window_start_ms": max((slug_start_ms(r["slug"]) for r in forward_rows), default=None),
        "pre_boundary_returns_excluded": True,
        "source_conflicting_duplicates": source_coverage["conflicting_duplicates"],
    }
    engine = CausalSignals()
    features = [engine.update(r) for r in rows]
    hold = int(registry["candidate"]["hold_seconds"])
    delays = [int(registry["candidate"]["primary_latency_seconds"]), int(registry["candidate"]["stress_latency_seconds"])]
    by_delay = {}
    all_attempts = []
    all_blocks = []
    for delay in delays:
        raw = replay(rows, features, "roll_conflict", hold, delay, "taker")
        forward = [a for a in raw if slug_start_ms(a["slug"]) >= boundary]
        summary, blocks = summarize_latency(forward, registry, delay)
        by_delay[str(delay)] = summary
        all_attempts.extend(dict(a, forward_latency_seconds=delay) for a in [enrich_fee_precision(x) for x in forward])
        all_blocks.extend(blocks)
    primary = by_delay[str(delays[0])]
    stress = by_delay[str(delays[1])]
    decision = gate(primary, stress, coverage, registry)
    result = {
        "registry_version": registry["version"],
        "candidate": registry["candidate"],
        "registration": registry["registration"],
        "coverage": coverage,
        "primary": primary,
        "stress_3s": stress,
        "checkpoints": checkpoint_status(primary, coverage, registry),
        "decision": decision,
        "multiple_testing": registry["confidence"]["multiple_testing_method"],
        "research_only": True,
        "generated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "forward_readiness.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_root / "forward_attempts.csv", all_attempts)
    write_csv(output_root / "forward_blocks.csv", all_blocks)
    status = [
        "# Canary Forward v1 status",
        "",
        "Research/shadow only. Real orders remain disabled.",
        "",
        f"- Candidate: `roll_conflict / taker / {hold}s`",
        f"- Forward boundary: `{registry['registration']['forward_min_window_start_utc']}` (whole-window start)",
        f"- Forward windows observed: **{coverage['forward_distinct_windows']}** across **{coverage['forward_utc_days']}** UTC day(s)",
        f"- Primary valued attempts: **{primary['valued_attempts']}**; exit fraction: **{primary['valued_exit_fraction']:.4f}**",
        f"- Human-review evidence ready: **{decision['human_review_evidence_ready']}**",
        f"- Live-canary eligible: **{decision['live_canary_eligible']}**",
        "",
        "Failure of an evidence check is retained; v1 does not change candidate or thresholds in response.",
    ]
    (output_root / "STATUS.md").write_text("\n".join(status) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="forward_inputs/captured")
    parser.add_argument("--output", default="forward_results")
    parser.add_argument("--registry", default=str(DEFAULT_REGISTRY))
    args = parser.parse_args()
    result = run(Path(args.input), Path(args.output), Path(args.registry))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
