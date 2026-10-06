"""One offline historical quote-factor diagnostic; private outputs only.

This consumes a frozen protocol and existing CSV bytes, never execution inputs
or settlement labels. All scheduled markets remain in coverage denominators.
Nothing in this module contacts a service, selects a strategy, or places orders.
"""
from __future__ import annotations

import argparse
import bisect
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path

import numpy as np

try:
    from archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, day, market_start, number, timestamp, write_private
except ImportError:
    from analysis.archive_baseline_audit import PRICE_FIELDS, SIZE_FIELDS, day, market_start, number, timestamp, write_private


FEATURES = ("momentum", "spread", "imbalance")
TARGETS = ("bid_change_cents", "ask_change_cents")


def validate_protocol(p):
    train, validation = p["train_dates"], p["validation_dates"]
    for date in train + validation:
        datetime.strptime(date, "%Y-%m-%d")
    if not train or not validation or max(train) >= min(validation):
        raise ValueError("non_chronological_or_overlapping_dates")
    if len(set(train + validation)) != len(train + validation):
        raise ValueError("duplicate_dates")
    if p["window_seconds"] != 300 or p["target_token"] != "up":
        raise ValueError("unsupported_scope")
    for name in ("decision_offset_seconds", "lookback_seconds", "horizon_seconds",
                 "max_quote_age_seconds", "future_tolerance_seconds", "stability_block_hours",
                 "min_train_labels", "min_validation_labels"):
        if type(p[name]) is not int or p[name] <= 0:
            raise ValueError("invalid_positive_integer")
    if not (p["max_quote_age_seconds"] < p["lookback_seconds"] < p["decision_offset_seconds"] and
            p["decision_offset_seconds"] + p["horizon_seconds"] + p["future_tolerance_seconds"] < 300):
        raise ValueError("invalid_causal_timing")
    if 24 % p["stability_block_hours"]:
        raise ValueError("invalid_stability_block")
    if p["features"] != list(FEATURES) or p["targets"] != list(TARGETS):
        raise ValueError("unsupported_features_or_targets")
    if not 0 < p["quantile_cuts"][0] < p["quantile_cuts"][1] < 1:
        raise ValueError("invalid_quantile_cuts")
    if not np.isfinite(p["ridge_penalty"]) or p["ridge_penalty"] <= 0:
        raise ValueError("invalid_ridge_penalty")
    if p["models"].get("zero") != [] or p["models"].get("train_mean") != [] or p["models"].get("momentum") != ["momentum"]:
        raise ValueError("missing_fixed_baselines")
    if not all(set(fields) <= set(FEATURES) and len(fields) == len(set(fields)) for fields in p["models"].values()):
        raise ValueError("unknown_or_duplicate_feature")


def quote_values(row):
    """Read only recorded prices/sizes; final_price and all other labels are ignored."""
    values = tuple(number(row.get(name)) for name in PRICE_FIELDS + SIZE_FIELDS)
    if any(value is None for value in values):
        return values, "missing_or_nonfinite_quote_or_size"
    if any(not 0 < value < 100 for value in values[:4]):
        return values, "quote_outside_binary_range"
    if any(value <= 0 for value in values[4:]):
        return values, "nonpositive_touch_size"
    if values[1] > values[0] or values[3] > values[2]:
        return values, "crossed_quote"
    return values, None


def load_frozen_quotes(root, protocol):
    import csv
    dates = set(protocol["train_dates"] + protocol["validation_dates"])
    records, conflicts, counts, row_reasons = {}, set(), Counter(), Counter()
    source_files = []
    for item in protocol["files"]:
        path = (root / item["path"]).resolve()
        if root.resolve() not in path.parents:
            raise ValueError("source_outside_frozen_root")
        data = path.read_bytes()
        if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("frozen_source_hash_mismatch")
        source_files.append(item)
        for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
            counts["scanned_rows"] += 1
            slug, start = market_start(row)
            ts = timestamp(row.get("ts_iso"))
            if (start is None or day(start) not in dates) and (ts is None or day(ts) not in dates):
                continue
            counts["date_scoped_rows"] += 1
            if start is None:
                row_reasons["invalid_btc5m_identity"] += 1
                continue
            if day(start) not in dates:
                row_reasons["adjacent_window_outside_fixed_dates"] += 1
                continue
            if ts is None or not start <= ts < start + 300:
                row_reasons["invalid_or_outside_window_sample_time"] += 1
                continue
            values, reason = quote_values(row)
            row_reasons.update([reason] if reason else [])
            key = (slug, ts)
            if key in records:
                counts["duplicate_timestamps"] += 1
                if records[key]["values"] != values:
                    conflicts.add(slug)
                    counts["conflicting_timestamps"] += 1
            else:
                # Keep invalid observations: never jump past them to a prettier quote.
                records[key] = {"ts": ts, "values": values, "invalid_reason": reason}
    grouped = defaultdict(list)
    for (slug, _), row in records.items():
        grouped[slug].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda r: r["ts"])
    return grouped, conflicts, {"counts": dict(counts), "row_reasons_overlap": dict(row_reasons),
                                "conflicting_windows": len(conflicts), "source_files": source_files}


def scheduled_samples(grouped, conflicts, p):
    """One decision per complete market; features frozen before future lookup."""
    samples = []
    for date in p["train_dates"] + p["validation_dates"]:
        day_start = timestamp(date + "T00:00:00Z")
        for offset in range(0, 86400, 300):
            start = int(day_start + offset)
            slug = f"btc-updown-5m-{start}"
            decision = start + p["decision_offset_seconds"]
            item = {"market": slug, "date": date, "start": start, "decision_time": decision,
                    "split": "train" if date in p["train_dates"] else "validation",
                    "features": None, "targets": None, "status": None}
            samples.append(item)
            if slug in conflicts:
                item["status"] = "excluded_conflicting_window"
                continue
            rows = grouped.get(slug, [])
            times = [row["ts"] for row in rows]
            if not rows:
                item["status"] = "absent_window"
                continue
            index = bisect.bisect_left(times, decision) - 1
            if index < 0 or decision - times[index] > p["max_quote_age_seconds"] or rows[index]["invalid_reason"]:
                item["status"] = "excluded_decision_quote"
                continue
            current = rows[index]
            lag_cutoff = decision - p["lookback_seconds"]
            lag_index = bisect.bisect_right(times, lag_cutoff) - 1
            if lag_index < 0 or lag_cutoff - times[lag_index] > p["max_quote_age_seconds"] or rows[lag_index]["invalid_reason"]:
                item["status"] = "excluded_lookback_quote"
                continue
            lag = rows[lag_index]
            ask, bid, _, _, ask_size, bid_size, _, _ = current["values"]
            item["features"] = {
                "momentum": (ask + bid - lag["values"][0] - lag["values"][1]) / 2,
                "spread": ask - bid,
                "imbalance": (bid_size - ask_size) / (bid_size + ask_size),
            }
            item["feature_times"] = [lag["ts"], current["ts"]]
            future_index = bisect.bisect_left(times, decision + p["horizon_seconds"])
            if future_index >= len(rows) or times[future_index] > decision + p["horizon_seconds"] + p["future_tolerance_seconds"]:
                item["status"] = "unknown_future_missing"
                continue
            future = rows[future_index]
            if future["invalid_reason"]:
                item["status"] = "unknown_future_invalid"
                continue
            item["target_time"] = future["ts"]
            item["targets"] = {"bid_change_cents": future["values"][1] - bid,
                               "ask_change_cents": future["values"][0] - ask}
            item["status"] = "labeled_quote_change"
    return samples


def coverage(rows):
    statuses = Counter(row["status"] for row in rows)
    return {"scheduled_windows": len(rows), "status_counts": dict(sorted(statuses.items())),
            "feature_eligible_windows": sum(row["features"] is not None for row in rows),
            "labeled_windows": statuses["labeled_quote_change"],
            "unknown_future_windows": sum(n for name, n in statuses.items() if name.startswith("unknown_future_"))}


def fit(rows, target, fields, penalty):
    """Train-only scaling, intercept and fixed ridge; no validation selection."""
    y = np.array([row["targets"][target] for row in rows], dtype=float)
    model = {"fields": fields, "intercept": float(y.mean()), "center": [], "scale": [], "coefficients": []}
    if fields:
        x = np.array([[row["features"][f] for f in fields] for row in rows], dtype=float)
        center, scale = x.mean(axis=0), x.std(axis=0)
        model["constant_features"] = [field for field, value in zip(fields, scale) if value == 0]
        scale[scale == 0] = 1
        z = (x - center) / scale
        coefficients = np.linalg.solve(z.T @ z / len(y) + penalty * np.eye(len(fields)), z.T @ (y - y.mean()) / len(y))
        model.update(center=center.tolist(), scale=scale.tolist(), coefficients=coefficients.tolist())
    return model


def predict(model, row):
    raw = model["intercept"] + sum((row["features"][f] - c) / s * b for f, c, s, b in
                                  zip(model["fields"], model["center"], model["scale"], model["coefficients"]))
    return float(np.clip(raw, -100, 100))


def score(rows, target, model):
    eligible = [row for row in rows if row["features"] is not None]
    known = [row for row in eligible if row["targets"] is not None]
    unknown = [row for row in eligible if row["targets"] is None]
    errors = [predict(model, row) - row["targets"][target] for row in known]
    ss = sum(error * error for error in errors)
    bound = sum((100 + abs(predict(model, row))) ** 2 for row in unknown)
    return {"n_labeled": len(known), "n_unknown": len(unknown), "n_scheduled": len(rows),
            "conditional_mse": ss / len(known) if known else None,
            "conditional_mae": sum(abs(error) for error in errors) / len(known) if known else None,
            "feature_eligible_mse_lower_bound": ss / len(eligible) if eligible else None,
            "feature_eligible_mse_upper_bound": (ss + bound) / len(eligible) if eligible else None,
            "bound_scope": "unknown quote changes bounded by [-100,100] cents; decision exclusions remain outside this bound"}


def factor_effects(train, validation, target, quantiles):
    result = {}
    for field in FEATURES:
        train_x = [row["features"][field] for row in train]
        low, high = np.quantile(train_x, quantiles)
        effects = {"train_cutpoints": [float(low), float(high)]}
        for name, rows in (("train", train), ("validation", validation)):
            known = [row for row in rows if row["targets"] is not None and row["features"] is not None]
            x = np.array([row["features"][field] for row in known])
            y = np.array([row["targets"][target] for row in known])
            corr = float(np.corrcoef(x, y)[0, 1]) if len(x) >= 3 and x.std() > 0 and y.std() > 0 else None
            bottom = [row["targets"][target] for row in known if row["features"][field] <= low]
            top = [row["targets"][target] for row in known if row["features"][field] >= high]
            effects[name] = {"n": len(known), "pearson": corr, "n_low": len(bottom), "n_high": len(top),
                             "high_minus_low_cents": float(np.mean(top) - np.mean(bottom)) if low < high and top and bottom else None,
                             "cutpoints_distinct": bool(low < high)}
        result[field] = effects
    return result


def diagnose(samples, p):
    train_all = [row for row in samples if row["split"] == "train"]
    validation = [row for row in samples if row["split"] == "validation"]
    train = [row for row in train_all if row["features"] is not None and row["targets"] is not None]
    result = {"coverage": {"train": coverage(train_all), "validation": coverage(validation)}, "targets": {}, "failures": []}
    if len(train) < p["min_train_labels"]:
        result["failures"].append("insufficient_training_labels")
        return result
    if coverage(validation)["labeled_windows"] < p["min_validation_labels"]:
        result["failures"].append("insufficient_validation_labels_for_interpretation")
    for target in p["targets"]:
        models = {name: fit(train, target, fields, p["ridge_penalty"]) for name, fields in p["models"].items()}
        models["zero"]["intercept"] = 0.0
        scored = {name: {"train": score(train_all, target, model), "validation": score(validation, target, model),
                         "fit": model} for name, model in models.items()}
        for entry in scored.values():
            mse = entry["validation"]["conditional_mse"]
            entry["validation"]["mse_improvement_vs"] = {
                baseline: scored[baseline]["validation"]["conditional_mse"] - mse if mse is not None else None
                for baseline in ("zero", "train_mean", "momentum")}
        blocks = defaultdict(list)
        for row in validation:
            dt = datetime.fromtimestamp(row["start"], timezone.utc)
            blocks[f'{row["date"]}T{dt.hour // p["stability_block_hours"] * p["stability_block_hours"]:02d}'].append(row)
        ablations = {}
        if "all" in scored:
            full_mse = scored["all"]["validation"]["conditional_mse"]
            for field in FEATURES:
                reduced = next((name for name, fields in p["models"].items()
                                if set(fields) == set(FEATURES) - {field}), None)
                reduced_mse = scored[reduced]["validation"]["conditional_mse"] if reduced else None
                ablations[field] = {"reduced_model": reduced,
                    "mse_improvement_when_added": reduced_mse - full_mse if reduced_mse is not None and full_mse is not None else None}
        result["targets"][target] = {
            "models": scored, "effects": factor_effects(train, validation, target, p["quantile_cuts"]),
            "leave_one_factor_out": ablations,
            "validation_time_blocks": {block: {"coverage": coverage(rows),
                "effects": factor_effects(train, rows, target, p["quantile_cuts"]),
                "models": {name: score(rows, target, model) for name, model in models.items()}}
                for block, rows in sorted(blocks.items())},
            "validation_dates": {date: {"coverage": coverage([row for row in validation if row["date"] == date]),
                "effects": factor_effects(train, [row for row in validation if row["date"] == date], target, p["quantile_cuts"]),
                "models": {name: score([row for row in validation if row["date"] == date], target, model) for name, model in models.items()}}
                for date in p["validation_dates"]}}
    result["failures"].extend(["historical_exposure_not_independent_oos", "sample_time_not_verified_feed_availability",
                               "quote_association_not_execution_or_settlement", "too_few_dates_for_confirmatory_significance"])
    if any(row["status"].startswith("unknown_future_") for row in samples):
        result["failures"].append("unknown_future_quotes_remain_in_denominator")
    return result


def run(root, protocol):
    validate_protocol(protocol)
    grouped, conflicts, intake = load_frozen_quotes(root, protocol)
    samples = scheduled_samples(grouped, conflicts, protocol)
    result = diagnose(samples, protocol)
    for item in protocol["files"]:
        if hashlib.sha256((root / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("source_changed_during_diagnostic")
    return {"scope": "historical_quote_association_only", "protocol": protocol, "intake": intake,
            "diagnostic": result, "window_ledger": samples, "raw_unchanged": True,
            "real_fills": 0, "pnl": None, "canary_allowed": False,
            "effect_scope": "All effects and conditional errors describe observed labels only. Unknown future quotes are neither zero changes nor losses; their counts and error bounds remain explicit.",
            "leakage_boundary": "Features use sample timestamps strictly before decision; targets are future quotes within the same market used only for scoring. Exact feed availability is unverified."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--poly-root", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    protocol_bytes = args.protocol.read_bytes()
    result = run(args.poly_root, json.loads(protocol_bytes))
    result["protocol_sha256"] = hashlib.sha256(protocol_bytes).hexdigest()
    result["code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    write_private(args.output, result)
    print("PRIVATE_DIAGNOSTIC_COMPLETE; no findings printed, no trading evidence created.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
