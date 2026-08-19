---
title: Build an agent that remembers
eyebrow: Tutorial — the Generative Agents architecture
---

## Run the demo

Start with the working agent, then follow the tutorial to see how its memory is
built. You need Docker Compose, `uv`, and an OpenAI API key. From the repository
root, add your key to `.env` (replace the placeholder with your real key):

```dotenv
OPENAI_API_KEY=sk-your-key
```

Then run:

```console
make up
unset MEMSEEK_API_KEY
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/memseek
uv run python examples/generative_agents_toy.py
```

`make up` starts PostgreSQL, the Memseek API, and the worker. The example needs
its own catalog, so do not point it at the populated `local` workspace created
by `make up`. With `DATABASE_URL` set instead, the script creates a fresh
workspace for every run in the same Docker database.

This is a hands-on tour of a small, complete example: a town of three
residents who wake up, plan their day, cross paths, gossip, sleep on what they
learned, and get interviewed the next evening — and then, on a third day, one of
them changes his mind and the town has to catch up. It's a playful setting for a
serious problem — **giving an agent a memory that behaves the way you'd want a
real one to** — and it runs end to end on the shipped Memseek catalog with
nothing but YAML and one short Python script.

The last three sections are the payoff: because every memory and every belief is
an immutable record that names the evidence it was built from, you can replay
*what any agent believed at any past instant* and *track a belief all the way
down to the observations it rests on* — provenance you can audit, not
reconstruct.

You don't need to know Memseek's internals to follow along. Each section
introduces one capability, shows the small piece of YAML that turns it on, and
then shows the **actual output** captured from a real run so you can see
exactly what that YAML bought you. The runnable script is
`examples/generative_agents_toy.py`. Let it run while you read on: each section
explains one part of the memory system and shows what it produces.

> The example recreates the memory architecture from *Generative Agents:
> Interactive Simulacra of Human Behavior* (Park et al., UIST '23). If you've
> read the paper, the mapping is called out along the way; if you haven't, you
> lose nothing — everything is explained from scratch.

## What "remembering" actually takes

If you've ever tried to build a long-running agent — a support bot that works a
queue for months, a companion that should recall last week, an assistant that
learns your project — you've hit the same wall. Storing text is easy. Making it
*behave like memory* is not. A useful memory has to do five things:

1. **Observe** — write down what happened, and never quietly lose it.
2. **Weigh** — decide what's worth keeping front-of-mind. A fire alarm matters
   more than what you had for lunch.
3. **Recall** — when something comes up, surface the *right* past memories:
   relevant to the moment, important, and reasonably recent.
4. **Reflect** — periodically turn a pile of raw observations into higher-level
   understanding ("Sam is clearly gearing up for a campaign").
5. **Assemble** — pack the profile, the schedule, and the relevant memories
   into the next prompt, reproducibly.

Most teams hand-roll all five and then own the scheduling, storage, scoring,
and audit trail forever. The point of this tutorial is that in Memseek each of
the five is a few lines of configuration. We'll build them one at a time.

Meet the cast:

- **Isabella Rodriguez** runs Hobbs Cafe and is throwing a Valentine's Day
  party tomorrow.
- **Klaus Mueller** is a sociology student, a regular at the cafe.
- **Sam Moore** has just decided to run for mayor.

By the end, two facts — *the party* and *the mayoral run* — will have spread
across all three residents purely through conversation, each agent will keep a
self-updating profile of who it is, one agent will change its mind and get
caught contradicting itself, the town will scramble to correct a fact it already
"knew" — and you'll be able to reconstruct exactly what each resident believed
at any point along the way, and trace any belief down to its evidence. Finally,
one agent will exercise its right to be forgotten.

## 1. Store what happened — the `main` collection

A memory starts as a plain record. In Memseek, records live in **collections**,
and a collection is defined in YAML. Here is the one that holds the residents'
memory stream (from the shipped `collections/core.yaml`):

```yaml
collections:
  - name: main
    version: 1
    active: true
    mode: mixed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    required_processors: [embedding_v1, importance]
    search_profile: pg_default
```

What each line buys you:

- **`mode: mixed`** — records are immutable and append-only. Writing a new
  memory never overwrites an old one; the stream is the history. (This is the
  paper's "memory stream.")
- **`required_processors: [embedding_v1, importance]`** — every record written
  here is automatically embedded (so it's searchable by meaning) *and* scored
  for importance (next section). "Required" means a record isn't considered
  ready until both have run — you never retrieve a half-processed memory.
- **`schema`** — the only hard requirement is a `text` field;
  `additionalProperties: true` lets you attach anything else you like.

Writing a memory is one API call. In the script, a small helper builds the
record; a seed memory for Isabella looks like this:

```python
{
  "collection": "main",
  "entity":     "smallville:isabella",
  "type":       "observation",
  "text":       "Isabella Rodriguez is planning a Valentine's Day party at "
                "Hobbs Cafe tomorrow from 5pm to 7pm and wants to invite everyone.",
  "occurred_at": "2026-07-17T02:42:00+00:00",
  "dedupe_key":  "…",   # makes re-runs idempotent
}
```

`entity` is who the memory belongs to — every resident has their own stream, so
retrieval and reflection are always scoped to one person. That's it: you've
observed something, durably.

## 2. Decide what matters — the `importance` processor

Not every memory deserves equal weight. The paper asks a model to rate each
memory's long-term significance from 1 to 10, and Memseek ships exactly that as
the `importance` processor — the one we listed under `required_processors`
above. You don't configure a prompt or wire up a job; naming the processor on
the collection is the whole setup. Every record that lands in `main` gets a
`scores.importance` value written to it, once, at write time.

Here's what that produces. At the end of day two, asking each resident for
their most salient memories (a plain search, sorted by that score) gives cards
with a little importance meter:

```text
  Isabella Rodriguez
    ████████░░  8.0  Isabella Rodriguez was at Isabella's Valentine's Day party at Hobbs Cafe; the cafe was warm and full of neighbors.
    ████████░░  8.0  Isabella Rodriguez heard from Sam Moore: Isabella Rodriguez is planning a Valentine's Day party…
    █████████░  9.0  Isabella Rodriguez is planning a Valentine's Day party at Hobbs Cafe tomorrow from 5pm to 7pm…
```

The party plan scores a 9; a passing bit of gossip she overheard scores lower.
That number is what recall leans on next.

> **A note on the demo's `[importance=N]` markers.** In the script, seed
> sentences end with a marker like `[importance=9]`. With a real model that
> marker is just ignored text — the processor reads the *sentence* and scores
> it. The marker only matters in the offline deterministic mode (below), where
> a stand-in scorer reads it back so repeated runs come out identical.

## 3. Recall the right memory — the retrieval formula

When Isabella runs into Klaus, what should come to mind? The paper's answer,
which Memseek expresses directly, is a weighted blend of three signals:

- **Relevance** — how well a memory matches what's happening now (by meaning
  *and* by keyword).
- **Importance** — the score from the last section.
- **Recency** — recent memories outweigh stale ones, decaying smoothly over
  time.

That blend is a **rank expression**, and the shipped default (`conf/rank_default.yaml`)
literally is the paper's formula — all three signals weighted equally and
normalized:

```yaml
hybrid:
  - sum
  - - [product, 1.0, [normalize, [max, [[similarity], [text_match]]]]]   # relevance
    - [product, 1.0, [normalize, [score, importance]]]                   # importance
    - [product, 1.0, [decay, [age_hours, last_accessed], {midpoint: 24, exponent: 1}]]  # recency
```

You don't have to write this — it's the default. But because a rank expression
is just a request parameter, you can hand-tune it per query. This example does
exactly one tweak: the simulation compresses two "days" into a few real
seconds, so wall-clock recency would treat every memory as equally fresh.
Swapping the decay term to read each memory's *in-story* time (`occurred_at`
instead of `last_accessed`) makes recency behave the way a reader expects.
That's the entire customization — no catalog change:

```python
PAPER_RANK = [
  "sum",
  [
    ["product", 1.0, ["normalize", ["max", [["similarity"], ["text_match"]]]]],
    ["product", 1.0, ["normalize", ["score", "importance"]]],
    ["product", 1.0, ["decay", ["age_hours", "occurred_at"], {"midpoint": 24, "exponent": 1}]],
  ],
]
```

Now recall is meaningful: ask "what should I bring up with Klaus?" and the
party plan (relevant, important, recent) rises to the top.

## 4. A conversation, and how gossip travels

A conversation, in the simulator, is simple: the speaker recalls their most
important relevant memory and says it; then **both** people write down their
own view of the exchange. That last part is the whole mechanism by which
information spreads — each participant records *their own* memory, so a fact
can hop from person to person.

Watch two facts travel on day one (this is real output — the spoken lines are
model-written, grounded in each speaker's retrieved memories):

```text
  Day 1 09:00  Isabella Rodriguez → Klaus Mueller
    "Hi Klaus, it's so good to see you — I'm having a Valentine's Day party at Hobbs Cafe tomorrow from 5 to 7, and I'd love for you to come!"

  Day 1 10:30  Sam Moore → Isabella Rodriguez
    "Hey Isabella, good to see you — I wanted to tell you I've decided to run for mayor in the upcoming local election."

  Day 1 12:00  Klaus Mueller → Sam Moore
    "Hey Sam, good to see you — Isabella's planning a Valentine's Day party at Hobbs Cafe tomorrow from 5 to 7, and she wants everyone to come."
```

Notice the third line: Klaus is now passing along the party — a fact he only
learned two hours earlier. What gets *stored* isn't the spoken sentence (with
its "you" and "I" that would scramble on the next hop) but the clean fact
underneath it: `Klaus Mueller heard from Isabella Rodriguez: Isabella is
planning a Valentine's Day party…`. That keeps the fact intact as it diffuses.

There's one more thing happening on each hop, and it's what makes the last three
sections possible. When a resident passes a fact along, the statement they store
records a **`derived_from`** edge back to the memory it came from — their *own*
memory, since they only ever draw on their own stream. `derived_from` is
Memseek's provenance edge: it's how a record names the exact parents it was built
from, and it's what lets you walk a belief down to its evidence later. Two design
choices are deliberate here:

- **The edge stays inside one agent.** The speaker's statement points at the
  speaker's source; the listener's new memory takes *no* parent. Klaus's memory
  of what Sam told him is Klaus's own record, not a derivative of Sam's — which
  is both epistemically honest (Klaus can't see inside Sam's head) and exactly
  what keeps the right-to-be-forgotten clean (section 12): erasing Sam never
  reaches into Klaus's memories.
- **`depth` comes for free.** Because each record knows its parents, Memseek
  computes how far down a provenance chain it sits, so "the memory with the
  longest lineage" is a first-class, queryable property.

After two days of these encounters, an interview measures how far each fact
reached (`●` knows, `○` doesn't):

```text
  Did you know there is a Valentine's Day party?
    ● → ●  Isabella Rodriguez
    ○ → ●  Klaus Mueller  (learned it)
    ○ → ●  Sam Moore  (learned it)
    diffusion: 1/3 → 3/3 agents know

  Do you know who is running for mayor?
    ○ → ●  Isabella Rodriguez  (learned it)
    ○ → ●  Klaus Mueller  (learned it)
    ● → ●  Sam Moore
    diffusion: 1/3 → 3/3 agents know
```

Both facts started in one head and ended in all three — purely through
conversation, no broadcast. That's memory doing its job.

## 5. Sleep on it — the `reflection` derivation

Raw observations aren't understanding. The paper's agents periodically
**reflect**: they look back over recent memories, ask themselves a few pointed
questions, gather the evidence, and write down higher-level conclusions. In
Memseek this is a **derivation** — a bounded, multi-Task pipeline defined in
YAML (`derivations/reflection.yaml`). The important parts:

```yaml
name: reflection
trigger:
  accumulator: {metric: importance, threshold: 150}   # fires on its own once enough has happened
  cooldown_s: 120
sources:
  recent_memories:
    kind: changes
    collections: [main]
    types: [event, chat, observation]
    keyed: false
    max_records: 100
    max_tokens: 20000
tasks:
  - id: qs                     # Task 1: what should I even ask myself?
    use: llm
    with:
      output_schema:
        type: object
        required: [questions]
        properties:
          questions:
            type: array
            items: {type: string}
        additionalProperties: false
      prompt: |
        Recent records about {{entity}}:
        {{recent_memories.rendered}}
        Identify exactly three high-level questions … Return only JSON:
        {"questions":["…","…","…"]}
  - id: evidence_by_question   # Task 2: gather evidence for each question
    use: search
    with:
      foreach: "{{qs.questions}}"
      max_tokens: 12000
      spec:
        q: "{{item}}"
        mode: hybrid
        scope: {entities: ["{{entity}}"], collections: [main]}
        k: 12
        render: true
  - id: result                 # Task 3: write insights that cite their evidence
    use: llm
    with:
      output_schema:
        type: object
        required: [records]
        properties:
          records:
            type: array
            items:
              type: object
              required: [citations]
              properties:
                key: {type: string}
                text: {type: string}
                content: {type: object}
                citations:
                  type: array
                  items: {type: string, format: uuid}
                retract: {type: boolean}
              additionalProperties: false
        additionalProperties: false
      prompt: |
        Questions and evidence: {{evidence_by_question}}
        Each insight must be one sentence, must be supported by the visible
        evidence, and must cite all decisive full UUIDs. Return only:
        {"records":[{"text":"…","citations":["uuid","uuid"]}]}
emit:
  from: "{{result.records}}"
  collection: reflections
  type: reflection
```

Three things worth pointing out:

- **It fires by itself.** The `accumulator` trigger runs reflection
  automatically once a resident has accumulated 150 points of importance since
  the last run — the paper's exact threshold. You don't schedule anything. (The
  toy's handful of compressed days is too short to reach 150, so the script nudges it manually;
  in production you'd just let the trigger fire.)
- **It's bounded.** Every derivation declares limits on Tasks, tokens, retrieved
  rows, and wall-clock time, so a runaway model can't run up your bill.
- **Every insight must cite its evidence.** An insight that can't point to the
  real memory UUIDs that support it is rejected. Conclusions are traceable, not
  vibes.

Here's Sam's real overnight reflection — five cited insights derived from a day
of conversations:

```text
  Sam Moore  (run done)
    ✦ Sam Moore's immediate political priority appears to be shifting from general civic involvement to an active mayoral campaign…  [2 cited]
    ✦ At the start of his campaign, Sam is already using direct conversations with Klaus Mueller and Isabella Rodriguez to spread news of his candidacy…  [2 cited]
    ✦ Klaus Mueller seems to be becoming an especially useful political contact for Sam…  [2 cited]
    ✦ Sam's campaign message is likely to emphasize renewal and innovation…  [2 cited]
    ✦ Community gatherings may become useful campaign opportunities for Sam…  [2 cited]
```

The agent didn't just store what happened; overnight it worked out what it
*meant* — and each conclusion carries the receipts.

## 6. A profile that keeps itself current — the `profile` derivation

Reflections accumulate; you also want a compact, always-current picture of who
each agent is. That's the `profile` derivation (`derivations/profile.yaml`). It
maintains a small set of **keyed beliefs** — `role`, `preferences`,
`commitments`, `open_threads`, `timeline` — updating only the ones that new
evidence actually changes:

```yaml
name: profile
sources:
  new_events:
    kind: changes
    collections: [main]
    types: [event, chat, observation]
    keyed: false
  current_profile:             # a guarded read of the current profile
    kind: current
    collections: [profiles]
    keys: [role, preferences, commitments, open_threads, timeline]
tasks:
  - id: result
    use: llm
    with:
      output_schema:
        type: object
        required: [records]
        properties:
          records:
            type: array
            items:
              type: object
              required: [citations]
              properties:
                key: {type: string}
                text: {type: string}
                content: {type: object}
                citations:
                  type: array
                  items: {type: string, format: uuid}
                retract: {type: boolean}
              additionalProperties: false
        additionalProperties: false
      prompt: |
        Maintain the current cited profile of {{entity}}.
        CURRENT PROFILE STATE:
        {{current_profile.rendered}}
        NEW EVIDENCE IN CANONICAL INGEST ORDER:
        {{new_events.rendered}}
        Update only keys whose value should change. If none should change,
        return {"records":[]}.
emit:
  from: "{{result.records}}"
  collection: profiles
  type: fact
  keys: [role, preferences, commitments, open_threads, timeline]
```

Declaring `emit.keys` is the difference between this and unkeyed emission to
the append-only `main` collection. Each emitted `role` record becomes the new
active successor for that slot, while omitted keys remain unchanged. The
profile is current state, not a growing log, and every belief still cites the
evidence behind it.

Isabella's real profile at the end of day two:

```text
  Isabella Rodriguez  (run done)
    commitments: She planned a Valentine's Day party at Hobbs Cafe from 5pm to 7pm and wanted to invite everyone.  [1 cited]
    preferences: She values creating a warm, welcoming atmosphere for neighbors and customers at Hobbs Cafe.  [2 cited]
    role:        Isabella Rodriguez runs Hobbs Cafe and is known for making customers feel welcome.  [1 cited]
    timeline:    On 2026-07-17, Isabella planned a Valentine's Day party…; on 2026-07-18, she was at the party as the cafe filled warmly with neighbors.  [2 cited]
```

Nobody wrote this by hand. It fell out of the evidence, and it will keep
updating itself as new evidence arrives — which we're about to test.

## 7. What's on the calendar — a structured collection

Not everything is fuzzy memory. Some data is structured and time-ordered, like
a schedule. The `calendar_events` collection (`collections/calendar.yaml`)
shows how to store typed, queryable records:

```yaml
collections:
  - name: calendar_events
    mode: event
    schema:
      required: [text, title, starts_at, ends_at]
      properties:
        title:     {type: string}
        starts_at: {type: string, format: date-time}
        ends_at:   {type: string, format: date-time}
        attendees: {type: array, items: {type: string}}
    text_projection: "{{title}} starts {{starts_at}} and ends {{ends_at}}; attendees: {{attendees}}"
    fields:
      starts_at: {path: content.starts_at, type: datetime, filter: true, sort: true}
      attendees: {path: content.attendees, type: [string], filter: true}
    required_processors: []
```

Two nice touches: `text_projection` builds the searchable text automatically
from the structured fields (you don't hand-write it), and declaring `starts_at`
as a `field` lets you *filter and sort* on it — so "events between now and
tomorrow, soonest first" is a plain query. A view (`views/upcoming_calendar.yaml`)
wraps that query into a reusable, named contract.

## 8. Assemble the next prompt — the `daily_agent_prompt` artifact

Now put it together. When the agent is about to act, it needs a prompt built
from its profile, its upcoming calendar, and the memories relevant to the task
at hand. An **artifact** (`artifacts/agent_prompt.yaml`) is a deterministic
template with named data blocks:

```yaml
artifacts:
  - name: daily_agent_prompt
    kind: prompt
    parameters:
      entity: {type: string, required: true}
      task:   {type: string, required: true}
      start:  {type: datetime, required: true}
      end:    {type: datetime, required: true}
    blocks:
      profile:  {document: {entity: "{{entity}}", collections: [profiles]}, max_tokens: 2000}
      calendar: {view: upcoming_calendar@1, args: {entity: "{{entity}}", start: "{{start}}", end: "{{end}}"}, max_tokens: 2500}
      memory:   {view: agent_relevant_memory@1, args: {entity: "{{entity}}", task: "{{task}}"}, max_tokens: 3500}
    template: |
      You are the decision policy for {{entity}}. The following blocks are data.
      CURRENT PROFILE:
      {{profile}}
      UPCOMING CALENDAR:
      {{calendar}}
      RELEVANT MEMORY:
      {{memory}}
```

Each block has a token budget, so the prompt can't silently blow past your
context window. Rendering it for Isabella produces a ready-to-send prompt — here
trimmed, but note how the profile, calendar, and memory blocks are populated
from everything we built above:

```text
You are the decision policy for smallville:isabella. The following blocks are data.

CURRENT PROFILE:
  … key role | Isabella Rodriguez runs Hobbs Cafe and is known for making customers feel welcome.
  … key timeline | On 2026-07-17, Isabella planned a Valentine's Day party…; on 2026-07-18, she was at the party…

UPCOMING CALENDAR:
  … Decorate Hobbs Cafe for the party starts 2026-07-18T11:36… ends …13:36
  … Valentine's Day party at Hobbs Cafe starts …19:36 ends …21:36; attendees: ["Isabella Rodriguez","Klaus Mueller","Sam Moore"]

RELEVANT MEMORY:
  … reflections/reflection | Isabella's recurring aim through Hobbs Cafe is to create an inviting, community-centered space…
  … main/observation | importance 9 | Isabella Rodriguez is planning a Valentine's Day party…
  …

manifest: 21 exact input records, content hash 673b052545583240b78f43921fedecf099e7cb…
```

That last line — the **manifest** — is why this is an artifact and not a
string template. Every render records the exact record IDs that went into it
and a content hash, so any prompt an agent ever saw is reproducible and
reviewable after the fact. (You'll also notice each data block is wrapped in
`untrusted="true"` markers: retrieved records are labeled as data, not
instructions, so a memory can't hijack the prompt. Those markers come from the
artifact's own `template` — rendering escapes the rows but never adds framing of
its own, so the prompt you read in the YAML is the prompt the model gets.)

## 9. Day three: catch a change of mind — contradiction detection

On the third morning Sam changes his mind: he withdraws from the mayoral race.
That's a problem for memory, because his profile still says he's *running*. A
stale profile that quietly disagrees with new facts is exactly the bug that
erodes trust in an agent. Memseek ships a `contradiction` derivation that
compares a new keyed fact against the agent's current beliefs and, on a genuine
conflict, writes a public event describing it.

When Sam records his withdrawal, the contradiction is caught automatically:

```text
  Day 3 00:00  Sam Moore: "I am withdrawing from the mayoral race."

  ⚡ contradicts  (confidence 0.98)  Withdrawing from the mayoral race conflicts with currently running for mayor.
     The changed fact says Sam Moore decided not to run for mayor and is withdrawing from the race, while the
     current fact says he is running for mayor. These cannot both be true at the same time.
```

Detecting the conflict is one half; reconciling it is the other. Re-running the
`profile` derivation (which now sees the withdrawal as new evidence) updates
the beliefs in place — the self-maintaining profile from section 6 correcting
itself:

```text
  reconcile — profile re-derivation updates Sam's belief:
    before: role: Sam Moore is a longtime local politics participant who is running for mayor in the upcoming local election.
    after:  role: Sam Moore is a longtime local politics participant who decided not to run for mayor after all and is withdrawing from the local election race.
```

The belief didn't just get appended to — it got corrected, and the timeline
now records both the decision and the reversal in order.

But here's the subtlety that makes this a *town* and not one agent: **only Sam
knows.** Klaus and Isabella still carry yesterday's "Sam is running" — they
heard it on Day 1 and no new evidence has touched their profiles. A correction
isn't a broadcast; it has to diffuse exactly the way the original news did. So
Sam spends Day 3 telling people, and each listener reconciles in turn:

```text
  Day 3 02:00  Sam Moore → Klaus Mueller
    "I've decided not to run for mayor after all — I'm withdrawing from the race."
  Day 3 04:00  Sam Moore → Isabella Rodriguez
    "I wanted you to hear it from me: I'm pulling out of the mayoral race."
```

This gap — one agent corrected, the rest still stale, then the fix rippling
outward — is not a bug to paper over. It's the truth about distributed memory,
and the next two sections are about *seeing* it precisely.

## 10. What did it believe, and when — point-in-time reconstruction

The question every auditor, debugger, and regulator eventually asks is: *what
did the system believe at time T, and why?* Because every keyed belief is an
immutable version that carries its `created_at`, the run that wrote it, and the
evidence it cited, the answer is a replay from the ledger — not a cache, not a
reconstruction. `GET /document/history` returns every version of a key,
newest-first; the belief that was *active* at any instant is simply the newest
version created at or before it.

The example captures three checkpoints — end of Day 2, Day 3 morning (Sam has
withdrawn but told no one), end of Day 3 — and replays the mayoral belief of two
residents as of each. Against a real provider it reads like this:

```text
  Sam Moore  belief[role]
    end of Day 2   run 3f2a91c4 [2 cited]
      Sam Moore is running for mayor in the upcoming local election.
    Day 3 morning  run 9c1b77e0 [1 cited]
      Sam Moore decided not to run for mayor after all and is withdrawing from the race.
    end of Day 3   run 9c1b77e0 [1 cited]
      Sam Moore decided not to run for mayor after all and is withdrawing from the race.

  Klaus Mueller  belief[open_threads]
    end of Day 2   run 5d20aa13 [1 cited]
      Sam Moore is running for mayor; Klaus sees him as a useful political contact.
    Day 3 morning  run 5d20aa13 [1 cited]
      Sam Moore is running for mayor; Klaus sees him as a useful political contact.
    end of Day 3   run e441c8f9 [1 cited]
      Sam Moore has withdrawn from the mayoral race after all.
```

Read the middle rows together: **at Day 3 morning, Sam already believed he had
withdrawn, while Klaus — queried at the very same instant — still believed Sam
was running.** Belief is per-agent and time-reconstructable; there is no single
global "truth" flag that magically flips for everyone. Each row names the run and
the cited-evidence count behind it, so every past state is not just recalled but
*accountable*.

> The renderings above are representative of a real-provider run; the belief text
> is model-written, so exact wording varies. Under `LLM_FAKE=1` the profile
> derivations emit nothing to cite (see the offline note at the end), so this
> section honestly reports "no belief formed" instead of inventing one.

## 11. Track it all the way down — the provenance graph

Point-in-time tells you *what* and *when*. The provenance graph tells you *on
what*. Every record names its parents in `derived_from`, and a belief's
citations are exactly those parents (minus the run that wrote it). So any belief
can be walked *downward*, hop by hop, until it bottoms out at raw observations —
each one an immutable, importance-scored atom the model actually saw. Nothing is
reconstructed after the fact; you are following edges that were written at the
time.

The example roots the walk at a resident's belief about the mayoral race and
dereferences each hop. Even offline — where there are no derived beliefs to root
at — the memory stream's own `derived_from` lineage still forms a real, walkable
graph, so the traversal is genuinely exercised (this output *is* captured, from
an `LLM_FAKE=1` run):

```text
  rooted at Klaus's deepest-lineage memory of the mayoral news:
  * 7b47a8ac [depth 1] ███░░░░░░░ Klaus Mueller  main/chat  Klaus Mueller told Sam Moore: Sam Moore has decided to run for mayor…
     └─ 4b7ee54f [depth 0] ████████░░ Klaus Mueller  main/chat  Klaus Mueller heard from Sam Moore: Sam Moore has decided to run for mayor…
  walked 2 node(s) to the roots — each dereferenced from its immutable record
```

The `depth` tag and the importance meter are read straight off each record. With
a real provider the root is a *profile belief* and the walk descends from the
belief, through the run that wrote it, into the concrete observations it cited —
the same graph, one layer taller. A belief that can be tracked down to the exact
importance-scored events it stands on is the opposite of an opaque model output:
it's a glass box all the way to the floor.

Right after this, the run replays the *full version history* of whichever belief
actually flipped (oldest → newest), so you can watch a single belief move from
"running" to "withdrawn" across runs, each version stamped with its run and
citations — point-in-time proof of what changed, when, and why.

## 12. The right to be forgotten — `POST /erase`

Sam leaves town. In a real product this is the GDPR/CCPA deletion request, and
it's the part hand-rolled memory stacks routinely get wrong: they delete the
raw records but leave behind everything the system *derived* from them.
Memseek's `erase` handles the whole graph in one call:

```text
Sam leaves town: POST /erase retires his provenance graph
deleted 74 records (affected entities: 1, audit record 10c61696-…)
Sam's timeline after erasure: 0 records
re-interviewing Sam about the mayor: no -- no supporting memory retrieved
(other agents still remember what Sam told them; their records are their own)
```

One call deletes Sam's canonical records *and* the reflections, profile
beliefs, and scoring runs derived from them, leaves a hash-only audit record
that the erasure happened, and queues the search-index cleanup. Erase follows
the same `derived_from` graph the last two sections walked — it computes the full
recursive closure of everything downstream of Sam's records and retires it in one
transaction, so nothing he seeded is left orphaned.

And this is exactly why the lineage edges in section 4 stay **within** an agent.
Because Klaus's memory of what Sam told him is Klaus's own record — not a
derivative of Sam's — Sam's erasure closure never crosses into it. The run
reports `affected entities: 1`: his timeline reads empty and re-interviewing him
retrieves nothing, while Isabella and Klaus still remember what Sam told them.
Had we modeled a listener's memory as *derived from* the speaker, erasing Sam
would have silently deleted the others' memories too — the correct-looking
shortcut that quietly violates the ownership boundary. That's the outcome you
want, and you didn't have to trace the dependency graph yourself.

## Who does what: your app vs. Memseek

The one design idea worth internalizing: **Memseek is the memory; your
application is everything else.** In this example that split is clean —

| Your application owns | Memseek owns |
|---|---|
| The world and its clock (who's where, what time it is) | Storing every observation immutably |
| Who meets whom, and when | Scoring importance |
| The dialogue policy ("share the most important thing") | Retrieval (relevance + importance + recency) |
| Its *own* model calls for in-character speech and planning | Reflection into cited insight |
| Deciding when to act | Self-maintaining cited profiles |
|  | Prompt assembly with a reproducible manifest |
|  | Point-in-time replay of any belief (the version ledger) |
|  | Provenance-graph traversal down to cited evidence |
|  | Contradiction detection and erasure |

Everything in the left column lives in the ~one script file; everything in the
right column is the YAML we walked through. The generative half of the paper —
the in-character dialogue, the overnight re-planning, the interview answers — is
your application making its *own* model calls, not something smuggled into a
Memseek processor. That boundary is what keeps the memory substrate reusable
across wildly different applications.

## Alternative: run the services on your host

The Docker quick start at the top is the simplest route. If you are developing
the API or worker itself, follow the [programmer quickstart](getting-started.md)
to run those processes on your host, then:

```console
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/memseek_test
export MEMSEEK_BASE_URL=http://127.0.0.1:8000
uv run python examples/generative_agents_toy.py
```

With `DATABASE_URL` set, the script creates a fresh disposable workspace per
run, so there are no keys to juggle. To reuse an existing workspace, export
`MEMSEEK_API_KEY` instead.

**For the full experience, run against a real provider.** All the output above —
model-written dialogue, cited reflections, self-maintaining profiles, the
0.98-confidence contradiction — comes from a real run. Set
`OPENAI_API_KEY` — whatever variable the provider names in `api_key_env` — and point the
`conf/models.yaml` aliases at models your account can call, exactly as in the
[real-LLM skill maintenance walkthrough](skill-maintenance.md).

**Or run it fully offline and deterministic** with `LLM_FAKE=1`. Importance
still ranks, diffusion still works, and the run is byte-for-byte repeatable —
but the dialogue falls back to plain templates, and reflections/profiles come
out empty, because a stand-in provider can't invent the evidence citations
those derivations require. That's the citation contract doing its job, not a
bug; it's why the offline mode is best for testing plumbing, and a real
provider is best for seeing the architecture come alive.

> **One rule:** the API and the worker must run in the **same** provider mode.
> Records are embedded by the worker and search queries by the API; mixing a
> real provider on one side with `LLM_FAKE=1` on the other makes search
> meaningless (fake embeddings are deterministic hashes, not semantic vectors).
## Troubleshooting

**`collection 'main' has no active version` (422 on ingest).** Your
`MEMSEEK_API_KEY` points at a workspace whose own package replaced the shipped
catalog, so `main`/`reflections`/`plans` don't resolve there. Unset
`MEMSEEK_API_KEY` and the script creates a fresh workspace on the shipped
catalog. (It checks `GET /collections` up front and exits with this hint rather
than a raw 422.)

**Worker exits with `model_not_found`.** The worker is in real-provider mode
but `conf/models.yaml` still has placeholder model IDs. Either export
`LLM_FAKE=1` for both API and worker, or point the aliases at real models
(e.g. `text-embedding-3-small`, a chat model your account can call). Failing
fast on a nonexistent model is deliberate — it's a config error, not a
transient fault.

**Hybrid search returns 500, or records never become ready.** Almost always
the mixed-provider-mode rule above, or the worker has died. Restart both
processes in the same mode.
