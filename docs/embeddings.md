---
title: "Embeddings"
eyebrow: Provider configuration
---

A completion is read once and thrown away. An **embedding is stored**, and then
compared against vectors produced months later. That difference is why the
embedding model is not an alias like `cheap` or `strong`, but its own block in
`conf/models.yaml` with everything that makes two vectors comparable stated in
one place:

```yaml
embedding:
  provider: openai                 # a key from providers:
  model: text-embedding-3-small
  dimensions: 1536
  space: default-v1
  batch: 64                        # optional
  max_text_chars: 16000            # optional
```

Nothing about the embedding model lives in the environment. Point at a different
model and every property that changed is visible in the same diff, in the same
file, next to the space id that has to change with it.

## Fields

- **`provider`** (required) — a key from [`providers:`](models.md). This is the
  endpoint the request goes to, so it can be a different service than your
  completions use.
- **`model`** (required) — the provider's model name.
- **`dimensions`** (required) — the vector width you expect. Every response is
  checked against it, and a response of the wrong width is a transport error
  rather than a silently stored bad vector. It is also checked against the
  database column at startup.
- **`space`** (required) — the name of the vector space these embeddings belong
  to. Vector and hybrid search read **only** the active space.
- **`batch`** (optional, default 64, max 256) — how many texts go in one
  request.
- **`max_text_chars`** (optional, default 16000) — record text longer than this
  is truncated from the middle before embedding, and the annotation records that
  it was truncated.
- **`params`** (optional) — extra request-body fields sent to the endpoint
  verbatim.

## Using a provider other than OpenAI

Declare the endpoint, then point the embedding block at it. Nothing about your
completion aliases changes:

```yaml
providers:
  openai:
    adapter: openai_compat
    base_url: https://api.openai.com/v1
    api_key_env: OPENAI_API_KEY
  embeddings:
    adapter: openai_compat
    base_url: https://api.voyageai.com/v1
    api_key_env: VOYAGE_API_KEY

aliases:
  strong:
    targets: ["openai:large-model"]     # completions still go to OpenAI
    params: {temperature: 0}
    context_tokens: 120000

embedding:
  provider: embeddings                  # embeddings go somewhere else entirely
  model: voyage-3
  dimensions: 1536
  space: voyage3-v1
  batch: 128
  params:
    input_type: document
```

`params` is passed through as-is because every vendor spells its options
differently — OpenAI takes `dimensions`, Voyage takes `input_type`, others take
neither. Memseek deliberately does not invent a neutral vocabulary for them,
because translating between vendor dialects is exactly where a quiet
mistranslation would live. Whatever you put here is sent; the response is still
validated against your declared `dimensions`. The keys `model`, `input`, and
`texts` are reserved and rejected, since overwriting them would detach the
request from the batch the rest of the system believes it sent.

Any endpoint that speaks the OpenAI `/embeddings` shape works through the
`openai_compat` adapter. One that does not needs its own adapter.

## `space` is the promise you are making

Two models' vectors are not comparable — not even approximately. `space` is the
declaration that everything stored under a name came from the same model,
through the same endpoint, with the same preprocessing. Vector and hybrid search
filter on it, so a record embedded under a different space is invisible to
vector recall rather than wrongly ranked.

**Change any field above `space`, and change `space` too.** Reusing a space id
across two models is the one mistake here that produces no error and no
crash — only results that are quietly meaningless.

Because the whole block is part of what an embedding *means*, changing it also
changes the config hash of every embedding processor, so existing rows are
visibly attributed to the model that actually produced them.

## Swapping the model without losing recall

Editing this block does not re-embed anything. Existing rows keep their vectors,
their old space, and their recorded provenance; new rows get the new space; and
vector search only ever reads the active one. The migration is staged
deliberately: `reembed` into the new space while the old one keeps serving reads,
check coverage, cut over, and roll back by cutting over again if you need to.
[Changing definitions](changing-definitions.md#change-the-embedding-model)
walks the whole sequence.

One constraint is not a definition change: `dimensions` must match the
`vector(n)` column the database actually has. The shipped schema is 1536, so
same-dimension swaps are the supported case today; a different width needs a
schema migration first. Startup refuses to run rather than write vectors the
column cannot hold.
