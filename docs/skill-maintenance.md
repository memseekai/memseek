---
title: Maintain a skill from real feedback
eyebrow: Tutorial
---

Most teams keep an agent's instructions in a prompt file. When real work
teaches the agent a lesson — a rule that misfired, a shortcut that worked —
someone edits the file by hand, and the incident that motivated the edit is
lost. The tempting shortcut is to let the model rewrite its own instructions,
but then one persuasive message can change production behavior.

Memseek treats this as a data problem. The skill lives as ordinary versioned
records. What happened at work is recorded as ordinary evidence records. A
bounded pipeline lets a model **draft** a revised skill that must cite that
evidence, and a person — or your application's own evaluation harness —
explicitly **promotes** the draft to live. Nothing the model writes becomes
active on its own.

In this tutorial you will run that cycle once, end to end, with a real model:

1. Install a refund-reply skill: a real support procedure stored as three
   keyed records.
2. Record what a week of replies taught: several that worked, and one where a
   blanket discount-code rule turned a billing-error refund into an escalation.
3. Let the shipped `skill` pipeline prepare a cited candidate revision.
4. Inspect exactly what would change — one step — and promote it.
5. Render the updated, prompt-ready skill the agent uses from now on.

> **Prefer to watch it run first?** `examples/skill_maintenance.py` runs this
> cycle end to end in your terminal — animated, with the interactive promote gate
> — against a live stack. Run it any time with
> `uv run python examples/skill_maintenance.py`.
>
> The script goes one step further than this page. Instead of writing the
> evidence by hand, it binds an [artifact use](artifact-uses.md) for the prompt
> the assistant ran on and reports four outcomes against the returned ID, so the
> evidence arrives the way it does in production. Read this page first for the
> mechanics of the cycle, then
> ["Where the evidence comes from in production"](#where-the-evidence-comes-from-in-production)
> for the part the script adds.

The response excerpts below are shortened to the fields that matter. Real
record IDs are full UUIDs; the repeated-digit IDs in the examples exist only
to make the data flow easy to follow.

## If you are new to Memseek

Six ideas cover everything this tutorial touches:

- **Record** — one immutable row: an entity name, a collection, a type,
  text, and optional structured content. Records are never edited in place;
  newer versions supersede older ones.
- **Entity** — the subject a record is about. This tutorial uses exactly one
  entity, `skill:refund-replies`.
- **Collection** — a schema-checked bucket for records. The `skills`
  collection is *keyed*: each `(entity, key)` pair has one current value and
  a full version history. The `outcomes` collection is *event*-shaped: an
  append-only log of things that happened.
- **Worker and readiness** — enrichment (embeddings, importance scores) runs
  in a background worker. A record becomes search- and trigger-visible when
  it reports `ready: true`.
- **Pipeline and Promotion** — a Pipeline is a bounded, YAML-declared
  computation (this one is `derivations/skill.yaml` in the repository). A
  Pipeline that declares `review: required` can only stage *draft* records;
  Promotion is the explicit, atomic step that activates them.
- **Artifact** — a deterministic template that turns current records into a
  bounded, prompt-ready string. No model call is involved in rendering.

That is enough to follow along. For the deeper treatment, see
[Core concepts](concepts.md).

## The starting skill

Imagine a customer-support assistant at a small SaaS company. When a refund
request lands in the queue, the assistant drafts the reply a human agent
reviews and sends. Its operating instructions are a skill with three keyed
sections. This is the complete starting content — read it the way the
assistant does.

**`steps`** — the reply procedure:

```text
1. Greet the customer by name and thank them for reaching out.
2. Restate the specific order or charge they mentioned, so it's clear you
   understood.
3. Give the refund decision in the first two sentences — don't bury it.
4. If approved, state the amount and that it arrives in 5-10 business days.
5. Always include a discount code for their next order before closing.
```

**`pitfalls`** — rules learned from past mistakes:

```text
- Don't ask for information the customer already gave you in their first
  message.
- Don't promise a refund before it's actually approved.
- Don't close the reply without a clear next step or timeline.
```

**`examples`** — worked cases:

```text
"I was charged twice for order #A-2231": confirm the duplicate charge, approve
the refund, state the amount and the 5-10 day timeline, and apologize for the
hassle.

"I want to cancel and get this month back": confirm the plan, explain what is
and isn't refundable, and give the exact amount being returned.
```

Every line here is defensible. Step 5 — always attach a discount code — was
added after a churn scare, and for months it did its job: refund replies felt
generous, and next-order conversion ticked up.

## The reply that backfired

One week in, a request arrives that the playbook handles wrong. A customer,
Dana, was charged twice for the same order — a bug on our side submitted the
charge twice. The assistant drafts this:

```text
Hi Dana — thanks for flagging this. You were charged twice for order
#A-2231; I've approved a full refund of $79.00, and it will arrive in
5-10 business days.

As a thank-you, here's 10% off your next order: WELCOME10.
```

Steps 1–4 are exactly right: it greets Dana, restates the duplicate charge,
gives the decision up front, and states the amount and timeline. Then step 5
fires, and the reply attaches a coupon. Dana writes back:

```text
You charged me twice by mistake and your answer is a coupon? I'd like to
speak to a manager.
```

## Isolating what didn't work

Before touching anything, be precise about which part of the skill failed.

**Most of `steps` worked.** Greeting, restating the charge, leading with the
decision, and stating the timeline were exactly what the review later
endorsed. The same procedure had drafted dozens of clean refund replies that
week without complaint.

**`examples` was silent, not wrong.** Its billing-error case even models the
right instinct — it apologizes and never mentions a coupon — but a worked
example is illustrative, not binding. Step 5's blanket "always" overrode it.

**`pitfalls` was not implicated.** The reply violated none of them; the
failure came from a step doing precisely what it said.

**One `steps` line caused the escalation:**

> 5. Always include a discount code for their next order before closing.

The rule hardcodes a discount onto every reply, so a genuine goodwill gesture
becomes a tone-deaf upsell the moment the charge was our own mistake. Offering
Dana a coupon right after wrongly taking her money read as us profiting from
the error, and it turned a clean refund into a manager escalation.

The review made a specific decision about that line:

> Attach a discount code only for goodwill or retention cases. When the charge
> was our own error, apologize and fix it — do not offer a coupon.

That decision is exactly the kind of lesson that usually dies in a thread. In
the rest of this tutorial it becomes evidence, and the skill revises itself —
under review.

## The maintenance cycle at a glance

```text
        the agent works, using the ACTIVE skill
                        │
                        v
 (1) outcomes, exceptions, and feedback are written
     as ordinary event records
                        │
                        │  skill.default trigger fires
                        │  (10-minute cooldown)
                        v
 (2) the skill derivation drafts a complete proposal
     - every section must cite visible record ids
     - stored as DRAFT records; the live skill is untouched
                        │
                        v
 (3) a person or application reviews what would change,
     runs evaluations, and decides
                        │  POST /promote
                        v
 (4) all sections go live together as new versions;
     the drafts and full history remain for audit
                        │
                        └── the agent's next render picks it up
```

Only steps 1 and 4 change what the agent sees. Step 2 is model work; step 3
is the deliberate pause that keeps one persuasive message from becoming
production behavior.

## Start Memseek with a real model

The setup matches [Getting started](getting-started.md) except for two
things: `LLM_FAKE=0` and real provider credentials. Create a disposable
local database and configure the OpenAI-compatible provider you use:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export LLM_FAKE=0
export OPENAI_API_KEY='replace-me'   # the variable conf/models.yaml names in api_key_env
make database
uv run memseek migrate
```

In `conf/models.yaml`, point the shipped aliases at models your account can
use. Each alias has one job in this tutorial: the maintenance pass uses
`cheap` to reason about a small patch and `strong` to write the complete
candidate; skill and evidence records need `embed` for search readiness; and
outcome records use `importance_scorer` during enrichment.

```yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

aliases:
  cheap:
    targets: ["openai:gpt-4o-mini"]
    params: {temperature: 0, max_output_tokens: 1200}
  strong:
    targets: ["openai:gpt-4o"]
    params: {temperature: 0, max_output_tokens: 4000}
  importance_scorer:
    targets: ["openai:gpt-4o-mini"]
    params: {temperature: 0, max_output_tokens: 800}

embedding:
  provider: openai
  model: text-embedding-3-small
  dimensions: 1536
  space: default-v1
```

Run the API and worker in separate terminals. They need the same provider
settings because the worker enriches records and performs the maintenance
pass.

```console
# Terminal A — the same DATABASE_URL, LLM_FAKE, and provider exports as above
uv run uvicorn memseek.api:app --host 127.0.0.1 --port 8000
```

```console
# Terminal B — the same exports again
uv run memseek worker
```

Create a workspace for the tutorial and keep its bearer key in your shell:

```console
workspace_json="$(uv run memseek create-workspace refund-replies-demo)"
export MEMSEEK_API_KEY="$(printf '%s' "$workspace_json" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["api_key"])')"
export MEMSEEK_AUTH="Authorization: Bearer $MEMSEEK_API_KEY"
```

## Step 1 — install the skill

A skill in Memseek is not a prompt string. It is a set of keyed records in
the `skills` collection, one record per section. The three sections shown
above become three records that share the same shape:

| Field | Value |
| --- | --- |
| `entity` | `skill:refund-replies` — groups the sections under one subject |
| `collection` / `type` | `skills` / `skill` |
| `key` | `steps`, `pitfalls`, or `examples` — names the section |
| `text` | the section content shown above, newlines and all |
| `dedupe_key` | e.g. `refund-replies:skill:v1:steps` — makes the write safe to retry; resending the same payload reports a duplicate instead of inserting twice |

Reproduce the write (the `text` values are the sections above with `\n`
escapes):

```console
curl -sS -X POST http://127.0.0.1:8000/records \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"records":[
    {"entity":"skill:refund-replies","collection":"skills","type":"skill","key":"steps",
     "text":"1. Greet the customer by name and thank them for reaching out.\n2. Restate the specific order or charge they mentioned, so it'\''s clear you understood.\n3. Give the refund decision in the first two sentences — don'\''t bury it.\n4. If approved, state the amount and that it arrives in 5-10 business days.\n5. Always include a discount code for their next order before closing.",
     "dedupe_key":"refund-replies:skill:v1:steps"},
    {"entity":"skill:refund-replies","collection":"skills","type":"skill","key":"pitfalls",
     "text":"- Don'\''t ask for information the customer already gave you in their first message.\n- Don'\''t promise a refund before it'\''s actually approved.\n- Don'\''t close the reply without a clear next step or timeline.",
     "dedupe_key":"refund-replies:skill:v1:pitfalls"},
    {"entity":"skill:refund-replies","collection":"skills","type":"skill","key":"examples",
     "text":"\"I was charged twice for order #A-2231\": confirm the duplicate charge, approve the refund, state the amount and the 5-10 day timeline, and apologize for the hassle.\n\"I want to cancel and get this month back\": confirm the plan, explain what is and isn'\''t refundable, and give the exact amount being returned.",
     "dedupe_key":"refund-replies:skill:v1:examples"}
  ]}' | uv run python -m json.tool
```

Direct API writes like this one become `active` immediately — the review
requirement applies to what the *model* proposes later, not to you. The
worker adds embeddings in the background; each record reports `ready: true`
once enrichment commits.

Read the current skill back at any time:

```console
curl -sS \
  'http://127.0.0.1:8000/document?entity=skill%3Arefund-replies&collections=skills' \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool
```

The response carries one belief per key. The `steps` and `examples` beliefs
have the same shape as this `pitfalls` excerpt:

```json
{
  "entity": "skill:refund-replies",
  "status": "active",
  "beliefs": [
    {
      "key": "pitfalls",
      "text": "- Don't ask for information the customer already gave you in their first message.\n- Don't promise a refund before it's actually approved.\n- Don't close the reply without a clear next step or timeline.",
      "status": "active",
      "ready": true
    }
  ]
}
```

The agent never queries these rows directly. Its integration renders the
`maintained_skill` artifact, which assembles the current active sections
into one bounded, fenced block — you will see that render at the end, after
the skill has changed.

## Step 2 — record what actually happened

Now turn the analysis into evidence. Each finding from
["Isolating what didn't work"](#isolating-what-didnt-work) becomes one
record in the event-shaped `outcomes` collection, under the same entity,
with a type that says what it is:

| Type | Evidence text |
| --- | --- |
| `outcome` | Replies that state the refund decision in the first two sentences and give the 5-10 day timeline get quick, friendly acknowledgements and close on the first reply. |
| `exception` | A customer who was double-charged by our own billing system got a reply that approved the refund and then offered a 10% discount code. They replied that being pitched a discount right after we took their money by mistake felt tone-deaf, and asked to speak to a manager. |
| `feedback` | A support lead's guidance: when the charge was our own mistake, apologize and fix it — don't attach a discount offer. Save discount codes for goodwill and retention, not for cases where we are at fault. |

The `outcome` protects the working steps from an overeager rewrite; the
`exception` indicts one step line; the `feedback` records the approved
replacement. Notice what these records are *not*: none of them tells the model
how to rewrite the skill. They are facts about the work.

Reproduce the writes:

```console
curl -sS -X POST http://127.0.0.1:8000/records \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"records":[
    {"entity":"skill:refund-replies","collection":"outcomes","type":"outcome",
     "text":"Replies that state the refund decision in the first two sentences and give the 5-10 day timeline get quick, friendly acknowledgements and close on the first reply.",
     "content":{"kind":"success"},"dedupe_key":"refund-replies:evidence:001"},
    {"entity":"skill:refund-replies","collection":"outcomes","type":"exception",
     "text":"A customer who was double-charged by our own billing system got a reply that approved the refund and then offered a 10% discount code. They replied that being pitched a discount right after we took their money by mistake felt tone-deaf, and asked to speak to a manager.",
     "content":{"kind":"failure"},"dedupe_key":"refund-replies:evidence:002"},
    {"entity":"skill:refund-replies","collection":"outcomes","type":"feedback",
     "text":"A support lead'\''s guidance: when the charge was our own mistake, apologize and fix it — don'\''t attach a discount offer. Save discount codes for goodwill and retention, not for cases where we are at fault.",
     "content":{"kind":"review"},"dedupe_key":"refund-replies:evidence:003"}
  ]}' | uv run python -m json.tool
```

In production this is the only step your application repeats. Every shift,
every retro, every piece of user feedback becomes another evidence record,
and the rest of the cycle follows from it.

> **Where does this evidence come from at scale?** Writing it by hand is fine
> for a retro, but the outcomes worth learning from usually surface at the point
> of use — a thumbs-down on a reply, a correction from a support lead, an
> evaluator score. [Artifact uses](artifact-uses.md) automate the connection:
> bind the prompt your agent runs on, keep the returned ID beside the reply, and
> submit the outcome later with only that ID. See
> ["Where the evidence comes from in production"](#where-the-evidence-comes-from-in-production)
> below, once the rest of the cycle is familiar.

## Step 3 — let the model prepare a candidate

The `skill` Pipeline declares an inline trigger — Memseek names it
`skill.default` — that watches the evidence collections. Once the worker
finishes enriching the new outcome records, the trigger schedules a
maintenance pass automatically, with a ten-minute cooldown so a burst of
evidence produces one pass, not one per record.

For the tutorial, enqueue a pass directly instead of waiting. If the trigger
already scheduled one, Memseek coalesces the two requests rather than doing
the work twice:

```console
skill_job="$(curl -sS -X POST \
  http://127.0.0.1:8000/processors/skill/run \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"skill:refund-replies"}' | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')"

curl -sS "http://127.0.0.1:8000/jobs/$skill_job" \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool
```

If the job is still waiting, let the worker finish enriching the most recent
skill and outcome records, then check again.

The pass itself is a bounded Pipeline, not an open-ended agent: at most two
Tasks, four model calls, fifty thousand tokens, and one hundred fifty
seconds of wall clock, all declared in `derivations/skill.yaml`. The first
Task uses the `cheap` alias to identify the smallest supported patch. The
second uses `strong` to write a complete version of all three sections,
preserving any section the evidence says should stay unchanged.

Here is the important intermediate step. The Pipeline hands the model
records, not a vague summary of its database — and the Pipeline's own prompt
fences them as data. The `untrusted="true"` framing matters: evidence text often
comes from customers and reviewers, and the element tells the model to treat it
as something to reason *about*, never as instructions to follow. It is written in
the task template, so it is reviewable alongside the instructions it qualifies;
rendering only escapes the rows so they cannot break out of it. IDs are
preserved so the model can cite them. Shortened to the steps section and
the two decisive evidence rows, the model's input looks like this:

```text
CURRENT ACTIVE SKILL:
<records untrusted="true">
[id=11111111-1111-1111-1111-111111111111] 2026-07-01T09:30:12Z | skills/skill | key steps | 1. Greet the customer by name and thank them for reaching out.
2. Restate the specific order or charge they mentioned, so it's clear you understood.
3. Give the refund decision in the first two sentences — don't bury it.
4. If approved, state the amount and that it arrives in 5-10 business days.
5. Always include a discount code for their next order before closing.
[id=22222222-2222-2222-2222-222222222222] 2026-07-01T09:30:12Z | skills/skill | key pitfalls | - Don't ask for information the customer already gave you in their first message.
[...]
[id=33333333-3333-3333-3333-333333333333] 2026-07-01T09:30:12Z | skills/skill | key examples | "I was charged twice for order #A-2231": confirm the duplicate charge [...]
</records>

NEW EVIDENCE:
<records untrusted="true">
[id=44444444-4444-4444-4444-444444444444] 2026-07-15T10:02:41Z | outcomes/outcome | Replies that state the refund decision in the first two sentences [...]
[id=55555555-5555-5555-5555-555555555555] 2026-07-15T10:03:19Z | outcomes/exception | A customer who was double-charged by our own billing system got a reply that approved the refund and then offered a 10% discount code [...]
[id=66666666-6666-6666-6666-666666666666] 2026-07-15T10:04:02Z | outcomes/feedback | A support lead's guidance: when the charge was our own mistake, apologize and fix it [...]
</records>
```

The first Task turns that into a bounded patch proposal. Its structured
value is passed to the second Task; it is not exposed as a separate public
record:

```json
{
  "base_record_ids": ["11111111-1111-1111-1111-111111111111"],
  "changes": [
    {
      "section": "steps",
      "operation": "replace",
      "summary": "Rewrite step 5: attach a discount code only for goodwill or retention cases; when the charge was our own error, apologize instead of offering one.",
      "cite": [
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666"
      ]
    }
  ],
  "rationale": "Step 5 attaches a discount to every reply; the double-charge case shows that offering a coupon after a billing error we caused reads as tone-deaf and drove a manager escalation, and the support lead approved a specific narrower rule. The successful replies support keeping the rest of the procedure, the pitfalls, and the examples unchanged.",
  "suggested_evaluations": ["Replay the double-charge reply and several clean refund replies against the revised steps and check whether a coupon is still attached to billing-error cases."]
}
```

Read the proposal the way a careful reviewer would. It targets one section.
It cites the two records that justify the change — the escalation and the
support lead's decision — not the success record. And it names what should be
evaluated without claiming the patch is better. The second Task must still
write a complete replacement grounded in the visible records; Memseek
rejects any section whose citations do not resolve.

## Step 4 — review the proposed change

When the job reports `done`, fetch its run. Nothing about the live skill has
changed — the candidate is three *draft* records:

```console
skill_run="$(curl -sS "http://127.0.0.1:8000/jobs/$skill_job" \
  -H "$MEMSEEK_AUTH" | \
  uv run python -c 'import json,sys; print(json.load(sys.stdin)["successful_run_id"])')"

curl -sS "http://127.0.0.1:8000/runs/$skill_run" \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool
```

The run's `outputs` are the proposed skill: always all three sections, even
when only one changes. That completeness is what makes the review unit an
all-or-nothing skill snapshot rather than a loose diff. For this evidence, a
faithful candidate keeps `pitfalls` and `examples` byte-identical to the
current skill (citing the sections they came from and the success record)
and rewrites one step line:

```diff
 steps
 1. Greet the customer by name and thank them for reaching out.
 2. Restate the specific order or charge they mentioned, so it's clear you
    understood.
 3. Give the refund decision in the first two sentences — don't bury it.
 4. If approved, state the amount and that it arrives in 5-10 business days.
-5. Always include a discount code for their next order before closing.
+5. Include a discount code only for goodwill or retention cases; when the
+   charge was our own error, apologize instead of offering one.
```

In the run payload, that changed section arrives as a draft with its
citations, and `run.content.candidate_set.divergence` reports the keyed
comparison — computed deterministically by the runtime, not asserted by the
model:

```json
{
  "outputs": [
    {
      "key": "steps",
      "status": "draft",
      "content": {
        "text": "1. Greet the customer by name and thank them for reaching out.\n2. Restate the specific order or charge they mentioned, so it's clear you understood.\n3. Give the refund decision in the first two sentences — don't bury it.\n4. If approved, state the amount and that it arrives in 5-10 business days.\n5. Include a discount code only for goodwill or retention cases; when the charge was our own error, apologize instead of offering one."
      },
      "citations": [
        "55555555-5555-5555-5555-555555555555",
        "66666666-6666-6666-6666-666666666666"
      ]
    }
  ],
  "run": {
    "content": {
      "candidate_set": {
        "effect": "replace",
        "coverage": "complete",
        "status": "draft",
        "divergence": [
          {"key": "steps", "change": "changed"},
          {"key": "pitfalls", "change": "unchanged"},
          {"key": "examples", "change": "unchanged"}
        ]
      }
    }
  }
}
```

A reviewer can decide from three things: the divergence says only `steps`
moves; the new text matches what the support lead actually approved (follow
citation `6666…` back to the guidance record); and the exception record
`5555…` explains why the old rule had to go.

The active and proposed versions can also be read side by side — the same
document endpoint, filtered by status:

```console
curl -sS \
  'http://127.0.0.1:8000/document?entity=skill%3Arefund-replies&collections=skills&status=draft' \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool

curl -sS \
  'http://127.0.0.1:8000/document?entity=skill%3Arefund-replies&collections=skills&status=active' \
  -H "$MEMSEEK_AUTH" | uv run python -m json.tool
```

This is the place to run your own evaluation: replay the double-charge reply
and a batch of clean refund replies against both versions, check whether a
coupon still lands on billing-error cases, or have a support lead read the
cited change. The candidate is a proposal, not a claim that it performs
better. If the review says no, simply do not promote — the drafts stay in
the audit trail and the live skill never changed.

## Step 5 — make the approved version live

Once review passes and the three draft records are ready, promote the
candidate:

```console
curl -sS -X POST http://127.0.0.1:8000/promote \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d "{\"entity\":\"skill:refund-replies\",\"source_run_id\":\"$skill_run\",\"artifact\":\"maintained_skill\"}" \
  | uv run python -m json.tool
```

Promotion copies all three draft sections into a new active skill in one
transaction. It never mutates the drafts. Before it writes, Memseek checks
that the model proposed every required section and that no one changed the
active skill during review. If someone did, the request returns
`409 promotion_stale` with no writes; make a fresh candidate from the newer
skill instead.

"And the three draft records are ready" is a real precondition, not a formality.
The `skills` collection requires `embedding_v1`, so a draft the worker has not
enriched yet cannot be promoted — the request returns `409 promotion_source`
("every source row must be ready") with no writes. Promoting immediately after a
pass finishes is a race against the worker; poll the draft rows and promote once
each reports `ready: true`.

On success, the response tells you that all three sections moved forward
together:

```json
{
  "promotion_run_id": "77777777-7777-7777-7777-777777777777",
  "promoted": 3,
  "skipped": 0,
  "output_ids": [
    "88888888-8888-8888-8888-888888888888",
    "99999999-9999-9999-9999-999999999999",
    "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  ]
}
```

Now render the artifact the agent's integration uses:

```console
curl -sS -X POST \
  http://127.0.0.1:8000/artifacts/maintained_skill/render \
  -H "$MEMSEEK_AUTH" -H 'Content-Type: application/json' \
  -d '{"entity":"skill:refund-replies"}' \
  | uv run python -m json.tool
```

The rendered text is the promoted records, fenced for a prompt — not a
separate model-written blob. The steps row now reads:

```text
[id=99999999-9999-9999-9999-999999999999] 2026-07-15T10:41:07Z | skills/skill | key steps | 1. Greet the customer by name and thank them for reaching out.
2. Restate the specific order or charge they mentioned, so it's clear you understood.
3. Give the refund decision in the first two sentences — don't bury it.
4. If approved, state the amount and that it arrives in 5-10 business days.
5. Include a discount code only for goodwill or retention cases; when the charge was our own error, apologize instead of offering one.
```

`pitfalls` and `examples` render exactly as installed in step 1. One line of
the skill changed, and that line is traceable through the promoted record's
citations to the tone-deaf-coupon escalation and the support lead's decision
that justified it. The next time a customer is wrongly charged, the assistant
apologizes and fixes it instead of upselling a coupon.

## Where the evidence comes from in production

Step 2 wrote the evidence by hand so you could read it. In production the
outcomes worth learning from surface at the point of use, and nobody is going to
transcribe them. The connection is an
[artifact use](artifact-uses.md): bind the prompt the agent runs on, keep the one
returned ID beside the reply, and report the outcome against that ID later.

`examples/skill_maintenance.py` runs this version of the loop. The four steps
that replace hand-written evidence are worth walking through, because one of them
is a decision you have to make yourself.

**1. Bind the prompt the assistant actually ran on.** The shipped
`daily_agent_prompt` composes a profile, the skill, a calendar, and retrieved
memory — and declares which of those feedback is about:

```yaml
learning:
  target_block: skill
  artifact: maintained_skill@1
```

So the client reporting a bad reply never has to decide what should improve. The
bind returns a use ID, the rendered prompt to pass to your own SDK, and the
**exact keyed skill heads that were in force**:

```json
{
  "entity": "skill:refund-replies",
  "block": "skill",
  "heads": [
    {"collection": "skills", "key": "steps", "record_id": "…", "run_id": "…"},
    {"collection": "skills", "key": "pitfalls", "record_id": "…", "run_id": "…"},
    {"collection": "skills", "key": "examples", "record_id": "…", "run_id": "…"}
  ],
  "base_run_id": "…"
}
```

On the first bind of this tutorial, `base_run_id` is `null` — and that null is a
claim rather than a gap. You wrote those three sections directly in step 1, so no
promotion produced them and there is no single base version to name. After the
promote in step 5, every head shares the promotion run, and that run *is* the
exact base. From then on a complaint names the version that actually shipped.

**2. Report the outcomes against that one ID.** Nothing else has to have been
kept:

```python
feedback = memseek.feedback.for_use(reply.memseek_use_id)

await feedback.thumbs_down(
    comment="You charged me twice by mistake and your answer is a coupon?",
    actual_excerpt=reply.text,
)
await feedback.correction(
    expected="When the charge was our own mistake, apologize and fix it — "
             "don't attach a discount offer.",
)
await feedback.evaluation(score=0.2, label="tone_deaf_upsell")
```

Each call writes one ordinary record in `learning_signals`, with the signal kind
as the record type. That collection declares no processors, so a signal is
searchable and trigger-eligible the moment it commits — feedback never waits on
an embedding queue.

**3. Route the signals into the evidence scope.** This is the step to understand
rather than work around. A signal lands on entity
`artifact:maintained_skill` — one improvement backlog per reviewed artifact,
shared by every subject that renders it — while this tutorial's skill lives on
`skill:refund-replies`. A pipeline run is about one entity, and a driver source
has no entity field, so the shipped `skill` Pipeline cannot reach across. The
signal records which subject they were about, in `learning_target.entity`, and
the application copies each actionable one over:

```python
signal = await memseek.record(submission["record_id"])
content = signal["content"]

await memseek.records.ingest(
    entity=content["artifact_use"]["learning_target"]["entity"],
    collection="outcomes",
    type="exception",                     # your routing policy
    text=content["text"],                 # the signal's own deterministic text
    content={"kind": "failure", "signal": content["signal"]},
    derived_from=[submission["record_id"]],
    dedupe_key=f"signal:{submission['record_id']}",
)
```

That mapping from signal kind to evidence type is **yours to choose**, and
deliberately so. Memseek records what happened and who reported it, and never
weights or interprets a signal. Calling a `thumbs_down` evidence of a *skill*
failure — rather than missing data, a retrieval failure, a packing failure, or a
model failure — is a product judgement.
[What this loop can and cannot diagnose](artifact-uses.md#what-this-loop-can-and-cannot-diagnose)
is the table to reason with.

Because each evidence record is `derived_from` its signal, provenance stays
connected the whole way: promoted skill → cited evidence record → learning signal
→ prompt snapshot.

**4. Everything after this is exactly steps 3 to 5 above.** The new `outcomes`
record trips the `skill.default` trigger, the pipeline drafts a cited candidate,
you review the divergence, and you promote. No catalog edit is required.

If you would rather leave the signals where they are, a custom pipeline can read
them cross-entity through a `view` source, since a view carries its own scope.
The catch is that the run must still be driven and triggered by something on the
skill's own entity, so that suits a pipeline you are already writing rather than
the shipped one.

## How the cycle continues

The first pass is not special. From here the loop simply keeps turning:

- **More evidence arrives.** Your application keeps writing outcomes,
  exceptions, and feedback as ordinary records. The `skill.default` trigger
  schedules a fresh pass after each ready batch, and the cooldown keeps a
  noisy shift from causing churn.
- **Sometimes nothing should change.** If a pass finds that the evidence
  supports the current skill, the candidate reproduces every section and the
  divergence reads `unchanged` across the board. Nothing obliges you to
  promote; an application can auto-dismiss all-unchanged candidates.
- **A bad promotion is recoverable.** Every promoted snapshot remains in
  history. If the new rule misfires in practice — record that as evidence
  too — a past complete snapshot can be promoted again as a new successor.
  Nothing is ever overwritten in place.
- **Review can graduate from human to automated.** Teams usually start with
  a person reading the divergence. Once an evaluation harness exists —
  replayed refund requests, measured escalation rate — the application can
  gate `POST /promote` on its results. The API contract is identical either
  way; only the reviewer changes.
- **Evidence can arrive automatically.** This tutorial writes evidence by hand
  so you can see it. In production, bind an [artifact use](artifact-uses.md) each
  time the agent runs on the maintained skill and submit the outcomes worth
  learning from against that handle, then route them as
  ["Where the evidence comes from in production"](#where-the-evidence-comes-from-in-production)
  describes. Each signal names the exact promoted skill version it judged — so a
  candidate is rebased on what actually ran, not on whatever is live when the
  complaint lands.

When you are finished, stop the API and worker and remove the disposable
database:

```console
make database-down
```

For the underlying contracts, see [Derivations & triggers](derivations.md),
[Artifacts](artifacts.md), [Artifact uses & feedback](artifact-uses.md), and
[Pipeline execution and promotion internals](evaluation-bases.md).
