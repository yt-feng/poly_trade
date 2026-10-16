"""Pure source-coverage audit; policy is supplied by a private experiment record.

No strategy, fitted model, fill simulation, network access or cash accounting.
Legacy publisher observations establish field availability, not stream authenticity.
"""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime
import math
import re
from urllib.parse import urlsplit


WINDOW_MS = 300_000
SLUG = re.compile(r"btc-updown-5m-(\d+)\Z")
CONDITION = re.compile(r"0x[0-9a-fA-F]{64}\Z")
PTB_PATHS = {
    "events[matching_slug].eventMetadata.priceToBeat",
    "gamma.events/slug/{slug}.eventMetadata.priceToBeat",
    "polymarket.event_page.crypto-prices.openPrice",
    "polymarket.client_api.crypto-price.openPrice",
}
REQUIRED_POLICY = {"decision_seconds_before_end", "anchor_max_age_ms",
                   "reference_max_age_ms", "metadata_max_age_ms"}
OPTIONAL_POLICY = {"snapshot_scope_start_ms", "snapshot_scope_end_ms",
                   "label_observed_through_ms"}


def _integer(value):
    return type(value) is int and value >= 0


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _positive(value):
    result = _decimal(value)
    return result if result is not None and result > 0 else None


def _epoch_ms(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
        if isinstance(value, float) and value != n:
            return None
        return n // 1000 if n > 10**14 else n * 1000 if n < 10**11 else n
    except (ValueError, TypeError, OverflowError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return int(stamp.timestamp() * 1000) if stamp.tzinfo is not None else None
        except (ValueError, OverflowError):
            return None


def _obj(value):
    return value if isinstance(value, dict) else {}


def validate_policy(policy):
    """Validate explicit policy before any caller starts fetching source data."""
    if not isinstance(policy, dict) or not REQUIRED_POLICY <= policy.keys():
        raise ValueError("Missing required audit policy fields")
    if policy.keys() - REQUIRED_POLICY - OPTIONAL_POLICY:
        raise ValueError("Unknown audit policy fields")
    before = policy["decision_seconds_before_end"]
    if not _number(before) or not 0 < before < WINDOW_MS / 1000:
        raise ValueError("Decision offset must be a finite number inside the window")
    if not float(before * 1000).is_integer():
        raise ValueError("Decision offset must resolve to whole milliseconds")
    for key in policy.keys() - {"decision_seconds_before_end"}:
        if not _integer(policy[key]):
            raise ValueError("Audit clocks and ages must be nonnegative integers")
    scope = {"snapshot_scope_start_ms", "snapshot_scope_end_ms"}
    if scope & policy.keys() and not scope <= policy.keys():
        raise ValueError("Both snapshot scope bounds are required")
    if scope <= policy.keys() and policy["snapshot_scope_start_ms"] > policy["snapshot_scope_end_ms"]:
        raise ValueError("Snapshot scope bounds are reversed")
    return dict(policy)


def _clock(value, now, age=None):
    return _integer(value) and value <= now and (age is None or now - value <= age)


def _reference_source(value):
    """Check the recorded rule identity, without claiming feed authentication."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        return (parsed.scheme == "https" and parsed.netloc == "data.chain.link"
                and parsed.path.rstrip("/") == "/streams/btc-usd-twap-60s-streams"
                and not parsed.query and not parsed.fragment)
    except ValueError:
        return False


def _features(row, start, cutoff, policy):
    """Evaluate a previously selected anchor without searching for a better row."""
    reasons = defaultdict(list)
    ms = row["sample_ms"]
    micro = _obj(row.get("microstructure"))
    rule = _obj(micro.get("rules"))
    flags = _obj(micro.get("valid"))
    slug = f"btc-updown-5m-{start // 1000}"

    def need(stage, condition, reason):
        if not condition:
            reasons[stage].append(reason)

    def decision_clock(value, age):
        return _clock(value, ms) and _clock(value, cutoff, age)

    need("anchor", cutoff - ms <= policy["anchor_max_age_ms"], "anchor_stale")
    need("identity", row.get("asset") == "btc" and row.get("slug") == slug,
         "snapshot_identity_mismatch")
    need("identity", type(row.get("schema_version")) is int and row["schema_version"] == 2,
         "snapshot_schema_unknown")
    need("identity", _integer(row.get("window_start_ms")) and row["window_start_ms"] == start,
         "window_start_mismatch")
    need("identity", type(micro.get("schema_version")) is int and micro["schema_version"] == 3,
         "microstructure_schema_unknown")
    need("identity", _integer(micro.get("asof_ms")) and micro["asof_ms"] == ms,
         "microstructure_clock_mismatch")
    need("identity", rule.get("slug") == slug, "rule_slug_mismatch")
    condition = rule.get("condition_id")
    need("identity", isinstance(condition, str) and bool(CONDITION.fullmatch(condition)),
         "condition_id_missing_or_invalid")
    tokens = [row.get("up_token_id"), row.get("down_token_id")]
    need("identity", all(isinstance(t, str) and t.isdecimal() for t in tokens)
         and tokens[0] != tokens[1], "token_identity_missing_or_conflicted")
    need("identity", decision_clock(rule.get("metadata_received_ms"), policy["metadata_max_age_ms"]),
         "metadata_clock_missing_future_or_stale")
    need("identity", rule.get("reference_kind") == "chainlink_twap_60"
         and _number(rule.get("twap_lookback_seconds")) and rule["twap_lookback_seconds"] == 60,
         "reference_rule_mismatch")
    need("identity", _reference_source(rule.get("resolution_source")),
         "resolution_source_missing_or_mismatched")

    ptb = _positive(rule.get("published_price_to_beat"))
    receipt = rule.get("price_to_beat_received_ms")
    need("ptb", isinstance(rule.get("published_price_to_beat"), str) and ptb is not None,
         "ptb_missing_or_invalid")
    need("ptb", rule.get("price_to_beat_path") in PTB_PATHS, "ptb_provenance_unknown")
    need("ptb", _clock(receipt, ms) and receipt >= start, "ptb_receipt_missing_or_noncausal")
    metadata = rule.get("metadata_received_ms")
    need("ptb", _integer(receipt) and _integer(metadata) and receipt <= metadata,
         "ptb_after_metadata")
    conflict = rule.get("price_to_beat_conflict", False)
    need("ptb", conflict is False, "ptb_conflict_or_unknown_flag")
    source_clock = rule.get("price_to_beat_source_timestamp_ms")
    if source_clock is not None:
        need("ptb", _integer(receipt) and _clock(source_clock, receipt) and source_clock >= start,
             "ptb_source_clock_invalid")
    if row.get("official_price_to_beat") is not None:
        need("ptb", ptb is not None and _positive(row["official_price_to_beat"]) == ptb,
             "ptb_duplicate_conflict")
    provenance = row.get("official_price_to_beat_provenance")
    if provenance is not None:
        provenance = _obj(provenance)
        need("ptb", provenance.get("received_ms") == receipt
             and provenance.get("path") == rule.get("price_to_beat_path"),
             "ptb_provenance_conflict")

    twap = _obj(micro.get("twap60"))
    payload = _obj(twap.get("payload"))
    price = _positive(twap.get("price"))
    need("twap", isinstance(twap.get("price"), str) and price is not None,
         "twap_price_missing_or_invalid")
    need("twap", twap.get("source") == "crypto_prices_twap_sixty"
         and type(twap.get("window_seconds")) is int and twap["window_seconds"] == 60
         and payload.get("symbol") == "btc/usd", "twap_legacy_identity_mismatch")
    if "window_s" in payload:
        need("twap", _number(payload["window_s"]) and payload["window_s"] == 60,
             "twap_payload_window_mismatch")
    for name in ("received_ms", "event_ms"):
        need("twap", decision_clock(twap.get(name), policy["reference_max_age_ms"]),
             "twap_" + name + "_missing_future_or_stale")
    if twap.get("publisher_ms") is not None:
        need("twap", _clock(twap["publisher_ms"], ms), "twap_publisher_clock_future_or_invalid")
    need("twap", isinstance(twap.get("connection_id"), str) and bool(twap["connection_id"]),
         "twap_connection_missing")
    need("twap", _epoch_ms(payload.get("timestamp")) == twap.get("event_ms")
         and _integer(twap.get("event_ms")), "twap_payload_event_clock_mismatch")
    full = _decimal(payload.get("full_accuracy_value"))
    with localcontext() as context:
        context.prec = 100
        published = full / Decimal(10**18) if full is not None else _decimal(payload.get("value"))
    need("twap", price is not None and published == price, "twap_published_value_mismatch")
    matched = _obj(micro.get("matching_resolution_reference"))
    need("twap", _positive(matched.get("price")) == price and bool(twap)
         and all(matched.get(k) == twap.get(k) for k in
                 ("received_ms", "event_ms", "connection_id", "source", "window_seconds")),
         "matching_reference_mismatch")
    for flag in ("twap60", "matching_reference_feed", "official_threshold", "resolution_reference"):
        need("twap", flags.get(flag) is True, "producer_flag_not_true:" + flag)

    spot = _obj(row.get("binance"))
    vol = _obj(_obj(micro.get("spot_returns_and_vol")).get("60s"))
    need("vol60", row.get("binance_source") == "binance_spot_ws"
         and row.get("binance_symbol") == "BTCUSDT" and row.get("binance_valid") is True,
         "spot_identity_or_validity_missing")
    need("vol60", _positive(spot.get("price")) is not None, "spot_price_missing_or_invalid")
    for name in ("received_ms", "event_ms"):
        need("vol60", decision_clock(spot.get(name), policy["reference_max_age_ms"]),
             "spot_" + name + "_missing_future_or_stale")
    need("vol60", vol.get("warmed") is True, "vol60_not_warmed")
    need("vol60", _integer(vol.get("valid_intervals")) and vol["valid_intervals"] >= 59,
         "vol60_intervals_insufficient")
    need("vol60", _number(vol.get("realized_vol_bps")) and vol["realized_vol_bps"] >= 0,
         "vol60_missing_or_invalid")
    need("vol60", _number(vol.get("return_bps")), "vol60_return_missing_or_invalid")

    availability = {stage: not reasons[stage] for stage in ("anchor", "identity", "ptb", "twap", "vol60")}
    keep = lambda obj, names: {name: obj.get(name) for name in names}
    return {
        "condition_id": condition if isinstance(condition, str) and CONDITION.fullmatch(condition) else None,
        "feature_availability": availability,
        "candidate_field_coverage": all(availability.values()),
        "failure_reasons": sorted({reason for group in reasons.values() for reason in group}),
        "source_evidence": "legacy_published_snapshot_only" if availability["twap"] else "unverified",
        "ptb_received_ms": receipt if _integer(receipt) else None,
        "twap_received_ms": twap.get("received_ms") if _integer(twap.get("received_ms")) else None,
        "twap_event_ms": twap.get("event_ms") if _integer(twap.get("event_ms")) else None,
        "vol60_basis": "binance_sampled_past_spot_not_oracle_sigma",
        "vol60_summary_present": bool(vol), "vol60_recomputed": False,
        "zero_volatility": vol.get("realized_vol_bps") == 0 if _number(vol.get("realized_vol_bps")) else None,
        "gamma_version": "unknown",
        "anchor_evidence": {
            "snapshot": keep(row, ("schema_version", "asset", "slug", "sample_ms", "window_start_ms",
                                    "sampler_lag_ms", "up_token_id", "down_token_id", "binance_source", "binance_symbol", "binance_valid")),
            "microstructure": keep(micro, ("schema_version", "asof_ms", "time_to_expiry_seconds")),
            "rules": keep(rule, ("slug", "condition_id", "metadata_received_ms", "published_price_to_beat",
                                 "price_to_beat_received_ms", "price_to_beat_path", "price_to_beat_conflict",
                                 "reference_kind", "twap_lookback_seconds", "resolution_source", "rules_sha256")),
            "twap60": keep(twap, ("price", "received_ms", "event_ms", "publisher_ms", "connection_id", "source", "window_seconds")),
            "twap_payload": keep(payload, ("symbol", "timestamp", "window_s", "full_accuracy_value", "value")),
            "spot": keep(spot, ("price", "received_ms", "event_ms")),
            "vol60": keep(vol, ("warmed", "valid_intervals", "realized_vol_bps", "return_bps")),
            "producer_flags": keep(flags, ("rules", "twap60", "matching_reference_feed", "official_threshold", "resolution_reference")),
            "source_record": row.get("__source__", {k: row[k] for k in ("source_path", "row_index") if k in row}),
            "schema_profile": {"top_keys": sorted(row), "microstructure_keys": sorted(micro),
                               "rules_keys": sorted(rule), "twap60_keys": sorted(twap), "vol60_keys": sorted(vol)},
        },
    }


def _label_audit(rows, slug, condition, end, horizon):
    outcomes = defaultdict(list)
    failures = set()
    ignored_after_cutoff = 0
    for row in rows:
        if row.get("kind") != "official_clob_winner":
            continue
        if row.get("slug") != slug and (condition is None or row.get("condition_id") != condition):
            continue
        when = row.get("available_at_ms")
        if _integer(when) and horizon is not None and when > horizon:
            ignored_after_cutoff += 1
            continue
        if condition is None:
            failures.add("label_identity_unverified")
            continue
        if row.get("slug") != slug or row.get("condition_id") != condition:
            failures.add("label_identity_conflict")
            continue
        if type(row.get("schema_version")) is not int or row["schema_version"] != 3:
            failures.add("label_schema_invalid")
            continue
        if row.get("source") != "clob/markets/condition_id":
            failures.add("label_source_invalid")
            continue
        if not _integer(when) or when < end:
            failures.add("label_availability_invalid")
            continue
        outcome = row.get("winning_outcome")
        if not isinstance(outcome, str) or outcome.lower() not in ("up", "down"):
            failures.add("label_outcome_unknown")
            continue
        outcomes[outcome.lower()].append(when)
    if len(outcomes) > 1:
        failures.add("label_outcome_conflict")
    valid = len(outcomes) == 1 and not failures
    if failures == {"label_identity_unverified"}:
        status = "identity_unverified"
    elif failures:
        status = "conflicted_or_invalid"
    elif valid:
        status = "official_label_observed"
    elif horizon is None or horizon < end:
        status = "label_horizon_not_covered"
    else:
        status = "label_not_observed_in_selected_files"
    return {"status": status, "official_label_observed": valid,
            "first_observed_ms": min(next(iter(outcomes.values()))) if valid else None,
            "outcome": next(iter(outcomes)) if valid else None,
            "failure_reasons": sorted(failures), "after_cutoff_records": ignored_after_cutoff,
            "cash_available": "unverified"}


def audit_records(snapshot_records, label_records, policy):
    """Return small coverage diagnostics from raw archive dicts or iterators.

    Optional source_path/row_index fields are ignored. Scheduled time buckets form
    the denominator, including missing/bad anchors. Labels never select features.
    """
    policy = validate_policy(policy)
    offset = int(policy["decision_seconds_before_end"] * 1000)
    chosen = {}
    duplicate_conflicts = set()
    seen_min = seen_max = None
    invalid = Counter()
    explicit_scope = "snapshot_scope_start_ms" in policy
    scope_start = policy.get("snapshot_scope_start_ms")
    scope_end = policy.get("snapshot_scope_end_ms")
    for row in snapshot_records:
        if not isinstance(row, dict):
            invalid["snapshot_not_object"] += 1
            continue
        if row.get("asset") != "btc":
            invalid["snapshot_other_or_missing_asset"] += 1
            continue
        ms = row.get("sample_ms")
        if not _integer(ms):
            invalid["snapshot_clock_invalid"] += 1
            continue
        if explicit_scope and not scope_start <= ms <= scope_end:
            invalid["snapshot_outside_explicit_scope"] += 1
            continue
        seen_min = ms if seen_min is None else min(seen_min, ms)
        seen_max = ms if seen_max is None else max(seen_max, ms)
        start = ms // WINDOW_MS * WINDOW_MS
        if ms > start + WINDOW_MS - offset:
            continue
        previous = chosen.get(start)
        if previous is None or ms > previous["sample_ms"]:
            chosen[start] = row
            duplicate_conflicts.discard(start)
        elif ms == previous["sample_ms"]:
            clean = lambda obj: {k: v for k, v in obj.items() if k not in ("source_path", "row_index", "__source__")}
            if clean(row) != clean(previous):
                duplicate_conflicts.add(start)
    if not explicit_scope:
        scope_start, scope_end = seen_min, seen_max
    labels = []
    label_clock_max = None
    unparsed_ws = 0
    for row in label_records:
        if not isinstance(row, dict):
            invalid["label_not_object"] += 1
            continue
        if row.get("kind") == "market_resolved":
            unparsed_ws += 1
        when = row.get("available_at_ms", row.get("label_available_at_ms"))
        if _integer(when):
            label_clock_max = when if label_clock_max is None else max(label_clock_max, when)
        if row.get("kind") == "official_clob_winner":
            labels.append(row)
    observed_clocks = [x for x in (seen_max, label_clock_max) if x is not None]
    horizon = policy.get("label_observed_through_ms", max(observed_clocks) if observed_clocks else None)
    per_market = []
    if scope_start is not None:
        first, last = scope_start // WINDOW_MS * WINDOW_MS, scope_end // WINDOW_MS * WINDOW_MS
        if (last - first) // WINDOW_MS > 10000:
            raise ValueError("Audit source scope exceeds bounded market count")
        for start in range(first, last + 1, WINDOW_MS):
            end, cutoff = start + WINDOW_MS, start + WINDOW_MS - offset
            slug = f"btc-updown-5m-{start // 1000}"
            row = chosen.get(start)
            result = {"slug": slug, "window_start_ms": start, "window_end_ms": end,
                      "decision_cutoff_ms": cutoff,
                      "source_window_partial": scope_start > start or scope_end < end,
                      "anchor_in_source_scope": scope_start <= cutoff <= scope_end,
                      "selected_sample_ms": row["sample_ms"] if row else None,
                      "condition_id": None,
                      "feature_availability": {k: False for k in ("anchor", "identity", "ptb", "twap", "vol60")},
                      "candidate_field_coverage": False, "failure_reasons": [],
                      "accumulated_same_oracle_integral_unverified": True}
            if row is None:
                result["failure_reasons"].append("anchor_missing")
            else:
                result.update(_features(row, start, cutoff, policy))
                result["anchor_age_ms"] = cutoff - row["sample_ms"]
            if not result["anchor_in_source_scope"]:
                result["failure_reasons"].append("anchor_outside_sample_scope")
                result["feature_availability"]["anchor"] = False
                result["candidate_field_coverage"] = False
            if start in duplicate_conflicts:
                result["failure_reasons"].append("anchor_duplicate_conflict")
                result["feature_availability"]["anchor"] = False
                result["candidate_field_coverage"] = False
            result["failure_reasons"] = sorted(set(result["failure_reasons"]))
            result["label"] = _label_audit(labels, slug, result["condition_id"], end, horizon)
            per_market.append(result)
    summary = {"n_expected_market_decisions": len(per_market),
               "n_with_anchor": sum(r["selected_sample_ms"] is not None for r in per_market),
               "n_candidate_field_coverage": sum(r["candidate_field_coverage"] for r in per_market),
               "n_official_label": sum(r["label"]["official_label_observed"] for r in per_market),
               "n_joint_with_label": sum(r["candidate_field_coverage"] and r["label"]["official_label_observed"] for r in per_market),
               "n_partial_source_windows": sum(r["source_window_partial"] for r in per_market),
               "unparsed_ws_resolution_events": unparsed_ws,
               "input_diagnostics": dict(invalid),
               "failure_counts": dict(Counter(x for r in per_market for x in r["failure_reasons"])),
               "label_status_counts": dict(Counter(r["label"]["status"] for r in per_market))}
    for name in ("ptb", "twap", "vol60"):
        summary["n_" + name] = sum(r["feature_availability"][name] for r in per_market)
    return {"schema_version": 1, "audit_kind": "candidate_field_coverage",
            "policy": policy, "snapshot_scope_start_ms": scope_start, "snapshot_scope_end_ms": scope_end,
            "snapshot_scope_basis": "supplied_bounds" if explicit_scope else "observed_rows_not_manifest_coverage",
            "label_observed_through_ms": horizon, "per_market": per_market, "summary": summary,
            "limitations": {"strategy_ready": False, "actual_fills_verified": False,
                "stream_authentication_verified": False, "stream_sequence_completeness_verified": False,
                "accumulated_same_oracle_integral_unverified": True, "cash_availability_verified": False,
                "legacy_current_twap_is_not_final_window_known_integral": True,
                "official_label_archive_suffixes_are_not_snapshot_join_keys": True}}
