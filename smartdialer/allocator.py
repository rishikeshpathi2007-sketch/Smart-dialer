"""Call Allocator: the only code that talks to a telecom provider.

Dispatch of a permit is two steps:
  1. (one DB transaction) mark the permit dispatched and move its calls to
     INITIATED. This is a write-ahead record: we commit our intent BEFORE the
     provider sees anything.
  2. (no transaction held) call the provider, using the call id as the
     idempotency key.

If the process dies between 1 and 2, the call sits in INITIATED with no
provider evidence. The reconciliation loop queries the provider with the same
key and either finds the call (and follows its state) or marks it FAILED so
the borrower is retried. We never blindly re-dial.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Config
from .providers import Provider, ProviderEvent, ProviderRejected, ProviderTimeout
from .safety import Permit, SafetyController
from .store import Store


class PermitError(Exception):
    pass


class Breaker:
    """Per-provider circuit breaker: opens after N consecutive errors, then cools down."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.fails = 0
        self.open_until = 0.0

    def allow(self, now: float) -> bool:
        return now >= self.open_until

    def ok(self) -> None:
        self.fails = 0

    def fail(self, now: float) -> None:
        self.fails += 1
        if self.fails >= self.cfg.breaker_threshold:
            self.open_until = now + self.cfg.breaker_cooldown
            self.fails = 0


@dataclass
class DispatchResult:
    dialed: int = 0
    timeouts: int = 0
    rejected: int = 0
    skipped: int = 0
    by_provider: dict = field(default_factory=dict)


class CallAllocator:
    def __init__(self, store: Store, cfg: Config, clock, providers: list[Provider],
                 controller: SafetyController):
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.providers = {p.name: p for p in providers}
        self.breakers = {p.name: Breaker(cfg) for p in providers}
        self.controller = controller
        self._rr = 0  # round-robin cursor across healthy providers

    def healthy(self) -> bool:
        now = self.clock.now()
        return any(b.allow(now) for b in self.breakers.values())

    def _pick(self, now: float):
        names = list(self.providers)
        for i in range(len(names)):
            name = names[(self._rr + i) % len(names)]
            if self.breakers[name].allow(now):
                self._rr = (self._rr + i + 1) % len(names)
                return self.providers[name]
        return None

    def dispatch(self, permit: Permit) -> DispatchResult:
        if not self.controller.verify(permit):
            raise PermitError("permit signature invalid: refusing to dial")
        result = DispatchResult()
        now = self.clock.now()
        provider = self._pick(now)
        if provider is None:
            return result  # calls stay reserved; lease expiry will release them

        with self.store.tx() as c:
            batch = self.store.begin_dispatch(c, permit.id, provider.name, now)
        if not batch:
            result.skipped = permit.granted  # permit already dispatched or expired
            return result

        breaker = self.breakers[provider.name]
        for call_id, number in batch:
            if not breaker.allow(self.clock.now()):
                result.skipped += 1   # provider tripped mid-batch; reconciliation handles the rest
                continue
            try:
                provider.originate(call_id, number)
                breaker.ok()
                result.dialed += 1
            except ProviderTimeout:
                breaker.fail(self.clock.now())
                result.timeouts += 1  # outcome unknown: reconciliation will query the provider
            except ProviderRejected:
                breaker.fail(self.clock.now())
                self.store.apply_event(ProviderEvent(f"reject:{call_id}", call_id, "FAILED"),
                                       self.clock.now(), reason="PROVIDER_REJECTED")
                result.rejected += 1
        result.by_provider[provider.name] = len(batch)
        return result
