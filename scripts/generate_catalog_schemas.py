"""Generate editor schemas from the same models used by the catalog compiler.

Run: uv run python scripts/generate_catalog_schemas.py
"""

import json
from pathlib import Path
from typing import Any

from pydantic import create_model

from memseek.definitions.base import StrictModel
from memseek.definitions.manifest import CatalogManifest
from memseek.definitions.models import (
    AgentDefinition,
    ArtifactDefinition,
    CollectionDefinition,
    ComputerDefinition,
    ContextPolicyDefinition,
    McpDefinition,
    ModelCatalog,
    ProcessorDefinition,
    ProgramDefinition,
    RankDefaults,
    SearchProfileDefinition,
    ToolsetDefinition,
    ViewDefinition,
)
from memseek.derive.schema import PipelineDefinition, StandaloneTrigger

ROOT = Path(__file__).resolve().parents[1] / "schemas"
MODELS = {
    "catalog": CatalogManifest,
    "models": ModelCatalog,
    "ranking": RankDefaults,
    "search_profiles": create_model(
        "SearchProfiles", __base__=StrictModel, profiles=(dict[str, SearchProfileDefinition], ...)
    ),
    "derivations": PipelineDefinition,
    "triggers": StandaloneTrigger,
    "mcp": McpDefinition,
}
for family, model in {
    "collections": CollectionDefinition,
    "processors": ProcessorDefinition,
    "views": ViewDefinition,
    "artifacts": ArtifactDefinition,
    "computers": ComputerDefinition,
    "programs": ProgramDefinition,
    "agents": AgentDefinition,
    "context_policies": ContextPolicyDefinition,
    "toolsets": ToolsetDefinition,
}.items():
    fields: dict[str, Any] = {family: (list[model], ...)}
    MODELS[family] = create_model(f"{model.__name__}File", __base__=StrictModel, **fields)


def schemas() -> dict[str, dict[str, Any]]:
    """Every editor schema, keyed by the file stem it is written to."""

    result = {}
    for name, model in MODELS.items():
        schema = model.model_json_schema()
        if name == "search_profiles":
            profile = schema["$defs"]["SearchProfileDefinition"]
            profile["properties"].pop("name", None)
            if "name" in profile.get("required", []):
                profile["required"].remove("name")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        result[name] = schema
    return result


if __name__ == "__main__":
    ROOT.mkdir(exist_ok=True)
    for name, schema in schemas().items():
        (ROOT / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
