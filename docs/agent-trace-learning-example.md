---
title: "Teach an agent from its own traces"
eyebrow: Guide
---

In this guide an AI agent answers finance questions for one customer,
Northwind Traders, from the raw webhooks their payment processor sent. It gets
some of them wrong, as agents do. Memseek records every step it takes as a
trace, reviews the runs against the payout statement the way a finance lead
would, finds the cause of each wrong answer in the trace and the data, and
writes the fix into the skill the agent loads next time. Then we ask new
questions and the agent gets them right.

By the end you will have:

- watched one agent run step by step in a trace viewer, down to each model call
  and tool call;
- seen a judgment model, Jev, catch a wrong answer that looked perfectly
  confident;
- read the diagnosis that pins the mistake on one event in the data;
- reviewed and promoted the skill change it led to;
- seen the next questions answered correctly, and followed the whole chain
  from the skill change back to the run that caused it.

Nothing here is specific to payments. Any agent you run in Memseek can be
traced and taught the same way.

The example lives in `examples/agent_trace_learning/`, and the script that
drives it is `examples/agent_trace_learning.py`.

## The loop

<figure class="trace-loop">
<div class="frame">
<svg viewBox="0 0 910 560" role="img" aria-label="A question and the account's payment webhooks go to run_task, which runs the agent under the pi harness. It writes an answer, one agent_runs record per run, and sends every step as spans to the trace store. silent_failures reads each run's trace, has Jev check the answer against the payout statement, and diagnoses wrong answers from the steps and the data. The diagnoses become outcomes, the skill derivation drafts a skill from them, and once a person promotes it the agent loads it on its next run.">
<defs>
<marker id="tl-a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="ah" d="M0,0 L10,5 L0,10 z"/></marker>
<marker id="tl-at" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="ah-t" d="M0,0 L10,5 L0,10 z"/></marker>
<marker id="tl-al" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="ah-l" d="M0,0 L10,5 L0,10 z"/></marker>
</defs>
<line class="e" x1="150" y1="198" x2="198" y2="198" marker-end="url(#tl-a)"/>
<text class="lbl" x="174" y="189" text-anchor="middle">question</text>
<line class="e" x1="150" y1="278" x2="198" y2="278" marker-end="url(#tl-a)"/>
<text class="lbl" x="174" y="269" text-anchor="middle">data</text>
<path class="e" d="M295,150 V64 H438" marker-end="url(#tl-a)"/>
<text class="lbl" x="366" y="56" text-anchor="middle">answer</text>
<line class="e" x1="390" y1="230" x2="438" y2="230" marker-end="url(#tl-a)"/>
<text class="lbl" x="414" y="221" text-anchor="middle">1 per run</text>
<line class="e" x1="590" y1="230" x2="638" y2="230" marker-end="url(#tl-a)"/>
<text class="lbl" x="614" y="221" text-anchor="middle">run_ok</text>
<line class="e" x1="765" y1="86" x2="765" y2="128" marker-end="url(#tl-a)"/>
<text class="lbl" x="773" y="112">reference</text>
<line class="e" x1="795" y1="332" x2="795" y2="378" marker-end="url(#tl-a)"/>
<text class="lbl" x="803" y="360">wrong</text>
<line class="e" x1="795" y1="436" x2="795" y2="468" marker-end="url(#tl-a)"/>
<text class="lbl" x="803" y="456">evidence</text>
<line class="e" x1="720" y1="502" x2="632" y2="502" marker-end="url(#tl-a)"/>
<text class="lbl" x="676" y="494" text-anchor="middle">drafts</text>
<line class="e-t" x1="330" y1="310" x2="330" y2="378" marker-end="url(#tl-at)"/>
<text class="lbl-t" x="338" y="350">spans · OTLP</text>
<path class="e-t" d="M610,408 H660 V334" marker-end="url(#tl-at)"/>
<text class="lbl-t" x="652" y="364" text-anchor="end">trace Task reads the run</text>
<path class="e-l" d="M480,515 H240 V312" marker-end="url(#tl-al)"/>
<text class="lbl" x="372" y="546" text-anchor="middle">a person reviews and promotes it; the agent loads it as its skill on the next run</text>
<rect class="box" x="20" y="170" width="130" height="56" rx="6"/>
<text class="nm" x="85" y="194" text-anchor="middle">tasks</text>
<text class="sub" x="85" y="212" text-anchor="middle">the questions</text>
<rect class="box" x="20" y="250" width="130" height="56" rx="6"/>
<text class="nm" x="85" y="274" text-anchor="middle">payment_events</text>
<text class="sub" x="85" y="292" text-anchor="middle">the account's data</text>
<rect class="der" x="200" y="150" width="190" height="160" rx="8"/>
<text class="nm" x="295" y="186" text-anchor="middle">run_task</text>
<text class="sub" x="295" y="210" text-anchor="middle">agent Task on a local Computer</text>
<text class="sub" x="295" y="228" text-anchor="middle">pi harness · gpt-6-luna</text>
<text class="sub" x="295" y="246" text-anchor="middle">loads its learned skill</text>
<text class="lbl-t" x="295" y="272" text-anchor="middle">trace: full</text>
<rect class="box" x="440" y="40" width="150" height="48" rx="6"/>
<text class="nm" x="515" y="61" text-anchor="middle">task_results</text>
<text class="sub" x="515" y="78" text-anchor="middle">the answers</text>
<rect class="box" x="440" y="200" width="150" height="60" rx="6"/>
<text class="nm" x="515" y="225" text-anchor="middle">agent_runs</text>
<text class="sub" x="515" y="243" text-anchor="middle">built in · runtime writes</text>
<rect class="ref" x="655" y="30" width="220" height="56" rx="6"/>
<text class="nm" x="765" y="54" text-anchor="middle">payouts</text>
<text class="sub" x="765" y="72" text-anchor="middle">the statement; the agent never sees it</text>
<rect class="jev" x="640" y="130" width="250" height="202" rx="8"/>
<text class="ti" x="765" y="154" text-anchor="middle">silent_failures · the review</text>
<line class="rule" x1="652" y1="166" x2="878" y2="166"/>
<text class="nm" x="654" y="190">1 episodes</text>
<text class="sub" x="654" y="207">trace Task writes each run out</text>
<line class="rule" x1="652" y1="218" x2="878" y2="218"/>
<text class="nm" x="654" y="242">2 review</text>
<text class="sub" x="654" y="259">Jev checks it against the statement</text>
<line class="rule" x1="652" y1="270" x2="878" y2="270"/>
<text class="nm" x="654" y="294">3 diagnose</text>
<text class="sub" x="654" y="311">LLM finds the cause in steps and data</text>
<rect class="box" x="720" y="380" width="150" height="56" rx="6"/>
<text class="nm" x="795" y="404" text-anchor="middle">outcomes</text>
<text class="sub" x="795" y="422" text-anchor="middle">diagnoses</text>
<rect class="der" x="720" y="470" width="150" height="64" rx="8"/>
<text class="nm" x="795" y="494" text-anchor="middle">skill</text>
<text class="sub" x="795" y="512" text-anchor="middle">LLM drafts the method</text>
<text class="sub" x="795" y="527" text-anchor="middle">+ statement + data</text>
<rect class="box" x="480" y="476" width="150" height="52" rx="6"/>
<text class="nm" x="555" y="498" text-anchor="middle">skills</text>
<text class="sub" x="555" y="515" text-anchor="middle">draft sections</text>
<rect class="store" x="300" y="380" width="310" height="56" rx="8"/>
<text class="ti" x="455" y="404" text-anchor="middle">Trace store · VictoriaTraces</text>
<text class="sub" x="455" y="423" text-anchor="middle">every span: prompts, responses, tool I/O</text>
</svg>
</div>
<figcaption>Three derivations around one agent. The teal path is what tracing adds: every step of every run goes to the trace store, and the review reads it back one run at a time.</figcaption>
</figure>

Every part of the example, and where it lives:

| Part | What it does | File |
| --- | --- | --- |
| The data | 23 webhook deliveries and the October payout statement | `data/northwind-payments-2026-10.json` |
| The agent | A pi agent that traces every run, with `trace: {content: full}` | `agent/agent.yaml` |
| Its instructions and skill | The standing instructions, and `learned_skill`, rendered from the reviewed `skills` records | `agent/artifacts.yaml` |
| `run_task` | Runs the agent on each new question, with the webhooks as its input | `agent/run_task.yaml` |
| `silent_failures` | Reads each successful run's trace, judges the answer, diagnoses the wrong ones | `learning/silent_failures.yaml` |
| `failed_runs` | Reads the trace of each run that errored or ran out of time, and has Jev name the cause | `learning/failed_runs.yaml` |
| `skill` | Drafts the skill change from the diagnoses | `learning/skill.yaml` |

## Before you start

You need:

- the Memseek repository with Docker, `uv` and `make` working. If you have not
  run Memseek before, follow [Getting started](getting-started.md) first;
- Node.js 22.19 or later, and the pi coding agent:
  `npm i -g --ignore-scripts @earendil-works/pi-coding-agent`;
- an OpenAI API key, for the agent and the LLM steps;
- a TypeSafe API key from the [TypeSafe console](https://console.typesafe.ai/),
  for Jev.

The agent runs on the `local` Computer provider, which runs pi on your
machine as your user. That is what lets you watch everything, and it is also
why this is for development only: the agent can read your files.

## Run the example

1. Put both keys in the `.env` file at the root of the repository:

   ```sh
   OPENAI_API_KEY=sk-...
   TYPESAFE_API_KEY=ts_...
   ```

2. Start the database and the observability stores:

   ```sh
   docker compose up -d --wait postgres
   make observability
   ```

   `make observability` starts VictoriaTraces for spans, VictoriaLogs and
   Vector for log lines, and vmalert for alerts, and prints their addresses.

3. In two terminals, start the API and the worker on your machine with tracing
   on. The worker must be on the host because it starts pi:

   ```sh
   set -a; . ./.env; set +a
   export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/memseek
   export PROVIDER_OPENAI_COMPAT_API_KEY="$OPENAI_API_KEY"
   export TRACE_EXPORT_URL=http://127.0.0.1:10428/insert/opentelemetry/v1/traces
   export TRACE_STORE_URL=http://127.0.0.1:10428/select/jaeger
   mkdir -p logs
   docker compose exec postgres psql -U postgres -d memseek \
     -c "create extension if not exists vector"
   uv run memseek migrate
   ```

   ```sh
   # terminal A
   uv run uvicorn memseek.api:app --port 8000 2>&1 | tee -a logs/api.log
   ```

   ```sh
   # terminal B
   caffeinate -i uv run memseek worker 2>&1 | tee -a logs/worker.log
   ```

   `tee` writes the log lines where Vector ships them to VictoriaLogs.
   `caffeinate` keeps a Mac awake: a sleeping laptop freezes every timer, and a
   run then looks hung. On Linux, leave it out.

4. In a third terminal, with the same exports, run the demo:

   ```sh
   make trace-learning-demo
   ```

It takes about five minutes. Without `MEMSEEK_API_KEY` set, the script creates
a fresh workspace each time. Add `ARGS=--no-promote` to stop after the first
cycle and promote the skill yourself.

## The data and its traps

Northwind Traders' payment processor sends a webhook for every charge, refund
and failure. Memseek holds each delivery as one record in `payment_events`,
keyed by its delivery id. The agent gets all of them as its input and nothing
else.

The data looks simple, and it has the traps real webhook data has:

| Trap | In the data | A careless answer |
| --- | --- | --- |
| Amounts are in cents | `"amount": 4800` is €48.00 | is a hundred times too high |
| A retried delivery repeats an event | `evt_0005` arrives as `dlv_0005` and `dlv_0006` | counts €860.00 twice |
| Test-mode events | `ch_t1` and `ch_t2` have `"livemode": false` | adds €1,499.00 of test charges |
| Refunds are their own events | `charge.refunded` for `re_01` and `re_02` | forgets to subtract them, or subtracts a test refund |
| Delivery time is not event time | `evt_0020` was created at 23:58:40 UTC on 30 September and delivered on 1 October | counts a September charge in October |

The processor's payout statement says what October really was: 12 charges,
€2,037.98 gross, 2 refunds of €83.99 in all, and €1,953.99 net. It lives in
`payouts`. The reviewers check answers against it. The agent never sees it.

Summing every charge delivery, cents as euros, gives €4,779.98, more than
twice the right gross.

## Cycle 1: the agent answers

The script asks two questions, one record each in `tasks`:

```text
Q1. What did Northwind Traders collect in October 2026, net of refunds?
Q2. How many successful charges did Northwind Traders take in October 2026,
    and what was their total in euros?
```

Each new task triggers `run_task`. Its `agent` Task gives the agent the
question and every webhook delivery, in `inputs/input.json`, and starts it
under the pi harness.

### What the agent sees

Inside its Computer the agent has:

- `/.memseek/instructions.md`: who it is and what to return;
- `/.memseek/skills/task-skill/SKILL.md`: its skill, rendered from the
  account's active `skills` records. On the first cycle it is only a header;
- `/inputs/input.json`: the question and the 23 deliveries.

### What the trace records

The agent is declared with `trace: {content: full}`. Before the run starts,
the worker gives it a trace id. The pi harness then sends one span to
VictoriaTraces for every model call and every tool call, with the prompt, the
response, the tool's arguments and its result. When the run ends, the worker
sends the run's root span with the outcome. See [Agent traces](agent-traces.md)
for what each span holds.

The script prints a link to each run's trace. This is what the first cycle
looked like when we ran it:

```text
Q1. What did Northwind Traders collect in October 2026, net of refunds?
    answer   Northwind Traders collected €1,953.99 net of refunds in October 2026. ...
    correct  €1,953.99  ->  right
    run      4 model calls, 6 tool calls (0 failed), 17.574 s

Q2. How many successful charges did Northwind Traders take in October 2026, ...
    answer   Northwind Traders had 13 successful live charges in October 2026,
             totaling €2,136.98. I counted unique `charge.succeeded` events dated
             in October, excluded duplicate deliveries and test-mode charges, and
             converted amounts from cents to euros; ...
    correct  12 charges, €2,037.98  ->  WRONG
    run      3 model calls, 5 tool calls (0 failed), 11.056 s
```

Q2 is wrong by exactly one €99.00 charge, and it sounds sure of itself. It
even says it counted events dated in October.

### Read the wrong run's trace

Open the `trace` link for Q2. The timeline shows the root span,
`invoke_agent worker`, and under it every step:

```text
0.0 s   invoke_agent worker                       11.1 s
0.8 s     chat gpt-6-luna                          plans its first reads
1.9 s     execute_tool read   instructions.md
1.9 s     execute_tool read   manifest.json
1.9 s     execute_tool bash   find … -type f
2.5 s     chat gpt-6-luna
3.3 s     execute_tool read   task-skill/SKILL.md  → only the header
3.3 s     execute_tool read   inputs/input.json
3.6 s     chat gpt-6-luna                          7.3 s, then the answer
```

Select a span to see its attributes. The last `chat` span holds the answer.
Between reading `input.json` and answering there is no tool call: the agent
read 23 deliveries with Unix timestamps and classified them in its head. One
of them, `1790812720`, is 23:58:40 UTC on 30 September. It landed on the
wrong side of the month.

The trace is the only place this is visible. The answer, the run record and
the metrics all look fine.

## The review: judge and diagnose from the trace

Every successful run writes one `agent_runs` record of type `run_ok`, holding
its trace id. That write triggers `silent_failures`, 30 seconds later so the
last spans have arrived and runs that finish close together are reviewed
together. It works in three steps.

### 1. `episodes`: read each run's trace

The built-in `trace` Task takes each run record, fetches its trace from
VictoriaTraces by trace id, checks that the trace belongs to that run, and
writes it out as a numbered episode: the question, each step with its
arguments and output, and the final answer.
[Reading traces back](victoria.md#reading-traces-back) explains the check.

### 2. `review`: Jev checks the answer against the statement

```yaml
- id: review
  use: judge
  input: "{{episodes.items}}"
  with:
    model: jev
    context:
      statement: "{{statement.records}}"
    questions:
      correct:
        type: noul
        instructions: >-
          ... Check the final answer against the payout statement in
          `context.statement`. Is the figure the agent reported consistent
          with the statement?
    keep: {correct: {noul_lte: 0.3}}
```

Jev reads each episode next to the statement and answers with a score from 0
to 1. Runs scoring 0.3 or less are kept as wrong. In our run Jev passed Q1 and
kept Q2.

### 3. `diagnose`: find the cause in the steps and the data

Knowing an answer is wrong is not enough to fix the agent. A reviewer needs to
know why. The `diagnose` step is an `llm` Task that sees the statement, the
account's webhook deliveries, and each kept episode. It recomputes the figure
from the data, names the events behind the difference, and reads the agent's
steps for the cause. For Q2 it wrote:

```text
Agent answered 13 charges totaling €2,136.98; the correct result is 12 charges
totaling €2,037.98, matching the payout statement. Difference: The extra €99.00
is event evt_0020 (charge ch_00), delivered as dlv_0023. Its delivery was
received on October 1, but its event.created timestamp is September 30, 2026
at 23:58:40 UTC, so it is outside October. ... Right method: Filter on
event.created in UTC to the October 1 inclusive–November 1 exclusive period,
count distinct successful live charge event IDs, exclude test-mode events, and
convert cents to euros. Deduplicating repeated deliveries does not remove
evt_0020 because it is a distinct event; the date filter must exclude it.
```

Each diagnosis is written to `outcomes`, citing the run record it is about.

We tried this loop without the diagnose step first. The skill writer saw the
wrong figure and the statement, could not tell which charge was extra, and
explained the gap away. It even wrote the wrong totals into the skill as
examples. A diagnosis from the trace and the data is what makes the lesson
right.

## The skill writer drafts the change

The new outcome triggers the `skill` derivation. It reads the diagnoses, the
current skill, the statement, and the account's data, and proposes the
smallest change the evidence supports. Two rules shape it:

- **Method only.** No amount or count from the runs or the statement may
  appear in the skill, because later questions ask about other figures.
- **Evidence only.** The account's data is there to check that a lesson holds,
  not to invent lessons no run needed.

It drafted three sections:

```text
[steps]     For a month-based charge query, filter by the event's created
            timestamp in UTC, using the month's start inclusively and the next
            month's start exclusively. Keep successful live-mode charge events,
            count each distinct event ID once despite repeated deliveries, and
            convert amounts from cents to the requested currency units.
[pitfalls]  Do not use a webhook's received-at date to determine which month an
            event belongs to. Delivery deduplication alone does not fix a
            boundary error: a distinct event created before the month must be
            excluded even if delivered during it. ...
[examples]  Boundary trap: evt_0020 has a created timestamp on September 30 but
            a delivery received on October 1; exclude it from October based on
            created time. Duplicate-delivery trap: evt_0005 appears in dlv_0005
            and dlv_0006, so count the event once. Mode trap: evt_0006 is a
            successful charge but has livemode false, so exclude it.
```

The examples come from the account's own data, and none of them gives away a
figure.

## Review and promote

The drafts are `draft` records in `skills`. Nothing reaches the agent until a
person promotes them. The script does it for you, the same way you would:

```python
await client.promote(
    entity="account:northwind",
    source_run_id="<the skill run id the script printed>",
    artifact="learned_skill",
)
```

Promotion copies the drafts into new active records, atomically. If the
skill changed since the drafts were made, it refuses. Run the script with
`ARGS=--no-promote` to read the drafts first and promote them yourself.

## Cycle 2: new questions, learned skill

The script asks two questions the agent has not seen:

```text
Q3. What was the largest refund Northwind Traders issued in October 2026,
    and which customer received it?
Q4. What was Northwind Traders' average successful charge in October 2026,
    in euros?
```

Neither answer is in the skill. Both depend on the same traps.

```text
Q3. answer   The largest refund was €60.00 to Larkspur Hotel. I counted successful
             live-mode refund events created in October 2026, counted the duplicate
             delivery of evt_0008 only once, and excluded the test-mode refund.
    correct  €60.00, to Larkspur Hotel  ->  right
    run      3 model calls, 4 tool calls (0 failed), 6.641 s

Q4. answer   The average successful charge in October 2026 was €169.83. I counted
             12 distinct live-mode `charge.succeeded` events by their UTC creation
             dates, ...
    correct  €169.83 (12 charges from 6 customers)  ->  right
    run      4 model calls, 6 tool calls (0 failed), 8.362 s

=== Before and after the promoted skill ===
  cycle 1 (no skill):      1/2 right, 7 model calls, 0 failed tool calls, 29 s
  cycle 2 (learned skill): 2/2 right, 7 model calls, 0 failed tool calls, 15 s
```

Open Q4's trace. The agent reads `SKILL.md`, which now holds the three
sections, and then runs one short script over exactly the 12 qualifying
amounts:

```text
python3 - <<'PY'
a=[4800,12500,2399,4800,86000,12500,7350,7350,2399,43000,15900,4800]
print(sum(a),len(a),sum(a)/len(a),sum(a)/len(a)/100)
PY
=> 203798 12 16983.166666666668 169.83166666666668
```

The September charge is not in the list. Each cycle-2 answer also says it
counted events by their creation time, which is the lesson in its own words.

## Follow the whole chain

Every derivation job is a trace too, with the job's run id as its trace id.
The script prints a `job` link for each run. Job traces link to each other, so
you can start from the skill change and walk back to the run that caused it:

1. Open the `skill` job trace the script printed. Its root span,
   `derive skill`, has an `input` link to the `silent_failures` job whose
   outcomes it read. Use the span's **Links** tab and **Open linked trace**.
2. In the `silent_failures` job, the `task episodes` span has a `trace_read`
   link to each agent run it read, and the root span has `input` links to the
   `run_task` jobs.
3. In a `run_task` job, the `task work` span links to the agent run with
   `agent_run`, and the agent run's root span links back with `derivation`.

The records hold the same chain: the skill section cites the outcome, the
outcome cites the run record, and the run record holds the trace id.

The log lines of every job carry the same id as `trace_id`. In the logs UI at
<http://127.0.0.1:9428/select/vmui/>, query a job's lines:

```text
trace_id:=<the job trace id> | sort by (_time)
```

Two more queries worth trying, in the traces UI:

| Query | Shows |
| --- | --- |
| `"span_attr:memseek.origin.derivation":run_task` | Every agent run `run_task` started |
| `name:"derive silent_failures"` | Every review, with how many traces it read |

## What varies between runs

The agent is a language model, so each run is a little different. In our runs
the first cycle got 0 or 1 of its 2 answers right, and the boundary trap was
the one it fell into most. The loop only learns from what the review catches: a
cycle with no wrong answers drafts nothing, and the script says so and stops.
The two cycles also ask different questions, so compare them as a
demonstration, not a benchmark.

## Change the example

- **Your own data.** Replace `payment_events` with the records your agent
  works from, and `payouts` with whatever a reviewer checks answers against:
  a ledger, a ticket's resolution, a test suite's result.
- **Your own judge.** The `review` question is yours to write. A choice
  question can classify the cause, and an `llm` Task can judge instead of Jev.
- **Learn from good runs too.** Add a judge that keeps high scores with
  `noul_gte`, and emit them as `outcome` records: the skill writer reads
  `outcome` and `exception` alike, and good runs make good examples.
- **Keep prompts out of the store.** `trace: {content: metadata}` records
  names, timings, token counts and errors, but no prompts, responses or tool
  data. The episode then shows the steps without their content.

## If something goes wrong

| Symptom | Cause and fix |
| --- | --- |
| The script waits and no answer comes | Check the worker terminal. `harness 'pi' exited 1` with `401` in the trace means a rejected API key. `pi: not found` means the worker's `PATH` lacks Node or pi. |
| A run's `trace` link shows nothing | The worker has no `TRACE_EXPORT_URL`, or VictoriaTraces is not running. |
| `silent_failures` keeps retrying | It waits for a run's trace to arrive for 30 s, then skips it. `TRACE_STORE_URL` must point at `/select/jaeger`. |
| A run takes many minutes against a 150 s limit | The machine slept. Run the worker under `caffeinate -i` on a Mac. |
| The skill derivation fails with `validation` | An LLM step cited a record it could not see. The worker log has the run; it retries on its own. |
| Jev judges every answer right | Then there is nothing to learn this time. Run the script again: the agent varies. |

## Next steps

- [Agent traces](agent-traces.md): turning tracing on for any agent, and what a
  trace holds.
- [Following Memseek](observability.md): logs, job traces and alerts.
- [Victoria in Memseek](victoria.md): the stores, and what was verified against
  them.
- [Judgment models](judgment-models.md): writing questions for Jev.
