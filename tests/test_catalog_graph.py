"""Contract tests for the interactive catalog-package graph."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memseek.catalog_graph import (
    KINDS,
    RELATIONS,
    build_package_graph,
    compile_catalog_directory,
    sole_package,
)
from memseek.catalog_graph_page import render_graph_page
from memseek.config import Settings

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CATALOGS = tuple(sorted(path.name for path in EXAMPLES.glob("*_catalog")))
RENEWAL = EXAMPLES / "computer_renewal_catalog"


@pytest.fixture
def renewal_graph(bare_settings: Settings):
    catalog = compile_catalog_directory(RENEWAL, settings=bare_settings)
    return build_package_graph(catalog, sole_package(catalog))


def test_every_worked_example_projects_a_complete_graph(bare_settings: Settings) -> None:
    """No example catalog leaves a reference the projection cannot resolve."""

    assert CATALOGS, "the worked example catalogs are the fixtures for this test"
    for name in CATALOGS:
        catalog = compile_catalog_directory(EXAMPLES / name, settings=bare_settings)
        graph = build_package_graph(catalog, sole_package(catalog))
        ids = {node.id for node in graph.nodes}
        assert len(ids) == len(graph.nodes), f"{name} projected a duplicate node"
        for node in graph.nodes:
            assert node.kind in KINDS
            assert node.definition is not None, f"{name} left {node.id} outside its package"
        for edge in graph.edges:
            assert edge.relation in RELATIONS
            assert edge.flow in {"forward", "none"}
            assert edge.source in ids
            assert edge.target in ids


def test_the_graph_follows_the_computer_execution_path(renewal_graph) -> None:
    """The Computer specification's two execution modes are both drawn."""

    edges = {(edge.source, edge.relation, edge.target) for edge in renewal_graph.edges}
    # Deterministic mode: evidence triggers the Pipeline, which runs the
    # Program in the fast workspace and emits extracted terms.
    assert ("collection:renewal_evidence@1", "triggers", "derivation:contract_extract") in edges
    assert ("derivation:contract_extract", "runs", "computer:fast_workspace@1") in edges
    assert ("derivation:contract_extract", "runs", "program:contract_extract@3") in edges
    assert ("derivation:contract_extract", "writes", "collection:contract_terms@1") in edges
    # Agent mode: the nightly Pipeline runs the Agent in the research
    # workspace, whose writeback lands in its declared Collections.
    assert ("derivation:renewal_assessment", "runs", "agent:renewal_analyst@2") in edges
    assert ("derivation:renewal_assessment", "writes", "collection:renewal_risks@1") in edges
    assert ("computer:research_workspace@1", "writes", "collection:task_observations@1") in edges
    assert ("computer:research_workspace@1", "writes", "collection:renewal_proposals@1") in edges
    # The Agent's tool surface is a declared node of its own, and what a tool
    # reads reaches it from the left.
    assert ("agent:renewal_analyst@2", "uses", "toolset:renewal@1") in edges
    assert ("toolset:renewal@1", "exposes", "tool:renewal@1:renewal_research") in edges
    assert (
        "artifact:renewal_research_skill@1",
        "reads",
        "tool:renewal@1:renewal_research",
    ) in edges
    # The MCP interface exposes the same parts under tool names.
    assert (
        "mcp:renewal_computer@1",
        "exposes",
        "mcp_tool:renewal_computer@1:prepare_renewal",
    ) in edges
    assert (
        "mcp_tool:renewal_computer@1:prepare_renewal",
        "runs",
        "agent:renewal_analyst@2",
    ) in edges


def test_bindings_are_marked_so_they_cannot_order_the_layout(renewal_graph) -> None:
    """A selected model or policy is a setting, not a step in the flow."""

    flows = {(edge.source, edge.relation, edge.target): edge.flow for edge in renewal_graph.edges}
    assert flows[("agent:renewal_analyst@2", "uses", "model:renewal_reasoner")] == "none"
    assert flows[("agent:renewal_analyst@2", "uses", "context_policy:evidence_spine@1")] == "none"
    assert flows[("agent:renewal_analyst@2", "uses", "toolset:renewal@1")] == "none"
    assert flows[("processor:embedding_v1", "annotates", "collection:contract_terms@1")] == "none"
    assert flows[("collection:contract_terms@1", "routes", "search_profile:pg_default")] == "none"
    assert flows[("collection:renewal_evidence@1", "reads", "derivation:contract_extract")] == (
        "forward"
    )


def test_node_facts_carry_the_declared_execution_contract(renewal_graph) -> None:
    """The panel shows what the definition actually commits to."""

    nodes = {node.id: node for node in renewal_graph.nodes}
    workspace = dict(nodes["computer:research_workspace@1"].facts)
    assert workspace["provider"] == "fake"
    assert "worker-javascript to container" in workspace["runtime"].replace("→", "to")
    assert "/outbox/proposals" in workspace["writeback"]
    assert "[review]" in workspace["writeback"]
    agent = dict(nodes["agent:renewal_analyst@2"].facts)
    assert agent["model"] == "renewal_reasoner"
    assert agent["tools"] == "renewal@1"
    assert "24 steps" in agent["limits"]
    toolset = dict(nodes["toolset:renewal@1"].facts)
    assert "renewal_research" in toolset["tools"]
    assessment = dict(nodes["derivation:renewal_assessment"].facts)
    assert assessment["trigger"] == "cron 0 2 * * * (dirty)"
    assert assessment["emits"] == "renewal_risk into renewal_risks"


def test_selecting_a_package_requires_an_unambiguous_reference(bare_settings: Settings) -> None:
    catalog = compile_catalog_directory(RENEWAL, settings=bare_settings)
    assert sole_package(catalog) == "computer_renewal_demo@1.0.0"
    with pytest.raises(KeyError):
        build_package_graph(catalog, "computer_renewal_demo@9.9.9")


def test_the_page_embeds_the_graph_and_stands_alone(renewal_graph) -> None:
    page = render_graph_page(renewal_graph)
    assert page.startswith("<!doctype html>")
    assert "computer_renewal_demo@1.0.0" in page
    assert renewal_graph.catalog_hash in page
    # An embedded closing tag would end the script element early.
    assert "</script>" not in page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
    payload = page.split("const DATA = ", 1)[1].split(";\n", 1)[0]
    embedded = json.loads(payload.replace("<\\/", "</"))
    assert len(embedded["nodes"]) == len(renewal_graph.nodes)
    assert len(embedded["edges"]) == len(renewal_graph.edges)

    body = render_graph_page(renewal_graph, standalone=False)
    assert "<!doctype html>" not in body
    assert body.startswith("<title>")
