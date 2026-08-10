"""The compatibility classifier and the additive predicate.

These are pure functions over compiled catalogs, so they are tested directly and
adversarially: the value of an additive verdict is that it is *provable*, which
means every near-miss on the allowlist must be rejected.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from memseek.config import Settings
from memseek.definitions import load_definition_catalog
from memseek.definitions.compat import (
    HashRewrite,
    StoredGroup,
    classify_catalogs,
    contract_verdict,
    plan_stored_groups,
)
from memseek.definitions.hashing import BINDING_FIELDS, CONTRACT_FIELDS
from memseek.definitions.loader import _hashed
from memseek.definitions.models import CollectionDefinition

_BASE: dict[str, Any] = {
    "name": "events",
    "version": 1,
    "active": True,
    "mode": "event",
    "schema": {
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    },
    "required_processors": ["embedding_v1"],
    "search_profile": "pg_default",
}


def _collection(**overrides: Any) -> CollectionDefinition:
    raw = copy.deepcopy(_BASE)
    schema_overrides = overrides.pop("schema_patch", None)
    raw.update(overrides)
    if schema_overrides is not None:
        raw["schema"] = {**raw["schema"], **schema_overrides}
    return _hashed(CollectionDefinition.model_validate(raw))


def test_contract_and_binding_fields_partition_the_definition() -> None:
    """Every authored collection field belongs to exactly one side of the split."""

    authored = {
        field.serialization_alias or name
        for name, field in CollectionDefinition.model_fields.items()
        if name not in {"definition_hash", "contract_hash"}
    }
    assert set(CONTRACT_FIELDS) | set(BINDING_FIELDS) == authored
    assert not set(CONTRACT_FIELDS) & set(BINDING_FIELDS)


def test_binding_edits_leave_the_record_contract_untouched() -> None:
    base = _collection()
    for patch in (
        {"active": False},
        {"optional_processors": ["sentiment_v1"]},
        {"search_profile": "memory_tpuf"},
        {"allowed_search_profiles": ["pg_default", "memory_tpuf"]},
    ):
        changed = _collection(**patch)
        assert changed.contract_hash == base.contract_hash, patch
        if "active" not in patch:
            assert changed.definition_hash != base.definition_hash, patch


def test_adding_an_optional_property_is_additive() -> None:
    base = _collection()
    grown = _collection(
        schema_patch={"properties": {"text": {"type": "string"}, "channel": {"type": "string"}}}
    )
    verdict = contract_verdict(base, grown)
    assert verdict.additive
    assert verdict.added_properties == ("channel",)
    # additionalProperties was false, so the key could not previously exist and
    # no stored value can contradict the new subschema.
    assert verdict.verify_keys == ()


def test_declaring_a_field_over_a_new_property_is_additive() -> None:
    base = _collection()
    grown = _collection(
        schema_patch={"properties": {"text": {"type": "string"}, "channel": {"type": "string"}}},
        fields={"channel": {"path": "content.channel", "type": "string", "filter": True}},
    )
    verdict = contract_verdict(base, grown)
    assert verdict.additive
    assert verdict.added_fields == ("channel",)


def test_relaxing_additional_properties_is_additive() -> None:
    closed = _collection()
    opened = _collection(schema_patch={"additionalProperties": True})
    assert contract_verdict(closed, opened).additive


def test_reordering_required_processors_is_additive() -> None:
    base = _collection(required_processors=["embedding_v1", "importance"])
    reordered = _collection(required_processors=["importance", "embedding_v1"])
    assert base.contract_hash != reordered.contract_hash
    assert contract_verdict(base, reordered).additive


def test_adding_a_property_to_an_open_schema_needs_data_verification() -> None:
    """An open schema already admitted the key, so only real rows can decide."""

    open_base = _collection(schema_patch={"additionalProperties": True})
    grown = _collection(
        schema_patch={
            "additionalProperties": True,
            "properties": {"text": {"type": "string"}, "channel": {"type": "string"}},
        }
    )
    verdict = contract_verdict(open_base, grown)
    assert verdict.additive
    assert verdict.verify_keys == ("channel",)
    assert verdict.needs_data_check


@pytest.mark.parametrize(
    ("patch", "reason"),
    [
        ({"mode": "keyed"}, "mode changed"),
        ({"text_projection": "{{text}}"}, "text_projection changed"),
        ({"required_processors": ["embedding_v1", "importance"]}, "required_processors changed"),
        (
            {
                "schema_patch": {
                    "required": ["text", "channel"],
                    "properties": {"text": {"type": "string"}, "channel": {"type": "string"}},
                }
            },
            "schema.required gained ['channel']",
        ),
        (
            {"schema_patch": {"properties": {"text": {"type": "string", "maxLength": 10}}}},
            "schema.properties.text redefined",
        ),
        (
            {"schema_patch": {"type": "object", "minProperties": 2}},
            "schema.minProperties changed",
        ),
    ],
)
def test_reinterpreting_edits_are_refused_with_a_reason(patch: dict[str, Any], reason: str) -> None:
    verdict = contract_verdict(_collection(), _collection(**patch))
    assert not verdict.additive
    assert reason in verdict.reasons


def test_narrowing_additional_properties_is_reinterpreting() -> None:
    opened = _collection(schema_patch={"additionalProperties": True})
    closed = _collection(schema_patch={"additionalProperties": False})
    verdict = contract_verdict(opened, closed)
    assert not verdict.additive
    assert "schema.additionalProperties narrowed" in verdict.reasons


def test_removing_a_property_or_field_is_reinterpreting() -> None:
    with_extra = _collection(
        schema_patch={"properties": {"text": {"type": "string"}, "channel": {"type": "string"}}},
        fields={"channel": {"path": "content.channel", "type": "string", "filter": True}},
    )
    verdict = contract_verdict(with_extra, _collection())
    assert not verdict.additive
    assert "schema.properties.channel removed" in verdict.reasons
    assert "fields.channel removed" in verdict.reasons


def test_retyping_a_declared_field_is_reinterpreting() -> None:
    before = _collection(
        schema_patch={"properties": {"text": {"type": "string"}, "n": {"type": "number"}}},
        fields={"n": {"path": "content.n", "type": "number", "sort": True}},
    )
    after = _collection(
        schema_patch={"properties": {"text": {"type": "string"}, "n": {"type": "number"}}},
        fields={"n": {"path": "content.n", "type": "string", "sort": True}},
    )
    verdict = contract_verdict(before, after)
    assert not verdict.additive
    assert "fields.n redefined" in verdict.reasons


def test_contract_verdict_refuses_to_compare_different_versions() -> None:
    with pytest.raises(ValueError, match="one collection name and version"):
        contract_verdict(_collection(), _collection(version=2))


def test_classify_reports_binding_edits_as_invisible(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    assert classify_catalogs(catalog, catalog) == ()


def test_plan_rewrites_a_pre_split_stored_hash(settings: Settings) -> None:
    """A stored whole-definition hash predates the split and moves forward."""

    catalog = load_definition_catalog(settings)
    main = catalog.collections[("main", 1)]
    groups = (
        StoredGroup(collection="main", version=1, contract_hash=main.definition_hash, rows=7),
    )
    rewrites, blockers = plan_stored_groups(groups, previous=catalog, incoming=catalog)
    assert blockers == ()
    assert rewrites == (
        HashRewrite(
            collection="main",
            version=1,
            stored_hash=main.definition_hash,
            target_hash=main.contract_hash,
            rows=7,
            reason="generation_upgrade",
        ),
    )


def test_plan_is_a_noop_once_hashes_are_current(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    main = catalog.collections[("main", 1)]
    groups = (StoredGroup(collection="main", version=1, contract_hash=main.contract_hash, rows=3),)
    assert plan_stored_groups(groups, previous=catalog, incoming=catalog) == ((), ())


def test_plan_blocks_an_unexplained_stored_hash(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    groups = (StoredGroup(collection="main", version=1, contract_hash="f" * 64, rows=2),)
    rewrites, blockers = plan_stored_groups(groups, previous=catalog, incoming=catalog)
    assert rewrites == ()
    assert len(blockers) == 1
    assert "no installed definition explains" in blockers[0].reasons[0]


def test_plan_blocks_a_missing_collection_version(settings: Settings) -> None:
    catalog = load_definition_catalog(settings)
    groups = (StoredGroup(collection="gone", version=4, contract_hash="a" * 64, rows=1),)
    _rewrites, blockers = plan_stored_groups(groups, previous=catalog, incoming=catalog)
    assert "does not contain this collection version" in blockers[0].reasons[0]
    assert blockers[0].required_action == "include gone@4 in the package"


def test_a_new_optional_definition_field_does_not_restate_stored_identities() -> None:
    """An unused optional field must stay out of the dump that feeds the hash.

    A workspace catalog is read back by recompiling its stored YAML and checking
    the hash still matches what was persisted. If adding an optional field
    emitted a null for every definition that never declared it, every catalog
    published before the field existed would fail that check with a 503 — the
    service would refuse to serve data it had stored itself.
    """

    from memseek.definitions.hashing import dump_definition, sha256_canonical
    from memseek.definitions.models import McpDefinition, McpToolDefinition

    interface = McpDefinition(
        name="agent_memory",
        version=1,
        title="Agent memory",
        tools=(
            McpToolDefinition(
                name="recall", kind="view", view="memory_recall@1", description="Search."
            ),
            McpToolDefinition(name="record", kind="record", description="Read one."),
        ),
    )
    dumped = dump_definition(interface, semantic=True)

    # No tool binds a collection, so the key must be absent rather than null.
    assert all("collection" not in tool for tool in dumped["tools"])

    # And the hash must equal the one a build without the field would produce.
    legacy = copy.deepcopy(dumped)
    for tool in legacy["tools"]:
        tool.pop("collection", None)
    assert sha256_canonical(dumped) == sha256_canonical(legacy)

    # A tool that does bind one still carries it.
    ingest = McpToolDefinition(
        name="remember", kind="ingest", collection="messages@1", description="Append one."
    )
    assert dump_definition(ingest)["collection"] == "messages@1"
