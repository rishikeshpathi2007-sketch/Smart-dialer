"""Telecom provider interface and two mock providers that behave differently.

The dialer only ever talks to the `Provider` interface. It never looks at a
provider's internals, so swapping in a real telecom integration means writing
one more subclass.

Failure modes the dialer must survive (all modelled here):
* ProviderTimeout  - the call may or may not have been placed (ambiguous).
* ProviderRejected - the provider definitely did not place the call.
* duplicate events, out-of-order events, events delayed during an outage.
"""
from __future__ import annotations

import heapq
import random
from dataclasses import dataclass
from typing import Callable, List, Optional

from .clock import SimClock


class ProviderTimeout(Exception):
    """Outcome unknown: the request may or may not have taken effect."""


class ProviderRejected(Exception):
    """Definitively rejected: the call was not placed."""


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str   # provider-side id; duplicates carry the same id
    call_id: str    # our call id, used as the provider's idempotency key
    state: str      # RINGING | ANSWERED | COMPLETED | FAILED


class Provider:
    name = "base"

    def originate(self, call_id: str, number: str) -> None:
        raise NotImplementedError

    def hangup(self, call_id: str) -> None:
        raise NotImplementedError

    def query(self, call_id: str) -> Optional[str]:
        """Authoritative provider-side state, or None if the provider has no record."""
        raise NotImplementedError

    def poll_events(self) -> List[ProviderEvent]:
        raise NotImplementedError


class MockProvider(Provider):
    """Simulated provider driven by a SimClock.

    Parameters control its personality:
    * setup_secs     - (min, max) time from originate to RINGING
    * ring_secs      - ringing time before answer
    * talk_secs_fn   - sampled talk duration for answered calls
    * timeout_rate   - originate/query times out *before* the call is placed
    * ambiguous_rate - originate times out *after* the call was placed
    * reject_rate    - originate is rejected outright
    * dup_rate       - probability an event is delivered twice
    * jitter         - random extra delivery delay per event (causes reordering)
    """

    def __init__(
        self,
        name: str,
        clock: SimClock,
        answer_rate_fn: Callable[[float], float],
        seed: int = 1,
        setup_secs=(2.0, 4.0),
        ring_secs: float = 5.0,
        no_answer_secs: float = 20.0,
        talk_secs_fn: Callable[[random.Random], float] = lambda r: 120.0,
        timeout_rate: float = 0.0,
        ambiguous_rate: float = 0.0,
        reject_rate: float = 0.0,
        dup_rate: float = 0.0,
        jitter: float = 0.0,
    ):
        self.name = name
        self._clock = clock
        self._answer_rate_fn = answer_rate_fn
        self._rng = random.Random(seed)
        self._setup = setup_secs
        self._ring = ring_secs
        self._no_answer = no_answer_secs
        self._talk = talk_secs_fn
        self.timeout_rate = timeout_rate
        self.ambiguous_rate = ambiguous_rate
        self.reject_rate = reject_rate
        self.dup_rate = dup_rate
        self.jitter = jitter
        self.outage = False            # set True to simulate the provider going down
        self._truth: dict[str, list[tuple[float, str]]] = {}   # call_id -> [(time, state)]
        self._heap: list[tuple[float, int, str, str, str]] = []  # (deliver_at, seq, call_id, state, event_id)
        self._seq = 0
        self.dial_count = 0            # real dials placed (used to prove no double-dial)

    # ---- Provider interface -------------------------------------------------
    def originate(self, call_id: str, number: str) -> None:
        now = self._clock.now()
        if self.outage or self._rng.random() < self.timeout_rate:
            raise ProviderTimeout(f"{self.name}: originate timed out")
        if call_id in self._truth:
            return  # idempotency key already seen: do not dial again
        if self._rng.random() < self.reject_rate:
            raise ProviderRejected(f"{self.name}: rejected")
        self._place(call_id, now)
        if self._rng.random() < self.ambiguous_rate:
            # The call IS placed, but the caller only sees a timeout.
            raise ProviderTimeout(f"{self.name}: originate ack lost")

    def hangup(self, call_id: str) -> None:
        now = self._clock.now()
        if call_id not in self._truth:
            return
        self._truth[call_id] = [(t, s) for t, s in self._truth[call_id] if t <= now]
        self._truth[call_id].append((now, "COMPLETED"))
        self._heap = [h for h in self._heap if h[2] != call_id]
        heapq.heapify(self._heap)
        self._push(now, call_id, "COMPLETED")

    def query(self, call_id: str) -> Optional[str]:
        if self.outage or self._rng.random() < self.timeout_rate:
            raise ProviderTimeout(f"{self.name}: query timed out")
        now = self._clock.now()
        states = [s for t, s in self._truth.get(call_id, []) if t <= now]
        return states[-1] if states else None

    def poll_events(self) -> List[ProviderEvent]:
        if self.outage:
            return []   # events are backlogged; they arrive late when the outage ends
        now = self._clock.now()
        out: list[ProviderEvent] = []
        while self._heap and self._heap[0][0] <= now:
            _, _, call_id, state, event_id = heapq.heappop(self._heap)
            out.append(ProviderEvent(event_id, call_id, state))
            if self.dup_rate and self._rng.random() < self.dup_rate:
                # Same event delivered again a moment later.
                self._push(now + 1.0, call_id, state, event_id)
        return out

    # ---- internals ----------------------------------------------------------
    def _place(self, call_id: str, now: float) -> None:
        self.dial_count += 1
        ring_at = now + self._rng.uniform(*self._setup)
        timeline = [(now, "INITIATED"), (ring_at, "RINGING")]
        if self._rng.random() < self._answer_rate_fn(now):
            answer_at = ring_at + self._ring
            end_at = answer_at + self._talk(self._rng)
            timeline += [(answer_at, "ANSWERED"), (end_at, "COMPLETED")]
        else:
            timeline += [(ring_at + self._no_answer, "FAILED")]
        self._truth[call_id] = timeline
        for t, s in timeline:
            if s != "INITIATED":
                self._push(t + self._rng.uniform(0.0, self.jitter), call_id, s)

    def _push(self, at: float, call_id: str, state: str, event_id: Optional[str] = None) -> None:
        self._seq += 1
        eid = event_id or f"{self.name}:{call_id}:{state}"
        heapq.heappush(self._heap, (at, self._seq, call_id, state, eid))


def provider_a(clock: SimClock, answer_rate_fn, seed: int = 1, talk_secs_fn=None) -> MockProvider:
    """Fast, reliable, low failure rate."""
    kwargs = {}
    if talk_secs_fn is not None:
        kwargs["talk_secs_fn"] = talk_secs_fn
    return MockProvider("provider-a", clock, answer_rate_fn, seed=seed,
                        setup_secs=(1.0, 2.0), timeout_rate=0.001, **kwargs)


def provider_b(clock: SimClock, answer_rate_fn, seed: int = 2, talk_secs_fn=None) -> MockProvider:
    """Slower, occasional timeouts, ambiguous timeouts, duplicate and out-of-order events."""
    kwargs = {}
    if talk_secs_fn is not None:
        kwargs["talk_secs_fn"] = talk_secs_fn
    return MockProvider("provider-b", clock, answer_rate_fn, seed=seed,
                        setup_secs=(4.0, 9.0), timeout_rate=0.03, ambiguous_rate=0.02,
                        reject_rate=0.005, dup_rate=0.10, jitter=4.0, **kwargs)
