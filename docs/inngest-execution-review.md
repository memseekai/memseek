# Inngest execution ideas for Memseek

## Executive conclusion

Memseek should borrow selected execution patterns from Inngest, but it should not adopt Inngest
as its primary workflow engine.

Inngest demonstrates useful patterns for durable step execution, keyed concurrency, queue
fairness, throttling, and execution observability. Those patterns address real opportunities in
Memseek. However, Memseek already owns the domain-specific execution machinery that protects its
core guarantees: PostgreSQL-backed jobs, claim-token fencing, Evaluation Bases, transactional
canonical commits, stale guards, provenance, reviewed Promotion, successor reconciliation,
retries, and dead letters.

Replacing the native worker with Inngest would therefore introduce a second durability authority
without eliminating most of Memseek's existing machinery. It would also move event payloads and
step results into another state store, complicating data residency, erasure, debugging, and the
atomic relationship between a job transition and a canonical database commit.

The recommended direction is:

1. Make Memseek's configured concurrency real and prevent one execution lane from starving
   another.
2. Add content-free execution measurements so later changes are driven by evidence.
3. Add deployment-wide provider flow control only when multi-worker deployments or provider
   quotas require it.
4. Add durable Task checkpoints only when repeated work in multi-Task Pipelines becomes a
   material cost or reliability problem.
5. Treat workspace-visible Task outputs as a separate product and privacy decision, even if the
   underlying checkpoints are implemented.

Inngest itself is not a required dependency for any of these improvements.

## What Inngest contributes

The relevant Inngest capability is its execution model rather than its event SDK.

### Durable steps

An Inngest function is divided into named steps. A successful step result is persisted and reused
when a later step fails, so the retry resumes at the failed step instead of repeating all earlier
work. This is particularly valuable when earlier steps contain slow or expensive model calls.

This pattern maps naturally to Memseek Pipeline Tasks, with an important qualification: a
checkpoint at the Task seam can resume between Tasks, but it cannot resume individual operations
inside one Task. A future `research` Task containing several model/tool iterations would need its
own internal checkpoint seam to resume in the middle of that loop.

### Flow control

Inngest distinguishes several controls:

- **Concurrency** limits simultaneously executing steps and can be keyed by account, tenant, or
  another resource.
- **Throttling** queues excess runs and starts them later at a controlled rate.
- **Rate limiting** is intentionally lossy: excess runs are skipped rather than queued.
- **Debouncing** waits for a quiet period and executes with the latest event.
- **Priority** adjusts which queued work is selected first.
- **Batching** groups related events before execution.

For Memseek, concurrency, queued throttling, and fairness are the useful ideas. Lossy rate
limiting is generally a poor fit for canonical background work because accepted work should not
silently disappear.

### Observability

Inngest exposes queue and function health, failure rates, throughput, traces, and per-step timing.
Memseek already records detailed immutable run receipts and structured logs, but it does not yet
provide the same aggregate view of queue pressure, scheduling delay, slot utilization, checkpoint
reuse, or provider wait time.

### Capabilities Memseek already has

Several Inngest features already have deep Memseek equivalents and should not be reimplemented
under new names:

| Inngest pattern | Existing Memseek behavior |
| --- | --- |
| Idempotent events | Record dedupe keys and job stimulus dedupe |
| Function retries | Claim-fenced retry with bounded exponential backoff |
| Failure handling | Dead-lettered jobs and failed run receipts |
| Debounce | `quiet` triggers and coalesced entity/derivation mailboxes |
| Scheduled work | Persisted cron scans with bounded catch-up |
| Batching | Enrichment batches and bounded backfill batches |
| Event coalescing | One active derive job per workspace, derivation, and entity |
| Cancellation | Cancellable backfill handles at batch boundaries |
| Durable progress | Backfill cursors, derivation cursors, and successor jobs |
| Step/run audit | Evaluation Basis, Task trace hashes, model attempts, and output IDs |

## Memseek's current execution model

Memseek uses PostgreSQL as both the canonical data store and the durable execution authority. A
job is claimed with `FOR UPDATE SKIP LOCKED`, an attempt count, a lease deadline, and a unique
claim token. Heartbeats, completion, retry, dead-letter, and not-ready release all require that
exact token and a live lease. A worker that loses its lease cannot commit stale work.

For derivations, the worker resolves an Evaluation Basis, runs declared Tasks, compiles a
Candidate Set, rechecks guarded reads and active heads, writes the run and outputs, completes the
job, and queues any successor in guarded transactions. This locality is important: moving queue
ownership outside PostgreSQL would not remove the need for these checks or transactions.

The relevant implementation areas are:

- `src/memseek/jobs.py`: claiming, leases, retry, dead-letter, wait, and job status.
- `src/memseek/worker.py`: lane ordering, heartbeat wrappers, and job dispatch.
- `src/memseek/derive/runner.py`: Evaluation Basis resolution, sequential Task execution,
  Candidate Set compilation, and atomic commit.
- `src/memseek/llm/runtime.py`: process-local model concurrency.
- `src/memseek/config.py`: declared worker, index, model, search, and Task concurrency settings.

## Findings

### 1. Configured worker concurrency is not effective concurrency

The specification says derive and cron work use `WORKER_CONCURRENCY` and projection work uses
`INDEX_CONCURRENCY`. The current worker does not create those numbers of concurrent job handlers.
Instead, `run_worker_once()` calls lane drain functions sequentially, and each major drain function
claims and executes one job at a time in an unbounded `while True` loop.

`WORKER_CONCURRENCY` and `INDEX_CONCURRENCY` currently influence database-pool sizing, but they do
not produce the corresponding number of simultaneous derivation or projection executions inside
one worker process.

This is a specification/implementation gap, not merely an optional optimization.

### 2. Lane starvation is possible

The worker currently drains projection jobs before derivations, drains all currently runnable
derivations before backfills, and then drains projections again. If a lane is continually
replenished, later lanes may wait for an unbounded period:

- A sustained projection backlog can delay derivations.
- A derivation that continually queues successors can delay backfills.
- Static lane order acts as an implicit priority system, but it has no fairness quantum.

Claim ordering inside a lane is oldest-first across the whole deployment. It provides reasonable
FIFO behavior, but it does not prevent a workspace with a large backlog from consuming most
available slots in a multi-tenant deployment.

### 3. Job lifecycle policy lacks locality

Claiming and generic transitions live in `jobs.py`, heartbeat behavior lives in `worker.py`, and
some handlers complete or retry their jobs inside domain modules so the transition can share a
transaction with canonical changes. Derivation failure persistence also transitions the job,
after which the outer worker attempts to classify and transition the same failure again and
relies on lease loss to make the second transition harmless.

The current behavior is fenced, but the policy is spread across several modules. A deeper
execution scheduler should own the common lifecycle while allowing a handler to complete its
lease inside a domain transaction when atomicity requires it.

### 4. Task retries repeat completed work

Pipeline Tasks execute sequentially in memory. Task traces retain hashes, timing, provenance,
citations, and model-call metadata, but Task values are not persisted. If Task 3 fails after Tasks
1 and 2 succeed, the next job attempt repeats Tasks 1 and 2.

This is safe, and the specification explicitly permits repeated external work after a process
crash. It can nevertheless be expensive and introduce additional nondeterminism when completed
Tasks made model or external-search calls.

### 5. Provider concurrency is process-local

The LLM and search semaphores constrain concurrent calls within one process. They do not create a
deployment-wide limit. For example, three worker processes with `LLM_MAX_CONCURRENCY=8` may attempt
up to 24 concurrent model calls.

There is also no queued requests-per-period throttle. Concurrency alone cannot smooth bursts or
guarantee compliance with a provider's per-minute quota.

### 6. Aggregate execution measurements are limited

Memseek records strong per-job and per-run evidence, including attempts, timings, token use,
selected inputs, Task hashes, output IDs, and bounded errors. Structured worker events include
some queue-lag and pass counters. What is missing is a stable aggregate picture of:

- Queue depth and oldest runnable age by lane.
- Time spent waiting for an execution slot.
- Active and idle slot utilization.
- Retry and dead-letter rates by job kind.
- Task latency and repeated work caused by retries.
- Provider wait time, throttling, and quota failures.
- Checkpoint hits, misses, and avoided model work if checkpoints are added.

## What the improvements would enable

### Fair concurrent scheduling

A bounded concurrent scheduler would enable:

- Actual use of the configured worker and index capacity.
- Several independent, I/O-bound derivations to make progress simultaneously.
- Bounded service opportunities for every lane rather than drain-to-empty ordering.
- More predictable latency for live derivations while projections or backfills are busy.
- Explicit, testable execution policy instead of implicit priority in call order.
- Cleaner horizontal scaling because a process claims only work for which it has a free slot.

With four non-index slots, a worker could have up to four independent jobs executing rather than
one. For workloads dominated by provider latency, throughput may approach the number of available
slots until a database, provider, or configured flow limit becomes the bottleneck. This is a
theoretical ceiling, not a promise of a four-times production improvement. The more reliable
benefit is latency isolation: a runnable lane waits for a scheduling round and slot availability,
not for another lane's entire backlog to drain.

### Durable Task checkpoints

Task checkpoints would enable:

- Retrying only the failed Task and the Tasks that follow it.
- Avoiding repeated model cost for already completed Tasks.
- Reducing retry latency and variation between attempts.
- Inspecting the exact validated intermediate values that influenced later Tasks.
- Safely resuming a dead job much later when its Evaluation Basis and all relevant hashes still
  match.

The work avoided on each retry is:

```text
sum(cost of every completed Task before the failure)
```

For three similarly expensive model Tasks where the third Task fails once, Task-level replay
avoids repeating the first two successful calls. It does not eliminate the possibility of a
duplicate external call when a process crashes after receiving a provider response but before
persisting the checkpoint.

### Provider flow control

Deployment-wide provider controls would enable:

- One real concurrency ceiling across all worker processes.
- Queued throttling instead of repeated `429` failures and retry storms.
- Per-workspace shares that prevent one tenant from consuming all provider capacity.
- Independent limits for different resolved provider connections.
- Predictable interaction between live derivations, enrichment, and bulk backfills.

### Execution telemetry

Content-free measurements would enable operators to answer:

- Whether work is waiting because of lane capacity, provider capacity, readiness, cooldown, or
  failures.
- Whether increasing concurrency improves throughput or merely moves the bottleneck.
- Whether retries repeat enough expensive Task work to justify checkpoints.
- Whether a workspace or job kind is acting as a noisy neighbor.
- Whether provider throttling should delay work or more capacity should be provisioned.

## What is required

| Improvement | Assessment | Reason |
| --- | --- | --- |
| Adopt Inngest | **Not required** | It duplicates the execution authority and weakens transaction locality. |
| Fair concurrent scheduler | **Required if the current specification stands** | The implementation does not currently honor its concurrency contract and can starve lanes. |
| Minimal execution telemetry | **Required with the scheduler** | Concurrency changes need observable queue, latency, utilization, and failure effects. |
| Provider-wide flow control | **Conditional** | Needed for multiple workers, recurring quota failures, or tenant fairness. |
| Task checkpoints | **Conditional** | Valuable when multi-Task retries repeat material cost or long Pipelines become common. |
| Durable raw Task inspection | **Optional product feature** | Helpful for debugging, but expands sensitive-data, authorization, storage, and erasure scope. |

The scheduler should not wait for checkpoints. Checkpoints should not be implemented merely
because Inngest has durable steps; their value depends on actual Pipeline shapes and retry cost.
Provider flow control similarly should follow deployment topology or observed provider pressure.

## Recommended gated implementation plan

### Phase 1: measure and correct scheduling

1. Add content-free measurements for queue depth, oldest runnable age, queue lag, slot wait,
   active slots, job duration, retry, dead-letter, and lane completion.
2. Introduce one deep execution scheduler module that owns capacity, fair lane selection,
   heartbeat supervision, shutdown, and common outcome classification.
3. Make `WORKER_CONCURRENCY` the actual capacity for derive, cron, retention, and backfill jobs.
4. Make `INDEX_CONCURRENCY` the actual capacity for index upsert and delete jobs.
5. Treat enrichment as a bounded lane with at most one unit scheduled per round.
6. Offer each non-empty lane at most one new claim before rotating to the next lane. Never drain a
   lane to empty as the unit of scheduling.
7. Claim only when the relevant lane has a free local slot, preserving the rule that a job must
   not spend its lease waiting on a local semaphore.
8. Preserve claim-token fencing and allow domain handlers to complete a job inside the same
   transaction as canonical changes.
9. Centralize retry/dead-letter classification so every failure receives one transition.
10. Benchmark one and four non-index slots with representative derivation, projection,
    enrichment, and backfill workloads.

Do not add persistent per-workspace scheduling state in the first scheduler slice unless tests
show that bounded lane rotation and concurrent FIFO claims still permit unacceptable noisy-neighbor
behavior. If they do, add a cluster-wide dispatch cursor per `(workspace, lane)` and claim from the
least-recently-served workspace with runnable work before selecting its oldest job.

### Phase 2: add deployment-wide provider flow control when triggered

Start this phase when at least one of the following is true:

- The deployment runs more than one worker process.
- Providers regularly return quota or `429` errors.
- A backfill measurably harms live derivation latency.
- One workspace can monopolize provider capacity.

Then:

1. Add a PostgreSQL-backed flow-control module at the provider Adapter seam.
2. Use leased concurrency permits keyed by the resolved provider connection.
3. Support an optional second per-workspace/provider limit for tenant fairness.
4. Add GCRA-style queued throttling with limit, period, and burst settings.
5. Treat limits as operational bindings excluded from definition and semantic hashes.
6. Apply the same controls to enrichment and Pipeline model calls.
7. When a job cannot acquire start capacity, release it until the calculated eligibility time
   without charging an execution attempt.
8. Do not use lossy rate limiting for accepted canonical background work.

If Task checkpoints do not yet exist, a provider wait must occur before beginning the Task so no
completed Task work is discarded merely to release the job.

### Phase 3: add Task checkpoints when retry waste justifies them

Start this phase when telemetry shows repeated expensive work, or when multi-Task Pipelines become
a central workload.

#### Checkpoint identity and reuse

A checkpoint is reusable only when all of these match:

- Workspace and job ID.
- Exact Evaluation Basis manifest hash.
- Engine/build version.
- Pipeline definition and resolved configuration hashes.
- Task ordinal, ID, selected Adapter, and implementation hash.
- Validated rendered Task input and configuration hash.
- Hash chain of all upstream Task results.

Reuse is limited to the same job row, including an explicit retry of that dead job. A newly
enqueued job always evaluates current data and never uses old checkpoints as a cross-job cache.
A reference to `{{run.now}}`, a changed view result, a changed model binding, or any changed input
naturally changes the rendered-input or configuration hash and causes a miss.

#### Stored data

Persist, after each successful Task:

- The validated JSON-compatible output.
- Source and citation record IDs.
- Output and hash-chain identities.
- Task timing and trace deltas.
- Model attempt metadata and token usage attributable to the original execution.
- The job claim and basis identities needed to fence the write.

Do not persist rendered prompts, rendered Task configuration, credentials, or raw provider
responses. Enforce a hard serialized Task-output bound before persistence.

Replayed usage must not be counted as newly incurred usage. A later run receipt should identify
the checkpoint origin and distinguish original cost from work reused during the current attempt.

#### Run linkage and erasure

- Link a checkpoint to every failed or successful run attempt that used it rather than copying
  its value into each run record.
- Keep checkpoints durable, with no automatic TTL, if the selected durable-replay policy is
  adopted.
- Delete pending and completed checkpoints whose source set intersects an erasure closure.
- Cascade run/job retention into checkpoint-link cleanup.
- Report a missing replay value honestly if erasure removed it after the run receipt was written.

#### Inspection surface

If workspace-visible inspection is approved as a product feature, add:

- `GET /runs/{run_id}/tasks` for bounded, paginated Task summaries.
- `GET /runs/{run_id}/tasks/{task_id}` for the validated Task output, provenance, hashes, timing,
  usage, and reuse metadata.
- Matching SDK methods such as `run_tasks()` and `run_task()`.

Authorization must be workspace-scoped. Responses must obey the global response bound. Inputs and
configuration remain hash-only, and prompts and raw provider exchanges remain unavailable.

### Phase 4: evaluate further controls only from evidence

After the first three phases, evaluate—but do not pre-commit to—dynamic priority, operator job
cancellation, pausing a derivation, or finer-grained checkpoints inside complex Tasks. Each adds a
new authored or operational contract and should address an observed workload rather than imitate
the full Inngest feature set.

## Recorded checkpoint decisions

If the checkpoint phase proceeds, the selected direction is:

- **Retention:** durable rather than transient.
- **Visibility:** authenticated workspace APIs may inspect validated Task outputs.
- **Reuse scope:** the same job and exact Evaluation Basis/hash chain only.
- **Replay meaning:** retry and inspection, not historical recommit.
- **Cross-job caching:** prohibited.
- **Historical writes:** prohibited; a new run must evaluate current state.
- **Erasure:** retained Task values derived from erased evidence must be removed.
- **Disclosure:** validated outputs may be returned; prompts, rendered configuration, secrets, and
  raw provider responses may not.

These choices deliberately change the current run-audit posture, which stores Task hashes rather
than Task values. Before implementing the inspection surface, record that policy change in an ADR
covering retention, authorization, erasure, and the sensitive-data footprint.

## Test plan

### Scheduler correctness

- Configure four worker slots and prove that four eligible independent jobs can execute
  simultaneously without a fifth being claimed.
- Configure one index slot and prove projection jobs remain serial while non-index jobs continue.
- Continuously replenish projections and prove derivations still start.
- Continuously replenish derivations and prove backfill, cron, retention, and enrichment receive
  scheduling opportunities.
- Run multiple worker processes and prove `SKIP LOCKED` and claim tokens prevent duplicate
  ownership.
- Lose a lease during work and prove no stale operation can complete or commit.
- Stop the worker and prove active handlers and heartbeat tasks shut down without orphaned local
  tasks or invalid job transitions.
- Inject a handler exception and prove it causes exactly one retry or dead-letter transition.

### Scheduler performance

- Compare one and four worker slots on provider-latency-dominated derivations.
- Report throughput, median and tail queue latency, database-pool utilization, provider
  concurrency, retry rate, and error rate.
- Repeat with mixed projections, derivations, enrichment, and backfill work.
- Verify that raising concurrency stops helping once the provider or database becomes the
  bottleneck instead of assuming linear scaling.

### Provider flow control

- Run several worker processes and prove the deployment-wide concurrency ceiling is never
  exceeded.
- Prove throttled calls are delayed and later executed rather than discarded.
- Prove provider waits do not charge a job attempt.
- Prove expired permits are recoverable after a crashed worker.
- Prove per-workspace shares prevent one workspace from consuming every provider slot.
- Prove distinct provider connections do not unintentionally share limits.

### Task checkpoints

- Complete Task 1, fail Task 2, retry, and prove Task 1 is not invoked again.
- Change each identity input independently—basis, definition, configuration, implementation,
  rendered input, engine version, and upstream output—and prove reuse is rejected.
- Prove a checkpoint write requires the live job claim token.
- Prove erased sources prevent reuse and remove retained Task values.
- Prove replayed model usage is not reported as newly incurred cost.
- Prove failed and successful attempts can reference one checkpoint without duplicating it.
- Prove a new job with otherwise identical inputs cannot use another job's checkpoint.
- Prove oversized Task results fail within the declared execution bound.
- Prove workspace APIs cannot read another workspace's Task values.
- Prove prompts, credentials, rendered configuration, and raw provider responses never appear in
  storage, logs, or Task inspection responses.

## Rollout

1. Ship content-free measurements first and establish baseline queue and provider behavior.
2. Ship the scheduler as an additive worker change with the existing job schema and public APIs.
3. Run the full suite with effective concurrency `1` to prove semantic equivalence.
4. Enable the configured default concurrency in staging and compare it with the baseline.
5. Roll out concurrency gradually while monitoring database connections, provider errors, queue
   latency, and lease loss.
6. Add provider flow-control storage and settings only when its trigger conditions are met.
7. Add checkpoint tables additively; existing jobs and runs simply have no checkpoint details.
8. Gate raw Task inspection on the separate policy/ADR review.

A rollback to an older binary must be able to ignore additive scheduler, limiter, or checkpoint
tables. No migration should reinterpret or rewrite canonical records.

## Acceptance criteria

The execution work is successful when:

- Configured worker and index concurrency correspond to actual simultaneous execution.
- A continuously replenished lane cannot prevent another runnable lane from progressing.
- Jobs are never claimed merely to wait for a local execution slot.
- No operation commits after lease loss, and no failure receives two state transitions.
- Evaluation Basis, Candidate Set, provenance, Promotion, and successor guarantees are unchanged.
- Benchmarks report measured throughput and latency rather than claiming a fixed multiplier.
- Multi-process provider limits are enforced when that phase is enabled.
- A retried multi-Task Pipeline can reuse completed Tasks only under the exact same job and hash
  chain.
- Erasure removes retained intermediate values derived from erased evidence.
- No Inngest SDK, server, event store, or external execution dependency is introduced.

## Assumptions and boundaries

- PostgreSQL remains Memseek's only durable execution and canonical commit authority.
- Pipeline Task order remains sequential; existing bounded `foreach` concurrency is unchanged.
- External model and search operations remain at-least-once around a crash between the external
  response and the local durable write.
- Fair scheduling changes execution order but not Pipeline semantics or canonical write rules.
- Provider limits are operational bindings, not semantic definition identity.
- Durable Task values are operational replay data, not canonical memory and not valid inputs to
  unrelated jobs.
- The current job, run, SDK, freshness, and canonical record interfaces remain backward
  compatible; any new fields or Task-inspection routes are additive.
- Dynamic priority, general job cancellation, function pausing, and historical recommit are out of
  scope until justified independently.

## Sources

### Inngest documentation

- [How Inngest functions execute: durable execution](https://www.inngest.com/docs/learn/how-functions-are-executed)
- [Error handling and retries](https://www.inngest.com/docs/guides/error-handling)
- [Flow control overview](https://www.inngest.com/docs/guides/flow-control)
- [Concurrency management](https://www.inngest.com/docs/guides/concurrency)
- [Throttling](https://www.inngest.com/docs/guides/throttling)
- [Debounce](https://www.inngest.com/docs/guides/debounce)
- [Batching](https://www.inngest.com/docs/guides/batching)
- [Function priority](https://www.inngest.com/docs/guides/priority)
- [Cancellation](https://www.inngest.com/docs/features/inngest-functions/cancellation)
- [Function versioning](https://www.inngest.com/docs/learn/versioning)
- [Observability and metrics](https://www.inngest.com/docs/platform/monitor/observability-metrics)
- [Platform usage limits](https://www.inngest.com/docs/usage-limits/inngest)
- [Security and stored step output](https://www.inngest.com/docs/learn/security)
- [Self-hosted architecture](https://www.inngest.com/docs/self-hosting)

### Memseek sources of truth

- `CONTEXT.md`: Pipeline, Task, Evaluation Basis, Candidate Set, and Promotion vocabulary.
- `DECISIONS.md`: implemented execution and evolution decisions.
- `spec/memseek-spec-v3.2-agentic-data-substrate.md`: normative job, concurrency, and fencing
  guarantees.
- `docs/operations.md`: current operator controls and diagnostics.
- `docs/derivations.md`: Pipeline execution and Task authoring.
- `docs/triggers.md`: coalescing, quiet periods, cron, and successor behavior.
- `docs/evaluation-bases.md`: run receipts, Task trace hashes, and guarded commits.
- `src/memseek/jobs.py`: durable job transitions.
- `src/memseek/worker.py`: current lane dispatcher.
- `src/memseek/derive/runner.py`: Task execution and transactional derivation commit.
- `src/memseek/llm/runtime.py`: process-local provider concurrency.
