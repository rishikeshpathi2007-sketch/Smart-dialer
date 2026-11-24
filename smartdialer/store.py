"""Persistence and state transitions.

SQLite (WAL mode, one connection per worker) is the single source of truth.
Every state change is a compare-and-set (CAS):

    UPDATE agents SET state='RESERVED' WHERE id=? AND state='AVAILABLE'

and the caller checks rowcount == 1. Writers that need several statements
together run inside BEGIN IMMEDIATE, so two workers can never interleave a
claim. The same CAS pattern maps directly onto Postgres
(UPDATE ... WHERE state = ...) if the prototype is ever moved off SQLite.
"""
from __future__ import annotations

import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, Optional

from .model import (
    ACTIVE_CALL_STATES,
    AgentState,
    BorrowerState,
    CallState,
    Config,
    agent_transition_ok,
    next_call_state,
)
from .providers import ProviderEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  campaign TEXT NOT NULL,
  state TEXT NOT NULL,
  heartbeat REAL NOT NULL,
  current_call_id TEXT,
  connected_at REAL,
  wrap_until REAL,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_agents_state ON agents(campaign, state);

CREATE TABLE IF NOT EXISTS borrowers (
  id TEXT PRIMARY KEY,
  campaign TEXT NOT NULL,
  number TEXT NOT NULL,
  state TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_borrowers_ready ON borrowers(campaign, state, next_attempt_at);

CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY,
  campaign TEXT NOT NULL,
  borrower_id TEXT NOT NULL,
  agent_id TEXT,
  permit_id TEXT,
  provider TEXT,
  state TEXT NOT NULL,
  reason TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  answered_at REAL,
  abandoned INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(campaign, state);

-- Every provider event we have ever seen. Primary key = dedupe.
CREATE TABLE IF NOT EXISTS call_events (
  event_id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL,
  state TEXT NOT NULL,
  result TEXT NOT NULL,
  received_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS permits (
  id TEXT PRIMARY KEY,
  campaign TEXT NOT NULL,
  mode TEXT NOT NULL,
  granted INTEGER NOT NULL,
  expires_at REAL NOT NULL,
  dispatched INTEGER NOT NULL DEFAULT 0
);

-- Decayed counters used for the answer-rate estimate.
CREATE TABLE IF NOT EXISTS campaign_stats (
  campaign TEXT PRIMARY KEY,
  attempts REAL NOT NULL DEFAULT 0,
  answered REAL NOT NULL DEFAULT 0,
  talk_secs REAL NOT NULL DEFAULT 0,
  talk_n INTEGER NOT NULL DEFAULT 0,
  connected_n INTEGER NOT NULL DEFAULT 0
);

-- One row per resolved attempt. Used for windowed abandon / shock checks.
CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign TEXT NOT NULL,
  answered INTEGER NOT NULL,
  abandoned INTEGER NOT NULL,
  ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS safety_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign TEXT NOT NULL,
  ts REAL NOT NULL,
  mode TEXT NOT NULL,
  requested INTEGER NOT NULL,
  granted INTEGER NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL
);
"""


@dataclass
class Snapshot:
    """Read-only view of a campaign used by pacing and by the safety controller."""
    now: float
    available: int        # agents AVAILABLE with a fresh heartbeat
    logged_in: int        # agents not OFFLINE with a fresh heartbeat
    in_flight: int        # calls QUEUED/RESERVED/INITIATED/RINGING (will try to connect)
    supply_soon: int      # agents that will be free within the horizon
    ready_borrowers: int
    attempts: float       # decayed
    answered: float       # decayed
    p_hat: float          # posterior-mean answer rate
    p_upper: float        # conservative (upper) answer rate
    recent_rate: float    # answer rate over the last `shock_window` outcomes
    recent_n: int
    pending_unbound: int  # predictive calls still ringing that will need an agent when answered
    abandon_rate: float   # abandoned / answered over the last 300 outcomes
    answered_window: int
    avg_talk: float


class Store:
    def __init__(self, path: str, cfg: Config, clock):
        self.cfg = cfg
        self.clock = clock
        self.conn = sqlite3.connect(path, timeout=60, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=60000")
        self.conn.execute("PRAGMA synchronous=NORMAL")  # WAL: durable across crashes, skips fsync per commit
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self):
        """Serialised write transaction (BEGIN IMMEDIATE takes the write lock up front)."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ setup
    def ensure_campaign(self, c, campaign: str) -> None:
        c.execute("INSERT OR IGNORE INTO campaign_stats(campaign) VALUES (?)", (campaign,))

    def add_agent(self, agent_id: str, campaign: str, now: float) -> None:
        with self.tx() as c:
            self.ensure_campaign(c, campaign)
            c.execute(
                "INSERT INTO agents(id, campaign, state, heartbeat) VALUES (?,?,?,?)",
                (agent_id, campaign, AgentState.AVAILABLE.value, now),
            )

    def add_borrower(self, borrower_id: str, campaign: str, number: str, now: float) -> None:
        with self.tx() as c:
            self.ensure_campaign(c, campaign)
            c.execute(
                "INSERT INTO borrowers(id, campaign, number, state, next_attempt_at) VALUES (?,?,?,?,?)",
                (borrower_id, campaign, number, BorrowerState.PENDING.value, now),
            )

    def heartbeat(self, agent_ids: Iterable[str], now: float) -> None:
        with self.tx() as c:
            c.executemany("UPDATE agents SET heartbeat=? WHERE id=?", [(now, a) for a in agent_ids])

    # ------------------------------------------------------------- CAS helpers
    @staticmethod
    def _cas(c, table: str, key: str, expected: Iterable[str], **sets) -> bool:
        expected = list(expected)
        cols = ", ".join(f"{k}=?" for k in sets)
        sep = ", " if cols else ""
        sql = (f"UPDATE {table} SET {cols}{sep}version=version+1 "
               f"WHERE id=? AND state IN ({','.join('?' * len(expected))})")
        cur = c.execute(sql, [*sets.values(), key, *expected])
        return cur.rowcount == 1

    def _set_agent(self, c, agent_id: str, src: Iterable[AgentState], dst: AgentState, **extra) -> bool:
        src = list(src)
        for s in src:
            if not agent_transition_ok(s, dst):
                raise ValueError(f"illegal agent transition {s.value} -> {dst.value}")
        return self._cas(c, "agents", agent_id, [s.value for s in src], state=dst.value, **extra)

    def _claim_agent(self, c, campaign: str, now: float) -> Optional[str]:
        """AVAILABLE -> RESERVED for one agent with a fresh heartbeat."""
        for _ in range(5):
            row = c.execute(
                "SELECT id FROM agents WHERE campaign=? AND state=? AND heartbeat>=? ORDER BY id LIMIT 1",
                (campaign, AgentState.AVAILABLE.value, now - self.cfg.heartbeat_timeout),
            ).fetchone()
            if row is None:
                return None
            if self._set_agent(c, row["id"], [AgentState.AVAILABLE], AgentState.RESERVED):
                return row["id"]
        return None

    def _claim_borrower(self, c, campaign: str, now: float) -> Optional[str]:
        """PENDING -> RESERVED for one borrower whose retry time has come."""
        for _ in range(5):
            row = c.execute(
                "SELECT id FROM borrowers WHERE campaign=? AND state=? AND next_attempt_at<=? "
                "ORDER BY next_attempt_at, id LIMIT 1",
                (campaign, BorrowerState.PENDING.value, now),
            ).fetchone()
            if row is None:
                return None
            if self._cas(c, "borrowers", row["id"], [BorrowerState.PENDING.value],
                         state=BorrowerState.RESERVED.value):
                return row["id"]
        return None

    # ----------------------------------------------------- reservation (permits)
    def count_ready_borrowers(self, c, campaign: str, now: float) -> int:
        return c.execute(
            "SELECT COUNT(*) FROM borrowers WHERE campaign=? AND state=? AND next_attempt_at<=?",
            (campaign, BorrowerState.PENDING.value, now),
        ).fetchone()[0]

    def reserve_progressive(self, c, campaign: str, n: int, permit_id: str, now: float) -> int:
        """Agent-first: each call is created only after an agent AND a borrower are claimed."""
        granted = 0
        for _ in range(n):
            agent_id = self._claim_agent(c, campaign, now)
            if agent_id is None:
                break
            borrower_id = self._claim_borrower(c, campaign, now)
            if borrower_id is None:
                self._set_agent(c, agent_id, [AgentState.RESERVED], AgentState.AVAILABLE)
                break
            call_id = uuid.uuid4().hex
            c.execute(
                "INSERT INTO calls(id, campaign, borrower_id, agent_id, permit_id, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (call_id, campaign, borrower_id, agent_id, permit_id,
                 CallState.RESERVED.value, now, now),
            )
            c.execute("UPDATE agents SET current_call_id=? WHERE id=?", (call_id, agent_id))
            granted += 1
        return granted

    def reserve_predictive(self, c, campaign: str, n: int, permit_id: str, now: float) -> int:
        """Borrower-first, agent-less: the call exists as QUEUED; an agent is only
        attached when the call is answered (or the call is abandoned)."""
        granted = 0
        for _ in range(n):
            borrower_id = self._claim_borrower(c, campaign, now)
            if borrower_id is None:
                break
            c.execute(
                "INSERT INTO calls(id, campaign, borrower_id, agent_id, permit_id, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, campaign, borrower_id, None, permit_id,
                 CallState.QUEUED.value, now, now),
            )
            granted += 1
        return granted

    def create_permit(self, c, permit_id: str, campaign: str, mode: str, granted: int, expires_at: float) -> None:
        c.execute("INSERT INTO permits(id, campaign, mode, granted, expires_at) VALUES (?,?,?,?,?)",
                  (permit_id, campaign, mode, granted, expires_at))

    def log_decision(self, c, campaign: str, now: float, mode: str, requested: int,
                     granted: int, decision: str, reason: str) -> None:
        c.execute("INSERT INTO safety_log(campaign, ts, mode, requested, granted, decision, reason) "
                  "VALUES (?,?,?,?,?,?,?)", (campaign, now, mode, requested, granted, decision, reason))

    # --------------------------------------------------------------- dispatch
    def begin_dispatch(self, c, permit_id: str, provider_name: str, now: float) -> list[tuple[str, str]]:
        """Write-ahead step: mark the permit dispatched and move its calls to INITIATED.

        Runs in one transaction, so either the whole permit is dispatched or none of it.
        Returns [(call_id, number)] for the provider calls to make afterwards.
        """
        permit = c.execute("SELECT * FROM permits WHERE id=? AND dispatched=0 AND expires_at>=?",
                           (permit_id, now)).fetchone()
        if permit is None:
            return []
        c.execute("UPDATE permits SET dispatched=1 WHERE id=?", (permit_id,))
        rows = c.execute(
            "SELECT c.id AS call_id, c.agent_id, c.state, b.id AS borrower_id, b.number "
            "FROM calls c JOIN borrowers b ON b.id=c.borrower_id "
            "WHERE c.permit_id=? AND c.state IN (?,?)",
            (permit_id, CallState.QUEUED.value, CallState.RESERVED.value),
        ).fetchall()
        out = []
        for r in rows:
            if not self._cas(c, "calls", r["call_id"], [r["state"]],
                             state=CallState.INITIATED.value, provider=provider_name, updated_at=now):
                continue
            c.execute("UPDATE borrowers SET attempts=attempts+1 WHERE id=?", (r["borrower_id"],))
            if r["agent_id"]:
                self._set_agent(c, r["agent_id"], [AgentState.RESERVED], AgentState.DIALING)
            out.append((r["call_id"], r["number"]))
        return out

    # ------------------------------------------------------------ call events
    def apply_event(self, ev: ProviderEvent, now: float, reason: Optional[str] = None) -> str:
        """Apply one provider (or synthetic) event. Returns applied/duplicate/stale/unknown."""
        with self.tx() as c:
            try:
                c.execute("INSERT INTO call_events(event_id, call_id, state, result, received_at) "
                          "VALUES (?,?,?,?,?)", (ev.event_id, ev.call_id, ev.state, "pending", now))
            except sqlite3.IntegrityError:
                return "duplicate"
            call = c.execute("SELECT * FROM calls WHERE id=?", (ev.call_id,)).fetchone()
            if call is None:
                c.execute("UPDATE call_events SET result='unknown' WHERE event_id=?", (ev.event_id,))
                return "unknown"
            new = next_call_state(CallState(call["state"]), CallState(ev.state))
            if new is None:
                c.execute("UPDATE call_events SET result='stale' WHERE event_id=?", (ev.event_id,))
                return "stale"
            self._transition(c, call, new, now, reason)
            c.execute("UPDATE call_events SET result='applied' WHERE event_id=?", (ev.event_id,))
            return "applied"

    def touch_call(self, call_id: str, now: float) -> None:
        with self.tx() as c:
            c.execute("UPDATE calls SET updated_at=? WHERE id=? AND state NOT IN (?,?,?)",
                      (now, call_id, CallState.COMPLETED.value, CallState.FAILED.value,
                       CallState.CANCELLED.value))

    def cancel_call(self, call_id: str, now: float, reason: str) -> None:
        """Used by recovery paths (lease expiry, vanished agent)."""
        with self.tx() as c:
            call = c.execute("SELECT * FROM calls WHERE id=?", (call_id,)).fetchone()
            if call is None or CallState(call["state"]) not in ACTIVE_CALL_STATES:
                return
            target = CallState.CANCELLED
            if CallState(call["state"]) in (CallState.ANSWERED, CallState.CONNECTED):
                target = CallState.COMPLETED
            self._transition(c, call, target, now, reason)

    def _transition(self, c, call, new: CallState, now: float, reason: Optional[str]) -> None:
        """Apply a call transition together with its side effects on agent and borrower."""
        call_id = call["id"]
        old = CallState(call["state"])
        if not self._cas(c, "calls", call_id, [old.value], state=new.value,
                         updated_at=now, reason=reason or call["reason"]):
            return  # lost a race with another writer; nothing else to do

        if new == CallState.ANSWERED:
            c.execute("UPDATE calls SET answered_at=? WHERE id=?", (now, call_id))
            if call["agent_id"]:
                # Progressive: the agent was reserved before the call was placed.
                self._set_agent(c, call["agent_id"], [AgentState.DIALING], AgentState.CONNECTED,
                                connected_at=now)
                self._advance_connected(c, call_id, now)
                self._record_outcome(c, call["campaign"], answered=1, abandoned=0, now=now)
            else:
                # Predictive: the answered call needs an agent NOW. Claim one or abandon.
                agent_id = self._claim_agent(c, call["campaign"], now)
                if agent_id is None:
                    c.execute("UPDATE calls SET abandoned=1 WHERE id=?", (call_id,))
                    self._record_outcome(c, call["campaign"], answered=1, abandoned=1, now=now)
                else:
                    self._set_agent(c, agent_id, [AgentState.RESERVED], AgentState.DIALING,
                                    current_call_id=call_id)
                    self._set_agent(c, agent_id, [AgentState.DIALING], AgentState.CONNECTED,
                                    connected_at=now)
                    c.execute("UPDATE calls SET agent_id=? WHERE id=?", (agent_id, call_id))
                    self._advance_connected(c, call_id, now)
                    self._record_outcome(c, call["campaign"], answered=1, abandoned=0, now=now)
            return

        if new in (CallState.COMPLETED, CallState.FAILED, CallState.CANCELLED):
            if call["answered_at"] is None and old == CallState.RINGING and new in (
                    CallState.FAILED, CallState.COMPLETED):
                self._record_outcome(c, call["campaign"], answered=0, abandoned=0, now=now)
            if call["agent_id"]:
                self._release_agent(c, call["agent_id"], call_id, now)
            b = c.execute("SELECT * FROM borrowers WHERE id=?", (call["borrower_id"],)).fetchone()
            if b is not None and b["state"] == BorrowerState.RESERVED.value:
                if new == CallState.COMPLETED and call["answered_at"] is not None:
                    self._cas(c, "borrowers", b["id"], [BorrowerState.RESERVED.value],
                              state=BorrowerState.DONE.value)
                elif new == CallState.CANCELLED:
                    self._cas(c, "borrowers", b["id"], [BorrowerState.RESERVED.value],
                              state=BorrowerState.PENDING.value, next_attempt_at=now)
                else:
                    if b["attempts"] >= self.cfg.max_attempts:
                        self._cas(c, "borrowers", b["id"], [BorrowerState.RESERVED.value],
                                  state=BorrowerState.EXHAUSTED.value)
                    else:
                        delay = self.cfg.retry_backoff * (2 ** max(b["attempts"] - 1, 0))
                        self._cas(c, "borrowers", b["id"], [BorrowerState.RESERVED.value],
                                  state=BorrowerState.PENDING.value, next_attempt_at=now + delay)

    def _advance_connected(self, c, call_id: str, now: float) -> None:
        self._cas(c, "calls", call_id, [CallState.ANSWERED.value], state=CallState.CONNECTED.value,
                  updated_at=now)
        c.execute("UPDATE campaign_stats SET connected_n=connected_n+1 WHERE campaign="
                  "(SELECT campaign FROM calls WHERE id=?)", (call_id,))

    def _release_agent(self, c, agent_id: str, call_id: str, now: float) -> None:
        a = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if a is None or a["current_call_id"] != call_id:
            return  # already released by someone else
        state = AgentState(a["state"])
        if state == AgentState.CONNECTED:
            talk = max(now - (a["connected_at"] or now), 0.0)
            c.execute("UPDATE campaign_stats SET talk_secs=talk_secs+?, talk_n=talk_n+1 "
                      "WHERE campaign=?", (talk, a["campaign"]))
            self._set_agent(c, agent_id, [AgentState.CONNECTED], AgentState.WRAP_UP,
                            wrap_until=now + self.cfg.wrap_secs, current_call_id=None)
        elif state in (AgentState.DIALING, AgentState.RESERVED):
            self._set_agent(c, agent_id, [state], AgentState.AVAILABLE, current_call_id=None)
        else:
            c.execute("UPDATE agents SET current_call_id=NULL WHERE id=?", (agent_id,))

    def _record_outcome(self, c, campaign: str, answered: int, abandoned: int, now: float) -> None:
        d = self.cfg.decay
        c.execute("INSERT INTO outcomes(campaign, answered, abandoned, ts) VALUES (?,?,?,?)",
                  (campaign, answered, abandoned, now))
        c.execute("UPDATE campaign_stats SET attempts=attempts*?+1, answered=answered*?+? "
                  "WHERE campaign=?", (d, d, answered, campaign))

    # ---------------------------------------------------------- snapshots
    def snapshot(self, c, campaign: str, now: float, horizon: float) -> Snapshot:
        cfg = self.cfg
        hb = now - cfg.heartbeat_timeout
        available = c.execute("SELECT COUNT(*) FROM agents WHERE campaign=? AND state=? AND heartbeat>=?",
                              (campaign, AgentState.AVAILABLE.value, hb)).fetchone()[0]
        logged_in = c.execute("SELECT COUNT(*) FROM agents WHERE campaign=? AND state!=? AND heartbeat>=?",
                              (campaign, AgentState.OFFLINE.value, hb)).fetchone()[0]
        in_flight = c.execute(
            "SELECT COUNT(*) FROM calls WHERE campaign=? AND state IN (?,?,?,?)",
            (campaign, CallState.QUEUED.value, CallState.RESERVED.value,
             CallState.INITIATED.value, CallState.RINGING.value)).fetchone()[0]
        pending_unbound = c.execute(
            "SELECT COUNT(*) FROM calls WHERE campaign=? AND agent_id IS NULL AND state IN (?,?,?)",
            (campaign, CallState.QUEUED.value, CallState.INITIATED.value,
             CallState.RINGING.value)).fetchone()[0]
        st = c.execute("SELECT * FROM campaign_stats WHERE campaign=?", (campaign,)).fetchone()
        avg_talk = st["talk_secs"] / st["talk_n"] if st["talk_n"] else 120.0
        connected = c.execute(
            "SELECT COUNT(*) FROM agents WHERE campaign=? AND state=? AND connected_at+? <= ?",
            (campaign, AgentState.CONNECTED.value, avg_talk, now + horizon)).fetchone()[0]
        wrapping = c.execute(
            "SELECT COUNT(*) FROM agents WHERE campaign=? AND state=? AND wrap_until<=?",
            (campaign, AgentState.WRAP_UP.value, now + horizon)).fetchone()[0]
        ready = self.count_ready_borrowers(c, campaign, now)

        prior_n = cfg.prior_weight
        p_hat = (st["answered"] + cfg.default_answer_rate * prior_n) / (st["attempts"] + prior_n)
        n_eff = max(st["attempts"] + prior_n, 1.0)
        p_upper = min(1.0, p_hat + cfg.z * math.sqrt(max(p_hat * (1 - p_hat), 0.0) / n_eff))

        recent = c.execute("SELECT answered FROM outcomes WHERE campaign=? ORDER BY id DESC LIMIT ?",
                           (campaign, cfg.shock_window)).fetchall()
        recent_n = len(recent)
        recent_rate = sum(r[0] for r in recent) / recent_n if recent_n else p_hat
        win = c.execute("SELECT COALESCE(SUM(answered),0), COALESCE(SUM(abandoned),0) FROM "
                        "(SELECT answered, abandoned FROM outcomes WHERE campaign=? ORDER BY id DESC LIMIT ?)",
                        (campaign, cfg.abandon_window)).fetchone()
        answered_window, abandoned_window = win[0], win[1]
        abandon_rate = abandoned_window / answered_window if answered_window else 0.0

        return Snapshot(
            now=now, available=available, logged_in=logged_in, in_flight=in_flight,
            supply_soon=available + cfg.connected_weight * (wrapping + connected), ready_borrowers=ready,
            attempts=st["attempts"], answered=st["answered"], p_hat=p_hat, p_upper=p_upper,
            recent_rate=recent_rate, recent_n=recent_n, abandon_rate=abandon_rate,
            pending_unbound=pending_unbound,
            answered_window=answered_window, avg_talk=avg_talk,
        )

    # ----------------------------------------------------------- recovery
    def stale_calls(self, c, states: Iterable[CallState], older_than: float) -> list:
        qs = [s.value for s in states]
        return c.execute(
            f"SELECT * FROM calls WHERE state IN ({','.join('?' * len(qs))}) AND updated_at<? "
            "ORDER BY updated_at LIMIT 500",
            (*qs, older_than)).fetchall()

    def expire_vanished_agents(self, now: float) -> list[tuple[str, str]]:
        """Agents whose heartbeat stopped. Returns [(call_id, provider)] to hang up."""
        hangups = []
        with self.tx() as c:
            rows = c.execute(
                "SELECT id, state, current_call_id, campaign FROM agents WHERE heartbeat<? AND state!=?",
                (now - self.cfg.heartbeat_timeout, AgentState.OFFLINE.value)).fetchall()
            for r in rows:
                state = AgentState(r["state"])
                if r["current_call_id"]:
                    call = c.execute("SELECT * FROM calls WHERE id=?", (r["current_call_id"],)).fetchone()
                    if call is not None and CallState(call["state"]) in ACTIVE_CALL_STATES:
                        target = CallState.COMPLETED if CallState(call["state"]) in (
                            CallState.ANSWERED, CallState.CONNECTED) else CallState.CANCELLED
                        self._transition(c, call, target, now, "AGENT_LOST")
                        if call["provider"]:
                            hangups.append((call["id"], call["provider"]))
                # Whatever it was doing, the agent is now gone.
                a = c.execute("SELECT state FROM agents WHERE id=?", (r["id"],)).fetchone()
                if a is not None and state != AgentState.OFFLINE:
                    cur = AgentState(a["state"])
                    if agent_transition_ok(cur, AgentState.OFFLINE):
                        self._cas(c, "agents", r["id"], [cur.value], state=AgentState.OFFLINE.value,
                                  current_call_id=None)
        return hangups

    def sweep(self, now: float) -> None:
        """Lease expiry for calls that were reserved but never dispatched; finish wrap-ups."""
        with self.tx() as c:
            for call in self.stale_calls(c, [CallState.QUEUED, CallState.RESERVED],
                                         now - self.cfg.lease_secs):
                self._transition(c, call, CallState.CANCELLED, now, "LEASE_EXPIRED")
            c.execute("UPDATE agents SET state=?, wrap_until=NULL WHERE state=? AND wrap_until<=?",
                      (AgentState.AVAILABLE.value, AgentState.WRAP_UP.value, now))

    def permit_row(self, permit_id: str):
        with self.tx() as c:
            return c.execute("SELECT * FROM permits WHERE id=?", (permit_id,)).fetchone()

    def counts(self, campaign: str) -> dict:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM agents WHERE campaign=? GROUP BY state",
                                 (campaign,)).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def call_counts(self, campaign: str) -> dict:
        rows = self.conn.execute("SELECT state, COUNT(*) n FROM calls WHERE campaign=? GROUP BY state",
                                 (campaign,)).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def abandoned_count(self, campaign: str) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM calls WHERE campaign=? AND abandoned=1",
                                 (campaign,)).fetchone()[0]
