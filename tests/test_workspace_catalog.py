"""Workspace-owned package installation through the service boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from memseek.api import create_app
from memseek.auth import create_workspace
from memseek.config import Settings
from memseek.db import DatabasePool, create_pool
from memseek.definitions import load_definition_catalog
from memseek.workspace_catalog import WorkspaceCatalogError


def _gbrain_catalog_files() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1] / "examples" / "gbrain_catalog"
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*.yaml")
    }


async def test_gbrain_example_catalog_replaces_the_default_catalog_for_a_workspace(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "gbrain-example-catalog")
    files = _gbrain_catalog_files()
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            published = await client.post(
                "/catalog",
                headers=headers,
                json={"package": "gbrain@0.13.0", "files": files},
            )
            collections = await client.get("/collections", headers=headers)
            views = await client.get("/views", headers=headers)
            artifacts = await client.get("/artifacts", headers=headers)
            tools = await client.get("/tools", headers=headers)

    assert published.status_code == 200, published.text
    assert published.json()["package"] == {"name": "gbrain", "version": "0.13.0"}
    assert collections.status_code == 200, collections.text
    active = {item["name"] for item in collections.json()["collections"] if item["active"]}
    assert active == {
        "atoms",
        "concepts",
        "edges",
        "facts",
        "pages",
        "patterns",
        "syntheses",
        "takes",
        "transcripts",
    }
    assert "main" not in active
    assert views.status_code == 200, views.text
    assert [item["name"] for item in views.json()["views"]] == [
        "gbrain_search",
        "graph_query",
        "orphan_pages",
    ]
    graph = next(item for item in views.json()["views"] if item["name"] == "graph_query")
    assert graph["input_schema"]["properties"]["depth"]["maximum"] == 4
    assert graph["input_schema"]["properties"]["predicates"]["items"]["enum"] == [
        "works_at",
        "invested_in",
        "founded",
        "advises",
        "attended",
        "mentions",
        "image_of",
        "wikilink_basename",
    ]
    assert artifacts.status_code == 200, artifacts.text
    context = next(
        item for item in artifacts.json()["artifacts"] if item["name"] == "gbrain_context"
    )
    assert context["input_schema"]["properties"]["entity"]["description"].startswith(
        "Entity whose pages"
    )
    assert tools.status_code == 200, tools.text
    discovered = tools.json()
    assert discovered["package"]["name"] == "gbrain"
    assert discovered["package"]["version"] == "0.13.0"
    assert discovered["interface"]["name"] == "gbrain"
    assert discovered["interface"]["version"] == 1
    assert [tool["name"] for tool in discovered["tools"]] == [
        "answer",
        "search_memory",
        "explore_graph",
        "find_orphan_pages",
        "context",
        "record",
    ]
    assert discovered["tools"][0]["input_schema"]["properties"]["save"]["const"] is False
    search = next(tool for tool in discovered["tools"] if tool["name"] == "search_memory")
    assert search["binding"]["kind"] == "view"
    assert search["binding"]["reference"] == "gbrain_search@1"
    assert len(search["binding"]["hash"]) == 64
    assert search["input_schema"]["properties"]["query"]["maxLength"] == 8_192
    assert search["input_schema"]["properties"]["limit"]["maximum"] == 50
    graph_tool = next(tool for tool in discovered["tools"] if tool["name"] == "explore_graph")
    assert graph_tool["input_schema"]["properties"]["depth"]["maximum"] == 4
    assert graph_tool["input_schema"]["properties"]["limit"]["maximum"] == 100


async def test_workspace_can_install_and_use_a_custom_yaml_catalog(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    credential = await create_workspace(db_pool, "catalog-api")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}
    payload: dict[str, Any] = {
        "package": "customer_memory@1.0.0",
        "files": {
            "collections/customer.yaml": """collections:
  - name: customer_chat
    version: 1
    active: true
    mode: event
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
    mode: event
    schema:
      type: object
      required: [text]
      properties:
        text: {type: string}
      additionalProperties: true
    search_profile: pg_default
""",
            "packages/customer_memory.yaml": """name: customer_memory
version: 1.0.0
collections: [customer_chat@1, customer_profiles@1]
processors: [customer_profile]
triggers: [customer_profile.default]
views: [customer_context@1]
artifacts: [customer_prompt@1]
search_profiles: [pg_default]
""",
            "derivations/customer_profile.yaml": """name: customer_profile
trigger:
  write:
    collections: [customer_chat]
    types: [line]
    statuses: [active]
sources:
  new_lines:
    kind: changes
    collections: [customer_chat]
    types: [line]
    statuses: [active]
    keyed: false
    max_records: 20
    max_tokens: 2000
limits:
  max_tasks: 1
  max_llm_calls: 2
  max_retrieved_records: 0
  max_visible_records: 20
  max_total_tokens: 4000
  max_wall_s: 30
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
      prompt: >-
        Return {"records":[]} for {{entity}} after reading
        {{new_lines.rendered}}.
emit:
  from: "{{result.records}}"
  collection: customer_profiles
  type: line
""",
            "conf/processors.yaml": """processors:
  - name: importance
    kind: score
    source: constant
    input: {collections: [customer_chat]}
    scale: [1, 10]
    value: 5
""",
            "views/customer.yaml": """views:
  - name: customer_context
    version: 1
    active: true
    parameters:
      entity: {type: string, required: true}
    query:
      q: "{{entity}}"
      mode: text
      scope:
        entities: ["{{entity}}"]
        collections: [customer_chat]
        types: [line]
      k: 10
      render: true
""",
            "artifacts/customer.yaml": """artifacts:
  - name: customer_prompt
    version: 1
    active: true
    kind: prompt
    lifecycle: live
    parameters:
      entity: {type: string, required: true}
    blocks:
      memory:
        document:
          entity: "{{entity}}"
          collections: [customer_chat]
          status: active
        max_tokens: 1000
    template: "{{memory}}"
""",
        },
    }
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            loaded = await client.post("/catalog", headers=headers, json=payload)
            metadata = await client.get("/catalog", headers=headers)
            collections = await client.get("/collections", headers=headers)
            tools = await client.get("/tools", headers=headers)
            inserted = await client.post(
                "/records",
                headers=headers,
                json={
                    "records": [
                        {
                            "collection": "customer_chat",
                            "entity": "user-42",
                            "type": "line",
                            "text": "Hello from a workspace-owned contract",
                        }
                    ]
                },
            )

    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["package"] == {"name": "customer_memory", "version": "1.0.0"}
    assert metadata.status_code == 200
    assert metadata.json()["source"] == "workspace"
    assert metadata.json()["package"] == {"name": "customer_memory", "version": "1.0.0"}
    assert "collections/customer.yaml" in metadata.json()["files"]
    assert collections.status_code == 200
    assert any(item["name"] == "customer_chat" for item in collections.json()["collections"])
    assert tools.status_code == 200, tools.text
    assert tools.json()["package"]["name"] == "customer_memory"
    assert tools.json()["interface"] is None
    assert tools.json()["tools"] == []
    assert inserted.status_code == 200, inserted.text
    assert inserted.json()["inserted"][0]["ready"] is True


async def test_unresolvable_stored_catalog_is_not_reported_as_a_bad_credential(
    settings: Settings,
    db_pool: DatabasePool,
) -> None:
    """A catalog that will not resolve must not masquerade as an auth failure.

    Reporting it as ``401 invalid bearer credential`` sends operators hunting a
    key rotation that never happened, so the catalog fault keeps its own status.
    """

    credential = await create_workspace(db_pool, "unresolvable-catalog")
    app = create_app(
        settings,
        catalog=load_definition_catalog(settings),
        pool=create_pool(settings),
        verify_storage=False,
    )
    headers = {"Authorization": f"Bearer {credential.api_key}"}

    async with app.router.lifespan_context(app):

        async def refuse(_workspace: str) -> Any:
            raise WorkspaceCatalogError(
                "catalog_storage", "stored catalog hash mismatch", status=503
            )

        app.state.catalog_registry.get = refuse
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            refused = await client.get("/collections", headers=headers)
            unauthorized = await client.get(
                "/collections", headers={"Authorization": "Bearer not-a-key"}
            )

    assert refused.status_code == 503, refused.text
    assert refused.json() == {
        "error": "catalog_storage",
        "detail": "stored catalog hash mismatch",
    }
    # A genuinely bad credential is still the only thing that reads as 401.
    assert unauthorized.status_code == 401, unauthorized.text
    assert unauthorized.json()["error"] == "unauthorized"
