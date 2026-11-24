"""Predictive pacing engine.

The pacing engine only *proposes* a number of calls. It has no handle on the
store's call-creation path and no handle on any provider. The Safety Controller
decides what is actually allowed.

The model, in one sentence:
    Dial n more calls only if the answers we expect from everything in flight
    (plus a z-sigma allowance for randomness) still fit in the agents we expect
    to have free within the horizon.

    expected answers  mu  = k * p        with k = in_flight + n
    noise             sd  = sqrt(k * p * (1 - p))
    dial n only if    mu + z * sd <= supply

`p` is the observed answer rate (posterior mean with decayed counts), so the
pacing adapts to the campaign. `supply` is agents AVAILABLE now plus agents
that will finish talk / wrap-up within the horizon.
"""
from __future__ import annotations

import math

from .model import Config
from .store import Snapshot


def max_new_calls(p: float, in_flight: int, supply: float, z: float, cap: int) -> int:
    """Largest n in [0, cap] such that (in_flight + n) calls still fit in `supply`."""
    if cap <= 0:
        return 0
    for n in range(cap, 0, -1):
        k = in_flight + n
        mu = k * p
        sd = math.sqrt(max(k * p * (1.0 - p), 0.0))
        if mu + z * sd <= supply:
            return n
    return 0


class PacingEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def propose(self, snap: Snapshot) -> tuple[int, dict]:
        cap = min(self.cfg.max_new_per_tick, snap.ready_borrowers)
        supply = snap.supply_soon
        n = max_new_calls(snap.p_hat, snap.in_flight, supply, self.cfg.z, cap)
        info = {
            "p_hat": round(snap.p_hat, 3),
            "in_flight": snap.in_flight,
            "supply": supply,
            "cap": cap,
            "expected_answers_after": round((snap.in_flight + n) * snap.p_hat, 2),
            "proposed": n,
        }
        return n, info
