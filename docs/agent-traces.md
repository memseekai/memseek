# Agent traces

Agent traces let derivations learn from what agents actually did. A traced
Agent's runs export OpenTelemetry spans (prompts, responses, tool calls,
timings) to a trace store you run, such as VictoriaTraces. Memseek stores no
spans.

The store, its setup and what was verified against it are summarized in
[Victoria in Memseek](victoria.md).

## Turn tracing on

1. Run a trace store that accepts OTLP/HTTP, and set `TRACE_EXPORT_URL` to its
   traces endpoint. For a local VictoriaTraces:

    ```sh
    TRACE_EXPORT_URL=http://127.0.0.1:10428/insert/opentelemetry/v1/traces
    ```

2. Add `trace:` to each Agent whose runs you want traced:

    ```yaml
    agents:
      - name: site_scraper
        # ...
        harness: pi
        trace: {content: full}
    ```

`content` is required. `full` sends prompts, responses, tool arguments and
results to the store. `metadata` sends names, statuses, timings and token
counts only. Memseek's erasure cannot delete data held in the store, so choose
`full` only where the store's retention and access suit that data.

An Agent without `trace:` exports nothing.

## What a trace contains

Each run is one trace. Its root span, `invoke_agent <agent>`, is exported by the
worker after the run ends, on every provider, so it exists and records the
outcome even when the harness was killed. It carries:

| Attribute | Value |
|---|---|
| `gen_ai.operation.name` | `invoke_agent` |
| `gen_ai.agent.name`, `gen_ai.request.model` | The Agent and its model alias |
| `memseek.agent_run.id` | The run's id, also on its run record |
| `memseek.workspace`, `memseek.mode` | The workspace, and `derivation` or `invocation` |
| `memseek.origin.*` | The derivation, config hash and Task, or the invocation |
| `error.type` | How a failed run ended: `timeout`, `validation`, `provider`, `cancelled`, ... |

A harness that declares `trace: otlp` exports the spans inside the run under
that root. The environment it receives is listed in `harnesses/README.md`.

## Who sees what

- The trace variables reach only the harness process, never skill packs, and
  the harness strips them from the agent it runs.
- `TRACE_EXPORT_TOKEN` is ingest-only, because harnesses receive it. Keep the
  store's query access separate from ingest in production.

## Run records

Every run of a traced Agent writes one record to the built-in `agent_runs`
collection, on the run's entity, however the run ended: success, failure,
timeout, or cancellation. The runtime writes these records. The public record
API refuses the collection, so a record always names a trace the runtime
recorded.

| Type | When |
|---|---|
| `run_ok` | The run returned a valid result. |
| `run_failed` | The run failed. `content.error_kind` says how: `timeout`, `provider`, `validation`, `budget`, `cancelled`, ... |

`content` holds `agent_run_id`, `trace_id`, `status`, `error_kind`,
`agent{ref, hash, harness, model}`, `origin` (the derivation and Task, or the
invocation), the `skills` mounted for the run, `metrics`, `started_at` and
`ended_at`. It never holds error text: that stays in the run audit.
`agent`, `origin`, `derivation` and `error_kind` are declared filter fields, so
a trigger's `where` can select on them.

A run record sits at derivation depth 0. Its edge to the run that hosted it is
a control edge, like a run's edge to its predecessor, so evidence learned from a
run can feed the next run's skill indefinitely without running out of
`MAX_DERIVATION_DEPTH`. Erasure still follows the edge.

## Learn from runs

A derivation reads run records like any other records, picks the runs it
cares about, and reads their traces with the built-in
[`trace`](derivations.md#built-in-trace) task. What it emits is ordinary,
cited, bounded evidence.

```yaml
name: silent_failures
trigger:
  write: {collections: [agent_runs], types: [run_ok]}
  debounce_s: 30                 # let the trace store index the run's last spans
sources:
  runs: {kind: changes, collections: [agent_runs], types: [run_ok], max_records: 10}
limits: {max_llm_calls: 0, max_judgments: 10, max_judgment_tokens: 200000, max_trace_reads: 10}
tasks:
  - id: episodes
    use: trace
    input: "{{runs.records}}"
    with: {max_chars: 16000, select: {agents: [worker]}}
  - id: review
    use: judge
    input: "{{episodes.items}}"
    with:
      model: jev
      questions:
        accomplished:
          type: noul
          instructions: "Did the agent in `item.text` accomplish the task stated in it?"
      keep: {accomplished: {noul_lte: 0.3}}
      records:
        text: "{{item.text}}"
        content: {kind: silent_failure}
emit: {from: "{{review.records}}", collection: outcomes, type: exception, max_records: 10}
```

Selection is yours: the source's `types`, a trigger's `where`, the `trace`
task's `select`, and the judge's `keep` each narrow it. Jev is one way to judge
an episode; an `llm` task or an installed task works the same way.

`examples/agent_trace_learning/` is a complete loop: a traced pi Agent runs
each task, `failed_runs` and `silent_failures` turn its bad runs into
`outcomes`, and the `skill` derivation proposes reviewed changes to the skill
the Agent mounts on its next run.

Keep `text` an exact `{{item.text}}`: a template that mixes literal text with a
value escapes `<`, `>` and `&` in what it stores.

## Run it locally

```sh
docker compose up -d postgres
docker compose --profile observability up -d
export TRACE_EXPORT_URL=http://127.0.0.1:10428/insert/opentelemetry/v1/traces
export TRACE_STORE_URL=http://127.0.0.1:10428/select/jaeger
```

Run the API and worker on the host, where pi is installed. Read one run's trace
with `curl http://127.0.0.1:10428/select/jaeger/api/traces/<trace_id>`.

### See the traces

Each Agent run's root span links to the derivation job that started it; see
[Following Memseek](observability.md) for job traces and logs.


VictoriaTraces serves its own UI, VMUI, at
`http://127.0.0.1:10428/select/vmui/`. Nothing else needs to run.

- **One run as a timeline.** Open
  `http://127.0.0.1:10428/select/vmui/#/trace?trace_id=<trace_id>`, with the
  `trace_id` from the run record. The root `invoke_agent` span holds the
  harness's `chat` and `execute_tool` spans in order, and a failed step is
  marked. Select a span to see its attributes, including prompts, responses and
  tool arguments when the Agent traces `full` content.
- **Finding runs.** The search page lists traces and has an errored-spans
  view. It takes LogsQL queries over spans:

| Query | Shows |
|---|---|
| `name:invoke_agent*` | One root span per run, with its `memseek.*` attributes |
| `"span_attr:error.type":*` | Every span that failed, and how |
| `"span_attr:gen_ai.operation.name":execute_tool` | Every tool call |
| `"span_attr:memseek.origin.derivation":run_task` | The runs of one derivation |

Quote a field name that contains `:`. New spans can take up to a minute to
show in searches; the timeline and the `trace` Task's reads have them at once.

### Try it without a model

`make traces-test` starts VictoriaTraces and runs one pi run from a scripted
fake pi, with `trace: {content: full}`. It reads the trace back the way the
`trace` Task does, then prints the episode and a link to the run's timeline in VMUI.
It needs the test database (`make database`) and node on PATH.

## Limits

- Memseek's erasure cannot delete what the trace store holds. Bound it with the
  store's retention, and keep `metadata` content where prompts must not leave.
- Only the pi harness exports spans from inside a run today. Other Agents get
  the worker's root span and a run record.
- A run cut off by a lost lease writes no run record, as it writes no run audit.
