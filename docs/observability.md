# Following Memseek

Memseek's worker and API write one JSON object per log line, and every
derivation job becomes a trace. This page sets up VictoriaLogs and
VictoriaTraces to collect both, and shows how to follow a job from one to the
other.

For the whole integration in one place, including what was verified against
the real stores, see [Victoria in Memseek](victoria.md).

Nothing here is required to run Memseek. Without it, the logs go to stdout and
the run audit in `_system` stays the record of what each job did.

## Start the stores

```sh
docker compose --profile observability up -d
```

This starts four services:

| Service | What it does | UI |
|---|---|---|
| `victorialogs` | Stores log lines, 30 days | <http://127.0.0.1:9428/select/vmui/> |
| `vector` | Ships Memseek's log lines to `victorialogs` | none |
| `vmalert` | Evaluates alert rules on the log lines every minute | <http://127.0.0.1:8880/vmalert/alerts> |
| `victoriatraces` | Stores spans, 30 days | <http://127.0.0.1:10428/select/vmui/> |

`--profile logs` starts the first three, and `--profile traces` only the
last. All three listen on loopback and have no auth.

### Where the logs come from

Vector collects from two places:

- **Containers.** The `api`, `worker`, `migrate` and `setup` containers of
  `make up`, read through the Docker socket.
- **Host processes.** Every `*.log` file in `./logs`. An API or worker that runs
  on the host, as the `local` Computer provider needs, writes there with `tee`:

```sh
mkdir -p logs
uv run memseek worker 2>&1 | tee -a logs/worker.log
uv run uvicorn memseek.api:app --port 8000 2>&1 | tee -a logs/api.log
```

Each line keeps its own fields. Vector adds `service`, which is the compose
service or the log file's name. Lines that are not JSON, such as Uvicorn's
access log, arrive with `level` set to `raw`.

### Exporting traces

Set the export URL on the worker:

```sh
export TRACE_EXPORT_URL=http://127.0.0.1:10428/insert/opentelemetry/v1/traces
```

With it set, every derivation job exports its trace. Agents with a `trace`
block also export their runs; see [Agent traces](agent-traces.md).

## What a derivation job looks like

A job's trace is built from its run audit once the audit is written, so the
trace always matches the `_system` run record.

- **The trace id is the run id**, without dashes. Run
  `9273fb3a-f906-47bf-a633-dd2f1fd03648` is trace
  `9273fb3af90647bfa633dd2f1fd03648`.
- **The root span** is `derive <derivation>`. It carries the run's status,
  error kind, trigger reasons, input and output counts, model calls and
  tokens.
- **One child span per Task**, `task <id>`, placed at the time it ran. It
  carries the Task's `use` and its model-call count.

Spans carry identities and timings only: no prompts, records or error text.

### Links between traces

A job's trace links to related traces instead of containing them. Each link
has a `memseek.link` attribute that says why.

| On span | Links to | `memseek.link` |
|---|---|---|
| `derive <name>` | each job that wrote a record this job read | `input` |
| `task <id>` of an `agent` Task | the Agent run it started | `agent_run` |
| `task <id>` of a `trace` Task | each Agent run whose trace it read | `trace_read` |
| `invoke_agent <agent>` | the derivation job that started the run | `derivation` |

In the trace view, open a span's **Links** tab and choose **Open linked
trace**. In a chain like the [agent trace learning example](agent-traces.md),
you can start at a `skill` job and follow `input` links back to the
`silent_failures` job, then to the `run_task` jobs, then to each Agent run.

## From a log line to its trace and back

Every line a derivation job writes carries `trace_id` and `span_id`. These
include `run.completed`, `derive.completed`, and `job.retry_scheduled` or
`job.dead_lettered`.

- **From a log line**, open
  `http://127.0.0.1:10428/select/vmui/#/trace?trace_id=<trace_id>`.
- **From a trace**, query its lines in the logs UI:

```text
trace_id:=9273fb3af90647bfa633dd2f1fd03648 | sort by (_time)
```

## Useful queries

In the logs UI:

| Query | Shows |
|---|---|
| `level:error \| stats by (event) count()` | What is failing, by kind |
| `event:=job.dead_lettered \| stats by (workspace, kind, error_kind) count()` | Dead letters |
| `event:=job.claimed \| stats by (workspace, kind) count()` | Work done, per workspace and kind |
| `event:=derive.completed \| stats by (derivation) count(), sum(output_count)` | Derivation throughput |
| `event:=projection.lag \| stats max(queue_lag_ms)` | The slowest projection job |
| `event:=trace.export_failed` | Spans the worker could not send |

In the traces UI:

| Query | Shows |
|---|---|
| `name:"derive skill"` | Every run of one derivation |
| `"span_attr:memseek.run.status":failed` | Failed derivation runs |
| `"span_attr:memseek.derivation":run_task AND duration:>30s` | Slow runs of one derivation |

A loop where one derivation keeps re-running on the same entity shows up as
`job.claimed` lines for it many times a minute.

## Alerts

`vmalert` evaluates
[observability/vmalert-rules.yaml](https://github.com/memseekai/memseek/blob/main/observability/vmalert-rules.yaml)
against the last minute of log lines, every minute:

| Alert | Fires when | Severity |
|---|---|---|
| `DerivationFailingRepeatedly` | One derivation in one workspace logs more than 3 retries or dead letters in a minute | critical |
| `DerivationDeadLettered` | A derivation job gives up | warning |
| `TraceExportFailing` | Any span export fails | warning |
| `AgentRunRecordsNotWritten` | Any Agent run record cannot be written | critical |
| `ProjectionLagging` | A projection job waited more than 60 s in the queue | warning |

A single job retries at most a few times with growing backoff, so repeated
failures within a minute mean new jobs keep failing. That is how a derivation
that re-triggers itself shows up.

The service sends no notifications: active alerts show in its UI and at
`/api/v1/alerts`. To deliver them, replace `-notifier.blackhole` in
`docker-compose.yml` with `-notifier.url` pointing at an Alertmanager.

A rule whose `stats` has no `by` always returns one row, even a count of 0, so
it needs a `| filter count:>0` style condition, or it fires all the time.

## Limits

- Only derivation jobs are traced. Processor, projection and invocation jobs
  write log lines but no job trace; a traced Agent's invocation run still
  exports its own trace.
- A job's trace is exported after its run record is written. A job cut off by a
  lost lease writes neither.
- `job.claimed` is logged before the job's run id exists, so it carries
  `job_id` but no `trace_id`.
