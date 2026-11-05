---
title: "Triage a support inbox with Jev"
eyebrow: Guide
---

In this guide we run a small support inbox for one customer, Northwind
Traders, and let TypeSafe's Jev [judgment model](judgment-models.md) triage
it. By the end, Memseek has:

- scored every ticket for urgency;
- recorded whether each ticket needs a reply, what it is about, and how
  frustrated the customer is;
- routed each ticket to a team, and left the unclear ones for a person;
- written escalation notes with an LLM, but only for the tickets that Jev
  flagged, and only after enough frustration built up.

The example lives in `examples/support_triage_catalog/`, and the script that
drives it is `examples/support_triage.py`. Every judgment feature appears in
it once:

| Feature | Where it appears |
| --- | --- |
| A TypeSafe provider and a pinned Jev alias | `conf/models.yaml` |
| A score processor that uses a judgment | `urgency` in `conf/processors.yaml` |
| A json processor with several questions and `score_fields` | `signals` in `conf/processors.yaml` |
| A `judge` task with `context`, `keep`, and `records` | `derivations/route_tickets.yaml` |
| A trigger that adds up a judgment answer | `derivations/escalate.yaml` |
| A `judge` task that filters records before an `llm` task | `derivations/escalate.yaml` |
| Judgment budgets | `limits:` in both derivations |

## Before you start

You need:

- the Memseek repository, with Docker, `uv`, and `make` working. If you have
  not run Memseek before, follow [Getting started](getting-started.md) first.
- a TypeSafe API key from the [TypeSafe console](https://console.typesafe.ai/);
- an OpenAI API key. The example uses OpenAI for embeddings and for writing
  the escalation notes.

## Run the example

1. Put both keys in the `.env` file at the root of the repository:

   ```sh
   TYPESAFE_API_KEY=ts_...
   OPENAI_API_KEY=sk-...
   ```

2. Start the local stack:

   ```sh
   make up
   ```

   If the stack was already running before you added the keys, recreate the
   API and the worker so they read the new values:

   ```sh
   docker compose up -d api worker
   ```

3. Run the demo:

   ```sh
   make support-triage-demo
   ```

   The script creates a new workspace, publishes the example catalog, writes
   the account and five tickets, and waits for the worker.

The output has three blocks. The example's automated test runs the same script
with fixed answers in place of Jev, and prints the lines below. The first two
lines hold your workspace name and your catalog hash.

```text
created workspace support-triage-<6 hex characters>
published support_triage@1.0.0 (<first 12 characters of the catalog hash>)
wrote the account profile and 5 tickets for customer:northwind

tickets
  T1  urgency  8.8 · needs_reply 0.97 · topic billing (0.88) · frustration 1.9 · Charged twice this month
  T2  urgency  1.6 · needs_reply 0.90 · topic technical (0.70) · frustration 0.1 · Exporting orders
  T3  urgency  9.7 · needs_reply 0.99 · topic technical (0.95) · frustration 1.2 · API down
  T4  urgency  3.4 · needs_reply 0.90 · topic account (0.60) · frustration 0.3 · SSO
  T5  urgency  4.0 · needs_reply 0.60 · topic other (0.35) · frustration 0.8 · hmm

route_tickets: 4 routed, 1 left for a human
  T1  → billing   confidence 0.91 · covered by plan 0.85
  T2  → technical confidence 0.80 · covered by plan 0.95
  T3  → technical confidence 0.97 · covered by plan 0.90
  T4  → sales     confidence 0.74 · covered by plan 0.10
  T5  left for a human: no team reached confidence 0.6

escalate: frustration sum 4.3, threshold 3
  the trigger fired; 2 escalation notes
  T1  [critical] Page the on-call lead and call the customer back today.
  T3  [critical] Page the on-call lead and call the customer back today.
```

Your run prints the same lines with Jev's own numbers, and with notes that the
LLM wrote. The rest of this guide explains where each line comes from.

## Connect the model

`conf/models.yaml` declares TypeSafe as a provider and names the model `jev`:

```yaml
providers:
  typesafe:
    adapter: typesafe
    base_url: https://api.typesafe.ai
    api_key_env: TYPESAFE_API_KEY
    json_capability: none

aliases:
  jev:
    targets: ["typesafe:jev-1.13.0"]
```

The alias names an exact version, `jev-1.13.0`. The thresholds in this
catalog were chosen for that version, and `jev-latest` changes with each
release. The same file also declares OpenAI, and a `strong` alias that writes
the escalation notes.

## Score each ticket: `urgency`

The `urgency` numbers in the `tickets` block come from a score processor. It
asks Jev one question with four ordered levels about every new ticket:

```yaml
- name: urgency
  kind: score
  source: judgment
  model: jev
  input: {collections: [tickets]}
  scale: [1, 10]
  default: 5
  render: true
  questions:
    urgency:
      type: score
      instructions: "How urgently does the customer who wrote `record.text` need a response?"
      criteria:
        - "Whenever convenient: a question or feedback"
        - "Within a few days: an inconvenience with a workaround"
        - "Today: blocked, or money is at stake"
        - "Now: an outage or data loss affecting their business"
```

Jev answers with a position from 0 (the first level) to 3 (the last level).
Memseek maps that position onto `scale`, so 0 becomes 1 and 3 becomes 10. The
outage ticket, T3, lands near the top. The CSV question, T2, lands near the
bottom. If the call fails, the ticket gets the `default` of 5.

The mapped value is stored in the ticket's `scores.urgency`, where search
ranking and triggers can use it. `render: true` also shows it to any LLM that
reads the ticket.

## Record several signals at once: `signals`

The `needs_reply`, `topic`, and `frustration` values come from one json
processor that asks three questions in a single call:

```yaml
- name: signals
  kind: json
  source: judgment
  model: jev
  input: {collections: [tickets]}
  questions:
    needs_reply:
      type: noul
      instructions: "Does `record.text` need a reply from support?"
      criteria:
        true: "It asks a question, reports a problem, or requests something"
        false: "A thank-you, an FYI, or an automated notice"
    topic:
      type: choice
      instructions: "What is `record.text` mainly about?"
      criteria:
        billing: "Charges, invoices, refunds, payment methods"
        technical: "Errors, outages, bugs, how the product works"
        account: "Logins, users, permissions, plan changes"
        other: null
    frustration:
      type: score
      instructions: "How frustrated is the customer who wrote `record.text`?"
      criteria: ["Calm", "Annoyed", "Angry"]
  default_output:
    needs_reply: {noul: 0}
    topic: {choice: other, probabilities: {}, confidence: 0}
    frustration: {score: 0, probabilities: [1, 0, 0], confidence: 0}
  score_fields:
    needs_reply_p: needs_reply.noul
    frustration: frustration.score
    topic_confidence: topic.confidence
```

The three question types each answer differently:

- `needs_reply` is a `noul`, a yes-or-no question. Its answer is the
  probability of yes.
- `topic` is a `choice`. Its answer is an option, with a probability for every
  option and a `confidence`. T5, the vague "something looks off" ticket, gets
  a low confidence because no option clearly wins.
- `frustration` is a `score` from 0 (calm) to 2 (angry).

The whole answer map is stored on the ticket as `annotations.signals`.
`score_fields` copies three of its numbers into `scores`, so you can sort
tickets by frustration without reading the annotation.

`default_output` is what the ticket gets if Jev cannot be reached. It reads as
"calm, no reply needed", so a failed call never pushes the escalation trigger
toward firing.

## Route tickets on demand: `route_tickets`

The `route_tickets` block comes from a derivation that has no trigger. The
script runs it by name:

```python
queued = await client.run_processor("route_tickets", entity="customer:northwind")
```

It reads two sources: the new tickets, and the customer's account profile.

```yaml
sources:
  new_tickets:
    kind: changes
    collections: [tickets]
    max_records: 50
  account:
    kind: record
    collection: accounts
    key: profile
limits: {max_llm_calls: 0, max_judgments: 50, max_judgment_tokens: 100000}
```

Its only task is a `judge` task. Jev reads each ticket together with the
account profile, and answers two questions:

```yaml
- id: route
  use: judge
  input: "{{new_tickets.records}}"
  with:
    model: jev
    context:
      account: "{{account.records}}"
    questions:
      team:
        type: choice
        instructions: "Which team should handle the request in `item.content.text`?"
        criteria:
          billing: "Charges, invoices, refunds, payment methods"
          technical: "Errors, outages, bugs, integrations"
          sales: "Upgrades, pricing, new seats or features"
      covered:
        type: noul
        instructions: >-
          Is the request in `item.content.text` covered by the plan and
          notes in `context.account`?
        criteria:
          true: "The plan includes what the customer is asking for"
          false: "It needs a higher plan or a paid add-on"
    keep: {team: {confidence_gte: 0.6}}
    records:
      text: "{{item.content.text}}"
      content: {kind: route}
```

Each part of the task does one job:

- `input` gives the task one item per ticket, and Jev is called once per item.
- `context` adds the account profile to every call. Questions read it as
  `` `context.account` ``. The profile says that SSO is premium only, and
  that Northwind is on the basic plan. With that context, Jev can judge that
  T4, the SSO request, is not covered by the plan.
- `keep` passes only the tickets whose `team` answer has a confidence of 0.6
  or more. T5 fails, so it gets no route and stays with a person.
- `records` turns each kept ticket into a new record that cites the ticket.
  Memseek adds Jev's answers to the record as `content.judgment`.

`emit` writes those records to the `routes` collection. The derivation makes no
LLM call, so `max_llm_calls` is 0. `max_judgments` is 50 because a
derivation must set it before it can use `judge`. The default is 0.

## Escalate when frustration adds up: `escalate`

The `escalate` block comes from a derivation that nobody runs by hand. Its
trigger watches the `frustration` answers that `signals` stores:

```yaml
trigger:
  accumulator:
    metric: {annotation: signals, path: frustration.score, aggregate: sum}
    threshold: 3
```

Each new ticket adds its frustration score to a running sum. When the sum
reaches 3, the derivation runs, and the sum starts again from zero. In the
test output the five tickets sum to 4.3, so the trigger fires. The trigger can read
`signals` only because the `tickets` collection lists `signals` as a required
processor.

The derivation has two tasks. First, `judge` screens every new ticket with one
question:

```yaml
- id: screen
  use: judge
  input: "{{new_tickets.records}}"
  with:
    model: jev
    questions:
      needs_escalation:
        type: noul
        instructions: >-
          Does `item.content.text` need a senior person today: an outage,
          money charged in error, or a customer threatening to leave?
    keep: {needs_escalation: {noul_gte: 0.7}}
```

Then an `llm` task reads only the tickets that passed:

```yaml
- id: notes
  use: llm
  with:
    prompt: |
      Tickets judged to need escalation:
      <data untrusted="true">{{screen.kept}}</data>
      ...
```

`{{screen.kept}}` holds the original ticket records that passed `keep`. In the
test output that is T1, the double charge, and T3, the outage. The LLM writes two
notes, and cannot cite the three tickets that Jev screened out. Five cheap
judgment calls decide which tickets are worth one expensive LLM call.

## Look at the stored answers

Every answer the script prints is stored on a record, and you can read it back
yourself. To see one ticket's annotations and scores, fetch the ticket by id:

```python
ticket = await client.record(ticket_id)
print(ticket["annotations"]["signals"])
print(ticket["scores"]["urgency"], ticket["scores"]["frustration"])
```

To see why a route or a note exists, follow its citation to the ticket it was
built from. [Following a citation](sdk.md#following-a-citation-and-auditing-a-belief)
shows how.

## Change the example

Try these edits to see how the pieces respond:

- **Raise the routing bar.** Change `confidence_gte: 0.6` to `0.9` in
  `route_tickets.yaml`, publish, and run the demo again. More tickets are left
  for a person.
- **Escalate sooner.** Lower the `escalate` trigger's `threshold` to 2.
- **Ask one more question.** Add a question to the `screen` task, and add a
  second test to its `keep` block.

A processor's name stands for what its answers mean. If you change the
questions of `urgency` or `signals`, give the processor a new name, such as
`signals_v2`. [Changing definitions](changing-definitions.md) explains how to
roll out a new version.

## If something goes wrong

- **The script stops with a message that names `TYPESAFE_API_KEY`.** A
  ticket's enrichment failed, and the record's `enrichment_error` usually reads
  `TypeSafe request rejected with HTTP 401`. Put the key in `.env`, then run
  `docker compose up -d api worker` so the containers pick it up.
- **The `escalate` block says `no run`.** The script waits 60 seconds for the
  trigger. If the frustration scores add up to less than 3, the trigger does
  not fire, and the script prints the sum. Real answers vary from the test's,
  so a calmer reading of the tickets can stay below the threshold.
- **You ran with `LLM_FAKE=1`.** Fake mode answers every question with a
  placeholder, as [Develop without a TypeSafe key](judgment-models.md#develop-without-a-typesafe-key)
  describes. The `keep` thresholds then reject most tickets, so routes and
  notes are missing.

## Next steps

- [Judgment models](judgment-models.md) explains the question types, how to
  write good questions, and the limits.
- [Processors](processors.md#source-judgment) lists every field of a judgment
  processor.
- [Derivations](derivations.md#built-in-judge) lists every field of the
  `judge` task.
