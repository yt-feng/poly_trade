from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import statistics
import sys
import types
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
BOT_ROOT = ROOT / "poly-bot-btc-5" if (ROOT / "poly-bot-btc-5").exists() else ROOT
DEFAULT_TRADE_ROOT = ROOT if (ROOT / "reports").exists() else ROOT.parent
DEFAULT_POLY_ROOT = Path("/Users/ytfeng/Code_Pj/poly")
DEFAULT_BACKUP_ROOT = BOT_ROOT / "research" / "aws_paper_backups"


def number(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def price_band(price: float) -> str:
    if price < 0.20:
        return "p00_20"
    if price <= 0.45:
        return "p20_45"
    if price <= 0.60:
        return "p45_60"
    if price <= 0.80:
        return "p60_80"
    return "p80_100"


def summarize_pnl(rows: Iterable[dict[str, Any]], pnl_key: str = "pnl") -> dict[str, Any]:
    data = list(rows)
    pnl = [number(row.get(pnl_key)) for row in data]
    wins = [value for value in pnl if value > 0]
    losses = [value for value in pnl if value < 0]
    return {
        "trades": len(pnl),
        "total_pnl": sum(pnl),
        "avg_pnl": statistics.fmean(pnl) if pnl else 0.0,
        "median_pnl": statistics.median(pnl) if pnl else 0.0,
        "win_rate": len(wins) / len(pnl) if pnl else 0.0,
        "profit_factor": (
            sum(wins) / abs(sum(losses))
            if losses
            else 999.0
            if wins
            else 0.0
        ),
    }


def normalize_metric_row(
    source: str,
    source_path: Path,
    row: dict[str, str],
    strategy_key: str = "strategy",
) -> dict[str, Any]:
    strategy = row.get(strategy_key, row.get("candidate", row.get("system", "")))
    return {
        "source": source,
        "source_path": str(source_path),
        "strategy": strategy,
        "family": row.get("family", ""),
        "split": row.get("split", ""),
        "trades": int(number(row.get("trades"), 0)),
        "total_return": number(
            row.get("total_return", row.get("avg_test_return", row.get("total_pnl")))
        ),
        "median_test_return": number(row.get("median_test_return")),
        "min_test_return": number(row.get("min_test_return")),
        "pct_positive_test": number(row.get("pct_positive_test")),
        "win_rate": number(row.get("win_rate", row.get("avg_test_win_rate"))),
        "profit_factor": number(row.get("profit_factor", row.get("avg_profit_factor"))),
        "max_drawdown": number(row.get("max_drawdown", row.get("max_test_drawdown"))),
        "robust_score": number(
            row.get(
                "robust_wf_score",
                row.get("execution_wf_score", row.get("research_score")),
            )
        ),
        "selected_count": int(number(row.get("selected_count"), 0)),
        "notes": "",
    }


def collect_long_history_evidence(
    trade_root: Path, poly_root: Path
) -> list[dict[str, Any]]:
    sources = [
        (
            "poly_walk_forward_summary",
            poly_root
            / "reports"
            / "monthly_strategy_walk_forward_latest_complete"
            / "walk_forward_strategy_summary.csv",
            "strategy",
        ),
        (
            "poly_trade_walk_forward_summary",
            trade_root
            / "reports"
            / "monthly_runs_walk_forward_validation"
            / "walk_forward_strategy_summary.csv",
            "strategy",
        ),
        (
            "monthly_full_refresh_summary",
            trade_root
            / "reports"
            / "monthly_runs_full_refresh"
            / "monthly_runs_full_refresh_summary.csv",
            "strategy",
        ),
        (
            "all_monthly_clob_systematic",
            trade_root
            / "reports"
            / "all_monthly_runs_clob_systematic"
            / "all_monthly_clob_strategy_summary.csv",
            "strategy",
        ),
        (
            "all_monthly_clob_robust",
            trade_root
            / "reports"
            / "all_monthly_runs_clob_robust_optimization"
            / "all_monthly_clob_robust_summary.csv",
            "strategy",
        ),
        (
            "quantml_execution_walk_forward",
            trade_root
            / "reports"
            / "quantml_candidate_execution_walk_forward"
            / "candidate_execution_summary.csv",
            "candidate",
        ),
        (
            "quantml_latest_factor_sweep",
            trade_root
            / "reports"
            / "quantml_latest_factor_sweep"
            / "factor_diagnostics.csv",
            "factor",
        ),
        (
            "systematic_intraday_tool",
            trade_root
            / "reports"
            / "systematic_intraday_tool"
            / "strategy_results.csv",
            "strategy",
        ),
        (
            "robust_trading_system_selected",
            trade_root
            / "reports"
            / "robust_trading_system"
            / "selected_system_split_metrics.csv",
            "system",
        ),
        (
            "robust_trading_system_stress",
            trade_root
            / "reports"
            / "robust_trading_system"
            / "stress_tests.csv",
            "scenario",
        ),
    ]

    keep_terms = (
        "classic_early_drop",
        "classic_milddrop",
        "milddrop",
        "breakout",
        "btc2_path_eff",
        "q_edge",
        "v1_active",
        "v1_conservative",
        "portfolio_micro",
        "micro_book",
        "book_consensus",
        "mean_reversion",
        "momentum",
        "session_",
        "base",
        "fee_plus",
        "delay",
        "skill_mean_reversion",
    )
    rows: list[dict[str, Any]] = []
    for source, path, key in sources:
        for row in read_csv_rows(path):
            strategy = row.get(key, row.get("strategy", row.get("candidate", "")))
            family = row.get("family", "")
            haystack = f"{strategy} {family}".lower()
            if not any(term in haystack for term in keep_terms):
                continue
            normalized = normalize_metric_row(source, path, row, key)
            if source == "quantml_latest_factor_sweep":
                normalized.update(
                    {
                        "trades": int(number(row.get("full_n"), 0)),
                        "total_return": number(row.get("full_ic")),
                        "pct_positive_test": number(row.get("test_auc")),
                        "win_rate": number(row.get("latest72h_auc")),
                        "notes": "factor IC/AUC row: total_return=full_ic, pct_positive_test=test_auc, win_rate=latest72h_auc",
                    }
                )
            rows.append(normalized)
    return rows


def collect_historical_proxy_buckets(trade_root: Path) -> list[dict[str, Any]]:
    path = (
        trade_root
        / "reports"
        / "all_monthly_runs_5m_research"
        / "all_history_trade_logs.csv"
    )
    rows = read_csv_rows(path)
    converted: list[dict[str, Any]] = []
    for row in rows:
        entry_price = number(row.get("entry_price"))
        converted.append(
            {
                "source": "all_history_trade_logs",
                "strategy": row.get("strategy", ""),
                "run_name": row.get("run_name", ""),
                "regime": row.get("regime", ""),
                "entry_minute": row.get("entry_minute", ""),
                "side": row.get("side", ""),
                "price_band": price_band(entry_price),
                "entry_price": entry_price,
                "pnl": number(row.get("pnl_usd")),
            }
        )

    specs = [
        ("price_band", ["price_band"]),
        ("side_price_band", ["side", "price_band"]),
        ("regime_price_band", ["regime", "price_band"]),
        ("strategy_price_band", ["strategy", "price_band"]),
        ("regime_minute_price_band", ["regime", "entry_minute", "price_band"]),
    ]
    output: list[dict[str, Any]] = []
    for bucket_type, keys in specs:
        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in converted:
            grouped[tuple(str(row.get(key, "")) for key in keys)].append(row)
        for key, grouped_rows in grouped.items():
            if len(grouped_rows) < 10:
                continue
            stats = summarize_pnl(grouped_rows)
            output.append(
                {
                    "bucket_type": bucket_type,
                    "bucket": " | ".join(f"{name}={value}" for name, value in zip(keys, key)),
                    **{k: round(v, 8) if isinstance(v, float) else v for k, v in stats.items()},
                    "avg_entry_price": round(
                        statistics.fmean(row["entry_price"] for row in grouped_rows), 8
                    ),
                    "unique_runs": len({row["run_name"] for row in grouped_rows}),
                }
            )
    output.sort(
        key=lambda row: (
            row["bucket_type"],
            row["trades"] >= 30,
            row["avg_pnl"],
            row["total_pnl"],
        ),
        reverse=True,
    )
    return output


def latest_backup(root: Path) -> Path | None:
    if not root.exists():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "paper_strategy_tournament_trades.jsonl").exists()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_v19_strategies() -> list[dict[str, Any]]:
    if "live_broker" not in sys.modules:
        broker_stub = types.ModuleType("live_broker")

        class _BrokerError(RuntimeError):
            pass

        class _LiveBroker:
            pass

        broker_stub.DustPositionError = _BrokerError
        broker_stub.LiveNetworkError = _BrokerError
        broker_stub.NoLiquidityError = _BrokerError
        broker_stub.UnhedgedPairError = _BrokerError
        broker_stub.LiveBroker = _LiveBroker
        broker_stub.API_CREDENTIAL_ACCOUNT = "test-api-credential"
        broker_stub.API_KEY_SERVICE = "test-api-key"
        broker_stub.API_PASSPHRASE_SERVICE = "test-api-passphrase"
        broker_stub.API_SECRET_SERVICE = "test-api-secret"
        broker_stub.KEYRING_ACCOUNT = "test-wallet"
        broker_stub.KEYRING_SERVICE = "test-keyring"
        sys.modules["live_broker"] = broker_stub
    sys.path.insert(0, str(BOT_ROOT))
    engine = importlib.import_module("engine")
    item = engine.AdaptiveEngine.__new__(engine.AdaptiveEngine)
    strategies = item._default_paper_tournament_strategies()
    return [row for row in strategies if row.get("strategy_version") == "1.9.0"]


def forward_prior_summary(data_dir: Path | None) -> list[dict[str, Any]]:
    if data_dir is None:
        return []
    try:
        from research_factorial_paper_tournament import (
            assign_time_splits,
            build_closed_trades,
            deduplicate_market_events,
            factorial_cell_rows,
            factorial_synergy_rows,
            mechanism_rows,
        )
    except Exception:
        return []

    events = read_jsonl(data_dir / "paper_strategy_tournament_trades.jsonl")
    trades = build_closed_trades(events)
    market_events = deduplicate_market_events(trades)
    split_map = assign_time_splits(market_events)
    cells = factorial_cell_rows(trades, split_map)
    synergies = factorial_synergy_rows(trades, cells, split_map)
    mechanisms = mechanism_rows(trades, split_map)

    rows: list[dict[str, Any]] = []
    for row in cells:
        if row.get("split") != "all":
            continue
        rows.append(
            {
                "source": "prior_forward_factorial_cells",
                "data_dir": str(data_dir),
                "key": f"{row.get('experiment_family')}::{row.get('experiment_cell')}",
                "events": row.get("n_events", 0),
                "pnl": row.get("total_pnl", 0.0),
                "avg_roi": row.get("avg_roi", 0.0),
                "win_rate": row.get("win_rate", 0.0),
                "interpretation": row.get("falsifier", ""),
            }
        )
    for row in synergies:
        if row.get("split") != "all":
            continue
        rows.append(
            {
                "source": "prior_forward_declared_synergy",
                "data_dir": str(data_dir),
                "key": row.get("experiment_family", ""),
                "events": row.get("paired_event_count", 0),
                "pnl": "",
                "avg_roi": row.get("paired_synergy_roi", 0.0),
                "win_rate": "",
                "interpretation": row.get("interpretation", ""),
            }
        )
    for row in mechanisms:
        if row.get("split") != "all":
            continue
        rows.append(
            {
                "source": "prior_forward_mechanisms",
                "data_dir": str(data_dir),
                "key": f"{row.get('strategy_version')}::{row.get('market_mechanism')}",
                "events": row.get("n_events", 0),
                "pnl": row.get("total_pnl", 0.0),
                "avg_roi": row.get("avg_roi", 0.0),
                "win_rate": row.get("win_rate", 0.0),
                "interpretation": row.get("falsifier", ""),
            }
        )
    rows.sort(key=lambda row: (number(row.get("events")), number(row.get("avg_roi"))), reverse=True)
    return rows


def long_history_status(name: str) -> tuple[str, str]:
    if "qedge" in name or "q_edge" in name:
        return (
            "strong_long_history_model_support",
            "Robust historical q-edge/value system is strong across train/validation/test and stress rows; v1.9 restricts it to low price.",
        )
    if "london_milddrop" in name:
        return (
            "session_proxy_supported",
            "Local CLOB history supports London milddrop/down continuation pockets better than broad all-session rules.",
        )
    if "btc2_path_eff" in name:
        return (
            "fragile_long_history_support",
            "QuantML execution WF has small positive average but weak positive-fold rate; keep as low-price challenger only.",
        )
    if "classic_early_drop" in name:
        return (
            "mixed_long_history_support",
            "Classic early drop is repeatedly selected in walk-forward, but test tails drift; use price/session/PM filters.",
        )
    if "early_gap" in name:
        return (
            "proxy_supported_down_bias",
            "Long-history early-drop/milddrop evidence supports DOWN continuation more than broad all-direction gap.",
        )
    if "up_low" in name:
        return (
            "no_exact_long_history_futures_micro",
            "Old archive lacks Binance futures vote fields; support is indirect from BTC move factors and prior forward votes8 pocket.",
        )
    if "absorb" in name or "xvenue" in name:
        return (
            "forward_only_pm_micro",
            "Old monthly archive does not include persisted PM book delta/trade-print microstructure.",
        )
    return ("needs_mapping", "No exact long-history proxy mapped yet.")


def decision_for_strategy(name: str) -> str:
    if "askwithdrawal_falsifier" in name:
        return "do_not_promote"
    if "qedge" in name or "london_milddrop" in name:
        return "paper_keep_as_historical_anchor"
    if "absorb" in name or "xvenue" in name:
        return "paper_only_collect_more_pm_micro"
    if "btc2_path_eff" in name:
        return "paper_low_weight_challenger"
    if "classic_early_drop" in name:
        return "paper_keep_with_low_price_filters"
    if "early_gap" in name:
        return "paper_keep_but_prefer_down_and_no_us_open"
    if "up_low" in name:
        return "paper_keep_as_convex_forward_test"
    return "paper_probe"


def build_v19_account_evidence(
    strategies: list[dict[str, Any]],
    long_rows: list[dict[str, Any]],
    forward_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    forward_text = "\n".join(
        f"{row.get('key')} {row.get('events')} {row.get('avg_roi')} {row.get('interpretation')}"
        for row in forward_rows
    ).lower()
    rows: list[dict[str, Any]] = []
    for strategy in strategies:
        name = str(strategy.get("name", ""))
        status, reason = long_history_status(name)
        live_note = "prior forward does not contain this v1.9 name yet"
        if "up_low" in name and "votes8" in forward_text:
            live_note = "prior forward showed a low-price UP + votes8 + positive flow pocket"
        elif ("absorb" in name or "xvenue" in name) and (
            "askwithdrawal" in forward_text or "absorption" in forward_text
        ):
            live_note = "prior v18 PM micro forward rejected broad ask-withdrawal-only fills; absorption sample still building"
        elif "early_gap" in name and ("gap" in forward_text or "early_gap" in forward_text):
            live_note = "prior forward supported DOWN gap filters, especially lower ask and no-US-open variants"
        elif "btc2_path_eff" in name:
            live_note = "prior QuantML execution WF supports a tiny challenger, not a main account"
        elif "classic_early_drop" in name:
            live_note = "prior long-history WF selects early drop often; latest full refresh says it needs filters"
        rows.append(
            {
                "strategy": name,
                "strategy_version": strategy.get("strategy_version", ""),
                "experiment_family": strategy.get("experiment_family", ""),
                "experiment_cell": strategy.get("experiment_cell", ""),
                "type": strategy.get("type", "candidate_gate"),
                "enabled": strategy.get("enabled", True),
                "price_band": f"{strategy.get('min_token_price', '')}-{strategy.get('max_token_price', '')}",
                "sessions": ";".join(strategy.get("allowed_sessions_et", []) or []),
                "directions": ";".join(strategy.get("allowed_directions", []) or []),
                "treatment_factors": ";".join(strategy.get("treatment_factors", []) or []),
                "long_history_status": status,
                "long_history_reason": reason,
                "forward_status": live_note,
                "decision": decision_for_strategy(name),
                "market_mechanism": strategy.get("market_mechanism", ""),
                "market_players": strategy.get("market_players", ""),
                "causal_path": strategy.get("causal_path", ""),
                "falsifier": strategy.get("falsifier", ""),
            }
        )
    return rows


def select_top_evidence(long_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = []
    for row in long_rows:
        score = (
            number(row.get("robust_score")) * 2.0
            + number(row.get("total_return"))
            + number(row.get("pct_positive_test"))
            + min(number(row.get("win_rate")), 1.0) * 0.5
            - max(number(row.get("max_drawdown")), 0.0) * 0.5
        )
        item = dict(row)
        item["evidence_score"] = round(score, 8)
        candidates.append(item)
    candidates.sort(key=lambda row: row["evidence_score"], reverse=True)
    return candidates


def fmt(value: Any, digits: int = 4) -> str:
    value = number(value)
    return f"{value:.{digits}f}"


def write_report(
    outdir: Path,
    trade_root: Path,
    poly_root: Path,
    backup_dir: Path | None,
    strategies: list[dict[str, Any]],
    top_evidence: list[dict[str, Any]],
    proxy_buckets: list[dict[str, Any]],
    forward_rows: list[dict[str, Any]],
    account_rows: list[dict[str, Any]],
) -> None:
    low_price = [
        row
        for row in proxy_buckets
        if row.get("bucket_type") in {"side_price_band", "regime_price_band"}
        and "p20_45" in str(row.get("bucket", ""))
    ][:12]
    account_decisions = defaultdict(int)
    for row in account_rows:
        account_decisions[str(row.get("decision", ""))] += 1

    lines = [
        "# v1.9 Historical Validation",
        "",
        f"Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"Trade repo: `{trade_root}`",
        f"Poly repo: `{poly_root}`",
        f"Prior forward backup: `{backup_dir}`" if backup_dir else "Prior forward backup: not found",
        "",
        "## Readout",
        "",
        f"- v1.9 accounts inspected: {len(strategies)}",
        f"- long-history evidence rows: {len(top_evidence)}",
        f"- direct historical proxy buckets: {len(proxy_buckets)}",
        f"- prior forward mechanism rows: {len(forward_rows)}",
        "",
        "Decision counts: "
        + ", ".join(f"{key}={value}" for key, value in sorted(account_decisions.items())),
        "",
        "## What This Means",
        "",
        "- PM order-book delta and active trade-print factors are not present in the April-July monthly archive, so those factors remain forward-only until the new collector spans more sessions.",
        "- The long archive does support session routing, mild/early DOWN continuation, and BTC-move/q-edge style features, but several strong-looking rules drift in later folds.",
        "- Low-price and low-mid execution should remain the default experimental surface. Historical proxy buckets below make high-price tickets unnecessary for this stage.",
        "- For v1.9, PM absorption accounts are data collectors; the historical anchors are the early-gap/classic-drop and low-price QuantML/BTC-move accounts.",
        "",
        "## Top Long-History Evidence",
        "",
        "| Source | Strategy | Split | Trades | Return/IC | Win/AUC | Drawdown | Score |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in top_evidence[:16]:
        lines.append(
            f"| {row.get('source')} | {row.get('strategy')} | {row.get('split')} | "
            f"{int(number(row.get('trades')))} | {fmt(row.get('total_return'))} | "
            f"{fmt(row.get('win_rate'))} | {fmt(row.get('max_drawdown'))} | "
            f"{fmt(row.get('evidence_score'))} |"
        )

    lines.extend(
        [
            "",
            "## Low-Price Historical Proxy Buckets",
            "",
            "| Bucket | Trades | PnL | Avg | Win | PF | Runs |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in low_price:
        lines.append(
            f"| {row.get('bucket')} | {int(number(row.get('trades')))} | "
            f"{fmt(row.get('total_pnl'))} | {fmt(row.get('avg_pnl'))} | "
            f"{fmt(row.get('win_rate'))} | {fmt(row.get('profit_factor'))} | "
            f"{int(number(row.get('unique_runs')))} |"
        )

    lines.extend(
        [
            "",
            "## Prior Forward Mechanism Rows",
            "",
            "| Source | Key | Events | PnL | Avg ROI | Win | Interpretation |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in forward_rows[:18]:
        lines.append(
            f"| {row.get('source')} | {row.get('key')} | {int(number(row.get('events')))} | "
            f"{row.get('pnl')} | {fmt(row.get('avg_roi'))} | {row.get('win_rate')} | "
            f"{row.get('interpretation')} |"
        )

    lines.extend(
        [
            "",
            "## v1.9 Account Disposition",
            "",
            "| Account | Decision | Long History | Forward Status |",
            "| --- | --- | --- | --- |",
        ]
    )
    for row in account_rows:
        lines.append(
            f"| {row.get('strategy')} | {row.get('decision')} | "
            f"{row.get('long_history_status')} | {row.get('forward_status')} |"
        )

    lines.extend(
        [
            "",
            "## Implementation Notes",
            "",
            "- Treat old monthly history and new PM microstructure as two different evidence layers.",
            "- Promote combinations only when a factor improves a base rule, not when it merely adds another correlated book signal.",
            "- Market-player hypothesis for the next pass: slow PM repricers create early DOWN/low-price BTC-move pockets; market makers absorbing flow need fresh PM book/trade-print evidence; futures traders only become useful when their move has not already been fully priced in by PM.",
        ]
    )
    (outdir / "v19_historical_validation_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-root", type=Path, default=DEFAULT_TRADE_ROOT)
    parser.add_argument("--poly-root", type=Path, default=DEFAULT_POLY_ROOT)
    parser.add_argument("--backup-root", type=Path, default=DEFAULT_BACKUP_ROOT)
    parser.add_argument("--paper-data-dir", type=Path, default=None)
    parser.add_argument(
        "--outdir",
        type=Path,
        default=ROOT / "research" / "v19_historical_validation" / "latest",
    )
    args = parser.parse_args()

    backup_dir = args.paper_data_dir or latest_backup(args.backup_root)
    strategies = load_v19_strategies()
    long_rows = collect_long_history_evidence(args.trade_root, args.poly_root)
    top_evidence = select_top_evidence(long_rows)
    proxy_buckets = collect_historical_proxy_buckets(args.trade_root)
    forward_rows = forward_prior_summary(backup_dir)
    account_rows = build_v19_account_evidence(
        strategies, top_evidence, forward_rows
    )

    args.outdir.mkdir(parents=True, exist_ok=True)
    write_csv(args.outdir / "long_history_evidence.csv", top_evidence)
    write_csv(args.outdir / "historical_proxy_buckets.csv", proxy_buckets)
    write_csv(args.outdir / "prior_forward_mechanism_rows.csv", forward_rows)
    write_csv(args.outdir / "v19_account_evidence.csv", account_rows)
    write_report(
        args.outdir,
        args.trade_root,
        args.poly_root,
        backup_dir,
        strategies,
        top_evidence,
        proxy_buckets,
        forward_rows,
        account_rows,
    )
    manifest = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "outdir": str(args.outdir),
        "trade_root": str(args.trade_root),
        "poly_root": str(args.poly_root),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "v19_accounts": len(strategies),
        "long_history_rows": len(top_evidence),
        "historical_proxy_buckets": len(proxy_buckets),
        "prior_forward_rows": len(forward_rows),
    }
    (args.outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
