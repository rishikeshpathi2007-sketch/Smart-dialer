"""Deterministic simulation of a campaign under different conditions.

    python -m smartdialer.simulate            # all scenarios, both modes
    python -m smartdialer.simulate --agents 100 --duration 1800

Every run uses a SimClock, so results are reproducible for a given seed.
"""
from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import dataclass, field

from .clock import SimClock
from .dialer import Dialer
from .model import Config
from .providers import provider_a, provider_b
from .store import Store


@dataclass
class Scenario:
    name: str
    answer_rate: callable          # f(t) -> probability a dialed call is answered
    talk_secs: float = 120.0
    provider_a_outage: tuple | None = None   # (start, end) seconds
    agent_drop: tuple | None = None          # (time, fraction) agents stop heartbeating
    description: str = ""


def scenarios() -> list[Scenario]:
    return [
        Scenario("A: 20% answer, 120s talk", lambda t: 0.20, 120.0, description="low answer rate"),
        Scenario("B: 50% answer, 90s talk", lambda t: 0.50, 90.0, description="medium"),
        Scenario("C: 70% answer, 180s talk", lambda t: 0.70, 180.0, description="high answer rate"),
        Scenario("D: changing 70->10->50%", lambda t: 0.70 if t < 600 else (0.10 if t < 1200 else 0.50),
                 120.0, provider_a_outage=(900, 960), description="changing rates + provider outage"),
        Scenario("E: 50% answer, 40% agents drop", lambda t: 0.50, 120.0,
                 agent_drop=(600, 0.40), description="sudden loss of agents"),
    ]


@dataclass
class Result:
    scenario: str
    mode: str
    utilization: float = 0.0
    dialed: int = 0
    answered: int = 0
    connected: int = 0
    abandoned: int = 0
    decisions: dict = field(default_factory=dict)
    drop_reaction_secs: float | None = None
    # Pacing, averaged over the ticks where the controller was consulted.
    pace_ticks: int = 0
    sum_p_hat: float = 0.0
    sum_proposed: int = 0
    sum_granted: int = 0
    sum_in_flight: int = 0
    sum_supply: float = 0.0

    def _avg(self, total):
        return total / self.pace_ticks if self.pace_ticks else 0.0

    @property
    def avg_p_hat(self) -> float:
        return self._avg(self.sum_p_hat)

    @property
    def avg_proposed(self) -> float:
        return self._avg(self.sum_proposed)

    @property
    def avg_granted(self) -> float:
        return self._avg(self.sum_granted)

    @property
    def avg_in_flight(self) -> float:
        return self._avg(self.sum_in_flight)

    @property
    def avg_supply(self) -> float:
        return self._avg(self.sum_supply)

    @property
    def abandon_rate(self) -> float:
        return self.abandoned / self.answered if self.answered else 0.0


def run(sc: Scenario, mode: str, agents: int = 50, borrowers: int = 20000, duration: int = 1800,
        cfg: Config | None = None, seed: int = 1) -> Result:
    cfg = cfg or Config()
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "sim.db")
    clock = SimClock(0.0)
    s = Store(db, cfg, clock)
    agent_ids = [f"agent{i:04d}" for i in range(agents)]
    for a in agent_ids:
        s.add_agent(a, "C", clock.now())
    for i in range(borrowers):
        s.add_borrower(f"b{i:06d}", "C", f"+1555{i:07d}", clock.now())
    s.close()

    talk = lambda rng: sc.talk_secs * rng.uniform(0.6, 1.4)
    pa = provider_a(clock, sc.answer_rate, seed=seed, talk_secs_fn=talk)
    pb = provider_b(clock, sc.answer_rate, seed=seed + 1, talk_secs_fn=talk)
    dialer = Dialer(db, "C", [pa, pb], mode=mode, cfg=cfg, clock=clock)

    dropped: set[str] = set()
    drop_at = None
    util_num = util_den = 0
    logged_in_hist: list[tuple[float, int]] = []
    res = Result(sc.name, mode)
    for step in range(duration):
        clock.advance(1.0)
        t = clock.now()
        if sc.provider_a_outage:
            pa.outage = sc.provider_a_outage[0] <= t < sc.provider_a_outage[1]
        if sc.agent_drop and drop_at is None and t >= sc.agent_drop[0]:
            n_drop = int(agents * sc.agent_drop[1])
            dropped = set(agent_ids[:n_drop])
            drop_at = t
        alive = [a for a in agent_ids if a not in dropped]
        dialer.store.heartbeat(alive, t)
        dialer.tick()
        if dialer.last_decision is not None:
            decision, info = dialer.last_decision
            res.pace_ticks += 1
            res.sum_p_hat += info.get("p_hat", 0.0)
            res.sum_proposed += info.get("proposed", 0)
            res.sum_granted += decision.permit.granted
            res.sum_in_flight += info.get("in_flight", 0)
            res.sum_supply += info.get("supply", 0)

        counts = dialer.store.counts("C")
        connected = counts.get("CONNECTED", 0)
        logged = agents - counts.get("OFFLINE", 0)
        util_num += connected
        util_den += logged
        if drop_at is not None:
            logged_in_hist.append((t, logged))
        if drop_at is not None and res.drop_reaction_secs is None and logged <= agents - len(dropped):
            res.drop_reaction_secs = t - drop_at

    res.utilization = util_num / util_den if util_den else 0.0
    res.dialed = pa.dial_count + pb.dial_count
    c = dialer.store.conn
    res.answered = c.execute("SELECT COUNT(*) FROM calls WHERE campaign='C' AND answered_at IS NOT NULL").fetchone()[0]
    res.abandoned = dialer.store.abandoned_count("C")
    res.connected = c.execute("SELECT COUNT(*) FROM calls WHERE campaign='C' AND answered_at IS NOT NULL "
                              "AND abandoned=0").fetchone()[0]
    res.decisions = dict(c.execute("SELECT decision, COUNT(*) FROM safety_log WHERE campaign='C' "
                                   "GROUP BY decision").fetchall())
    dialer.close()
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--duration", type=int, default=1800)
    ap.add_argument("--quick", action="store_true",
                    help="a 300-second run per scenario, for a fast check (the full run takes several minutes)")
    args = ap.parse_args()
    if args.quick:
        args.duration = 300
    cfg = Config()
    results = []
    print(f"{'scenario':32s} {'mode':12s} {'util':>6s} {'dialed':>7s} {'answered':>8s} "
          f"{'connected':>9s} {'abandoned':>9s} {'abandon%':>8s}  decisions")
    for sc in scenarios():
        for mode in ("progressive", "predictive"):
            r = run(sc, mode, agents=args.agents, duration=args.duration, cfg=cfg)
            results.append(r)
            extra = ""
            if r.drop_reaction_secs is not None:
                extra = f"  drop reacted in {r.drop_reaction_secs:.0f}s"
            print(f"{sc.name:32s} {mode:12s} {r.utilization*100:5.1f}% {r.dialed:7d} {r.answered:8d} "
                  f"{r.connected:9d} {r.abandoned:9d} {r.abandon_rate*100:7.1f}%  {r.decisions}{extra}")

    print()
    print("Pacing, averaged over the ticks where the controller was consulted:")
    print(f"{'scenario':32s} {'mode':12s} {'avg p-hat':>9s} {'avg proposed':>12s} {'avg granted':>11s} "
          f"{'avg in flight':>13s} {'avg supply':>10s}")
    for r in results:
        print(f"{r.scenario:32s} {r.mode:12s} {r.avg_p_hat:9.3f} {r.avg_proposed:12.1f} {r.avg_granted:11.2f} "
              f"{r.avg_in_flight:13.1f} {r.avg_supply:10.1f}")


if __name__ == "__main__":
    main()
