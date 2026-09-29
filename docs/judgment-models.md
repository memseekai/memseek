---
title: "Judgment models"
eyebrow: Provider configuration
---

A **judgment model** reads a piece of data and answers questions that you
wrote in advance. It answers with numbers, not text: a probability that the
answer is yes, the option it picked from a list, or a position on a scale you
defined. It never writes a sentence.

Memseek supports TypeSafe's **Jev** models as judgment models. You can use
them in three places:

- in a [processor](processors.md#source-judgment), to label or score each
  record as it arrives;
- in a [derivation](derivations.md#built-in-judge), through the `judge` task,
  to filter or label a batch of records;
- in a [trigger](triggers.md), which can fire a derivation when judgment
  answers add up past a threshold.

This page explains when a judgment model is the right tool, what you can ask
it, and how to connect Memseek to TypeSafe. To see all of it working together,
follow [Triage a support inbox with Jev](support-triage-example.md).

## When to use a judgment model instead of an LLM

An LLM writes text, and your code then has to parse that text and hope it
matches the shape you asked for. A judgment model returns a typed answer for
each question, with a probability for every option. The answer is fast and
cheap, and it tells you how sure the model is.

| You want to... | Use |
| --- | --- |
| Decide yes or no about each record ("does this ask for a refund?") | Judgment model |
| Put each record in one of a few categories | Judgment model |
| Rate each record on a scale you define | Judgment model |
| Keep only the records worth sending to an expensive model | Judgment model, then an LLM |
| Write a summary, a reply, or a new fact | LLM |
| Pull free-form values out of text (names, amounts, dates) | LLM |
| Reason across many records at once | LLM |

The best use of a judgment model is usually in front of an LLM rather than
instead of one. Jev reads a hundred tickets and keeps the five that matter,
and the LLM reads only those five. You pay LLM prices for five records instead
of a hundred.

Jev is billed per input token. For `jev-1.13.0` the price is $0.042 per
million input tokens, and output is free. Check
[TypeSafe's models page](https://docs.typesafe.ai/models) for current prices
and rate limits.

## Ask one of three question types

Every question has a `type`, `instructions` that state the question, and
`criteria` that describe the possible answers. You give each question a name,
and the answer comes back under that name.

### `noul`: a yes-or-no question

`noul` is TypeSafe's name for a yes-or-no question. The answer is the
probability that the answer is yes, from 0 (certainly no) to 1 (certainly
yes). A value near 0.5 means the model cannot tell.

```yaml
needs_reply:
  type: noul
  instructions: "Does `record.text` ask the support team to do something?"
  criteria:                        # optional: what yes and no mean
    true: "A question, a request, or a report that needs action"
    false: "A thank-you, an update, or chatter"
```

The answer looks like `{"noul": 0.93}`.

### `choice`: pick one option

A `choice` question picks one option from a list you define. You can describe
each option, or write `null` when the option name says enough. A choice
needs at least 2 options and allows up to 255.

```yaml
topic:
  type: choice
  instructions: "What is `record.text` mainly about?"
  criteria:
    billing: "Charges, invoices, refunds"
    technical: "Errors, outages, integrations"
    account: "Logins, users, settings"
    other: null
```

The answer names the option with the highest probability, and gives the
probability of every option:

```json
{"choice": "billing",
 "probabilities": {"billing": 0.86, "technical": 0.06, "account": 0.05, "other": 0.03},
 "confidence": 0.81}
```

### `score`: a position on a scale

A `score` question rates the record against levels you list in order, from
lowest to highest. A score needs 2 to 10 levels.

```yaml
frustration:
  type: score
  instructions: "How frustrated is the writer of `record.text`?"
  criteria: ["Calm", "Annoyed", "Angry"]
```

The answer is a number from 0 (the first level) to the number of levels minus
one (the last level). The number can fall between two levels:

```json
{"score": 1.7, "probabilities": [0.05, 0.2, 0.75], "confidence": 0.62}
```

### Read `confidence` before you act on an answer

`choice` and `score` answers include a `confidence` from 0 to 1. TypeSafe
calculates it from how the probabilities are spread. When all the probability
sits on one option, confidence is close to 1. When it is spread evenly,
confidence is close to 0. A `noul` answer has no separate confidence. Its
distance from 0.5 plays the same role.

Use confidence to decide what to do automatically and what to leave for a
person. The `judge` task's [`keep`](derivations.md#built-in-judge) block
exists for exactly that decision.

## Write questions the model can answer

A judgment model reads your instructions literally. These habits give the most
reliable answers:

- **Point at the field you mean.** Put the path in backticks, such as
  `` `record.text` `` in a processor or `` `item.content.text` `` in a `judge`
  task. The model then knows which part of the data the question is about.
- **Ask one thing per question.** Split "Is this urgent and about billing?"
  into two questions, and combine the answers in your definitions.
- **Describe the edge cases in `criteria`.** If you catch yourself explaining
  what you really meant, that explanation belongs in the criteria.
- **Keep arithmetic, counting, and date comparison out of questions.** Jev
  does not calculate reliably. Compute numbers and compare dates in code, and
  ask the model only what needs judgment.
- **Send only the data the question needs.** Unrelated text makes answers
  less accurate.

Questions are fixed text. You cannot use `{{...}}` templates inside them.
Anything that changes from run to run goes into the data the model reads, for
example the `judge` task's `context`. Because questions are fixed, editing a
question changes the definition, and Memseek treats it as a new version.

TypeSafe documents the cases where Jev is known to struggle on its
[Jev 1.13 jaggedness page](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
Read it before you rely on a threshold.

## Connect Memseek to TypeSafe

To use Jev, you need a TypeSafe API key and two entries in your catalog's
`conf/models.yaml`.

1. Create an API key in the [TypeSafe console](https://console.typesafe.ai/).
2. Set the key as `TYPESAFE_API_KEY` for both the API and the worker. The
   worker is the process that calls the model, so it must have the key. If you
   run Memseek with Docker Compose, put the key in the repository's `.env`
   file:

   ```sh
   TYPESAFE_API_KEY=ts_...
   ```

   `docker-compose.yml` passes it through to the `api` and `worker` services.
   If you run the processes yourself, export it in each terminal.

3. Declare the provider and an alias in `conf/models.yaml`:

   ```yaml
   providers:
     typesafe:
       adapter: typesafe
       base_url: https://api.typesafe.ai
       api_key_env: TYPESAFE_API_KEY   # the variable name, never the key
       json_capability: none           # Jev answers are typed by the questions

   aliases:
     jev:
       targets: ["typesafe:jev-1.13.0"]
   ```

4. Check the catalog before you publish it:

   ```sh
   uv run memseek catalog-validate --dir <your catalog>
   ```

### Pin a model version

Write a version such as `jev-1.13.0` in the alias, not `jev-latest`.
`jev-latest` moves to each new release, and a new release can shift the
probabilities that your thresholds depend on. With a pinned version you choose
when to upgrade, and you can check your thresholds first.

Every run records the version that TypeSafe reports having used. The run's
model audit shows `typesafe:jev-1.13.0` even when an alias points at
`jev-latest`.

### Keep judgment aliases separate from LLM aliases

An alias is either a judgment alias or an LLM alias. Memseek works out which
from the providers the alias targets, and checks every use when the catalog
loads:

- An LLM processor, the `llm` task, an agent, and the `defaults` block need an
  LLM alias.
- A judgment processor and the `judge` task need a judgment alias.
- A judgment alias takes no `params` and no `context_tokens`.

If you use the wrong kind, `catalog-validate` fails with a message such as
`model alias 'jev' cannot be used for completion`, and names the file and the
field.

## Develop without a TypeSafe key

With `LLM_FAKE=1`, Memseek answers every question with a fixed placeholder
instead of calling TypeSafe:

| Question type | Placeholder answer |
| --- | --- |
| `noul` | `0.5` |
| `choice` | the first option, with equal probability for every option |
| `score` | the middle level |

Use fake mode to check that your definitions load and your records flow. Do
not use it to tune thresholds. A `keep` block with a real threshold usually
rejects every placeholder answer, so a fake run can look empty.

## Know the limits

| Limit | Value |
| --- | --- |
| Questions in one processor or one `judge` task | 1 to 64 |
| Options in a `choice` question | 2 to 255 |
| Levels in a `score` question | 2 to 10 |
| Input | Text and JSON only. No images, audio, or files. |
| Size of one call (`jev-1.13.0`) | 32,000 tokens for the data plus the longest question |
| Judgment calls in one derivation run | `limits.max_judgments`, default 0 |
| Judgment input tokens in one derivation run | `limits.max_judgment_tokens`, default 400,000 |

A processor sends the record's text trimmed from the middle to
`SCORER_TEXT_CHARS` characters (12,000 by default), so a long record still
fits in one call.

## What happens when a call fails

Memseek retries a failed call once. If TypeSafe answers `429 Too Many
Requests` with a `retry-after` header, Memseek waits that long, up to 30
seconds, before the retry. An answer that is missing a question, or that holds
a number out of range, counts as a failure and is retried the same way.

If the retry also fails, the outcome depends on where the question was asked:

- A score processor writes its `default` value.
- A json processor writes its `default_output`. If it has none, the record is
  marked as failed for that processor and is not retried in a loop.
- A `judge` task fails the derivation run, and the run's error names the
  judgment that failed.

A missing or wrong API key is not retried. TypeSafe rejects the request, and
the processor falls back as above, or the derivation run fails.
