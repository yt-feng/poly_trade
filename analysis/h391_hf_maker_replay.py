"""Adapt a fixed public BTC5m OrderFilled tape into the pinned maker replay.

H387's upstream ``PublicClient`` path is unavailable from the cloud runner
(HTTP 451).  This adapter uses the already audited fixed HF OrderFilled
windows plus resolved Gamma labels, converts exchange-leg fills into the
source bot's print shape, and then runs the source bot's own offline engine.
It creates paper evidence only; no wallet identity, order, settlement or
cash receipt is admitted.
"""

from __future__ import annotations

import argparse
import gzip
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any


EXCHANGE_TAKERS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
}
USDC_SCALE = Decimal("1000000")


def _json_string(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def load_label(path: Path, start: int) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw or not isinstance(raw[0], dict):
        raise ValueError("gamma_event_shape")
    event = raw[0]
    markets = event.get("markets")
    if not isinstance(markets, list) or len(markets) != 1 or not isinstance(markets[0], dict):
        raise ValueError("gamma_market_shape")
    market = markets[0]
    slug = str(market.get("slug") or event.get("slug") or "")
    expected = f"btc-updown-5m-{start}"
    if slug != expected:
        raise ValueError("slug_mismatch")
    outcomes = _json_string(market.get("outcomes"))
    prices = _json_string(market.get("outcomePrices"))
    tokens = _json_string(market.get("clobTokenIds"))
    if not all(isinstance(x, list) and len(x) == 2 for x in (outcomes, prices, tokens)):
        raise ValueError("binary_label_shape")
    if set(map(str, outcomes)) != {"Up", "Down"}:
        raise ValueError("winner_not_binary")
    winners = [str(outcomes[i]) for i, price in enumerate(prices) if float(price) >= 0.999]
    if len(winners) != 1:
        raise ValueError("winner_not_binary")
    return {
        "slug": slug,
        "start": start,
        "winner": winners[0],
        "token_by_outcome": {str(outcomes[i]): str(tokens[i]) for i in range(2)},
    }


def _fill_price_and_side(row: dict[str, Any]) -> tuple[str, Decimal, Decimal, str]:
    maker_asset = str(row["makerAssetId"])
    taker_asset = str(row["takerAssetId"])
    maker_amount = Decimal(str(row["makerAmountFilled"])) / USDC_SCALE
    taker_amount = Decimal(str(row["takerAmountFilled"])) / USDC_SCALE
    if maker_asset == "0" and taker_asset != "0":
        if taker_amount <= 0:
            raise ValueError("zero_shares")
        return taker_asset, maker_amount / taker_amount, taker_amount, "BUY"
    if taker_asset == "0" and maker_asset != "0":
        if maker_amount <= 0:
            raise ValueError("zero_shares")
        return maker_asset, taker_amount / maker_amount, maker_amount, "SELL"
    raise ValueError("non_binary_asset_pair")


def load_prints(path: Path, label: dict[str, Any], print_cls: Any) -> list[Any]:
    token_to_outcome = {str(v): str(k) for k, v in label["token_by_outcome"].items()}
    result: list[Any] = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if str(row.get("taker", "")).lower() not in EXCHANGE_TAKERS:
                continue
            token, price, shares, side = _fill_price_and_side(row)
            outcome = token_to_outcome.get(token)
            if outcome not in {"Up", "Down"} or not (Decimal("0") < price < Decimal("1")) or shares <= 0:
                continue
            result.append(print_cls(int(str(row["timestamp"])), outcome, side, price, shares))
    return sorted(result, key=lambda row: (row.ts, row.outcome, row.side))


def build_backtest_payload(
    *, source_root: Path, fills_dir: Path, labels_dir: Path, queue_values: list[Decimal], source_commit: str, dataset_revision: str
) -> dict[str, Any]:
    sys.path.insert(0, str(source_root))
    backtest = importlib.import_module("spork30.backtest")
    config_module = importlib.import_module("spork30.config")
    windows = []
    for path in sorted(fills_dir.glob("*.fills.jsonl.gz")):
        start = int(path.name.split(".", 1)[0])
        label = load_label(labels_dir / f"{start}.json", start)
        prints = load_prints(path, label, backtest.Print)
        if prints:
            windows.append(backtest.WindowData(
                start=start,
                slug=label["slug"],
                condition="",
                winner=label["winner"],
                prints=prints,
            ))
    windows.sort(key=lambda row: row.start)
    if not windows:
        raise ValueError("no_usable_windows")
    strategy = config_module.StrategyConfig(
        split_size=Decimal("5"),
        max_lean_shares=Decimal("3"),
        fair_edge=Decimal("0"),
        maker_rebate_bps=Decimal("0"),
        ask_offset_ticks=0,
        requote_threshold_ticks=0,
        requote_min_seconds=0,
        improve_spread_ticks=0,
    )
    summaries = backtest.summarize(
        windows,
        queue_values,
        maker_fee_bps=Decimal("0"),
        seed=30,
        variants=[strategy],
        realised_vol=False,
    )
    per_window: dict[str, list[dict[str, Any]]] = {}
    summary_rows = []
    for summary in summaries:
        key = f"{summary.label},queue={summary.queue}"
        rows = [
            {"start": result.start, "winner": result.winner, "pnl": str(result.pnl), "fills": result.fills,
             "sold_up": str(result.sold_up), "sold_down": str(result.sold_down), "places": result.places}
            for result in summary.results
        ]
        per_window[key] = rows
        summary_rows.append(summary.as_dict())
    return {
        "format": "h391-hf-orderfilled-maker-replay-input-v1",
        "source": {
            "dataset_revision": dataset_revision,
            "source_commit": source_commit,
            "windows": len(windows),
            "prints": sum(len(window.prints) for window in windows),
            "labels": "Gamma resolved outcomePrices; paper label only",
        },
        "protocol": {
            "initial_cash_usd": "10",
            "split_size_usd": "5",
            "queues": [str(value) for value in queue_values],
            "max_lean_shares": "3",
            "maker_fee_bps": "0",
            "maker_rebate_bps": "0",
            "same_evolving_cash_ledger": True,
            "no_reset": True,
            "no_concatenation": True,
            "paper_only": True,
        },
        "windows": [window.start for window in windows],
        "summaries": summary_rows,
        "per_window": per_window,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--fills-dir", type=Path, required=True)
    parser.add_argument("--labels-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--dataset-revision", required=True)
    parser.add_argument("--queue", default="0,5,10")
    args = parser.parse_args()
    queues = [Decimal(item.strip()) for item in args.queue.split(",") if item.strip()]
    payload = build_backtest_payload(
        source_root=args.source_root,
        fills_dir=args.fills_dir,
        labels_dir=args.labels_dir,
        queue_values=queues,
        source_commit=args.source_commit,
        dataset_revision=args.dataset_revision,
    )
    args.output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": payload["format"], "windows": len(payload["windows"]), "summaries": len(payload["summaries"])}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
