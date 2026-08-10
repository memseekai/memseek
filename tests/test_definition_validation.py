"""Negative validation matrix for the complete declarative catalog.

Every case copies the shipped deployment assets and enters through the public
``load_definition_catalog`` boundary. This keeps the tests representative of
startup rather than testing isolated model implementation details.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from reference_catalog import materialize_reference_catalog

from memseek.config import Settings
from memseek.definitions import (
    DefinitionError,
    ParameterDefinition,
    load_definition_catalog,
    parameter_json_schema,
    parameters_json_schema,
)
from memseek.definitions.models import parameter_value_matches
from memseek.derive.schema import PipelineDefinition
from memseek.search.registry import SEARCH_BACKENDS


def _copy_catalog(destination: Path) -> Path:
    return materialize_reference_catalog(destination)


@pytest.fixture
def catalog_root(tmp_path: Path) -> Path:
    return _copy_catalog(tmp_path / "catalog")


def _settings(
    root: Path,
    *,
    max_derivation_depth: int = 4,
    search_profile_overrides_file: Path | None = None,
    turbopuffer_api_key: str = "",
) -> Settings:
    return Settings(
        models_file=root / "conf/models.yaml",
        processors_file=root / "conf/processors.yaml",
        rank_default_file=root / "conf/rank_default.yaml",
        search_profiles_file=root / "conf/search_profiles.yaml",
        collections_dir=root / "collections",
        derivations_dir=root / "derivations",
        triggers_dir=root / "triggers",
        views_dir=root / "views",
        artifacts_dir=root / "artifacts",
        mcp_dir=root / "mcp",
        packages_dir=root / "packages",
        max_derivation_depth=max_derivation_depth,
        search_profile_overrides_file=search_profile_overrides_file,
        turbopuffer_api_key=turbopuffer_api_key,
        llm_fake=True,
    )


def _replace(path: Path, old: str, new: str, *, count: int = 1) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"test mutation marker not found in {path}: {old!r}"
    path.write_text(source.replace(old, new, count), encoding="utf-8")


def _write_trigger(root: Path, name: str, document: str) -> Path:
    path = root / "triggers" / name
    path.write_text(document, encoding="utf-8")
    return path


def _load_error(
    root: Path,
    *,
    code: str | None = None,
    settings: Settings | None = None,
) -> DefinitionError:
    with pytest.raises(DefinitionError) as caught:
        load_definition_catalog(settings or _settings(root))
    error = caught.value
    if code is not None:
        assert error.code == code
    return error


def test_user_owned_yaml_collection_derivation_view_and_package(catalog_root: Path) -> None:
    (catalog_root / "collections/customer.yaml").write_text(
        """collections:
  - name: customer_events
    version: 1
    active: true
    mode: mixed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    search_profile: pg_default

  - name: customer_profiles
    version: 1
    active: true
    mode: keyed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
        tombstone: {type: boolean}
      additionalProperties: true
    search_profile: pg_default
""",
        encoding="utf-8",
    )
    (catalog_root / "derivations/customer_profile.yaml").write_text(
        """name: customer_profile
trigger:
  write:
    collections: [customer_events]
    types: [event]
    statuses: [active]
sources:
  new_events:
    kind: changes
    collections: [customer_events]
    types: [event]
    statuses: [active]
    keyed: false
    max_records: 100
    max_tokens: 12000
  current_profile:
    kind: current
    collections: [customer_profiles]
    types: [fact]
    statuses: [active]
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 100
  max_total_tokens: 20000
  max_wall_s: 90
model: strong
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
        Update {{entity}} using the current state {{current_profile.rendered}}
        and events {{new_events.rendered}}. Return only {"records":[]}.
emit:
  from: "{{result.records}}"
  collection: customer_profiles
  type: fact
  keys: [summary]
""",
        encoding="utf-8",
    )
    (catalog_root / "views/customer_context.yaml").write_text(
        """views:
  - name: customer_context
    version: 1
    active: true
    parameters:
      entity: {type: string, required: true}
      task: {type: string, required: true}
    query:
      q: "{{task}}"
      mode: text
      scope:
        entities: ["{{entity}}"]
        collections: [customer_events]
        types: [event]
      k: 10
      render: true
""",
        encoding="utf-8",
    )
    (catalog_root / "packages/customer_memory.yaml").write_text(
        """name: customer_memory
version: 1.0.0
collections: [customer_events@1, customer_profiles@1]
processors: [customer_profile]
triggers: [customer_profile.default]
views: [customer_context@1]
search_profiles: [pg_default]
""",
        encoding="utf-8",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    assert catalog.resolve_collection("customer_events").version == 1
    processor = catalog.resolve_processor("customer_profile")
    assert isinstance(processor, PipelineDefinition)
    assert processor.emit.collection == "customer_profiles"
    assert catalog.resolve_view("customer_context").query["scope"]["collections"] == [
        "customer_events"
    ]
    assert catalog.resolve_package("customer_memory", "1.0.0").triggers == (
        "customer_profile.default",
    )


def test_alias_target_must_name_a_declared_provider(catalog_root: Path) -> None:
    _replace(
        catalog_root / "conf/models.yaml",
        'targets: ["openai:gpt-5.4-2026-03-05"]',
        'targets: ["anthropic:gpt-5.4-2026-03-05"]',
    )

    error = _load_error(catalog_root)

    assert "undeclared provider" in str(error)


def test_embedding_block_must_name_a_declared_provider(catalog_root: Path) -> None:
    _replace(catalog_root / "conf/models.yaml", "  provider: openai\n", "  provider: voyage\n")

    error = _load_error(catalog_root)

    assert "undeclared provider" in str(error)


def test_embed_alias_is_rejected_in_favor_of_the_embedding_block(catalog_root: Path) -> None:
    """The embedding model is its own declaration, not a specially named alias."""

    _replace(
        catalog_root / "conf/models.yaml",
        "  cheap:\n",
        '  embed:\n    targets: ["openai:text-embedding-3-small"]\n\n  cheap:\n',
    )

    error = _load_error(catalog_root)

    assert "embedding: block" in str(error)


def test_provider_base_url_requires_https_off_localhost(catalog_root: Path) -> None:
    _replace(
        catalog_root / "conf/models.yaml",
        "base_url: https://api.openai.com/v1",
        "base_url: http://api.example.test/v1",
    )

    error = _load_error(catalog_root)

    assert "HTTPS" in str(error)


def test_provider_may_point_embeddings_at_a_second_endpoint(catalog_root: Path) -> None:
    """The whole point: embeddings on a different service than completions."""

    _replace(
        catalog_root / "conf/models.yaml",
        "    api_key_env: OPENAI_API_KEY\n",
        "    api_key_env: OPENAI_API_KEY\n"
        "  voyage:\n"
        "    adapter: openai_compat\n"
        "    base_url: https://api.voyageai.com/v1\n"
        "    api_key_env: VOYAGE_API_KEY\n"
        "    json_capability: none\n",
    )
    _replace(catalog_root / "conf/models.yaml", "  provider: openai\n", "  provider: voyage\n")
    _replace(
        catalog_root / "conf/models.yaml",
        "  model: text-embedding-3-small\n",
        "  model: voyage-3\n  params: {input_type: document}\n",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    assert catalog.models.embedding.target == "voyage:voyage-3"
    assert catalog.models.embedding.params == {"input_type": "document"}
    # Completion aliases are untouched and still resolve to their own endpoint.
    assert catalog.models.aliases["strong"].targets[0].startswith("openai:")
    assert catalog.models.providers["voyage"].base_url == "https://api.voyageai.com/v1"
    assert catalog.models.providers["voyage"].api_key_env == "VOYAGE_API_KEY"


def test_embedding_params_cannot_hijack_the_request(catalog_root: Path) -> None:
    _replace(
        catalog_root / "conf/models.yaml",
        "  model: text-embedding-3-small\n",
        "  model: text-embedding-3-small\n  params: {model: someone-elses-model}\n",
    )

    error = _load_error(catalog_root)

    assert "must not set" in str(error)


def test_changing_the_embedding_model_changes_the_processor_hash(catalog_root: Path) -> None:
    """A vector's meaning depends on the model, so its processor identity must too."""

    before = load_definition_catalog(_settings(catalog_root))
    embedding_processors = [
        name for name, definition in before.processors.items() if definition.kind == "embedding"
    ]
    assert embedding_processors

    _replace(
        catalog_root / "conf/models.yaml",
        "  model: text-embedding-3-small\n",
        "  model: text-embedding-3-large\n",
    )
    after = load_definition_catalog(_settings(catalog_root))

    for name in embedding_processors:
        assert before.processor_config_hashes[name] != after.processor_config_hashes[name]


@pytest.mark.parametrize(
    ("relative_path", "old", "new", "code"),
    [
        (
            "conf/processors.yaml",
            "model: importance_scorer",
            "model: missing_alias",
            "reference",
        ),
        (
            "collections/core.yaml",
            "required_processors: [embedding_v1, importance]",
            "required_processors: [embedding_v1, missing_processor]",
            "reference",
        ),
        (
            "derivations/harvest.yaml",
            "kind: changes\n    collections: [transcripts]",
            "kind: changes\n    collections: [missing_collection]",
            "reference",
        ),
        (
            "views/upcoming_calendar.yaml",
            "collections: [calendar_events]",
            "collections: [missing_collection]",
            "reference",
        ),
        (
            "artifacts/agent_prompt.yaml",
            "view: upcoming_calendar@1",
            "view: upcoming_calendar@999",
            "reference",
        ),
        (
            "packages/agentic_memory_core.yaml",
            "main@1",
            "main@999",
            "reference",
        ),
    ],
)
def test_definition_families_reject_missing_references(
    catalog_root: Path,
    relative_path: str,
    old: str,
    new: str,
    code: str,
) -> None:
    changed = catalog_root / relative_path
    _replace(changed, old, new)

    error = _load_error(catalog_root, code=code)

    assert error.file == str(changed)


def test_public_processor_names_collide_across_catalog_families(catalog_root: Path) -> None:
    processors = catalog_root / "conf/processors.yaml"
    _replace(processors, "name: sentiment_v1", "name: importance")

    error = _load_error(catalog_root, code="duplicate")

    assert error.file == str(processors)


def test_trigger_names_collide_between_inline_and_standalone_definitions(
    catalog_root: Path,
) -> None:
    trigger = _write_trigger(
        catalog_root,
        "duplicate.yaml",
        "name: harvest.default\nprocessor: harvest\nread: true\n",
    )

    error = _load_error(catalog_root, code="duplicate")

    assert error.file == str(trigger)


def test_standalone_trigger_rejects_missing_target(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "missing_target.yaml",
        "name: missing.target\nprocessor: absent_processor\nread: true\n",
    )

    error = _load_error(catalog_root, code="reference")

    assert error.file == str(trigger)


def test_write_trigger_rejects_undeclared_predicate_field(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "undeclared_field.yaml",
        """name: profile.undeclared
processor: profile
write:
  collections: [main]
  types: [chat]
  statuses: [active]
  where:
    undeclared_risk: {gte: 0.9}
""",
    )

    error = _load_error(catalog_root)

    assert error.file == str(trigger)
    assert error.path


def test_write_trigger_rejects_wrongly_typed_predicate_operand(catalog_root: Path) -> None:
    collections = catalog_root / "collections/core.yaml"
    _replace(
        collections,
        """      properties:
        text: {type: string}
      additionalProperties: true
    required_processors: [embedding_v1, importance]
""",
        """      properties:
        text: {type: string}
        risk: {type: number}
      additionalProperties: true
    fields:
      risk: {path: content.risk, type: number, filter: true}
    required_processors: [embedding_v1, importance]
""",
    )
    trigger = _write_trigger(
        catalog_root,
        "wrong_operand.yaml",
        """name: profile.wrong_operand
processor: profile
write:
  collections: [main]
  types: [chat]
  statuses: [active]
  where:
    risk: {gte: high}
""",
    )

    error = _load_error(catalog_root)

    assert error.file == str(trigger)
    assert error.path


def test_write_trigger_rejects_field_backed_by_optional_annotation(
    catalog_root: Path,
) -> None:
    collections = catalog_root / "collections/core.yaml"
    _replace(
        collections,
        """      additionalProperties: true
    required_processors: [embedding_v1, importance]
    search_profile: pg_default
""",
        """      additionalProperties: true
    fields:
      sentiment_confidence:
        {path: annotations.sentiment_v1.confidence, type: number, filter: true}
    required_processors: [embedding_v1, importance]
    optional_processors: [sentiment_v1]
    search_profile: pg_default
""",
    )
    trigger = _write_trigger(
        catalog_root,
        "optional_annotation.yaml",
        """name: profile.optional_annotation
processor: profile
write:
  collections: [main]
  types: [chat]
  statuses: [active]
  where:
    sentiment_confidence: {gte: 0.9}
""",
    )

    error = _load_error(catalog_root)

    assert error.file == str(trigger)
    assert error.path


def test_accumulator_rejects_non_numeric_annotation_path(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "bad_accumulator.yaml",
        """name: profile.bad_accumulator
processor: profile
accumulator:
  metric:
    annotation: sentiment_v1
    path: label
    aggregate: sum
  threshold: 1
""",
    )

    error = _load_error(catalog_root)

    assert error.file == str(trigger)


@pytest.mark.parametrize("score_path", ["missing", "label"])
def test_annotation_score_fields_require_existing_numeric_leaves(
    catalog_root: Path,
    score_path: str,
) -> None:
    processors = catalog_root / "conf/processors.yaml"
    _replace(
        processors,
        "sentiment_confidence: confidence",
        f"sentiment_confidence: {score_path}",
    )

    error = _load_error(catalog_root)

    assert error.file == str(processors)
    assert error.path


def test_annotation_score_projection_cannot_collide_with_scorer(catalog_root: Path) -> None:
    processors = catalog_root / "conf/processors.yaml"
    _replace(processors, "sentiment_confidence: confidence", "importance: confidence")

    error = _load_error(catalog_root)

    assert error.file == str(processors)


def test_annotation_input_rejects_unknown_collection(catalog_root: Path) -> None:
    processors = catalog_root / "conf/processors.yaml"
    _replace(processors, "collections: [main]", "collections: [main, missing_collection]")

    error = _load_error(catalog_root, code="reference")

    assert error.file == str(processors)


def test_client_scorer_forbids_a_default_value(catalog_root: Path) -> None:
    processors = catalog_root / "conf/processors.yaml"
    with processors.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n  - name: client_signal\n    kind: score\n    source: client\n"
            "    input: {collections: [main]}\n    scale: [0, 1]\n    default: 0.5\n"
        )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(processors)


def test_pipeline_emit_rejects_required_client_scorer(
    catalog_root: Path,
) -> None:
    processors = catalog_root / "conf/processors.yaml"
    with processors.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n  - name: client_signal\n    kind: score\n    source: client\n"
            "    input: {collections: [profiles]}\n    scale: [0, 1]\n"
        )
    collections = catalog_root / "collections/core.yaml"
    _replace(
        collections,
        "    required_processors: [embedding_v1]\n",
        "    required_processors: [embedding_v1, client_signal]\n",
    )

    error = _load_error(catalog_root, code="required_client_output")

    assert error.file == str(catalog_root / "derivations/profile.yaml")
    assert error.path == "emit.collection"


def test_public_only_collection_may_require_client_scorer(
    catalog_root: Path,
) -> None:
    processors = catalog_root / "conf/processors.yaml"
    with processors.open("a", encoding="utf-8") as stream:
        stream.write(
            "\n  - name: client_signal\n    kind: score\n    source: client\n"
            "    input: {collections: [calendar_events]}\n    scale: [0, 1]\n"
        )
    _replace(
        catalog_root / "collections/calendar.yaml",
        "    required_processors: []\n",
        "    required_processors: [client_signal]\n",
    )
    _replace(
        catalog_root / "packages/agentic_memory_core.yaml",
        "  - importance\n",
        "  - importance\n  - client_signal\n",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    assert "client_signal" in catalog.resolve_collection("calendar_events").required_processors


def test_automatic_processor_cycle_is_rejected(catalog_root: Path) -> None:
    harvest = catalog_root / "derivations/harvest.yaml"
    _replace(harvest, "collections: [transcripts]", "collections: [profiles]", count=2)
    _replace(harvest, "types: [transcript]", "types: [fact]", count=2)
    _replace(harvest, "keyed: false", "keyed: true")

    _load_error(catalog_root, code="automatic_cycle")


def test_automatic_processor_path_must_fit_depth_limit(catalog_root: Path) -> None:
    reflection = catalog_root / "derivations/reflection.yaml"
    _replace(
        reflection,
        """emit:
  from: "{{result.records}}"
  collection: reflections
  type: reflection
""",
        """emit:
  from: "{{result.records}}"
  collection: outcomes
  type: outcome
""",
    )

    _load_error(
        catalog_root,
        code="automatic_depth",
        settings=_settings(catalog_root, max_derivation_depth=1),
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("max_tokens: 30000", "max_tokens: 50001"),
        ("max_llm_calls: 2", "max_llm_calls: 1"),
    ],
)
def test_derivation_static_budgets_are_rejected(
    catalog_root: Path,
    old: str,
    new: str,
) -> None:
    _replace(catalog_root / "derivations/harvest.yaml", old, new)

    _load_error(catalog_root, code="budget")


def test_view_rejects_capability_missing_from_bound_backend(
    catalog_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = SEARCH_BACKENDS["pg"]
    capabilities = frozenset(
        capability for capability in descriptor.capabilities if capability != "structured"
    )
    monkeypatch.setitem(
        SEARCH_BACKENDS,
        "pg",
        replace(descriptor, capabilities=capabilities),
    )

    _load_error(catalog_root, code="capability")


def test_reviewed_artifact_candidate_must_emit_draft_state(catalog_root: Path) -> None:
    skill = catalog_root / "derivations/skill.yaml"
    _replace(skill, "  review: required", "  review: null")

    error = _load_error(catalog_root, code="artifact_lifecycle")

    assert error.file == str(catalog_root / "artifacts/skill.yaml")


@pytest.mark.parametrize("reference", ["main", "main@999"])
def test_package_collection_references_are_exact_and_present(
    catalog_root: Path,
    reference: str,
) -> None:
    package = catalog_root / "packages/agentic_memory_core.yaml"
    _replace(package, "main@1", reference)

    error = _load_error(catalog_root)

    assert error.file == str(package)
    assert error.code in {"package_reference", "reference"}


@pytest.mark.parametrize(
    "manifest_entry",
    ["  - harvest\n", "  - upcoming_calendar@1\n"],
)
def test_package_requires_transitive_trigger_and_artifact_dependencies(
    catalog_root: Path,
    manifest_entry: str,
) -> None:
    package = catalog_root / "packages/agentic_memory_core.yaml"
    _replace(package, manifest_entry, "")

    error = _load_error(catalog_root)

    assert error.file == str(package)


def test_package_ignores_unlisted_standalone_trigger_dependencies(
    catalog_root: Path,
) -> None:
    derivation = catalog_root / "derivations/harvest.yaml"
    optional_derivation = catalog_root / "derivations/optional_harvest.yaml"
    source = derivation.read_text(encoding="utf-8")
    source = source.replace("name: harvest", "name: optional_harvest", 1)
    source = source.replace(
        "trigger:\n"
        "  write:\n"
        "    collections: [transcripts]\n"
        "    types: [transcript]\n"
        "    statuses: [active]\n",
        "",
        1,
    )
    optional_derivation.write_text(source, encoding="utf-8")
    _write_trigger(
        catalog_root,
        "optional_harvest.yaml",
        "name: optional_harvest.default\n"
        "processor: optional_harvest\n"
        "write:\n"
        "  collections: [transcripts]\n"
        "  types: [transcript]\n"
        "  statuses: [active]\n",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    assert catalog.resolve_trigger("optional_harvest.default").processor == "optional_harvest"
    assert (
        "optional_harvest" not in catalog.resolve_package("agentic_memory_core", "2.2.0").processors
    )


def test_package_ignores_unlisted_inactive_collection_versions(catalog_root: Path) -> None:
    collections = catalog_root / "collections/core.yaml"
    source = collections.read_text(encoding="utf-8")
    source += """

  - name: main
    version: 2
    active: false
    mode: mixed
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    required_processors: [embedding_v1, importance]
    optional_processors: [sentiment_v1]
    search_profile: pg_default
"""
    collections.write_text(source, encoding="utf-8")

    catalog = load_definition_catalog(_settings(catalog_root))

    assert catalog.resolve_collection("main").version == 1
    assert catalog.resolve_collection("main", 2).active is False
    assert "sentiment_v1" not in catalog.resolve_package("agentic_memory_core", "2.2.0").processors


def test_catalog_definition_payloads_are_recursively_immutable(catalog_root: Path) -> None:
    catalog = load_definition_catalog(_settings(catalog_root))
    collection = catalog.resolve_collection("main")
    properties = cast(MutableMapping[str, Any], collection.content_schema["properties"])
    text_schema = cast(MutableMapping[str, Any], properties["text"])

    with pytest.raises(TypeError):
        text_schema["type"] = "number"


def test_deployment_binding_must_be_allowed_by_every_collection_version(
    catalog_root: Path,
) -> None:
    collections = catalog_root / "collections/core.yaml"
    with collections.open("a", encoding="utf-8") as stream:
        stream.write(
            """

  - name: reflections
    version: 2
    active: false
    mode: event
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    required_processors: [embedding_v1]
    search_profile: pg_default
    allowed_search_profiles: [pg_default]
"""
        )
    overrides = catalog_root / "conf/overrides.yaml"
    overrides.write_text(
        "collection_profiles:\n  reflections: memory_tpuf\n",
        encoding="utf-8",
    )

    _load_error(
        catalog_root,
        code="deployment_binding",
        settings=_settings(
            catalog_root,
            search_profile_overrides_file=overrides,
            turbopuffer_api_key="test-key",
        ),
    )


def test_search_profile_cannot_override_its_mapping_identity(catalog_root: Path) -> None:
    profiles = catalog_root / "conf/search_profiles.yaml"
    _replace(
        profiles,
        "  pg_default:\n    backend: pg",
        "  pg_default:\n    name: impostor\n    backend: pg",
    )

    error = _load_error(catalog_root)

    assert error.file == str(profiles)


def test_semantically_equal_rank_numbers_have_identical_hashes(tmp_path: Path) -> None:
    first_root = _copy_catalog(tmp_path / "first")
    second_root = _copy_catalog(tmp_path / "second")
    _replace(
        second_root / "conf/rank_default.yaml",
        "1.0",
        "1",
        count=-1,
    )

    first = load_definition_catalog(_settings(first_root))
    second = load_definition_catalog(_settings(second_root))

    assert first.rank_hash == second.rank_hash
    assert first.catalog_hash == second.catalog_hash


def test_computed_definition_hash_is_not_accepted_as_yaml_input(catalog_root: Path) -> None:
    collections = catalog_root / "collections/core.yaml"
    _replace(
        collections,
        "    active: true\n    mode: mixed",
        f"    active: true\n    definition_hash: {'0' * 64}\n    mode: mixed",
    )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(collections)


def test_extended_trigger_conditions_load_and_normalize(catalog_root: Path) -> None:
    _write_trigger(
        catalog_root,
        "session_quiet.yaml",
        """name: profile.session_quiet
processor: profile
quiet:
  collections: [main]
  types: [chat]
  statuses: [active]
  after_s: 900
debounce_s: 30
""",
    )
    _write_trigger(
        catalog_root,
        "calendar_at.yaml",
        """name: profile.calendar_at
processor: profile
at:
  collections: [calendar_events]
  statuses: [active]
  field: starts_at
  offset_s: -3600
""",
    )
    _write_trigger(
        catalog_root,
        "max_importance.yaml",
        """name: profile.max_importance
processor: profile
accumulator:
  metric: {scorer: importance, aggregate: max}
  threshold: 8
  comparison: gte
""",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    quiet = catalog.triggers["profile.session_quiet"]
    assert quiet.quiet is not None
    assert quiet.quiet.after_s == 900
    assert quiet.debounce_s == 30
    assert quiet.quiet.collection_versions == {"main": (1,)}
    at = catalog.triggers["profile.calendar_at"].at
    assert at is not None
    assert at.field == "starts_at"
    assert at.offset_s == -3600
    metric = catalog.triggers["profile.max_importance"].accumulator
    assert metric is not None
    assert not isinstance(metric.metric, str)
    assert metric.metric.scorer == "importance"
    assert metric.metric.aggregate == "max"


def test_quiet_trigger_rejects_scope_outside_driving_source(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "quiet_outside.yaml",
        """name: profile.quiet_outside
processor: profile
quiet:
  collections: [transcripts]
  statuses: [active]
  after_s: 60
""",
    )

    error = _load_error(catalog_root, code="trigger_scope")

    assert error.file == str(trigger)


def test_at_trigger_rejects_non_datetime_field(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "at_wrong_type.yaml",
        """name: profile.at_wrong_type
processor: profile
at:
  collections: [calendar_events]
  statuses: [active]
  field: external_id
""",
    )

    error = _load_error(catalog_root, code="field_compatibility")

    assert error.file == str(trigger)


def test_at_trigger_rejects_undeclared_field(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "at_undeclared.yaml",
        """name: profile.at_undeclared
processor: profile
at:
  collections: [calendar_events]
  statuses: [active]
  field: absent_deadline
""",
    )

    error = _load_error(catalog_root, code="field_reference")

    assert error.file == str(trigger)


def test_changed_trigger_rejects_unkeyed_scope(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "changed_unkeyed.yaml",
        """name: contradiction.changed_unkeyed
processor: contradiction
changed:
  collections: [profiles]
  statuses: [active]
  keyed: false
""",
    )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(trigger)


def test_census_trigger_rejects_undeclared_predicate_field(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "census_undeclared.yaml",
        """name: profile.census_undeclared
processor: profile
census:
  collections: [main]
  statuses: [active]
  threshold: 3
  where:
    undeclared_field: {gte: 1}
""",
    )

    error = _load_error(catalog_root, code="field_reference")

    assert error.file == str(trigger)


def test_lifecycle_trigger_requires_a_condition(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "lifecycle_empty.yaml",
        """name: profile.lifecycle_empty
processor: profile
lifecycle: {}
""",
    )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(trigger)


def test_accumulator_metric_rejects_scorer_and_annotation_together(
    catalog_root: Path,
) -> None:
    trigger = _write_trigger(
        catalog_root,
        "metric_both.yaml",
        """name: profile.metric_both
processor: profile
accumulator:
  metric: {scorer: importance, annotation: importance, path: value}
  threshold: 5
""",
    )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(trigger)


def test_accumulator_gte_threshold_must_be_positive(catalog_root: Path) -> None:
    trigger = _write_trigger(
        catalog_root,
        "gte_zero.yaml",
        """name: profile.gte_zero
processor: profile
accumulator:
  metric: count
  threshold: 0
""",
    )

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(trigger)


def test_accumulator_lte_comparison_allows_any_finite_threshold(catalog_root: Path) -> None:
    _write_trigger(
        catalog_root,
        "lte_negative.yaml",
        """name: profile.low_mood
processor: profile
accumulator:
  metric: {scorer: importance, aggregate: avg}
  threshold: -0.5
  comparison: lte
""",
    )

    catalog = load_definition_catalog(_settings(catalog_root))

    accumulator = catalog.triggers["profile.low_mood"].accumulator
    assert accumulator is not None
    assert accumulator.comparison == "lte"
    assert accumulator.threshold == -0.5


def test_package_mcp_binds_only_its_declared_targets(catalog_root: Path) -> None:
    package = catalog_root / "packages/agentic_memory_core.yaml"
    _replace(package, "  - upcoming_calendar@1\n", "")

    error = _load_error(catalog_root, code="package_dependency")

    assert error.file == str(package)
    assert error.path == "mcp.tools[2].view"


def test_mcp_view_target_must_be_an_exact_reference(catalog_root: Path) -> None:
    mcp = catalog_root / "mcp/agentic_memory_core.yaml"
    _replace(mcp, "view: agent_relevant_memory@1", "view: agent_relevant_memory")

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(mcp)


def test_learning_target_must_name_a_declared_block(catalog_root: Path) -> None:
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(artifact, "target_block: skill", "target_block: nonexistent")

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(artifact)


def test_learning_target_rejects_a_view_block(catalog_root: Path) -> None:
    # A view block is a ranked selection, not a promotable keyed value, so it
    # cannot identify the exact base version a candidate must replace.
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(artifact, "target_block: skill", "target_block: memory")

    error = _load_error(catalog_root, code="learning_target")

    assert error.file == str(artifact)
    assert error.path == "learning.target_block"


def test_learning_target_block_must_read_active_records(catalog_root: Path) -> None:
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(
        artifact,
        "          collections: [skills]\n          status: active",
        "          collections: [skills]\n          status: all",
    )

    error = _load_error(catalog_root, code="learning_target")

    assert error.file == str(artifact)
    assert error.path == "blocks.skill.document.status"


def test_learning_artifact_must_be_an_exact_reference(catalog_root: Path) -> None:
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(artifact, "artifact: maintained_skill@1", "artifact: maintained_skill")

    error = _load_error(catalog_root, code="schema")

    assert error.file == str(artifact)


def test_learning_artifact_must_exist(catalog_root: Path) -> None:
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(artifact, "artifact: maintained_skill@1", "artifact: maintained_skill@999")

    error = _load_error(catalog_root, code="reference")

    assert error.path == "learning.artifact"


def test_learning_artifact_must_be_reviewed(catalog_root: Path) -> None:
    # Only a reviewed artifact has the draft/promote lifecycle a candidate needs.
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(artifact, "artifact: maintained_skill@1", "artifact: daily_agent_prompt@1")

    error = _load_error(catalog_root, code="learning_target")

    assert error.path == "learning.artifact"


def test_learning_target_collections_must_be_maintained_by_that_artifact(
    catalog_root: Path,
) -> None:
    artifact = catalog_root / "artifacts/agent_prompt.yaml"
    _replace(
        artifact,
        "          collections: [skills]\n          status: active",
        "          collections: [plans]\n          status: active",
    )

    error = _load_error(catalog_root, code="learning_target")

    assert error.path == "blocks.skill.document.collections"


def test_package_must_include_the_learning_target_artifact(catalog_root: Path) -> None:
    package = catalog_root / "packages/agentic_memory_core.yaml"
    _replace(package, "  - maintained_skill@1\n", "")

    error = _load_error(catalog_root, code="package_dependency")

    assert error.file == str(package)


def test_view_fence_requires_render(catalog_root: Path) -> None:
    """A fence with no rendering to wrap is an authoring mistake, not a no-op."""

    view = catalog_root / "views/agent_memory.yaml"
    _replace(view, "      render: true\n", "")

    error = _load_error(catalog_root, code="search_spec")

    assert error.file == str(view)


def test_view_fence_tag_must_be_a_public_name(catalog_root: Path) -> None:
    view = catalog_root / "views/agent_memory.yaml"
    _replace(view, "        tag: records", "        tag: Records Untrusted")

    error = _load_error(catalog_root, code="search_spec")

    assert error.file == str(view)


def test_parameter_constraints_generate_schema_and_validate_values() -> None:
    parameter = ParameterDefinition.model_validate(
        {
            "type": "string",
            "description": "A bounded search intent.",
            "enum": ["brief", "detail"],
            "min_length": 5,
            "max_length": 6,
            "default": "brief",
        }
    )

    assert parameter_value_matches(parameter, "detail")
    assert not parameter_value_matches(parameter, "briefly")
    assert not parameter_value_matches(parameter, "other")
    assert parameters_json_schema({"style": parameter}) == {
        "type": "object",
        "properties": {
            "style": {
                "type": "string",
                "description": "A bounded search intent.",
                "enum": ["brief", "detail"],
                "minLength": 5,
                "maxLength": 6,
                "default": "brief",
            }
        },
        "additionalProperties": False,
    }

    predicates = ParameterDefinition.model_validate(
        {
            "type": "string_array",
            "item_enum": ["founded", "mentions"],
            "max_items": 2,
        }
    )
    assert parameter_value_matches(predicates, ["founded", "mentions"])
    assert not parameter_value_matches(predicates, ["unknown"])
    assert parameter_json_schema(predicates) == {
        "type": "array",
        "items": {"type": "string", "enum": ["founded", "mentions"]},
        "maxItems": 2,
    }
