"""Progressive dialer: one free agent, one call.

This module holds the progressive policy. The mechanics (atomic claims of an
agent and a borrower, and creating the RESERVED call row) live in
Store.reserve_progressive, so that the same compare-and-set rules apply.

Why progressive is safe by construction:
  * a call is created only after an AVAILABLE agent has been claimed;
  * the claim and the call row are written in one transaction;
  * so the number of agent-bound calls can never exceed the number of free agents.

Pacing is not involved here. The pacing engine's answer-rate estimate is not
needed, which is why progressive is the fallback when predictive dialing is
judged unsafe.
"""
from __future__ import annotations

import math

from .model import Config
from .store import Snapshot, Store


class ProgressiveDialer:
    def __init__(self, store: Store, cfg: Config):
        self.store = store
        self.cfg = cfg

    def request_size(self, snap: Snapshot) -> int:
        """How many calls progressive mode asks for on this tick: at most one per free agent."""
        return min(self.cfg.max_new_per_tick, snap.available)

    def spare_agents(self, snap: Snapshot) -> int:
        """Agents that a fallback may hand to progressive calls.

        Predictive calls already ringing will need an agent when they answer, so
        keep back enough agents to cover them (expected answers plus a z-sigma
        allowance) before handing the rest out.
        """
        p = snap.p_upper
        n = snap.pending_unbound
        reserve = n * p + self.cfg.z * math.sqrt(max(n * p * (1 - p), 0.0))
        spare = int(math.floor(snap.available - reserve))
        return max(0, min(self.cfg.max_new_per_tick, spare))

    def reserve(self, c, campaign: str, n: int, permit_id: str, now: float) -> int:
        """Claim up to n (agent, borrower) pairs inside the caller's transaction."""
        return self.store.reserve_progressive(c, campaign, n, permit_id, now)
