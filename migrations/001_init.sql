create extension if not exists vector;

create table workspace (
  id           text primary key
               check (id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
  api_key_hash text not null unique
               check (api_key_hash ~ '^[0-9a-f]{64}$'),
  created_at   timestamptz not null default now()
);

create table record (
  id               uuid primary key default gen_random_uuid(),
  seq              bigint generated always as identity unique,
  workspace        text not null references workspace(id),
  collection       text not null default 'main',
  collection_version int not null check (collection_version >= 1),
  collection_hash  text not null check (collection_hash ~ '^[0-9a-f]{64}$'),
  entity           text not null,
  key              text,
  type             text not null,
  status           text not null default 'active'
                   check (status in ('active', 'draft')),
  content          jsonb not null,
  embedding        vector(1536),
  embedding_space  text,
  scores           jsonb not null default '{}',
  annotations      jsonb not null default '{}',
  annotation_meta  jsonb not null default '{}',
  enrichment_meta  jsonb not null default '{}',
  enrichment_error text,
  enriched_at      timestamptz,
  run_id           uuid,
  depth            smallint not null default 0
                   check (depth between 0 and 16),
  derived_from     uuid[] not null default '{}',
  dedupe_key       text,
  occurred_at      timestamptz not null default now(),
  created_at       timestamptz not null default now(),
  last_accessed    timestamptz not null default now(),
  check (jsonb_typeof(content) = 'object'),
  check (content ? 'text' and jsonb_typeof(content->'text') = 'string'),
  check (jsonb_typeof(scores) = 'object'),
  check (jsonb_typeof(annotations) = 'object'),
  check (jsonb_typeof(annotation_meta) = 'object'),
  check (jsonb_typeof(enrichment_meta) = 'object'),
  check ((embedding is null and embedding_space is null) or
         (embedding is not null and embedding_space is not null)),
  check (cardinality(derived_from) <= 256)
);

create unique index record_dedupe
  on record (workspace, dedupe_key) where dedupe_key is not null;
create index record_entity_seq
  on record (workspace, entity, seq desc);
create index record_collection_entity_seq
  on record (workspace, collection, entity, seq desc);
create index record_keyed_current
  on record (workspace, entity, collection, key, status, seq desc)
  where key is not null;
create index record_type_seq
  on record (workspace, type, seq desc);
create index record_embedding_space
  on record (workspace, embedding_space) where embedding is not null;
create index record_run_id
  on record (workspace, run_id) where run_id is not null;
create index record_run_lookup
  on record (workspace, entity, ((content->>'derivation')), seq desc)
  where type = 'run';
create index record_run_job
  on record (workspace, ((content->>'job_id')), seq desc)
  where type = 'run' and content ? 'job_id';
create index record_pending_enrich
  on record (seq) where enriched_at is null;
create index record_derived_from
  on record using gin (derived_from);
create index record_vec
  on record using hnsw (embedding vector_cosine_ops);
create index record_fts
  on record using gin (to_tsvector('english', content->>'text'));
create index record_annotations
  on record using gin (annotations);

create table cursor (
  workspace  text not null references workspace(id),
  consumer   text not null,
  entity     text not null default '*',
  position   bigint not null default 0 check (position >= 0),
  scope_hash text not null check (scope_hash ~ '^[0-9a-f]{64}$'),
  updated_at timestamptz not null default now(),
  primary key (workspace, consumer, entity)
);

create table job (
  id              uuid primary key default gen_random_uuid(),
  workspace       text not null references workspace(id),
  kind            text not null
                  check (kind in ('derive', 'cron_scan', 'index_upsert', 'index_delete')),
  derivation      text,
  entity          text,
  payload         jsonb not null default '{}'
                  check (jsonb_typeof(payload) = 'object'),
  dedupe_key      text,
  run_after       timestamptz not null default now(),
  attempts        int not null default 0 check (attempts >= 0),
  lease_until     timestamptz,
  locked_by       text,
  done_at         timestamptz,
  dead_at         timestamptz,
  last_error_kind text,
  last_error      text,
  created_at      timestamptz not null default now(),
  check (
    (kind = 'derive' and derivation is not null and entity is not null) or
    (kind = 'cron_scan' and derivation is not null and entity is null) or
    (kind in ('index_upsert', 'index_delete') and derivation is null and entity is null)
  ),
  check (not (done_at is not null and dead_at is not null))
);

create index job_ready
  on job (run_after, created_at)
  where done_at is null and dead_at is null;
create unique index job_active_derive
  on job (workspace, derivation, entity)
  where kind = 'derive' and done_at is null and dead_at is null;
create unique index job_stimulus_dedupe
  on job (workspace, dedupe_key) where dedupe_key is not null;
