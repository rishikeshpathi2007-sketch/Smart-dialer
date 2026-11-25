# SmartDialer: decision record

**Status:** accepted for the prototype. **Date:** 25 November 2026.
**Detail:** the overview is in [ARCHITECTURE.md](ARCHITECTURE.md), and the full reasoning is in [DESIGN-DETAILS.md](DESIGN-DETAILS.md).

## Context

Collections agents waste time on calls that do not connect. Predictive dialing fixes that by starting calls before
an agent is free, but an answered call with no agent is abandoned, which is a compliance problem. The system must
raise utilisation without weakening that safety property.

## Decisions

### 1. Predictive dialing, with progressive dialing as the fallback

- **Decision:** dial ahead using an answer-rate estimate. Fall back to progressive when the abandon rate over the
  last 100 outcomes is above 3%, or when the last 30 outcomes show a significant surge in answers.
- **Why:** predictive gives the utilisation gain (the simulation shows it in every scenario). Progressive is the
  known-safe mode, so it is the place to fall back to.
- **Costs:** more code paths. Fallback reduces utilisation while it lasts.

### 2. The Safety Controller is the only component that creates calls

- **Decision:** pacing proposes a number. The controller re-reads state inside its own transaction and decides. Only
  calls written by the controller can be dialled.
- **Why:** the safety property becomes a data-flow fact, not a convention. Pacing has no write path to calls and no
  provider handle.
- **Costs:** every decision takes a database transaction, so the controller is on the hot path.

### 3. Signed, single-use permits

- **Decision:** each decision produces an HMAC-signed permit. The allocator dials only calls covered by a valid,
  unexpired, unspent permit.
- **Why:** a permit is a capability. Forging or replaying one is detected.
- **Costs:** the secret lives in one process. Several hosts would need a shared key service and an atomic
  dispatched flag in shared storage.

### 4. SQLite (WAL) with compare-and-set claims

- **Decision:** one SQLite database as the shared source of truth. Every claim is a compare-and-set in a
  transaction.
- **Options considered:** Postgres (the same SQL pattern, more operations work), Redis (fast, but claims would
  need Lua scripts and a second source of truth), Kafka (ordering and replay, but no help with atomic claims).
- **Why:** correct claims with no extra service, and the same SQL moves to Postgres unchanged.
- **Costs:** one writer. The load test shows the bottleneck near 1,000 agents on one host, and there is no
  multi-host coordination.

### 5. Events are idempotent and monotonic; unknown outcomes are reconciled by query

- **Decision:** each provider event is stored once by ID, in the same transaction as its state change. A call only
  moves forward. The reconciler asks the provider about calls that go quiet, using the call ID as the idempotency
  key.
- **Why:** providers duplicate and reorder events, and they time out after placing calls.
- **Costs:** final state is order-independent, but side effects such as agent claims are not. The LOST rule can
  hide a real failure.

### 6. Upper bound in the controller, mean in pacing

- **Decision:** pacing uses the estimated answer rate to aim for utilisation. The controller re-checks with the upper
  confidence bound, so uncertainty only ever reduces the number of calls.
- **Why:** utilisation and safety need different error directions.
- **Costs:** the conservative bound lowers utilisation. The constants (z, horizon, weights) were tuned on the same
  five scenarios, so they are not held out.

### 7. Python standard library only

- **Decision:** no third-party packages.
- **Why:** the prototype runs with no installation, so a reviewer can run it immediately.
- **Costs:** no static types enforced, and no async I/O. Throughput is limited by Python and SQLite together.

### 8. Providers behind an interface, with two mock behaviours

- **Decision:** the dialer uses a `Provider` interface. `provider_a` is fast and reliable. `provider_b` times out,
  duplicates events, reorders them and sometimes places a call after an ambiguous timeout.
- **Why:** the dialer should not depend on any one provider's details, and the failure modes are what the design
  must survive.
- **Costs:** mocks show the design handles these behaviours, not that a real provider behaves this way.

### 9. Simulated time for tests and simulation

- **Decision:** a `SimClock` drives every test and simulation.
- **Why:** runs are deterministic, and timing bugs reproduce.
- **Costs:** real clock jitter, network latency and clock skew between hosts are not modelled.

## Deliberately not done

- **Fencing tokens.** A slow agent declared lost can still act. Needed before production.
- **A shared circuit breaker.** Each worker learns about outages separately. Simpler, but slower to react.
- **Kafka, Redis or microservices.** Not needed at this scale. Each would add operating cost without removing the
  atomic-claim problem.

## Revisit when

- Load testing (see the README) shows a host can no longer keep up with the agent count.
- The system runs on more than one host.
- A real telecom provider replaces the mocks.
