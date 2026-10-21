"""CLI wrapper for the H498 frozen candidate manifest audit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .frozen_canary_candidate_manifest import audit
except ImportError:  # pragma: no cover - direct script execution
    from frozen_canary_candidate_manifest import audit


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit()
    if not report["all_checks_passed"]:
        raise SystemExit("h498_manifest_audit_failed")
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"format": report["format"], "all_checks_passed": True, "gate_passed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
