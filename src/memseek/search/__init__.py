"""Portable search schemas and extension contracts.

This package initializer deliberately exports only the dependency-light
schema and registry layer: the definition loader imports it while the rest
of the application is still initializing. The engine and named-view
execution live in ``memseek.search.engine`` and
``memseek.search.named_views`` and are imported as submodules.
"""

from .rank import RANK_JSON_SCHEMA, RankValidationError, validate_rank_expression
from .registry import (
    SEARCH_BACKENDS,
    CandidateHit,
    CandidateQuery,
    SearchBackend,
    SearchBackendDescriptor,
)
from .spec import (
    SEARCH_SPEC_JSON_SCHEMA,
    GraphBoostSpec,
    RerankSpec,
    SearchScope,
    SearchSource,
    SearchSpec,
)

__all__ = [
    "RANK_JSON_SCHEMA",
    "SEARCH_BACKENDS",
    "SEARCH_SPEC_JSON_SCHEMA",
    "CandidateHit",
    "CandidateQuery",
    "GraphBoostSpec",
    "RankValidationError",
    "RerankSpec",
    "SearchBackend",
    "SearchBackendDescriptor",
    "SearchScope",
    "SearchSource",
    "SearchSpec",
    "validate_rank_expression",
]
