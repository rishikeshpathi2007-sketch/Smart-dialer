"""Safety Controller.

It sits between the pacing engine and the telecom provider and is the ONLY
component that can create calls:

    Campaign > Pacing Engine > Safety Controller > Call Allocator > Provider

authorize() runs inside one serialised DB transaction:
  1. re-reads the campaign state (the pacing engine's view may be stale);
  2. applies its own, more conservative ceiling (upper-bound answer rate);
  3. enforces hard limits (in-flight cap, abandon-rate kill switch, answer-rate
     shock detection) and falls back to progressive dialing when they trip;
  4. writes the call rows and returns a signed Permit.

The Call Allocator will only dispatch calls that belong to a verified,
unexpired, not-yet-dispatched Permit. The Permit is HMAC-signed with a key that
only the controller holds, so pacing code cannot forge one.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import os
import uuid
from dataclasses import dataclass
from typing import Optional

from .model import Config
from .pacing import max_new_calls
from .store import Snapshot, Store


@dataclass(frozen=True)
class Permit:
    id: str
    campaign: str
    mode: str          # the mode actually used: "progressive" or "predictive"
    granted: int
    expires_at: float
    mac: str


@dataclass(frozen=True)
class Decision:
    permit: Permit
    decision: str      # APPROVE | REDUCE | REJECT | FALLBACK | HOLD
    reason: str
    requested: int


class SafetyController:
    def __init__(self, store: Store, cfg: Config, clock, secret: Optional[bytes] = None):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self._secret = secret or os.urandom(32)

    # ---- permit signing ----------------------------------------------------
    def _mac(self, permit_id: str, campaign: str, mode: str, granted: int, expires_at: float) -> str:
        msg = f"{permit_id}|{campaign}|{mode}|{granted}|{expires_at:.6f}".encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def verify(self, p: Permit) -> bool:
        expected = self._mac(p.id, p.campaign, p.mode, p.granted, p.expires_at)
        return hmac.compare_digest(expected, p.mac)

    # ---- the decision ------------------------------------------------------
    def authorize(self, campaign: str, mode: str, requested: int, provider_ok: bool) -> Decision:
        cfg = self.cfg
        now = self.clock.now()
        permit_id = uuid.uuid4().hex
        expires = now + cfg.permit_ttl
        with self.store.tx() as c:
            self.store.ensure_campaign(c, campaign)
            snap = self.store.snapshot(c, campaign, now, cfg.horizon)
            used_mode = mode
            fallback_reason = None
            granted = 0

            if not provider_ok:
                decision, reason = "REJECT", "PROVIDER_UNAVAILABLE"

            elif mode == "progressive":
                # Progressive is agent-first, so it is safe by construction:
                # the claims below can never exceed AVAILABLE agents.
                granted = self.store.reserve_progressive(c, campaign, requested, permit_id, now)
                decision, reason = self._verdict(requested, granted, "agents and borrowers claimed")

            else:  # predictive
                if snap.answered_window >= 30 and snap.abandon_rate > cfg.max_abandon_rate:
                    fallback_reason = f"ABANDON_RATE {snap.abandon_rate:.3f} > {cfg.max_abandon_rate}"
                elif snap.recent_n >= cfg.shock_window and self._surge(snap):
                    fallback_reason = (f"ANSWER_RATE_SHOCK recent {snap.recent_rate:.2f} "
                                       f"vs estimate {snap.p_hat:.2f}")

                if fallback_reason:
                    used_mode = "progressive"
                    # Keep back agents for predictive calls already ringing: they will
                    # need an agent when they answer, and must not find none.
                    p = snap.p_upper
                    reserve = snap.pending_unbound * p + cfg.z * math.sqrt(
                        max(snap.pending_unbound * p * (1 - p), 0.0))
                    spare = int(math.floor(snap.available - reserve))
                    want = max(0, min(cfg.max_new_per_tick, spare))
                    granted = self.store.reserve_progressive(c, campaign, want, permit_id, now)
                    decision, reason = "FALLBACK", fallback_reason
                else:
                    hard_cap = int(cfg.max_in_flight_per_agent * snap.logged_in) - snap.in_flight
                    cap = max(0, min(requested, hard_cap, snap.ready_borrowers))
                    # Re-check with the CONSERVATIVE answer rate (upper bound), using
                    # the state as it is right now, not as the pacing engine saw it.
                    safe_n = max_new_calls(snap.p_upper, snap.in_flight, snap.supply_soon, cfg.z, cap)
                    granted = self.store.reserve_predictive(c, campaign, safe_n, permit_id, now)
                    if requested == 0:
                        decision, reason = "HOLD", "pacing sees no room"
                    elif granted == 0:
                        decision = "REJECT"
                        reason = ("in-flight cap reached" if cap < requested
                                  else f"upper-bound check: p_upper={snap.p_upper:.2f} leaves no room")
                    elif granted < requested:
                        decision = "REDUCE"
                        reason = (f"pacing asked {requested}; upper-bound p={snap.p_upper:.2f} "
                                  f"allows {granted}")
                    else:
                        decision, reason = "APPROVE", f"{granted} calls within safety ceiling"

            self.store.create_permit(c, permit_id, campaign, used_mode, granted, expires)
            self.store.log_decision(c, campaign, now, used_mode, requested, granted, decision, reason)
            mac = self._mac(permit_id, campaign, used_mode, granted, expires)

        permit = Permit(permit_id, campaign, used_mode, granted, expires, mac)
        return Decision(permit, decision, reason, requested)

    def _surge(self, snap: Snapshot) -> bool:
        """Is the recent answer rate significantly above the estimate? (one-sided z-test)"""
        p = max(snap.p_hat, 0.05)
        n = snap.recent_n
        k = snap.recent_rate * n
        z = (k - n * p) / math.sqrt(max(n * p * (1 - p), 1e-9))
        return z > self.cfg.shock_z

    @staticmethod
    def _verdict(requested: int, granted: int, ok_reason: str) -> tuple[str, str]:
        if granted == 0:
            return "REJECT", "no available agent or borrower"
        if granted < requested:
            return "REDUCE", f"asked {requested}, only {granted} agents free ({ok_reason})"
        return "APPROVE", f"{granted} calls ({ok_reason})"
