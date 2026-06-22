# Engineering Notes / Retrospective

> The key design decisions, the tradeoffs behind them, and what I'd do next. The reader-facing
> summary of the simulation model lives in the README; this is the deeper rationale.

## Why a simulation (and what `step` means)

This is a **simulation**, not a live request/response service. A live service is reactive — a
request arrives, you process it, you return a result, there is no global clock. This project
instead *models* such a service running over a stretch of time, so it can answer questions you
cannot answer one-request-at-a-time: what happens when thousands of SOS requests arrive at once on
limited spectrum; whether the system avoids congestion collapse under sustained overload; whether
throughput recovers when the next-gen fleet comes online mid-run.

`step` is the simulation's clock — a discrete tick index (`0, 1, 2, …`), each representing a slice
of real time `config.time_step_seconds` long. Everything that happens in a tick shares that `step`,
which is why it threads through almost every function. Components use it for deadlines/expiry
(`step > deadline_step`), waiting time (`step - arrival_step`), and time-gated state (a fleet that
comes online at `online_at_step`, or starts failing at `fail_from_step`).

Two reasons it can't be purely one-request-at-a-time:

1. **Resource contention is inherently a batch decision.** Ten requests want one beam with room for
   three — you can't decide request #1 without knowing the others exist. Admission control and
   fairness *are* decisions over the batch competing for the same capacity in the same instant.
   `step` defines that batch; `RequestSource.poll(step)` is "give me this slice's batch," exactly
   like a Kafka consumer polling a time window.
2. **Determinism.** A seeded run keyed by `step` reproduces identically and tests in well under a
   few seconds.

The design is therefore **stateless workers, stateful conductor**: the ports/adapters are
effectively pure functions `f(inputs, step) → result` that hold no memory between ticks (so `step`
is *passed in*, never read from a wall clock — keeping them pure and deterministic), while the
`ControlLoop` holds everything that must persist across ticks (retry queue, current Tier-3 plan,
congestion history, RNG). That cross-tick state is precisely what makes congestion, backpressure,
and capacity-change-over-time observable. A real deployment would be event-driven (a
`KafkaRequestSource` behind the same port); `step` would then track real time windows instead of
simulated ones — no core changes required.

## Why ports & adapters (hexagonal)

The control logic (admission, scheduling, optimization, resilience) is the valuable, testable
part. It depends only on `typing.Protocol` interfaces in `src/satsim/ports/`. Every external
system — constellations, the request transport, the optimizer, telemetry sinks — sits behind one
of those ports with a license-free fake. Consequences:

- The entire system runs, tests, and containerizes with **zero external services**.
- Tests assert behavior against in-memory fakes in well under a second, deterministically.
- A real adapter (e.g. a Kafka consumer, a live constellation client) can be slotted in later
  without touching the core.

`Protocol` (structural typing) over ABCs: fakes and real adapters satisfy a port by shape, no
inheritance required, and it stays `mypy --strict` clean.

## Three-tier scheduling architecture

- **Tier 1 — deterministic synchronous control loop** is the authoritative engine. Discrete
  time steps + seeded RNG make every run reproducible, which is what makes the scenarios testable.
- **Tier 2 — reactive emergency lane** exists because life-safety traffic can't wait for ordinary
  batch processing, but *also* can't be allowed to consume everything. It is a preempt lane
  **within the same logical step** (not a thread — determinism preserved) with bounded reserved
  per-beam capacity, and it runs emergency-class admission control (severity, wait time, retry
  count, signal quality, geographic fairness). This is the core fairness-vs-efficiency tension in
  miniature: protect life-safety, but bound it so a surge can't cause congestion collapse.
- **Tier 3 — periodic global optimizer** does the planning the per-step loop can't afford every
  tick: recompute the admission `score→admit_probability` curve, fairness weights/airtime budgets,
  and fleet/beam assignment hints. Running it on a slower cadence is itself an efficiency choice.

## One scorer, three consumers

A request's score, estimated cost, and delivery probability are computed **once** by a pure
`RequestScorer` (not a port — deterministic, no external dependency). The same `RequestScore`
feeds the admission curve (score → admit probability), the congestion estimate (offered load =
Σ estimated cost), and the scheduler (ordering + delivery probability). Computing it in any one
of those places and re-deriving it in the others invites drift — admission and the scheduler
silently disagreeing about how urgent a request is — so it lives in exactly one place.

## Tier-2 first, Tier-1 spillover (urgent traffic)

Urgent classes go through the Tier-2 emergency lane first. The single priority guarantee lives
there (bounded reserved capacity + emergency-class admission control). Requests the lane sheds
fall through to Tier-1 admission as ordinary high-scored load — Tier 1 has no separate emergency
carve-out. Two carve-outs would be two places encoding the same priority, free to disagree.

## Control plane vs. MAC layer

The simulator is a **resource-management control plane** sitting above the link layer; it
abstracts PHY/MAC into "airtime units." The fluid schedulers (`HeuristicScheduler`,
`PriorityFairScheduler`) treat a beam as a scalar capacity. That's the right altitude for the
questions it answers (admission, fairness, congestion), and it deliberately omits SINR,
interference, per-symbol modeling, and slot/subchannel structure.

`SlotMacScheduler` adds an optional **MAC-level** model behind the *same* `ResourceScheduler`
port: each beam becomes a slot×subchannel **resource-block (RB)** grid, and how many RBs a request
needs comes from **MCS link adaptation** (payload bits ÷ bits-per-RB at the MCS its link supports —
better link → higher MCS → fewer RBs). It maps RBs back to airtime units so utilization/fairness
stay comparable, and the control loop is unchanged — selecting it is one config flag
(`scheduler = slot_mac`). This is the hexagonal payoff: a different abstraction level slots in
behind one interface. A fuller MAC (explicit cell placement, SINR/interference, HARQ) would extend
this same adapter; those remain non-goals for the control-plane focus.

## UI-readiness (e.g. Streamlit)

The system is built to be driven programmatically by a future parameter-sweep UI: construct a
`SimulationConfig` (every tunable is a typed field), call `build_simulation(config, sink).run()`,
and read results from the sink (`InMemoryTelemetrySink.steps` per-step series + the `RunSummary`).
No global state, fully deterministic per seed — so a Streamlit front-end can expose sliders for
config fields, run, and chart served/dropped/utilization/collapse-risk over steps without touching
the core. The MAC RB grid is also visualization-friendly (a slot×subchannel occupancy heatmap).

## Configuration & settings

All behavior is parameterized — there are no magic numbers buried in algorithm code. Tunables
live in typed, validated pydantic sub-models in `config.py`: `ScoringConfig` (score weights +
airtime cost model), `SchedulerConfig` (per-request cap, degrade floor), `EmergencyConfig`
(reservation + emergency-ranking weights), `OptimizerConfig` (cadence, util clamp, fairness
bounds, solver limit), `ArrivalConfig` (rates, link-quality range, surge jitter). Each component
reads its slice; defaults equal the original constants so behavior is unchanged.

**Two layers, on purpose.** Deep tunables stay in `SimulationConfig` (code-built, scenario-set).
Common *run* knobs — seed, steps, scheduler, optimizer backend, solver limit, verbosity — are
exposed via `RunSettings` (pydantic-settings) read from environment / `.env` with the
`SATSIM_` prefix. Precedence is **CLI flag > env/.env > scenario default**, and only env-set
fields (`model_fields_set`) count as overrides, so a `.env` never silently clobbers a scenario's
intentional choices. This keeps the env surface small and the algorithm config strongly typed.

## Why admission control sits *before* scheduling

Shedding load early (probabilistically, by score) keeps the scheduler's working set bounded and
prevents retransmission storms / congestion collapse. The scheduler then optimizes allocation of
what's already been admitted — it never has to be the thing that says "no" under overload.

## Kafka-style ingestion, but deterministic

Production ingestion was async/event-based (Kafka). Here that's modeled the hexagonal way: a
`RequestSource` consume-port with an `InMemoryBus` fake topic. The `DemandGenerator` publishes
(producer); the loop polls per step (consumer). This keeps the production framing — topic,
consumer lag (= queue depth), backpressure (= bounded queue + admission shedding) — while keeping
the core synchronous and deterministic. A full asyncio core was rejected: it would destroy
reproducibility for no architectural gain, since the transport is already abstracted. A real
`KafkaRequestSource` is a documented, unimplemented extension point.

## Optimizer: a *coupled* MILP, solver primary, heuristic fallback

Tier-3 planning is a MILP solved with OR-Tools (Apache-2.0), config-selectable, with a
deterministic `HeuristicOptimizer` behind the same port used (a) on solver timeout — part of the
resilience story — and (b) as the fast-test default so core tests never depend on the solver.

The model is deliberately **coupled** (an earlier version was two weakly-linked subproblems):

- **Variables:** binary region→fleet assignment `x[r,f]`, continuous per-(region, class) budgets
  `y[r,c]`, and per-region congestion slack `u[r]`.
- **The coupling:** `Σ_c y[r,c] ≤ Σ_f cap[r,f]·x[r,f]` — a region's budgets are bounded by its
  *assigned* fleet's capacity, so picking a weak fleet for a high-demand region actually costs
  served airtime. Assignment and demand are no longer independent.
- **Objective:** maximize priority-weighted served airtime + a fairness bonus for under-served
  classes + a demand-pressure-weighted assignment term, minus a congestion penalty (airtime above
  a soft utilization threshold) and a switching penalty (changing a region's fleet between runs,
  to avoid handoff thrashing). *Drop penalty is intentionally not a separate term: drop = demand −
  served with demand constant, so maximizing served already minimizes priority-weighted drops.*

This per-region/per-class formulation measurably improves capacity use over the heuristic
(utilization roughly doubled on `mixed_roadmap` in practice) because budgets land where demand is.
Per-region budgets and fleet hints are still **advisory** to the Tier-1 scheduler today (the loop
consults fairness weights + the admission curve); making them binding is the main next step.

## Fairness vs. efficiency (and why utilization is modest)

The system deliberately favors **value over raw throughput**. Under heavy mixed load, admission
sheds low-score traffic proportionally (~`1/util` floor) and the scheduler *degrades* degradable
classes (PHOTO/MAPS) to a reduced allocation rather than packing beams with cheap bulk. The
consequence is that average beam utilization is modest — that's a policy choice, not a bug:

- **Efficiency would say**: fill every beam, even with low-value bulk → high utilization.
- **Value/fairness says**: protect life-safety + high-value traffic, bound any single user's
  airtime (per-request caps), keep classes proportionally represented (Tier-3 fairness weights),
  and shed/degrade the rest so the system never tips into congestion collapse.

Degradation is the compromise that keeps degradable traffic *served* (partially) instead of
dropped, so capacity isn't wasted while still honoring priority. Tuning the admission floor or the
degrade fraction slides the system along the efficiency↔value axis.

## What I'd do next

- **Make Tier-3 budgets/hints binding.** `airtime_budgets` and `fleet_hints` are currently
  advisory (the scheduler doesn't receive the plan); only fairness weights + the admission curve
  feed back. The loop could enforce per-class budgets and prefer hinted fleets.
- **Congestion-driven safe mode.** Safe mode is fault-driven today; `OverloadConfig.collapse_*`
  thresholds could trip it proactively under sustained collapse risk.
- **A real `KafkaRequestSource`** behind the existing `RequestSource` port (no core changes).
- **Distributed/regional scheduler**: shard the control plane per region with the global optimizer
  coordinating cross-region fairness and spectrum budgets.
