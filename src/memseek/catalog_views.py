"""Read-only definition catalog payloads for the HTTP API.

`GET /collections`, `GET /processors`, and `GET /triggers` publish exactly
the loaded, normalized startup snapshot so operators can audit routing,
semantic identities, and hashes without reading deployment YAML.
"""

from __future__ import annotations

from typing import Any

from memseek.definitions import DefinitionCatalog


def collections_payload(catalog: DefinitionCatalog) -> dict[str, Any]:
    collections = []
    for (name, version), collection in sorted(catalog.collections.items()):
        binding = catalog.deployment_bindings.get(name)
        collections.append(
            {
                "name": name,
                "version": version,
                "hash": collection.definition_hash,
                # The narrower identity every record persists. Bindings live
                # outside it, so authors can tell which edits strand rows.
                "contract_hash": collection.contract_hash,
                "active": catalog.active_collections.get(name) == version,
                "mode": collection.mode,
                "schema": collection.content_schema,
                "text_projection": collection.text_projection,
                "fields": {
                    field_name: {
                        "path": field.path,
                        "type": list(field.type) if field.is_array else field.type,
                    }
                    for field_name, field in collection.fields.items()
                },
                "required_processors": list(collection.required_processors),
                "optional_processors": list(collection.optional_processors),
                "search_profile": binding if binding is not None else collection.search_profile,
                "declared_search_profile": collection.search_profile,
                "allowed_search_profiles": list(collection.allowed_search_profiles),
            }
        )
    return {"collections": collections}


def processors_payload(catalog: DefinitionCatalog) -> dict[str, Any]:
    """Annotation and derive processors with semantic identities and hashes."""

    processors: list[dict[str, Any]] = []
    for name, processor in sorted(catalog.processors.items()):
        processors.append(
            {
                "name": name,
                "mode": "annotate",
                "kind": processor.kind,
                "source": processor.source,
                "scores": sorted(
                    score for score, owner in catalog.score_owners.items() if owner == name
                ),
                "hash": catalog.processor_config_hashes.get(name),
            }
        )
    for name, derivation in sorted(catalog.derivations.items()):
        processors.append(
            {
                "name": name,
                "mode": "derive",
                "shape": "pipeline",
                "hash": catalog.processor_config_hashes.get(name),
                "definition_hash": derivation.definition_hash,
                "sources": {
                    source_name: source.model_dump(mode="json")
                    for source_name, source in derivation.sources.items()
                },
                "trigger": (
                    None
                    if derivation.trigger is None
                    else derivation.trigger.model_dump(mode="json")
                ),
                "limits": derivation.limits.model_dump(mode="json"),
                "tasks": [{"id": task.id, "use": task.use} for task in derivation.tasks],
                "emit": derivation.emit.model_dump(mode="json", by_alias=True),
            }
        )
    return {"processors": processors}


def triggers_payload(catalog: DefinitionCatalog) -> dict[str, Any]:
    """The normalized trigger catalog: inline defaults plus standalone files."""

    def dumped(value: Any) -> Any:
        return None if value is None else value.model_dump(mode="json")

    triggers = []
    for name, trigger in sorted(catalog.triggers.items()):
        triggers.append(
            {
                "name": name,
                "processor": trigger.processor,
                "hash": trigger.definition_hash,
                "read": trigger.read,
                "accumulator": dumped(trigger.accumulator),
                "cron": dumped(trigger.cron),
                "write": dumped(trigger.write),
                "quiet": dumped(trigger.quiet),
                "at": dumped(trigger.at),
                "changed": dumped(trigger.changed),
                "census": dumped(trigger.census),
                "lifecycle": dumped(trigger.lifecycle),
                "retraction": dumped(trigger.retraction),
                "cooldown_s": trigger.cooldown_s,
                "debounce_s": trigger.debounce_s,
            }
        )
    return {"triggers": triggers}


__all__ = ["collections_payload", "processors_payload", "triggers_payload"]
