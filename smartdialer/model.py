"""State machines, configuration and shared constants for the SmartDialer.

Two lifecycles are defined here:

* Agent lifecycle  - enforced by AGENT_TRANSITIONS (illegal moves are rejected).
* Call lifecycle   - provider events are *monotonic*: a call only moves forward
                     through its rank order, terminal states are final, and any
                     event that would move it backwards (or re-enter the same
                     state) is treated as stale / duplicate and ignored.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class AgentState(str, Enum):
    OFFLINE = "OFFLINE"
    AVAILABLE = "AVAILABLE"
    RESERVED = "RESERVED"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    WRAP_UP = "WRAP_UP"
    PAUSED = "PAUSED"


class CallState(str, Enum):
    QUEUED = "QUEUED"
    RESERVED = "RESERVED"
    INITIATED = "INITIATED"
    RINGING = "RINGING"
    ANSWERED = "ANSWERED"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BorrowerState(str, Enum):
    PENDING = "PENDING"
    RESERVED = "RESERVED"
    DONE = "DONE"
    EXHAUSTED = "EXHAUSTED"


AGENT_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.OFFLINE: {AgentState.AVAILABLE, AgentState.PAUSED},
    AgentState.AVAILABLE: {AgentState.RESERVED, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.RESERVED: {AgentState.DIALING, AgentState.AVAILABLE, AgentState.OFFLINE},
    AgentState.DIALING: {AgentState.CONNECTED, AgentState.AVAILABLE, AgentState.WRAP_UP, AgentState.OFFLINE},
    AgentState.CONNECTED: {AgentState.WRAP_UP, AgentState.OFFLINE},
    AgentState.WRAP_UP: {AgentState.AVAILABLE, AgentState.PAUSED, AgentState.OFFLINE},
    AgentState.PAUSED: {AgentState.AVAILABLE, AgentState.OFFLINE},
}


def agent_transition_ok(src: AgentState, dst: AgentState) -> bool:
    return dst in AGENT_TRANSITIONS.get(src, set())


# Forward-only rank for provider-driven call states.
CALL_RANK: dict[CallState, int] = {
    CallState.QUEUED: 0,
    CallState.RESERVED: 1,
    CallState.INITIATED: 2,
    CallState.RINGING: 3,
    CallState.ANSWERED: 4,
    CallState.CONNECTED: 5,
}
TERMINAL_CALL_STATES = {CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED}
ACTIVE_CALL_STATES = {
    CallState.QUEUED,
    CallState.RESERVED,
    CallState.INITIATED,
    CallState.RINGING,
    CallState.ANSWERED,
    CallState.CONNECTED,
}


def next_call_state(current: CallState, incoming: CallState) -> Optional[CallState]:
    """Return the state the call should move to, or None if the event is ignored.

    Rules (these are what make duplicate and out-of-order events harmless):
    * Nothing moves out of a terminal state.
    * Re-delivery of the current state is a no-op (duplicate).
    * A terminal event (COMPLETED / FAILED / CANCELLED) always wins over any
      non-terminal state, so a late RINGING/ANSWERED can never resurrect a call.
    * A non-terminal event is only applied if it moves the call forward.
      "ANSWERED, then RINGING" therefore leaves the call in ANSWERED.
    """
    if current in TERMINAL_CALL_STATES:
        return None
    if incoming == current:
        return None
    if incoming in TERMINAL_CALL_STATES:
        return incoming
    if CALL_RANK.get(incoming, -1) > CALL_RANK.get(current, -1):
        return incoming
    return None


@dataclass
class Config:
    # Reservation / lease timings (seconds).
    lease_secs: float = 30.0          # how long a RESERVED call may sit before recovery frees it
    permit_ttl: float = 5.0           # a permit must be dispatched within this window
    originate_timeout: float = 10.0   # INITIATED calls older than this are reconciled with the provider
    stale_after: float = 15.0         # non-terminal calls with no news for this long get queried
    heartbeat_timeout: float = 15.0   # agent considered gone after this much silence
    wrap_secs: float = 5.0            # agent wrap-up time after a call
    # Retry policy.
    max_attempts: int = 3
    retry_backoff: float = 30.0
    # Pacing / safety.
    default_answer_rate: float = 0.3  # prior used before any outcomes are observed
    prior_weight: float = 10.0        # pseudo-count behind the prior
    decay: float = 0.98               # per-outcome decay: recent outcomes count more
    z: float = 1.645                  # ~95% one-sided: overflow allowed in the pacing bound
    horizon: float = 20.0             # look-ahead for agents becoming free
    connected_weight: float = 0.5     # trust placed on agents that are only *expected* to free up
    max_new_per_tick: int = 200
    max_in_flight_per_agent: float = 1.5
    max_abandon_rate: float = 0.03    # compliance threshold for answered-but-no-agent calls
    abandon_window: int = 100         # recent resolved attempts used for the abandon-rate check
    shock_window: int = 20           # recent outcomes used for answer-rate shock detection
    shock_factor: float = 1.5         # recent answer rate > factor * estimate => shock
    breaker_threshold: int = 5        # consecutive provider errors before opening the breaker
    breaker_cooldown: float = 30.0
