"""Write a bounded, encrypted-journal diagnostic for a failed H387 replay."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def build_report(
    *,
    stage: str,
    exit_code: int,
    log_path: Path | None = None,
    backtest_path: Path | None = None,
) -> dict:
    log_bytes = log_path.read_bytes() if log_path and log_path.exists() else b""
    backtest_present = bool(backtest_path and backtest_path.exists() and backtest_path.stat().st_size > 0)
    report = {
        "status": "source_replay_failed" if stage == "public_tape_replay" else "source_unavailable",
        "stage": stage,
        "exit_code": int(exit_code),
        "live_orders_enabled": False,
    }
    if stage == "public_tape_replay":
        report.update(
            {
                "backtest_json_present": backtest_present,
                "source_log_sha256": hashlib.sha256(log_bytes).hexdigest(),
                # The vault is encrypted before publication. Bound the diagnostic
                # so an upstream exception cannot make the sealed part unbounded.
                "source_log_tail": log_bytes.decode("utf-8", "replace").splitlines()[-40:],
            }
        )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=["clone", "pin", "public_tape_replay"])
    parser.add_argument("--exit-code", required=True, type=int)
    parser.add_argument("--log", type=Path)
    parser.add_argument("--backtest", type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(build_report(stage=args.stage, exit_code=args.exit_code, log_path=args.log, backtest_path=args.backtest), sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
