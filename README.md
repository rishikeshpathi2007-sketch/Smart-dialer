# SmartDialer prototype

A working prototype of a collections SmartDialer: progressive and predictive
dialing, a Safety Controller between pacing and the telecom provider, explicit
agent and call state machines, two mock providers that behave differently, and
tests for the failure cases in the assignment.

Design, state machines, failure analysis and scale notes are in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Requirements

* Python 3 (tested on 3.13). No third-party packages.

## Setup

```powershell
git clone https://github.com/rishikeshpathi2007-sketch/Smart-dialer.git
cd Smart-dialer
```

## Run the tests

```powershell
python -m unittest discover -s tests -v
```

## Run the simulation

Runs scenarios A to E (20%, 50%, 70% answer rates; changing rates with a
provider outage; sudden loss of 40% of agents) in both modes:

```powershell
python -m smartdialer.simulate --agents 50 --duration 1800
```

## Run the load test

```powershell
python -m smartdialer.loadtest --agents 1000 --borrowers 20000 --ticks 120
```

## Layout

| File | Role |
|---|---|
| `smartdialer/model.py` | Agent and call state machines, call-event rules, config |
| `smartdialer/store.py` | SQLite persistence, CAS reservations, write-ahead dispatch, snapshots |
| `smartdialer/providers.py` | Provider interface; `provider_a` (fast, reliable) and `provider_b` (slow, timeouts, duplicates, reordering) |
| `smartdialer/pacing.py` | Predictive pacing engine (proposes only) |
| `smartdialer/safety.py` | Safety Controller: sole creator of calls; signed permits; fallback rules |
| `smartdialer/allocator.py` | Call Allocator: sole caller of providers; circuit breaker |
| `smartdialer/dialer.py` | Worker tick: ingest, reconcile, sweep, plan, dispatch |
| `smartdialer/simulate.py` | Scenario runner |
| `smartdialer/loadtest.py` | Reservation race and tick throughput |
| `tests/test_dialer.py` | Unit tests for races, crashes, duplicates, out-of-order events |

## How the pieces connect

```
Campaign > Pacing Engine (propose n) > Safety Controller (decide, sign permit)
         > Call Allocator (dispatch) > Telecom Provider
```

The pacing engine cannot create calls. The controller is the only code that
writes call rows, and the allocator only dials calls that belong to a valid,
unexpired, not-yet-dispatched permit.

## Results

Full run: `python -m smartdialer.simulate --agents 50 --duration 1800`
(50 agents, 30 simulated minutes, seeded and deterministic). Raw output is in
[sim_results.txt](sim_results.txt).

| Scenario | Mode | Utilisation | Dialed | Answered | Connected | Abandoned | Abandon % |
|---|---|---|---|---|---|---|---|
| A: 20% answer, 120s talk | progressive | 47.1% | 2023 | 367 | 367 | 0 | 0.0% |
| A: 20% answer, 120s talk | predictive | 66.6% | 2571 | 524 | 524 | 0 | 0.0% |
| B: 50% answer, 90s talk | progressive | 68.5% | 1411 | 704 | 704 | 0 | 0.0% |
| B: 50% answer, 90s talk | predictive | 73.6% | 1468 | 757 | 757 | 0 | 0.0% |
| C: 70% answer, 180s talk | progressive | 86.5% | 667 | 457 | 457 | 0 | 0.0% |
| C: 70% answer, 180s talk | predictive | 87.5% | 671 | 470 | 464 | 6 | 1.3% |
| D: 70% to 10% to 50%, provider A outage 900-960s | progressive | 63.7% | 1439 | 498 | 498 | 0 | 0.0% |
| D: 70% to 10% to 50%, provider A outage 900-960s | predictive | 72.8% | 2050 | 578 | 566 | 12 | 2.1% |
| E: 50% answer, 40% of agents stop heartbeating at 600s | progressive | 74.0% | 853 | 426 | 426 | 0 | 0.0% |
| E: 50% answer, 40% of agents stop heartbeating at 600s | predictive | 76.8% | 906 | 444 | 444 | 0 | 0.0% |

Scenario E at the assignment's exact size (100 agents, 40 of them drop out),
run separately:

| Mode | Utilisation | Answered | Abandoned | Abandon % | Lost agents detected |
|---|---|---|---|---|---|
| progressive | 74.9% | 869 | 0 | 0.0% | 15s |
| predictive | 79.3% | 915 | 5 | 0.5% | 15s |

Utilisation = agent-seconds in CONNECTED / agent-seconds logged in (WRAP_UP counts as idle).
In scenario E the dialer noticed the lost agents 15s after they stopped
heartbeating (the heartbeat timeout), and cancelled their calls.

**How to read this.** Predictive mode beats progressive on utilisation in every
scenario, most at low answer rates (67% vs 47% at 20%). The abandon rate stays
under the 3% limit in every run. Two caveats:

* The pacing constants (`z`, `horizon`, `connected_weight`, the shock test)
  were tuned against these same scenarios, so this is not a held-out result.
  Scenario D in particular drove the shock-test fix.
* Predictive mode spends much of its time in progressive fallback at higher
  answer rates (for example 304 of 1800 ticks in scenario C). That is the
  safety design working as intended, but it means the utilisation gain is
  smaller there.

### Load test

`python -m smartdialer.loadtest --agents 1000 --borrowers 20000 --ticks 60`:

* Reservation race: 1,000 agents, 8 threads, each with its own connection.
  Every agent was claimed exactly once (`exactly-once=True`, 0.48s).
* Dialer: 1,000 agents, 60 predictive ticks in 13.4s (4.5 ticks/s), single
  process, SQLite on Windows. This is the first thing to fall over at scale:
  see section 8 of the architecture doc.

## Limitations

* SQLite on one host: several worker processes share a correct database, but
  there is no multi-host coordination. See section 8 of the architecture doc.
* The providers are simulated. The `Provider` interface is what a real
  integration would implement.
* The 3% abandon threshold is a design parameter, not a compliance check.
