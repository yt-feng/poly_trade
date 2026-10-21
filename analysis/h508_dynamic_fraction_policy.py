"""Deterministic paper policy derived from the H508 old-clean stress grid.

This module is deliberately side-effect free.  It does not read a market,
access an account, submit an order, or infer a fill.  It only converts an
observed effective-depth and latency cell into the conservative gross cash
fraction that H508 selected on its selection windows.
"""
from __future__ import annotations

SUPPORTED_MAX_LATENCY_SECONDS = 8.0
SUPPORTED_MAX_SLIPPAGE = 0.01
MIN_EFFECTIVE_DEPTH_HAIRCUT = 0.10


def select_gross_cash_fraction(
    effective_depth_haircut: float,
    latency_seconds: float,
    slippage: float,
) -> float | None:
    """Return a conservative H508 fraction, or ``None`` outside its grid.

    The mapping uses the worst tested slippage and a conservative interpolation
    between the tested latency cells.  ``None`` is a hard paper-entry veto: a
    live caller must not silently extrapolate past the tested envelope.
    """
    depth = float(effective_depth_haircut)
    latency = float(latency_seconds)
    slip = float(slippage)
    if not 0.0 < depth <= 1.0:
        raise ValueError("effective_depth_haircut_invalid")
    if latency < 0.0:
        raise ValueError("latency_seconds_invalid")
    if slip < 0.0:
        raise ValueError("slippage_invalid")
    if depth < MIN_EFFECTIVE_DEPTH_HAIRCUT:
        return None
    if latency > SUPPORTED_MAX_LATENCY_SECONDS or slip > SUPPORTED_MAX_SLIPPAGE:
        return None

    if depth < 0.15:
        return 0.50
    if depth < 0.20:
        return 0.75
    if depth < 0.25:
        if latency <= 2.0:
            return 1.00
        if latency <= 4.0:
            return 0.95
        return 0.90
    return 1.00
