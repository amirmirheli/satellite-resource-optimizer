# Satellite Resource Optimization Simulator

A simulation of a satellite **resource-optimization control plane** — the kind of system that
allocates scarce radio resources (time slots, frequency, beam capacity) to many ground devices
requesting service via satellite (Emergency SOS, Messages, Find My, Roadside), across a network
**mid-migration between two heterogeneous constellations**.

It is a portfolio / learning project. It demonstrates admission control, scheduling under
scarcity, fairness-vs-efficiency tradeoffs, regulatory constraints, graceful degradation, and
observability — built as a **hexagonal (ports & adapters)** codebase where the control logic
depends only on interfaces and every external system sits behind a port with a **license-free
fake**, so the whole thing runs, tests, and containerizes with **zero external services**.

## Why a simulation (and what `step` means)

This is a **simulation**, not a live request/response service. A live service is reactive — a
request arrives, you process it, you return a result, there is no global clock. This project
instead *models* such a service running over a stretch of time, so it can answer questions you
cannot answer one-request-at-a-time: what happens when thousands of SOS requests arrive at once on
limited spectrum; whether the system avoids congestion collapse under sustained overload; whether
throughput recovers when the next-gen fleet comes online mid-run.

`step` is the simulation's clock — a discrete tick index (`0, 1, 2, …`), each representing a slice
of real time `config.time_step_seconds` long. Everything in a tick shares that `step`, which is
why it threads through almost every function (deadlines `step > deadline_step`, waiting time
`step - arrival_step`, and time-gated fleet state like `online_at_step` / `fail_from_step`).

Two reasons it can't be purely one-request-at-a-time:

1. **Resource contention is a batch decision.** Ten requests want one beam with room for three —
   you can't decide request #1 without knowing the others exist. Admission control and fairness
   *are* decisions over the batch competing for the same capacity in the same instant. `step`
   defines that batch; `RequestSource.poll(step)` is "give me this slice's batch," exactly like a
   Kafka consumer polling a time window.
2. **Determinism.** A seeded run keyed by `step` reproduces identically and tests in a few seconds.

The design is therefore **stateless workers, stateful conductor**: the ports/adapters are
effectively pure functions `f(inputs, step) → result` that hold no memory between ticks (so `step`
is *passed in*, never read from a wall clock), while the `ControlLoop` holds everything that must
persist across ticks (retry queue, current Tier-3 plan, congestion history, RNG). A real
deployment would be event-driven (a `KafkaRequestSource` behind the same port); `step` would then
track real time windows instead of simulated ones — no core changes required.

## Architecture (three-tier scheduling)

```
                          ┌─────────────────────────────────────────────┐
   DemandGenerator ──publish──▶  InMemoryBus  (fake Kafka topic)         │
   (Poisson + surges)            │                                       │
                                 │ poll(step)                            │
                                 ▼                                       │
   ┌──────────────────────────── Tier 1: deterministic control loop ────┴───────────┐
   │  ingest ─▶ [Tier 2: emergency lane] ─▶ score ─▶ admission ─▶ regulatory ─▶ sched │
   │   (RequestSource)   reserved per-beam   (once)   (shed via    (legal     (priority│
   │   bounded queue     capacity + emergency        score→prob)   fleet,band) /fair/  │
   │   + backoff/retry   admission control)                         degrade) cost)     │
   │                                                                                  │
   │  resilience: scheduler fallback → safe-mode · circuit-breaker → fleet failover   │
   │  telemetry: per-decision events + per-step counters ─────────────────────────────┐
   └──────────────────────────────────────────────────────────────────────────────┐ │
                         ▲ consults ResourcePlan                                     │ │
   ┌─────────────────────┴─ Tier 3: periodic global optimizer (every N steps) ──────┘ │
   │  Optimizer port:  SolverOptimizer (OR-Tools MILP)  ──timeout──▶  HeuristicOptimizer│
   │  emits ResourcePlan: admission curve · fairness weights · fleet/beam hints         │
   └───────────────────────────────────────────────────────────────────────────────────┘
```

- **Tier 1 — primary deterministic control loop.** Authoritative per-step engine; every
  allocation is decided here, reproducibly (seeded RNG). Owns the ingest pipeline, the retry queue
  (exponential backoff + jitter), and disposition (served / deferred / dropped / rejected).
- **Tier 2 — hybrid reactive emergency lane.** In-step preemptive fast path with bounded reserved
  per-beam capacity. Runs *emergency-class admission control* (severity, wait time, retry count,
  signal quality, geographic fairness) so a mass-casualty surge prioritizes life-safety without
  starving the system or monopolizing a beam. Lane-shed urgent traffic spills into Tier-1.
- **Tier 3 — periodic global optimization layer.** Every *N* steps, behind an `Optimizer` port,
  produces a `ResourcePlan` (admission curve, fairness weights, per-region/class budgets, fleet
  hints). The OR-Tools backend is a **coupled MILP** — binary region→fleet assignment plus
  continuous per-(region, class) budgets bounded by the *assigned* fleet's capacity — maximizing
  priority-weighted served airtime with fairness, congestion, and fleet-switching terms.
  Deterministic heuristic fallback on timeout. (See [`NOTES.md`](NOTES.md) for the full model.)
  A third backend, `AdaptiveOptimizer`, *learns* the admission curve online instead of
  recomputing it: it keeps a per-score-bucket admit probability as state and nudges it from the
  **realized** outcome of the curve it last broadcast — raising buckets that shed traffic while
  beams sat idle (the "rejecting with capacity to spare" case the offered-load formula can't see)
  and lowering them under congestion collapse, with the top of the curve pinned admit-all so
  high-value traffic is never shed. Select via `SATSIM_OPTIMIZER_BACKEND=adaptive`.

### Ports (the dependency boundary)

Each is a `typing.Protocol` with a license-free fake adapter (a real adapter could replace it):

| Port | Default adapter | Responsibility |
|---|---|---|
| `RequestSource` | `SyntheticRequestSource` | Kafka-style `poll(step)` ingestion |
| `ConstellationClient` | `FakeConstellation` ×2 | visible satellites/beams per fleet |
| `RegulatoryPolicy` | `TableRegulatoryPolicy` | per-region spectrum legality + caps |
| `AdmissionController` | `ProbabilisticAdmissionController` | Tier-1 load shedding |
| `EmergencyAdmission` | `EmergencyLane` | Tier-2 reserved-capacity lane |
| `ResourceScheduler` | `HeuristicScheduler` / `PriorityFairScheduler` / `SlotMacScheduler` / `ContentionMacScheduler` | beam allocation + degradation (fluid, granted MAC RB-grid, or random-access MAC) |
| `Optimizer` | `SolverOptimizer` / `HeuristicOptimizer` / `AdaptiveOptimizer` | Tier-3 global planning |
| `TelemetrySink` | `ConsoleTelemetrySink` / `InMemoryTelemetrySink` | structured observability |

## Requirements

- Python 3.11+ (3.12 pinned via `.python-version`).
- [uv](https://docs.astral.sh/uv/) for environment + dependency management.

## Quickstart

```bash
uv python install 3.12   # one-time: install the pinned interpreter
uv sync                  # create venv + install deps (incl. dev group)

# Run a scenario and print its telemetry summary:
uv run satsim                       # list scenarios
uv run satsim emergency_surge       # run one
uv run satsim mixed_roadmap --steps 60 --seed 7
uv run satsim constellation_fault --verbose   # stream per-step telemetry

# Quality gates:
uv run ruff check .              # lint
uv run mypy                      # type-check (strict)
uv run pytest -m "not solver"    # fast suite (fakes + heuristic optimizer)
uv run pytest -m solver          # solver tests (OR-Tools)
```

## Scenarios

Each scenario is a runnable `SimulationConfig` (see [`scenarios/`](src/satsim/scenarios/catalog.py)):

| Scenario | Demonstrates |
|---|---|
| `emergency_surge` | Thousands of SOS at once on limited spectrum; life-safety stays reliable |
| `poor_link_vs_good_link` | Bounded opportunity + airtime caps so poor-link users can't hog beams |
| `mixed_roadmap` | All classes competing; heavy best-effort degrades/backpressures, priority holds |
| `capacity_expansion` | Next-gen fleet comes online mid-run; throughput rises afterwards |
| `regulatory_denial` | A region licensed to no constellation → requests rejected (no legal option) |
| `constellation_fault` | A fleet starts failing → circuit breaker trips, traffic fails over |

## Interactive UI (Streamlit)

An optional Streamlit front-end lets you vary parameters and run the simulation interactively —
built as a thin shell over the UI-free [`experiment.py`](src/satsim/runtime/experiment.py) API:

```bash
uv sync --group ui                       # install the optional UI dependency
uv run --group ui streamlit run streamlit_app.py
```

Sidebar widgets pick a base scenario and override seed, duration, scheduler, optimizer backend,
load, link quality, emergency reservation, queue capacity, and an SOS surge. Adjust as many as you
like, then click **▶ Run simulation** — the (potentially expensive) run is gated behind the button
rather than firing on every widget change, and a one-line summary of each run is logged to the
terminal hosting `streamlit run`. The page then shows:

- **Headline metrics** — served / dropped / rejected / backlog, utilization, peak collapse risk.
- **"Is the idle capacity wasted?"** — demand pressure (offered ÷ capacity), the by-design /
  structural share of rejections, and scarcity-drops-while-idle, plus a utilization-vs-demand
  overlay — the evidence that modest utilization is a value/coverage outcome, not fumbled capacity.
- **Serve latency** — p50 / p95 / p99 / max end-to-end wait (the tail the average hides).
- **Per-step charts** — dispositions, system health (utilization/collapse-risk/fairness/queue
  depth), served-by-class, and rejections by reason.

The app imports nothing private — it builds a `SimulationConfig`, calls `run_experiment(config)`,
and charts the returned summary + per-step series — so the same API drives parameter-sweep scripts
or any other front-end.

**Deploy it** — to Streamlit Community Cloud (free, hosted) or as a Docker image
([`Dockerfile.ui`](Dockerfile.ui) / [`docker-compose.yml`](docker-compose.yml)). Step-by-step
runbook in [`DEPLOY.md`](DEPLOY.md). Quick container run:

```bash
docker compose up --build      # then open http://localhost:8501
```

## Configuration

Two layers, by design:

- **Deep tunables** live in typed, validated pydantic sub-models in
  [`config.py`](src/satsim/config.py): `ScoringConfig`, `SchedulerConfig`, `EmergencyConfig`,
  `OptimizerConfig`, `ArrivalConfig`, `OverloadConfig`, per-class `ClassProfile`s, fleet/region
  tables. No magic numbers in algorithm code — every constant is a defaulted, documented field.
- **Run-level knobs** come from the environment / `.env` via `pydantic-settings`
  ([`settings.py`](src/satsim/settings.py)), prefixed `SATSIM_`:

  ```bash
  SATSIM_SEED=7
  SATSIM_STEPS=120
  SATSIM_SCHEDULER=heuristic            # heuristic | priority_fair | slot_mac | contention_mac
  SATSIM_OPTIMIZER_BACKEND=solver       # heuristic | solver | adaptive
  SATSIM_SOLVER_TIME_LIMIT_S=0.5
  SATSIM_VERBOSE=false
  ```

  Precedence is **CLI flag > env/`.env` > scenario default**, and only env-*set* fields count as
  overrides — a `.env` never silently clobbers a scenario's intentional choices. Copy
  [`.env.example`](.env.example) to `.env` to use it.

## Project layout

```
src/satsim/
  domain/        pure dataclasses + enums (no I/O) — the vocabulary across ports
  ports/         interfaces only (typing.Protocol) — the dependency boundary
  adapters/      license-free fakes, grouped by capability:
    scheduling/    fluid schedulers (HeuristicScheduler, PriorityFairScheduler)
    mac/           RB-grid schedulers (SlotMacScheduler, ContentionMacScheduler)
    optimization/  Tier-3 planners (Heuristic / Solver / Adaptive, build_optimizer)
    admission/     Tier-1 ProbabilisticAdmissionController + Tier-2 EmergencyLane
    network/       FakeConstellation + TableRegulatoryPolicy (the environment)
    io/            SyntheticRequestSource + telemetry sinks
  runtime/       services that compose the layers:
    loop.py        ControlLoop (Tier 1) + build_simulation composition root
    experiment.py  UI-free run + chart-data API (drives the Streamlit app)
    cli.py         scenario runner entry point
  scenarios/     named scenarios → SimulationConfig (catalog.py)
  config.py      typed, validated simulation config (pydantic)
  settings.py    run-level env/.env settings (pydantic-settings)
  scoring.py     RequestScorer — one source of truth for score/cost/delivery-prob
  demand.py      DemandGenerator (Poisson baseline + scheduled surges)
  bus.py         InMemoryBus (fake Kafka-style topic)
  resilience.py  CircuitBreaker
  rng.py         seedable RNG (determinism)
streamlit_app.py interactive UI (optional `ui` dependency group)
tests/           unit + integration + scenario tests (all on fakes, sub-second)
```

## Testing

- `uv run pytest -m "not solver"` — the fast default path: everything on in-memory fakes with the
  heuristic optimizer, fully deterministic, well under a few seconds.
- `uv run pytest -m solver` — additionally exercises the OR-Tools `SolverOptimizer`.
- CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs ruff + mypy `--strict` + both
  test paths on `uv`.

## Notes & caveats

- **Docker:** the `Dockerfile` is written to spec but was **not built/verified locally** (no
  Docker daemon on the dev machine).
- **OR-Tools** is the only non-trivial runtime dependency. The default test/CI path uses the
  zero-dependency `HeuristicOptimizer`, so it stays fast and deterministic; the solver is exercised
  only by `solver`-marked tests and is config-selectable at runtime.
- **Utilization is a value choice.** Under heavy mixed load the system prioritizes high-value
  traffic and degrades/sheds cheap bulk rather than packing beams — so average utilization is
  modest by design, not by accident. See [`NOTES.md`](NOTES.md) for the fairness-vs-efficiency
  discussion.

See [`NOTES.md`](NOTES.md) for the engineering retrospective (design decisions and tradeoffs).
