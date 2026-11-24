"""Basic load test.

    python -m smartdialer.loadtest --agents 1000 --borrowers 20000 --ticks 120

1. Reservation race: N threads (each with its own SQLite connection) claim
   from the same pool of agents at once. Every agent must be claimed exactly
   once and never twice.
2. Dialer throughput: runs predictive ticks against all agents and reports
   ticks/s, calls dispatched, and how the dialer behaves as the pool grows.
"""
from __future__ import annotations

import argparse
import os
import tempfile
import threading
import time

from .clock import SimClock
from .dialer import Dialer
from .model import Config
from .providers import provider_a
from .store import Store


def reservation_race(agents: int, threads: int) -> tuple[bool, float]:
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "race.db")
    cfg = Config()
    clock = SimClock(0.0)
    s = Store(db, cfg, clock)
    for i in range(agents):
        s.add_agent(f"a{i:05d}", "C", 0.0)
    s.close()

    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def worker():
        st = Store(db, cfg, clock)
        barrier.wait()
        mine = []
        while True:
            with st.tx() as c:
                got = st._claim_agent(c, "C", 0.0)
            if got is None:
                break
            mine.append(got)
        with lock:
            claimed.extend(mine)
        st.close()

    t0 = time.perf_counter()
    ts = [threading.Thread(target=worker) for _ in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - t0
    ok = len(claimed) == agents and len(set(claimed)) == agents
    return ok, elapsed


def tick_throughput(agents: int, borrowers: int, ticks: int) -> dict:
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "load.db")
    cfg = Config()
    clock = SimClock(1000.0)
    s = Store(db, cfg, clock)
    ids = [f"a{i:05d}" for i in range(agents)]
    for a in ids:
        s.add_agent(a, "C", clock.now())
    for i in range(borrowers):
        s.add_borrower(f"b{i:06d}", "C", f"+1555{i:07d}", clock.now())
    s.close()
    pa = provider_a(clock, lambda t: 0.5, seed=5, talk_secs_fn=lambda r: 90.0)
    dialer = Dialer(db, "C", [pa], mode="predictive", cfg=cfg, clock=clock)
    t0 = time.perf_counter()
    for _ in range(ticks):
        clock.advance(1.0)
        dialer.store.heartbeat(ids, clock.now())
        dialer.tick()
    elapsed = time.perf_counter() - t0
    dialer.close()
    return {"agents": agents, "ticks": ticks, "seconds": round(elapsed, 2),
            "ticks_per_sec": round(ticks / elapsed, 1), "dials": pa.dial_count}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=1000)
    ap.add_argument("--borrowers", type=int, default=20000)
    ap.add_argument("--ticks", type=int, default=120)
    ap.add_argument("--threads", type=int, default=8)
    args = ap.parse_args()

    ok, secs = reservation_race(args.agents, args.threads)
    print(f"reservation race: {args.agents} agents, {args.threads} threads -> "
          f"exactly-once={ok} in {secs:.2f}s")
    r = tick_throughput(args.agents, args.borrowers, args.ticks)
    print(f"dialer: {r['agents']} agents, {r['ticks']} ticks in {r['seconds']}s "
          f"({r['ticks_per_sec']} ticks/s), dials={r['dials']}")


if __name__ == "__main__":
    main()
