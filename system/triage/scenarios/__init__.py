"""Institution-specific scenario logic.

Each module exposes `decide(ctx, alerts) -> dict | None`. Return None to fall
back to the generic per-rule handlers in engine.py. A returned dict needs:
verdict, reason_code, reason (2-4 sentences, names this case's facts and doc
IDs; for RFI, exactly what to ask for) and evidence (list of IDs).
"""
from __future__ import annotations

import importlib
import os


def scenario_decide(ctx, alerts):
    if os.environ.get("TRIAGE_GENERIC_ONLY"):  # used by tests to exercise the generic engine
        return None
    try:
        mod = importlib.import_module(f"{__name__}.{ctx.inst}")
    except ModuleNotFoundError:
        return None
    return mod.decide(ctx, alerts)
