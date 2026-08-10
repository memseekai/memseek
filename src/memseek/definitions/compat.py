"""Definition-change classification and compatibility planning.

Publishing a catalog over a workspace that already holds records is the moment
where definition change either stays safe or becomes a migration.  This module is
the single place that decides which, so the preflight report, the publish gate,
and the hash-rewrite command can never disagree about what a change means.

Three classes, defined relative to *stored data* rather than to the YAML text:

``invisible``
    Nothing durable references what changed.  A binding edit, an ``active`` flip,
    a new view.  Always safe.
``additive``
    Something durable references it, but every stored value keeps its meaning —
    either because the definition is new, or because the change is provably a
    superset of what came before.  Safe, and for collections it is applied by
    rewriting stored contract hashes forward.
``reinterpreting``
    Stored values would be read differently.  This needs a new version or a new
    name; the previous definition stays in the catalog while its data exists.

The additive predicate is deliberately closed and short.  Its value is that
acceptance is *provable*, so a case is only added here with a subsumption
argument: every value valid under the previous definition must remain valid under
the incoming one.  Where that cannot be shown structurally — adding a property to
a schema that already allowed arbitrary keys — the predicate reports the keys
that need verifying against real rows instead of guessing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from .hashing import BINDING_FIELDS, contract_payload, dump_definition

if TYPE_CHECKING:
    from .loader import DefinitionCatalog
    from .models import CollectionDefinition, DeclaredField

type ChangeClass = Literal["invisible", "additive", "reinterpreting"]
type ChangeStatus = Literal["added", "removed", "modified"]

# Ordered so a report's verdict is just the maximum over its changes.
_CLASS_ORDER: tuple[ChangeClass, ...] = ("invisible", "additive", "reinterpreting")
_CLASS_RANK: Mapping[str, int] = {name: rank for rank, name in enumerate(_CLASS_ORDER)}


@dataclass(frozen=True, slots=True)
class ContractVerdict:
    """Whether an incoming record contract subsumes the stored one."""

    additive: bool
    reasons: tuple[str, ...] = ()
    """Why the change is not additive, in author-facing terms."""
    verify_keys: tuple[str, ...] = ()
    """Content keys whose existing values must be checked against the new schema."""
    verify_absent_annotations: tuple[str, ...] = ()
    """Annotation names that must not exist yet for a field repoint to be safe."""
    added_properties: tuple[str, ...] = ()
    added_fields: tuple[str, ...] = ()
    repointed_fields: tuple[str, ...] = ()

    @property
    def needs_data_check(self) -> bool:
        return bool(self.verify_keys or self.verify_absent_annotations)


def _json_equal(left: Any, right: Any) -> bool:
    """Compare two already-normalized JSON values."""

    return left == right


def _additional_permits_keys(schema: Mapping[str, Any]) -> bool:
    """Whether a schema already admitted properties it does not name.

    JSON Schema's default for ``additionalProperties`` is ``true``, so an absent
    key permits arbitrary values — the case where a newly declared property can
    contradict data that already exists.
    """

    additional = schema.get("additionalProperties", True)
    return additional is not False


def _schema_verdict(previous: Mapping[str, Any], incoming: Mapping[str, Any]) -> ContractVerdict:
    """Decide whether ``incoming`` accepts every value ``previous`` accepted."""

    reasons: list[str] = []
    added: list[str] = []
    verify: list[str] = []

    structural = {"properties", "required", "additionalProperties"}
    for key in sorted({*previous, *incoming} - structural):
        if not _json_equal(previous.get(key), incoming.get(key)):
            reasons.append(f"schema.{key} changed")

    previous_required = set(previous.get("required", ()))
    incoming_required = set(incoming.get("required", ()))
    if previous_required != incoming_required:
        newly = sorted(incoming_required - previous_required)
        dropped = sorted(previous_required - incoming_required)
        if newly:
            reasons.append(f"schema.required gained {newly}")
        if dropped:
            reasons.append(f"schema.required dropped {dropped}")

    previous_additional = previous.get("additionalProperties", True)
    incoming_additional = incoming.get("additionalProperties", True)
    # Relaxing false -> true only ever admits more values; anything else narrows.
    if not _json_equal(previous_additional, incoming_additional) and not (
        previous_additional is False and incoming_additional is True
    ):
        reasons.append("schema.additionalProperties narrowed")

    previous_properties: Mapping[str, Any] = previous.get("properties") or {}
    incoming_properties: Mapping[str, Any] = incoming.get("properties") or {}
    for name in sorted(previous_properties):
        if name not in incoming_properties:
            reasons.append(f"schema.properties.{name} removed")
        elif not _json_equal(previous_properties[name], incoming_properties[name]):
            reasons.append(f"schema.properties.{name} redefined")
    for name in sorted(set(incoming_properties) - set(previous_properties)):
        added.append(name)
        if _additional_permits_keys(previous):
            # The key was previously unconstrained, so a stored row may already
            # hold a value the new subschema rejects. Only real rows can say.
            verify.append(name)

    return ContractVerdict(
        additive=not reasons,
        reasons=tuple(reasons),
        verify_keys=tuple(verify),
        added_properties=tuple(added),
    )


def contract_verdict(
    previous: CollectionDefinition, incoming: CollectionDefinition
) -> ContractVerdict:
    """Classify a same-``(name, version)`` record-contract change.

    Returns an additive verdict only when every record admitted by ``previous``
    keeps its meaning under ``incoming``.  The allowlist is:

    * a new schema property that is not added to ``required``;
    * a new ``fields`` entry (existing entries must be identical);
    * a ``fields`` entry repointed along a declared supersession chain;
    * ``additionalProperties`` relaxed from ``false`` to ``true``;
    * ``required_processors`` reordered without changing membership.

    Two of those can only be *proved* against real rows, so the verdict names what
    to check rather than assuming: ``verify_keys`` for content values a newly
    declared property could contradict, and ``verify_absent_annotations`` for a
    superseding annotation that must not exist yet.
    """

    if (previous.name, previous.version) != (incoming.name, incoming.version):
        raise ValueError("contract_verdict compares one collection name and version")

    previous_payload = contract_payload(previous)
    incoming_payload = contract_payload(incoming)
    if previous_payload == incoming_payload:
        return ContractVerdict(additive=True)

    reasons: list[str] = []
    if previous_payload["mode"] != incoming_payload["mode"]:
        reasons.append("mode changed")
    if previous_payload["text_projection"] != incoming_payload["text_projection"]:
        reasons.append("text_projection changed")
    if set(previous.required_processors) != set(incoming.required_processors):
        # Readiness gates visibility, so this reinterprets which rows are usable.
        reasons.append("required_processors changed")

    added_fields: list[str] = []
    repointed: list[str] = []
    verify_absent: list[str] = []
    previous_fields = previous_payload["fields"] or {}
    incoming_fields = incoming_payload["fields"] or {}
    for name in sorted(previous_fields):
        if name not in incoming_fields:
            reasons.append(f"fields.{name} removed")
            continue
        if _json_equal(previous_fields[name], incoming_fields[name]):
            continue
        newer = _supersession_repoint(previous.fields.get(name), incoming.fields.get(name))
        if newer is None:
            reasons.append(f"fields.{name} redefined")
            continue
        repointed.append(name)
        verify_absent.append(newer)
    added_fields.extend(sorted(set(incoming_fields) - set(previous_fields)))

    schema = _schema_verdict(previous_payload["schema"], incoming_payload["schema"])
    reasons.extend(schema.reasons)

    return ContractVerdict(
        additive=not reasons,
        reasons=tuple(reasons),
        verify_keys=schema.verify_keys,
        verify_absent_annotations=tuple(sorted(set(verify_absent))),
        added_properties=schema.added_properties,
        added_fields=tuple(added_fields),
        repointed_fields=tuple(repointed),
    )


def _supersession_repoint(
    previous: DeclaredField | None, incoming: DeclaredField | None
) -> str | None:
    """Return the newer annotation name when a field moved along a supersession chain.

    Moving a field from ``annotations.tone_v1.label`` to ``annotations.tone_v2.label``
    where ``tone_v2 supersedes tone_v1`` reads a *superset* of what it read before:
    the loader injects the fallback path, so a row holding only the older annotation
    still answers with the same value.  The only rows that could read differently are
    those that already hold the newer annotation, which is what the caller verifies.
    """

    if previous is None or incoming is None:
        return None
    if previous.path == incoming.path:
        return None
    if previous.path not in incoming.fallback_paths:
        return None
    # Everything except the path must be identical, or this is a real redefinition.
    if previous.model_dump(exclude={"path"}) != incoming.model_dump(exclude={"path"}):
        return None
    root, *parts = incoming.path.split(".")
    if root != "annotations" or not parts:
        return None
    return parts[0]


@dataclass(frozen=True, slots=True)
class DefinitionChange:
    """One definition that differs between the installed and incoming catalog."""

    family: str
    name: str
    status: ChangeStatus
    change_class: ChangeClass
    version: int | None = None
    differing_fields: tuple[str, ...] = ()
    detail: str = ""
    required_action: str = ""
    previous_hash: str | None = None
    incoming_hash: str | None = None

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}" if self.version is not None else self.name

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "family": self.family,
            "name": self.name,
            "status": self.status,
            "class": self.change_class,
        }
        if self.version is not None:
            payload["version"] = self.version
        if self.differing_fields:
            payload["differing_fields"] = list(self.differing_fields)
        if self.detail:
            payload["detail"] = self.detail
        if self.required_action:
            payload["required_action"] = self.required_action
        if self.previous_hash is not None:
            payload["previous_hash"] = self.previous_hash
        if self.incoming_hash is not None:
            payload["incoming_hash"] = self.incoming_hash
        return payload


def _differing_fields(previous: Any, incoming: Any) -> tuple[str, ...]:
    """Name the top-level fields that differ between two definitions."""

    left = dump_definition(previous)
    right = dump_definition(incoming)
    return tuple(
        sorted(key for key in {*left, *right} if left.get(key) != right.get(key)) or ("definition",)
    )


def _collection_change(
    previous: CollectionDefinition, incoming: CollectionDefinition
) -> DefinitionChange | None:
    if previous.definition_hash == incoming.definition_hash:
        return None
    verdict = contract_verdict(previous, incoming)
    fields = _differing_fields(previous, incoming)
    if previous.contract_hash == incoming.contract_hash:
        binding_edits = tuple(item for item in fields if item in BINDING_FIELDS)
        return DefinitionChange(
            family="collection",
            name=previous.name,
            version=previous.version,
            status="modified",
            change_class="invisible",
            differing_fields=binding_edits or fields,
            detail="bindings changed; the record contract is unchanged",
            required_action="none — stored records are unaffected",
            previous_hash=previous.contract_hash,
            incoming_hash=incoming.contract_hash,
        )
    if verdict.additive:
        detail = "record contract grew"
        if verdict.added_properties:
            detail += f"; new schema properties {list(verdict.added_properties)}"
        if verdict.added_fields:
            detail += f"; new declared fields {list(verdict.added_fields)}"
        if verdict.repointed_fields:
            detail += (
                f"; fields {list(verdict.repointed_fields)} now prefer a superseding annotation"
            )
        action = "none — stored contract hashes are rewritten forward on publish"
        if verdict.verify_keys:
            action = (
                "existing values for "
                f"{list(verdict.verify_keys)} are verified against the new schema on publish"
            )
        return DefinitionChange(
            family="collection",
            name=previous.name,
            version=previous.version,
            status="modified",
            change_class="additive",
            differing_fields=fields,
            detail=detail,
            required_action=action,
            previous_hash=previous.contract_hash,
            incoming_hash=incoming.contract_hash,
        )
    return DefinitionChange(
        family="collection",
        name=previous.name,
        version=previous.version,
        status="modified",
        change_class="reinterpreting",
        differing_fields=fields,
        detail="; ".join(verdict.reasons),
        required_action=(
            f"add {previous.name} version {previous.version + 1} with this change and keep "
            f"version {previous.version} in the package"
        ),
        previous_hash=previous.contract_hash,
        incoming_hash=incoming.contract_hash,
    )


_SIMPLE_FAMILIES: tuple[tuple[str, str, bool, str], ...] = (
    # attribute, family label, versioned, required action when modified
    ("processors", "processor", False, "publish under a new processor name"),
    ("derivations", "derivation", False, "publish under a new derivation name"),
    ("triggers", "trigger", False, "none — only future runs cite the new definition"),
    ("views", "view", True, "bump the view version if consumers pin it"),
    ("artifacts", "artifact", True, "bump the artifact version if consumers pin it"),
    ("mcps", "mcp", True, "bump the interface version"),
    ("search_profiles", "search_profile", False, "none — routing only"),
)

# What a modified definition of each family means for data that already exists.
_INVISIBLE: ChangeClass = "invisible"
_REINTERPRETING: ChangeClass = "reinterpreting"
_MODIFIED_CLASS: Mapping[str, ChangeClass] = {
    "processor": _REINTERPRETING,
    "derivation": _REINTERPRETING,
    "trigger": _INVISIBLE,
    "view": _INVISIBLE,
    "artifact": _INVISIBLE,
    "mcp": _INVISIBLE,
    "search_profile": _INVISIBLE,
    "package": _INVISIBLE,
}

_MODIFIED_DETAIL: Mapping[str, str] = {
    "processor": (
        "annotations already written keep their value and config hash; they are never recomputed"
    ),
    "derivation": (
        "completed runs keep their recorded contract; a changes source keeps its cursor only if "
        "its source scope is unchanged"
    ),
    "trigger": "provenance only",
    "view": "consumers see the new behavior immediately",
    "artifact": "past renders keep the hash they were produced under",
    "mcp": "declared interface changed",
    "search_profile": "affects routing for every collection that names it",
}


def classify_catalogs(
    previous: DefinitionCatalog, incoming: DefinitionCatalog
) -> tuple[DefinitionChange, ...]:
    """Classify every definition difference between two compiled catalogs."""

    changes: list[DefinitionChange] = []

    previous_collections = dict(previous.collections)
    incoming_collections = dict(incoming.collections)
    for key in sorted(set(previous_collections) | set(incoming_collections)):
        name, version = key
        before = previous_collections.get(key)
        after = incoming_collections.get(key)
        if before is None and after is not None:
            changes.append(
                DefinitionChange(
                    family="collection",
                    name=name,
                    version=version,
                    status="added",
                    change_class="additive",
                    detail="new collection version",
                    required_action="none",
                    incoming_hash=after.contract_hash,
                )
            )
        elif before is not None and after is None:
            changes.append(
                DefinitionChange(
                    family="collection",
                    name=name,
                    version=version,
                    status="removed",
                    change_class="reinterpreting",
                    detail="removed from the catalog",
                    required_action=(
                        "keep this version in the package while any record references it"
                    ),
                    previous_hash=before.contract_hash,
                )
            )
        elif before is not None and after is not None:
            change = _collection_change(before, after)
            if change is not None:
                changes.append(change)

    for attribute, family, versioned, action in _SIMPLE_FAMILIES:
        before_map: Mapping[Any, Any] = getattr(previous, attribute)
        after_map: Mapping[Any, Any] = getattr(incoming, attribute)
        for key in sorted(set(before_map) | set(after_map), key=str):
            before = before_map.get(key)
            after = after_map.get(key)
            name, version = key if versioned and isinstance(key, tuple) else (key, None)
            if before is None:
                changes.append(
                    DefinitionChange(
                        family=family,
                        name=str(name),
                        version=version,
                        status="added",
                        change_class="additive",
                        detail=f"new {family}",
                        required_action="none",
                        incoming_hash=getattr(after, "definition_hash", None),
                    )
                )
                continue
            if after is None:
                removed_class: ChangeClass = (
                    "reinterpreting" if family in {"processor", "derivation"} else "invisible"
                )
                changes.append(
                    DefinitionChange(
                        family=family,
                        name=str(name),
                        version=version,
                        status="removed",
                        change_class=removed_class,
                        detail=f"{family} removed from the catalog",
                        required_action=(
                            "keep it while history references it"
                            if removed_class == "reinterpreting"
                            else "none"
                        ),
                        previous_hash=getattr(before, "definition_hash", None),
                    )
                )
                continue
            if before.definition_hash == after.definition_hash:
                continue
            changes.append(
                DefinitionChange(
                    family=family,
                    name=str(name),
                    version=version,
                    status="modified",
                    change_class=_MODIFIED_CLASS[family],
                    differing_fields=_differing_fields(before, after),
                    detail=_MODIFIED_DETAIL[family],
                    required_action=action,
                    previous_hash=before.definition_hash,
                    incoming_hash=after.definition_hash,
                )
            )

    if previous.rank_hash != incoming.rank_hash:
        changes.append(
            DefinitionChange(
                family="rank_default",
                name="rank_default",
                status="modified",
                change_class="invisible",
                detail="ranking changes retroactively for every query; no stored data changes",
                required_action="none",
                previous_hash=previous.rank_hash,
                incoming_hash=incoming.rank_hash,
            )
        )

    return tuple(changes)


@dataclass(frozen=True, slots=True)
class StoredGroup:
    """One distinct stored collection identity in a workspace."""

    collection: str
    version: int
    contract_hash: str
    rows: int


@dataclass(frozen=True, slots=True)
class HashRewrite:
    """A planned, provably safe rewrite of a stored contract hash."""

    collection: str
    version: int
    stored_hash: str
    target_hash: str
    rows: int
    reason: Literal["generation_upgrade", "additive_contract"]
    verify_keys: tuple[str, ...] = ()
    verify_absent_annotations: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "collection": self.collection,
            "version": self.version,
            "stored_hash": self.stored_hash,
            "target_hash": self.target_hash,
            "rows": self.rows,
            "reason": self.reason,
        }
        if self.verify_keys:
            payload["verify_keys"] = list(self.verify_keys)
        if self.verify_absent_annotations:
            payload["verify_absent_annotations"] = list(self.verify_absent_annotations)
        return payload


@dataclass(frozen=True, slots=True)
class Blocker:
    """A stored identity the incoming catalog cannot account for."""

    collection: str
    version: int
    stored_hash: str
    rows: int
    reasons: tuple[str, ...]
    required_action: str

    def as_json(self) -> dict[str, Any]:
        return {
            "collection": self.collection,
            "version": self.version,
            "stored_hash": self.stored_hash,
            "rows": self.rows,
            "reasons": list(self.reasons),
            "required_action": self.required_action,
        }


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """What publishing an incoming catalog would do to a workspace's records."""

    workspace: str
    changes: tuple[DefinitionChange, ...] = ()
    rewrites: tuple[HashRewrite, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    annotation_vintage: tuple[dict[str, Any], ...] = ()
    """Per-processor counts of annotations written under a superseded config hash."""
    stored_rows: int = 0
    notes: tuple[str, ...] = ()

    @property
    def verdict(self) -> ChangeClass:
        """The worst class among the changes this publish contains.

        Independent of whether the publish is allowed: a reinterpreting change
        with no rows to strand is still reported as reinterpreting, because that
        is what it is, and is still publishable.
        """

        ranks = [_CLASS_RANK[change.change_class] for change in self.changes]
        if self.rewrites:
            ranks.append(_CLASS_RANK["additive"])
        if self.blockers:
            ranks.append(_CLASS_RANK["reinterpreting"])
        return _CLASS_ORDER[max(ranks, default=0)]

    @property
    def publishable(self) -> bool:
        """Whether this publish can proceed without stranding a stored record."""

        return not self.blockers

    def as_json(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "verdict": self.verdict,
            "publishable": self.publishable,
            "stored_rows": self.stored_rows,
            "changes": [change.as_json() for change in self.changes],
            "rewrites": [rewrite.as_json() for rewrite in self.rewrites],
            "blockers": [blocker.as_json() for blocker in self.blockers],
            "annotation_vintage": [dict(item) for item in self.annotation_vintage],
            "notes": list(self.notes),
        }


def plan_stored_groups(
    groups: tuple[StoredGroup, ...],
    *,
    previous: DefinitionCatalog,
    incoming: DefinitionCatalog,
) -> tuple[tuple[HashRewrite, ...], tuple[Blocker, ...]]:
    """Decide what the incoming catalog must do with each stored identity.

    A group resolves in one of four ways:

    1. it already matches an incoming contract hash — nothing to do;
    2. it matches the *previous generation's* full ``definition_hash`` for the
       same version, so it predates the contract split and is rewritten forward;
    3. the previous catalog explains it and the contract change is additive, so it
       is rewritten forward (after any data verification the plan requests);
    4. nothing explains it — a blocker.
    """

    rewrites: list[HashRewrite] = []
    blockers: list[Blocker] = []
    for group in groups:
        key = (group.collection, group.version)
        target = incoming.collections.get(key)
        if target is not None and target.contract_hash == group.contract_hash:
            continue
        if target is None:
            blockers.append(
                Blocker(
                    collection=group.collection,
                    version=group.version,
                    stored_hash=group.contract_hash,
                    rows=group.rows,
                    reasons=("the incoming catalog does not contain this collection version",),
                    required_action=(f"include {group.collection}@{group.version} in the package"),
                )
            )
            continue
        before = previous.collections.get(key)
        if before is not None and group.contract_hash == before.definition_hash:
            # Written before the identity split: the stored value is the old
            # whole-definition hash of a definition we still have.
            rewrites.append(
                HashRewrite(
                    collection=group.collection,
                    version=group.version,
                    stored_hash=group.contract_hash,
                    target_hash=target.contract_hash,
                    rows=group.rows,
                    reason="generation_upgrade",
                )
            )
            continue
        if before is not None and group.contract_hash == before.contract_hash:
            verdict = contract_verdict(before, target)
            if verdict.additive:
                rewrites.append(
                    HashRewrite(
                        collection=group.collection,
                        version=group.version,
                        stored_hash=group.contract_hash,
                        target_hash=target.contract_hash,
                        rows=group.rows,
                        reason="additive_contract",
                        verify_keys=verdict.verify_keys,
                        verify_absent_annotations=verdict.verify_absent_annotations,
                    )
                )
                continue
            blockers.append(
                Blocker(
                    collection=group.collection,
                    version=group.version,
                    stored_hash=group.contract_hash,
                    rows=group.rows,
                    reasons=verdict.reasons,
                    required_action=(
                        f"add {group.collection} version {group.version + 1} with this change and "
                        f"keep version {group.version} in the package"
                    ),
                )
            )
            continue
        blockers.append(
            Blocker(
                collection=group.collection,
                version=group.version,
                stored_hash=group.contract_hash,
                rows=group.rows,
                reasons=(
                    "no installed definition explains this stored contract; the workspace was "
                    "written under a catalog this publish cannot see",
                ),
                required_action=(
                    "restore the definition these records were written under, then publish the "
                    "change as a new version"
                ),
            )
        )
    return tuple(rewrites), tuple(blockers)


__all__ = [
    "Blocker",
    "ChangeClass",
    "CompatibilityReport",
    "ContractVerdict",
    "DefinitionChange",
    "HashRewrite",
    "StoredGroup",
    "classify_catalogs",
    "contract_verdict",
    "plan_stored_groups",
]
