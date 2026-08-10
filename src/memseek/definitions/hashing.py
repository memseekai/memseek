"""Canonical JSON hashing and the collection identity split.

A record permanently stores the identity of the collection definition that
admitted it, so what that identity covers is a product decision, not an
implementation detail.  It covers exactly the fields that determine how a stored
row is *read* — the **record contract**.  Fields that only determine what else
happens to a row — its optional enrichment and its search routing — are
**bindings**, and deliberately stay out of the persisted identity so they can be
changed without minting a new collection version.

``definition_hash`` still covers the whole definition.  It identifies the
authored definition (and feeds ``catalog_hash``), and because it is unchanged it
remains the lookup key that maps pre-split stored rows onto their contract.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from .errors import DefinitionError

if TYPE_CHECKING:
    from .models import CollectionDefinition

# The fields a stored row's interpretation depends on.  ``name`` and ``version``
# are always included; the rest are listed under their serialization aliases.
CONTRACT_FIELDS = (
    "name",
    "version",
    "mode",
    "schema",
    "text_projection",
    "fields",
    "required_processors",
)

# Everything else in a collection block.  ``active`` selects the default version,
# and the rest bind enrichment, routing, and read permission to it.
BINDING_FIELDS = (
    "active",
    "optional_processors",
    "search_profile",
    "allowed_search_profiles",
    "answerable",
)


def canonical_json(value: Any) -> bytes:
    """Serialize a JSON-compatible value to one deterministic byte string."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DefinitionError("canonical_json", str(exc)) from exc


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def dump_definition(model: BaseModel, *, semantic: bool = False) -> dict[str, Any]:
    """Normalize a definition for hashing.

    ``semantic`` excludes ``active`` so moving the default version between
    versions never restates a definition's identity.
    """

    from .models import VersionedDefinition

    exclude: set[str] = {"definition_hash", "contract_hash"}
    if semantic and isinstance(model, VersionedDefinition):
        exclude.add("active")
    return model.model_dump(mode="json", by_alias=True, exclude=exclude)


def contract_payload(definition: CollectionDefinition) -> dict[str, Any]:
    """Return only the record-contract fields of a collection definition."""

    dumped = dump_definition(definition, semantic=True)
    missing = [field for field in CONTRACT_FIELDS if field not in dumped]
    if missing:
        # A renamed or removed contract field must fail loudly at import time
        # rather than silently shrink what a stored identity covers.
        raise DefinitionError(
            "contract_fields",
            f"collection contract fields are missing from the dump: {missing}",
        )
    return {field: dumped[field] for field in CONTRACT_FIELDS}


def binding_payload(definition: CollectionDefinition) -> dict[str, Any]:
    """Return only the binding fields of a collection definition."""

    dumped = definition.model_dump(
        mode="json", by_alias=True, exclude={"definition_hash", "contract_hash"}
    )
    return {field: dumped[field] for field in BINDING_FIELDS if field in dumped}


def collection_contract_hash(definition: CollectionDefinition) -> str:
    """Hash the record contract — the identity persisted on every record."""

    return sha256_canonical(contract_payload(definition))


__all__ = [
    "BINDING_FIELDS",
    "CONTRACT_FIELDS",
    "binding_payload",
    "canonical_json",
    "collection_contract_hash",
    "contract_payload",
    "dump_definition",
    "sha256_canonical",
]
