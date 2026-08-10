---
title: "Providers and model aliases"
eyebrow: Provider configuration
---

`conf/models.yaml` is the **one place** a provider's endpoint and model name
appear. Everywhere else in your design — processors, derivations, prompts —
refers to a model by a name you chose, called an **alias**.

This matters more than it looks. Model names change constantly: a vendor
deprecates one, you move to a cheaper one, you switch providers entirely. If
`gpt-...` is written into forty definitions, that becomes a forty-file change
and a risky deploy. With aliases it is one line.

So: never write a provider model name inside a processor, derivation, or prompt.
Define an alias once and use the alias.

An alias is a name in your design — it is not a model and not an API key. See
[Model alias](glossary.md#model-alias) for the distinction.

The embedding model is **not** an alias. It gets its own block, described on
[Embeddings](embeddings.md), because a vector is stored and later compared and
so needs more said about it than a chat model does.

## First name your endpoints, then name the jobs

```yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY

aliases:
  cheap:
    targets: ["openai:small-model"]   # provider:model
    params:
      temperature: 0
      max_output_tokens: 1200
    context_tokens: 60000

  strong:
    targets: ["openai:large-model"]
    params: {temperature: 0, max_output_tokens: 4000}
    context_tokens: 120000

defaults:
  derivation: strong     # used when a derivation doesn't name a model
  fold: strong           # required, currently reserved (see below)
```

Name aliases after the **job** — `cheap`, `strong` — not after the model.
`strong` still means "the good one" after you upgrade it; `gpt_4_turbo` becomes
a lie.

## Providers are connections, not vendors

A `providers:` entry is one **endpoint**. Two entries may use the same adapter
and differ only in `base_url` — that is exactly how you run one model on a
hosted API and another on a local server, or send your embeddings somewhere
other than your completions:

```yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
  local:
    adapter: openai_compat
    base_url: http://localhost:8000/v1     # HTTP is allowed only on localhost
    json_capability: json_object           # this server has no schema mode
```

Provider fields:

- **`adapter`** (required) — which protocol implementation to speak.
  `openai_compat` handles any endpoint that speaks the OpenAI API; `fake` is a
  deterministic stand-in for local development and tests.
- **`base_url`** (required) — the endpoint's absolute URL. HTTPS is required
  except on `localhost`.
- **`api_key_env`** (optional) — the **name** of the environment variable
  holding this endpoint's key, e.g. `OPENAI_API_KEY`. The key itself never
  appears in YAML, so your design stays safe to commit and to share between
  environments — but which credential an endpoint uses is stated, not guessed.
  Omit it for an endpoint that needs no key.
- **`json_capability`** (optional, default `json_schema`) — how strictly this
  endpoint can be asked for structured output. See below.
- **`json_schema_strict`** (optional, default `false`) — send `strict: true`
  with a JSON-schema request. Requires `json_capability: json_schema`.
- **`token_limit_field`** (optional, default `max_completion_tokens`) — set it
  to `max_tokens` for legacy OpenAI-compatible servers that require that spelling.

These last three describe the *endpoint*, so an endpoint that cannot honor
schema-constrained output says so next to its own URL rather than in a
process-wide setting that would also, wrongly, describe every other endpoint.

## Alias fields

- **`targets`** (required) — one or more `provider:model` strings, where
  `provider` is a key you declared in `providers:`. Listing more than one gives
  the extras as fallbacks, tried in order.
- **`params`** (optional) — generation settings passed through to the provider.
  `temperature` must be between 0 and 2, and `max_output_tokens` must be a
  positive whole number. Options the provider doesn't support are rejected
  rather than silently dropped.
- **`context_tokens`** (optional) — how large a prompt this model accepts. This
  is used to budget prompts before they are sent, so a derivation fails loudly
  at build time rather than being truncated by the provider. Must be at least
  4096; the deployment default is 60,000.

## Rules the file must satisfy

- You need **at least one provider** and **at least one alias**.
- Every alias target must name a provider you declared. An unknown provider is
  a startup error, not a runtime surprise.
- You must declare an **`embedding:`** block — see [Embeddings](embeddings.md).
  There is no alias named `embed`; using that name is an error that points you
  at the block.
- **`defaults.derivation`** and **`defaults.fold`** are both required, and both
  must name an alias you defined above.
    - `defaults.derivation` is what a model step falls back to when neither the
      step nor its derivation names one.
    - `defaults.fold` is validated but not yet used by anything at runtime.
      Declare it — pointing it at the same alias as `derivation` is the usual
      choice — and do not expect changing it to affect behavior today.

## Who picks which alias

| The thing calling a model | How it chooses |
| --- | --- |
| An LLM-backed [processor](processors.md) | Its own `model:` field. |
| An embedding processor | The `embedding:` block; it has no choice to make. |
| A model step inside a [derivation](derivations.md) | The step's `model:`, else the derivation's `model:`, else `defaults.derivation`. |

Changing an alias's target or params shows up in the record of every run that
used it, so an unexplained change in output can always be traced to a model
change. The alias **name** is the stable thing your definitions depend on.

## Structured output from models

When a derivation asks a model for structured data, Memseek can request it in
one of three ways, in decreasing order of strictness:

| Mode | What is asked of the provider |
| --- | --- |
| `json_schema` | Return output conforming to this exact schema. |
| `json_object` | Return valid JSON, shape unconstrained. |
| `none` | Return plain text. |

Each provider declares the strongest mode it supports with `json_capability`.
Lower it to `json_object` or `none` only when that endpoint genuinely does not
support schema-constrained output. This is a choice made *before* the request,
not a fallback after one fails: Memseek never quietly retries a rejected request
with a weaker format, because that would hide a misconfigured provider behind
degraded results.

`json_schema_strict` is off by default because compatible endpoints support
different subsets of JSON Schema, and a strict request that one provider accepts
another will reject.

Whichever mode is used, Memseek validates the returned data against your full
schema itself. You get the same guarantee regardless of what the provider was
willing to enforce.
