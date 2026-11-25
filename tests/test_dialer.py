"""Tests for the failure cases the assignment calls out.

Run with:  python -m unittest discover -s tests -v
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from smartdialer.clock import SimClock
from smartdialer.dialer import Dialer
from smartdialer.model import (
    AgentState,
    CallState,
    Config,
    next_call_state,
)
from smartdialer.pacing import max_new_calls
from smartdialer.providers import MockProvider, ProviderEvent, ProviderRejected, ProviderTimeout
from smartdialer.safety import Permit, SafetyController
from smartdialer.store import Store
from smartdialer.allocator import PermitError


_OPEN_DIALERS = []   # closed in tearDownModule, so no connection is left for the garbage collector


def make_env(agents=5, borrowers=50, mode="progressive", providers=None, cfg=None, answer=0.5, clock=None):
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "dialer.db")
    clock = clock or SimClock(1000.0)   # providers must share this clock, or their events never come due
    cfg = cfg or Config()
    s = Store(db, cfg, clock)
    for i in range(agents):
        s.add_agent(f"agent{i}", "C", clock.now())
    for i in range(borrowers):
        s.add_borrower(f"b{i}", "C", f"+1555{i:06d}", clock.now())
    s.close()
    if providers is None:
        providers = [MockProvider("p1", clock, lambda t: answer, seed=7, setup_secs=(1.0, 1.0),
                                  talk_secs_fn=lambda r: 60.0)]
    dialer = Dialer(db, "C", providers, mode=mode, cfg=cfg, clock=clock)
    _OPEN_DIALERS.append(dialer)
    return dialer, clock, providers, db


def heartbeat_all(dialer, clock, n):
    dialer.store.heartbeat([f"agent{i}" for i in range(n)], clock.now())


class StateMachineTests(unittest.TestCase):
    def test_duplicate_state_is_ignored(self):
        self.assertIsNone(next_call_state(CallState.ANSWERED, CallState.ANSWERED))

    def test_out_of_order_ringing_after_answered_is_ignored(self):
        self.assertIsNone(next_call_state(CallState.ANSWERED, CallState.RINGING))

    def test_completed_then_answered_is_ignored(self):
        self.assertIsNone(next_call_state(CallState.COMPLETED, CallState.ANSWERED))

    def test_terminal_beats_non_terminal(self):
        self.assertEqual(next_call_state(CallState.RINGING, CallState.COMPLETED), CallState.COMPLETED)

    def test_forward_moves_allowed_and_skips_allowed(self):
        self.assertEqual(next_call_state(CallState.INITIATED, CallState.ANSWERED), CallState.ANSWERED)


class AgentReservationTests(unittest.TestCase):
    def test_two_workers_cannot_both_reserve_the_same_agent(self):
        """Eight threads, each with its own SQLite connection, race for one agent."""
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "race.db")
        clock = SimClock(0.0)
        cfg = Config()
        seed = Store(db, cfg, clock)
        seed.add_agent("solo", "C", 0.0)
        seed.close()

        wins = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker():
            s = Store(db, cfg, clock)
            barrier.wait()
            with s.tx() as c:
                got = s._claim_agent(c, "C", 0.0)
            with lock:
                if got:
                    wins.append(got)
            s.close()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(wins, ["solo"])

    def test_progressive_never_exceeds_available_agents(self):
        dialer, clock, providers, _ = make_env(agents=5, borrowers=50, mode="progressive")
        heartbeat_all(dialer, clock, 5)
        dialer.tick()
        cc = dialer.store.call_counts("C")
        live = sum(v for k, v in cc.items() if k in ("RESERVED", "INITIATED", "RINGING", "ANSWERED", "CONNECTED"))
        self.assertLessEqual(live, 5)
        self.assertEqual(providers[0].dial_count, 5)


class EventTests(unittest.TestCase):
    def _one_call(self):
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive", answer=1.0)
        heartbeat_all(dialer, clock, 1)
        dialer.tick()
        call = dialer.store.conn.execute("SELECT id FROM calls").fetchone()["id"]
        return dialer, clock, call

    def test_duplicate_provider_event_creates_one_transition(self):
        dialer, clock, call = self._one_call()
        ev = ProviderEvent("same-id", call, "RINGING")
        self.assertEqual(dialer.store.apply_event(ev, clock.now()), "applied")
        self.assertEqual(dialer.store.apply_event(ev, clock.now()), "duplicate")

    def test_answered_answered_answered_completed(self):
        dialer, clock, call = self._one_call()
        results = [dialer.store.apply_event(ProviderEvent(f"e{i}", call, "ANSWERED"), clock.now())
                   for i in range(3)]
        self.assertEqual(results[0], "applied")
        self.assertEqual(results[1:], ["stale", "stale"])
        dialer.store.apply_event(ProviderEvent("done", call, "COMPLETED"), clock.now())
        state = dialer.store.conn.execute("SELECT state FROM calls WHERE id=?", (call,)).fetchone()["state"]
        self.assertEqual(state, "COMPLETED")

    def test_completed_then_ringing_then_answered_stays_completed(self):
        dialer, clock, call = self._one_call()
        dialer.store.apply_event(ProviderEvent("c", call, "COMPLETED"), clock.now())
        dialer.store.apply_event(ProviderEvent("r", call, "RINGING"), clock.now())
        dialer.store.apply_event(ProviderEvent("a", call, "ANSWERED"), clock.now())
        state = dialer.store.conn.execute("SELECT state FROM calls WHERE id=?", (call,)).fetchone()["state"]
        self.assertEqual(state, "COMPLETED")
        self.assertEqual(dialer.store.counts("C").get("AVAILABLE"), 1)  # agent released


class CrashRecoveryTests(unittest.TestCase):
    def test_crash_right_after_answered_is_reconciled(self):
        """Worker dies after ANSWERED is stored. Provider later says COMPLETED.
        Recovery must end with a consistent, non-stuck agent."""
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive", answer=1.0)
        heartbeat_all(dialer, clock, 1)
        dialer.tick()
        call = dialer.store.conn.execute("SELECT id FROM calls").fetchone()["id"]
        # Deliver RINGING and ANSWERED, then "crash": no COMPLETED event is ever processed.
        dialer.store.apply_event(ProviderEvent("r", call, "RINGING"), clock.now())
        dialer.store.apply_event(ProviderEvent("a", call, "ANSWERED"), clock.now())
        self.assertEqual(dialer.store.counts("C")["CONNECTED"], 1)
        # Provider has moved on; advance past the provider's talk time and let reconciliation run.
        clock.advance(500)
        for _ in range(3):
            heartbeat_all(dialer, clock, 1)
            dialer.tick()
            clock.advance(20)
        state = dialer.store.conn.execute("SELECT state FROM calls WHERE id=?", (call,)).fetchone()["state"]
        self.assertEqual(state, "COMPLETED")
        self.assertIn(dialer.store.counts("C")["AVAILABLE"], (1,))

    def test_worker_crash_after_reserve_releases_agent_and_borrower(self):
        """Agent + borrower reserved, then the worker dies before dispatch.
        Lease expiry must free both."""
        dialer, clock, providers, _ = make_env(agents=2, borrowers=2, mode="progressive")
        heartbeat_all(dialer, clock, 2)
        with dialer.store.tx() as c:
            permit_id = "dead-permit"
            dialer.store.create_permit(c, permit_id, "C", "progressive", 2, clock.now() + 5)
            dialer.store.reserve_progressive(c, "C", 2, permit_id, clock.now())
        self.assertEqual(dialer.store.counts("C")["RESERVED"], 2)
        clock.advance(dialer.cfg.lease_secs + 1)
        heartbeat_all(dialer, clock, 2)
        dialer.tick()
        dead = dialer.store.conn.execute(
            "SELECT COUNT(*) FROM calls WHERE permit_id='dead-permit' AND state='CANCELLED'").fetchone()[0]
        self.assertEqual(dead, 2)   # the orphaned reservations were cancelled, not left stuck
        self.assertEqual(providers[0].dial_count, 2)  # the re-planned calls were dialled once each
        # Every borrower still RESERVED must be on a live call; none is orphaned.
        orphaned = dialer.store.conn.execute(
            "SELECT COUNT(*) FROM borrowers b WHERE b.state='RESERVED' AND NOT EXISTS "
            "(SELECT 1 FROM calls c WHERE c.borrower_id=b.id AND c.state IN "
            "('QUEUED','RESERVED','INITIATED','RINGING','ANSWERED','CONNECTED'))").fetchone()[0]
        self.assertEqual(orphaned, 0)

    def test_ambiguous_timeout_is_reconciled_without_double_dial(self):
        """Provider places the call but the ack is lost. The dialer must not re-dial."""
        clock = SimClock(1000.0)
        provider = MockProvider("p1", clock, lambda t: 0.0, seed=3, setup_secs=(1.0, 1.0),
                                ambiguous_rate=1.0)
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive",
                                               providers=[provider], clock=clock)
        heartbeat_all(dialer, clock, 1)
        dialer.tick()
        self.assertEqual(provider.dial_count, 1)
        self.assertEqual(dialer.store.call_counts("C").get("INITIATED"), 1)
        clock.advance(dialer.cfg.originate_timeout + 1)
        heartbeat_all(dialer, clock, 1)
        provider.ambiguous_rate = 0.0
        for _ in range(3):
            dialer.tick()
            clock.advance(2)
        self.assertEqual(provider.dial_count, 1)   # never dialed twice


class PermitTests(unittest.TestCase):
    def test_forged_permit_is_refused(self):
        dialer, clock, providers, _ = make_env(agents=3, borrowers=3, mode="progressive")
        forged = Permit("x", "C", "progressive", 3, clock.now() + 100, "0" * 64)
        with self.assertRaises(PermitError):
            dialer.allocator.dispatch(forged)
        self.assertEqual(providers[0].dial_count, 0)

    def test_permit_cannot_be_dispatched_twice(self):
        dialer, clock, providers, _ = make_env(agents=3, borrowers=3, mode="progressive")
        heartbeat_all(dialer, clock, 3)
        decision = dialer.controller.authorize("C", "progressive", 3, provider_ok=True)
        dialer.allocator.dispatch(decision.permit)
        second = dialer.allocator.dispatch(decision.permit)
        self.assertEqual(second.dialed, 0)
        self.assertEqual(providers[0].dial_count, 3)

    def test_pacing_cannot_create_calls_without_controller(self):
        """Calls can only be created by reserve_* inside the controller's transaction."""
        dialer, clock, providers, _ = make_env(agents=3, borrowers=3, mode="predictive")
        heartbeat_all(dialer, clock, 3)
        self.assertEqual(dialer.store.call_counts("C"), {})


class PredictiveTests(unittest.TestCase):
    def test_pacing_formula_gives_more_calls_for_higher_answer_rate(self):
        cap = 200
        low = max_new_calls(0.2, 0, 50, 1.645, cap)
        high = max_new_calls(0.7, 0, 50, 1.645, cap)
        self.assertGreater(low, high)
        self.assertLessEqual(high * 0.7, 50)

    def test_pacing_respects_in_flight(self):
        self.assertEqual(max_new_calls(0.5, 100, 20, 1.645, 50), 0)

    def test_abandon_rate_trips_fallback_to_progressive(self):
        dialer, clock, providers, db = make_env(agents=5, borrowers=200, mode="predictive", answer=0.9)
        s = dialer.store
        with s.tx() as c:
            for _ in range(40):
                s._record_outcome(c, "C", answered=1, abandoned=1, now=clock.now())
        heartbeat_all(dialer, clock, 5)
        dialer.tick()
        decision = dialer.last_decision[0]
        self.assertEqual(decision.decision, "FALLBACK")
        self.assertEqual(decision.permit.mode, "progressive")

    def test_provider_outage_stops_new_permits(self):
        dialer, clock, providers, _ = make_env(agents=5, borrowers=50, mode="predictive")
        heartbeat_all(dialer, clock, 5)
        providers[0].outage = True
        for _ in range(dialer.cfg.breaker_threshold):
            dialer.allocator.breakers["p1"].fail(clock.now())
        dialer.tick()
        self.assertEqual(dialer.last_decision[0].decision, "REJECT")
        self.assertEqual(dialer.last_decision[0].reason, "PROVIDER_UNAVAILABLE")
        self.assertEqual(providers[0].dial_count, 0)


class AgentDisappearanceTests(unittest.TestCase):
    def test_agent_vanishing_during_setup_cancels_call_and_requeues_borrower(self):
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive", answer=0.0)
        heartbeat_all(dialer, clock, 1)
        dialer.tick()
        self.assertEqual(dialer.store.counts("C")["DIALING"], 1)
        # Stop heartbeating; wait past the heartbeat timeout.
        clock.advance(dialer.cfg.heartbeat_timeout + 1)
        dialer.tick()
        self.assertEqual(dialer.store.counts("C")["OFFLINE"], 1)
        states = dialer.store.conn.execute("SELECT state FROM calls").fetchall()
        self.assertTrue(all(r["state"] in ("CANCELLED", "COMPLETED", "FAILED") for r in states))
        b = dialer.store.conn.execute("SELECT state FROM borrowers").fetchone()["state"]
        self.assertEqual(b, "PENDING")


class DispatchTests(unittest.TestCase):
    def test_reject_from_provider_releases_agent(self):
        class Rejecting(MockProvider):
            def originate(self, call_id, number):
                raise ProviderRejected("no")
        clock = SimClock(1000.0)
        provider = Rejecting("p1", clock, lambda t: 1.0)
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive",
                                               providers=[provider])
        heartbeat_all(dialer, clock, 1)
        dialer.tick()
        self.assertEqual(dialer.store.counts("C")["AVAILABLE"], 1)


class MidFlightFailureTests(unittest.TestCase):
    def test_crash_after_initiated_committed_but_before_provider_call(self):
        """Write-ahead INITIATED is committed, then the worker dies before originate().
        The provider never saw the call, so reconciliation must fail it and retry the borrower."""
        dialer, clock, providers, _ = make_env(agents=1, borrowers=1, mode="progressive", answer=1.0)
        heartbeat_all(dialer, clock, 1)
        decision = dialer.controller.authorize("C", "progressive", 1, provider_ok=True)
        with dialer.store.tx() as c:
            batch = dialer.store.begin_dispatch(c, decision.permit.id, "p1", clock.now())
        self.assertEqual(len(batch), 1)              # INITIATED is committed ...
        self.assertEqual(providers[0].dial_count, 0)  # ... but the provider was never called
        for _ in range(12):                      # 60s: reconcile at ~10s, retry backoff 30s
            clock.advance(5)
            heartbeat_all(dialer, clock, 1)
            dialer.tick()
        call = dialer.store.conn.execute("SELECT state, reason FROM calls ORDER BY created_at LIMIT 1").fetchone()
        self.assertEqual(call["state"], "FAILED")
        self.assertEqual(call["reason"], "NOT_FOUND_AT_PROVIDER")
        # Borrower was requeued and then dialled exactly once, by a fresh permit.
        self.assertEqual(providers[0].dial_count, 1)

    def test_outage_during_existing_call_defers_then_completes(self):
        """Provider goes down while a call is ringing. Its events freeze, the dialer
        defers the query instead of guessing, nothing new gets through, and the
        existing call completes once the provider is back."""
        clock = SimClock(1000.0)
        provider = MockProvider("p1", clock, lambda t: 1.0, seed=9, setup_secs=(1.0, 1.0),
                                ring_secs=5.0, talk_secs_fn=lambda r: 60.0)
        dialer, clock, providers, _ = make_env(agents=1, borrowers=3, mode="progressive",
                                               providers=[provider], clock=clock)
        heartbeat_all(dialer, clock, 1)
        dialer.tick()                       # call dialled and ringing
        self.assertEqual(provider.dial_count, 1)
        provider.outage = True
        deferred = 0
        for _ in range(12):
            clock.advance(5)
            heartbeat_all(dialer, clock, 1)
            rep = dialer.tick()
            deferred += rep.get("reconcile_deferred", 0)
        self.assertGreater(deferred, 0)                 # queries were deferred, not guessed
        self.assertEqual(provider.dial_count, 1)         # no new call got through the outage
        provider.outage = False
        for _ in range(10):
            clock.advance(5)
            heartbeat_all(dialer, clock, 1)
            dialer.tick()
        state = dialer.store.conn.execute("SELECT state FROM calls ORDER BY created_at LIMIT 1").fetchone()["state"]
        self.assertEqual(state, "COMPLETED")


class OutageBehaviourTests(unittest.TestCase):
    def test_outage_pauses_pacing_keeps_borrowers_and_retries_resume(self):
        dialer, clock, providers, _ = make_env(agents=5, borrowers=20, mode="predictive", answer=0.5)
        heartbeat_all(dialer, clock, 5)
        providers[0].outage = True
        for _ in range(dialer.cfg.breaker_threshold):
            dialer.allocator.breakers["p1"].fail(clock.now())
        dialer.tick()
        self.assertEqual(dialer.last_decision[0].decision, "REJECT")     # pacing paused
        self.assertEqual(providers[0].dial_count, 0)
        untouched = dialer.store.conn.execute(
            "SELECT COUNT(*) FROM borrowers WHERE state='PENDING' AND attempts=0").fetchone()[0]
        self.assertEqual(untouched, 20)                                  # no retry was used up
        providers[0].outage = False
        clock.advance(dialer.cfg.breaker_cooldown + 1)
        for _ in range(3):
            heartbeat_all(dialer, clock, 5)
            dialer.tick()
            clock.advance(1)
        self.assertGreater(providers[0].dial_count, 0)                   # dialling resumes


def tearDownModule():
    for d in _OPEN_DIALERS:
        d.close()


if __name__ == "__main__":
    unittest.main()
