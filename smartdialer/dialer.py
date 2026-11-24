"""Dialer worker: one tick = ingest events, reconcile, sweep, plan, dispatch.

Any number of workers can run this against the same SQLite file. Correctness
does not depend on which worker does what: every write is a CAS inside a
serialised transaction, and every provider event is deduplicated by id.
"""
from __future__ import annotations

from collections import Counter

from .allocator import CallAllocator
from .clock import SystemClock
from .model import ACTIVE_CALL_STATES, CallState, Config
from .pacing import PacingEngine
from .providers import Provider, ProviderEvent, ProviderTimeout
from .safety import SafetyController
from .store import Store


class Dialer:
    def __init__(self, db_path: str, campaign: str, providers: list[Provider], mode: str = "predictive",
                 cfg: Config | None = None, clock=None):
        assert mode in ("progressive", "predictive")
        self.campaign = campaign
        self.mode = mode
        self.cfg = cfg or Config()
        self.clock = clock or SystemClock()
        self.providers = {p.name: p for p in providers}
        self.store = Store(db_path, self.cfg, self.clock)
        self.controller = SafetyController(self.store, self.cfg, self.clock)
        self.allocator = CallAllocator(self.store, self.cfg, self.clock, providers, self.controller)
        self.pacing = PacingEngine(self.cfg)
        self.last_decision = None

    # ------------------------------------------------------------ one tick
    def tick(self) -> dict:
        now = self.clock.now()
        report: Counter = Counter()
        self._ingest(now, report)
        self._reconcile(now, report)
        self._sweep(now, report)
        self._plan(now, report)
        return dict(report)

    def _ingest(self, now: float, report: Counter) -> None:
        for p in self.providers.values():
            try:
                events = p.poll_events()
            except ProviderTimeout:
                continue
            for ev in events:
                report[f"event_{self.store.apply_event(ev, now)}"] += 1

    def _reconcile(self, now: float, report: Counter) -> None:
        """Calls whose provider outcome we have not heard about are queried, not assumed."""
        with self.store.tx() as c:
            rows = self.store.stale_calls(c, [CallState.INITIATED], now - self.cfg.originate_timeout)
            rows += self.store.stale_calls(
                c, [CallState.RINGING, CallState.ANSWERED, CallState.CONNECTED],
                now - self.cfg.stale_after)
        for r in rows:
            provider = self.providers.get(r["provider"])
            if provider is None:
                continue
            try:
                state = provider.query(r["id"])
            except ProviderTimeout:
                report["reconcile_deferred"] += 1
                continue
            if state is None:
                if r["state"] == CallState.INITIATED.value:
                    ev = ProviderEvent(f"reconcile:{r['id']}:NOT_FOUND", r["id"], "FAILED")
                    self.store.apply_event(ev, now, reason="NOT_FOUND_AT_PROVIDER")
                else:
                    ev = ProviderEvent(f"reconcile:{r['id']}:LOST", r["id"], "COMPLETED")
                    self.store.apply_event(ev, now, reason="LOST_AT_PROVIDER")
            else:
                self.store.apply_event(ProviderEvent(f"reconcile:{r['id']}:{state}", r["id"], state),
                                       now, reason="RECONCILED")
            report["reconciled"] += 1

    def _sweep(self, now: float, report: Counter) -> None:
        self.store.sweep(now)
        for call_id, provider_name in self.store.expire_vanished_agents(now):
            report["agent_lost_hangup"] += 1
            provider = self.providers.get(provider_name)
            if provider is not None:
                try:
                    provider.hangup(call_id)
                except ProviderTimeout:
                    pass  # the provider will eventually report COMPLETED; reconcile covers it

    def _plan(self, now: float, report: Counter) -> None:
        with self.store.tx() as c:
            snap = self.store.snapshot(c, self.campaign, now, self.cfg.horizon)
        if self.mode == "progressive":
            requested = min(self.cfg.max_new_per_tick, snap.available)
            info = {"agents_available": snap.available}
            if requested == 0:
                return
        else:
            requested, info = self.pacing.propose(snap)

        decision = self.controller.authorize(self.campaign, self.mode, requested,
                                             provider_ok=self.allocator.healthy())
        self.last_decision = (decision, info)
        report[f"decision_{decision.decision}"] += 1
        if decision.permit.granted:
            res = self.allocator.dispatch(decision.permit)
            report["dialed"] += res.dialed
            report["timeouts"] += res.timeouts
            report["rejected"] += res.rejected

    def close(self) -> None:
        self.store.close()
