"""A small interactive gbrain-shaped memory you can inspect as it grows.

This is the executable counterpart to ``gbrain_showcase.html``. It seeds a
tiny knowledge base, then lets you explore the graph, structurally isolated
orphan pages, derived fact index, patterns, concept index, consolidated takes,
search, cited answers, and transcript-derived atoms through the public SDK.
The graph is deliberately queried through the normal named-view route -- it
does not introduce a graph-specific endpoint.

Run it against a local stack from this repository:

    make database && source .env.sh
    export PROVIDER_OPENAI_COMPAT_API_KEY=sk-...     # or OPENAI_API_KEY
    uv run memseek migrate
    uv run uvicorn memseek.api:app &                 # terminal A
    uv run memseek worker &                          # terminal B
    uv run python examples/gbrain_showcase.py        # terminal C

Set ``MEMSEEK_API_KEY`` to use an existing workspace, or ``DATABASE_URL`` to
create a fresh disposable one. The script publishes this repository's isolated
``examples/gbrain_catalog`` as ``gbrain@0.13.0`` into that workspace before writing any
records. A real provider is needed for ``answer`` and transcript atom
extraction; ``LLM_FAKE=1`` still demonstrates deterministic links, the named
graph view, the fact index, and hybrid retrieval.

Piping stdin runs a short scripted tour and exits, which also makes this a
convenient smoke walkthrough once a stack and worker are running.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from _workspace_explorer import print_workspace_explorer

from memseek.config import get_settings
from memseek.sdk import MemseekClient, MemseekHTTPError

RUN = secrets.token_hex(3)
ENTITY = f"gbrain-showcase:{RUN}"
DEFAULT_SEED = "people/maya"
PACKAGE = "gbrain@0.13.0"
CATALOG_ROOT = Path(__file__).resolve().parent / "gbrain_catalog"


# Terminal styling is deliberately modest. It is disabled for pipes, CI, and
# users who opt out through NO_COLOR, like the other interactive examples.
_COLOR = (
    sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
)


def _style(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _COLOR else ""


RESET, BOLD, DIM = _style(0), _style(1), _style(2)
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (
    _style(31),
    _style(32),
    _style(33),
    _style(34),
    _style(35),
    _style(36),
    _style(90),
)
BCYAN, BMAGENTA = _style(96), _style(95)


def paint(value: str, *styles: str) -> str:
    return ("".join(styles) + value + RESET) if _COLOR else value


def rule(char: str = "─", width: int = 76) -> str:
    return paint(char * width, GREY)


def short(value: object | None) -> str:
    return str(value)[:8] if value else "—"


def citation_ids(record: dict[str, Any]) -> list[str]:
    """Return only the evidence a record cites, from either read projection.

    A run's ``outputs`` already report ``citations`` with the producing run
    excluded. A full ``GET /records/{id}`` dereference instead reports
    ``derived_from``, whose first parent is that run -- provenance, not
    evidence. Printing the raw list would credit the run as a source, so drop
    it and let both paths show the same thing.
    """

    citations = record.get("citations")
    if citations is not None:
        return [str(value) for value in citations]
    run_id = record.get("run_id")
    return [
        str(parent)
        for parent in record.get("derived_from") or []
        if run_id is None or str(parent) != str(run_id)
    ]


def title(heading: str, detail: str = "") -> None:
    print(f"\n{rule('━')}")
    line = f"  {paint(heading, BOLD, BCYAN)}"
    if detail:
        line += paint(f"   {detail}", GREY)
    print(line)
    print(rule("━"))


def note(message: str) -> None:
    print(paint(f"  · {message}", GREY))


def wrapped(message: str, *, indent: str = "    ", width: int = 70) -> None:
    for line in textwrap.wrap(message, width=width):
        print(indent + line)


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


class Spinner:
    """Show a compact wait indicator while asynchronous worker work lands."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._task: asyncio.Task[None] | None = None
        self._started = 0.0

    async def _animate(self) -> None:
        frame = 0
        while True:
            elapsed = asyncio.get_running_loop().time() - self._started
            glyph = paint(self.FRAMES[frame % len(self.FRAMES)], CYAN)
            clock = paint(f"({elapsed:4.1f}s)", GREY)
            sys.stdout.write(f"\r  {glyph} {self.label} {clock}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Spinner:
        if _COLOR:
            self._started = asyncio.get_running_loop().time()
            self._task = asyncio.create_task(self._animate())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


# The small corpus is deliberately rich enough to make the deterministic graph
# and the declared-fact extractor visibly different from each other. The final
# page is an intentional orphan -- it links to nothing and nothing links to it --
# so the orphan_pages view has a real isolated page to report.
PAGES: tuple[dict[str, str], ...] = (
    {
        "key": "people/maya",
        "title": "Maya Ortiz",
        "type": "person",
        "body": """Maya founded [Acme](companies/acme) after investing in companies/orbit.

## Facts
- Maya founded Acme.
- Maya invested in Orbit.
""",
    },
    {
        "key": "companies/acme",
        "title": "Acme",
        "type": "company",
        "body": """Acme's board is advised by [Nora](people/nora).

## Facts
- Acme is a climate software company.
- Nora advises Acme's board.
""",
    },
    {
        "key": "people/nora",
        "title": "Nora Bell",
        "type": "person",
        "body": """Nora advises [Acme](companies/acme) and attended the launch meeting.

## Facts
- Nora advises Acme.
""",
    },
    {
        "key": "companies/orbit",
        "title": "Orbit",
        "type": "company",
        "body": """Orbit is a portfolio company associated with [Maya](people/maya).

## Facts
- Orbit is in Maya's investment portfolio.
""",
    },
    {
        "key": "notes/unfiled",
        "title": "Unfiled Note",
        "type": "note",
        "body": """A quick draft that has not been wired into the rest of the brain yet. It names no page and no page names it.

## Facts
- This note is not connected to any other page.
""",
    },
)

INITIAL_TRANSCRIPT = (
    "Maya committed to introduce Nora to the Acme board before the next funding meeting."
)


class GbrainDemo:
    """A small read/write wrapper around the SDK calls used by this walkthrough."""

    def __init__(self, client: MemseekClient, *, live_model: bool) -> None:
        self.client = client
        self.live_model = live_model
        self._sequence = 0

    def _dedupe_key(self, kind: str) -> str:
        self._sequence += 1
        return f"gbrain-showcase:{RUN}:{kind}:{self._sequence}"

    @staticmethod
    def _record_ids(result: dict[str, Any]) -> list[str]:
        rows = result.get("inserted", []) + result.get("duplicates", [])
        return [str(row["id"]) for row in rows]

    async def seed_pages(self) -> list[str]:
        result = await self.client.records.ingest_many(
            [
                {
                    "collection": "pages",
                    "entity": ENTITY,
                    "key": page["key"],
                    "type": "page",
                    "text": f"{page['title']}\n\n{page['body'].strip()}",
                    "content": {
                        "title": page["title"],
                        "type": page["type"],
                        "body": page["body"],
                    },
                    "dedupe_key": self._dedupe_key("page"),
                }
                for page in PAGES
            ]
        )
        return self._record_ids(result)

    async def add_transcript(self, text: str) -> str:
        result = await self.client.records.ingest(
            collection="transcripts",
            entity=ENTITY,
            type="transcript",
            text=text,
            dedupe_key=self._dedupe_key("transcript"),
        )
        return self._record_ids(result)[0]

    async def wait_ready(
        self, record_ids: Sequence[str], label: str, timeout_s: float = 90.0
    ) -> None:
        """Wait for required enrichment, without driving the worker ourselves."""

        pending = list(record_ids)
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner(label) as spinner:
            while pending:
                record = await self.client.record(pending[-1])
                if record.get("ready"):
                    pending.pop()
                    continue
                spinner.label = (
                    f"{label} — {len(record_ids) - len(pending)}/{len(record_ids)} ready"
                )
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("records were not enriched — is `memseek worker` running?")
                await asyncio.sleep(0.5)

    async def graph(self, seed: str = DEFAULT_SEED) -> dict[str, Any]:
        """Use the named-view contract, not a graph-specific HTTP surface."""

        return await self.client.query_view(
            "graph_query", seed=seed, direction="both", depth=2, limit=12
        )

    async def orphans(self) -> dict[str, Any]:
        """Report structurally isolated pages through the same view route."""

        return await self.client.query_view("orphan_pages", limit=20)

    async def wait_graph(self, timeout_s: float = 90.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("extracting and embedding deterministic links"):
            while True:
                graph = await self.graph()
                if graph.get("citations"):
                    return graph
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("graph edges did not arrive — is `memseek worker` running?")
                await asyncio.sleep(0.75)

    async def fact_record(self) -> dict[str, Any] | None:
        document = await self.client.document(entity=ENTITY, collections="facts")
        belief = next(
            (item for item in document.get("beliefs", []) if item.get("key") == "page_facts"),
            None,
        )
        return await self.client.record(str(belief["id"])) if belief is not None else None

    async def concept_record(self) -> dict[str, Any] | None:
        document = await self.client.document(entity=ENTITY, collections="concepts")
        concept_index = next(
            (item for item in document.get("beliefs", []) if item.get("key") == "concept_index"),
            None,
        )
        return (
            await self.client.record(str(concept_index["id"]))
            if concept_index is not None
            else None
        )

    async def take_record(self) -> dict[str, Any] | None:
        document = await self.client.document(entity=ENTITY, collections="takes")
        take_index = next(
            (item for item in document.get("beliefs", []) if item.get("key") == "take_index"),
            None,
        )
        return await self.client.record(str(take_index["id"])) if take_index is not None else None

    async def wait_facts(self, timeout_s: float = 90.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("building the bounded declared-facts index"):
            while True:
                record = await self.fact_record()
                if record is not None and record.get("ready"):
                    return record
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("fact index did not arrive — is `memseek worker` running?")
                await asyncio.sleep(0.75)

    async def atom_run_ids(self) -> set[str]:
        return await self._derivation_run_ids("atom_extraction")

    async def pattern_run_ids(self) -> set[str]:
        return await self._derivation_run_ids("pattern_detection")

    async def concept_run_ids(self) -> set[str]:
        return await self._derivation_run_ids("concept_synthesis")

    async def consolidate_run_ids(self) -> set[str]:
        return await self._derivation_run_ids("consolidate")

    async def _derivation_run_ids(self, processor: str) -> set[str]:
        runs = await self.client.runs(
            entity=ENTITY,
            processor=processor,
            operation="derive",
            limit=100,
        )
        return {str(run["id"]) for run in runs.get("runs", [])}

    async def wait_for_atoms(
        self, known_run_ids: set[str], timeout_s: float = 90.0
    ) -> tuple[str, list[dict[str, Any]]]:
        """Wait for the worker-owned atom derivation and open its audited outputs."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("extracting durable transcript atoms"):
            while True:
                runs = await self.client.runs(
                    entity=ENTITY,
                    processor="atom_extraction",
                    operation="derive",
                    limit=100,
                )
                fresh = next(
                    (run for run in runs.get("runs", []) if str(run["id"]) not in known_run_ids),
                    None,
                )
                if fresh is not None:
                    status = str(fresh.get("status") or "unknown")
                    detail = await self.client.run(str(fresh["id"]))
                    outputs = list(detail.get("outputs", []))
                    if outputs:
                        await self.wait_ready(
                            [str(output["id"]) for output in outputs], "embedding extracted atoms"
                        )
                    return status, outputs
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("atom derivation did not run — is `memseek worker` running?")
                await asyncio.sleep(0.75)

    async def atoms(self) -> list[dict[str, Any]]:
        """Read atoms through the ordinary timeline, then dereference their full content."""

        return await self._timeline_records("atoms")

    async def patterns(self) -> list[dict[str, Any]]:
        return await self._timeline_records("patterns")

    async def _timeline_records(self, collection: str) -> list[dict[str, Any]]:
        """Read one append-only collection without adding a collection-specific route."""

        timeline = await self.client._request(
            "GET",
            "/timeline",
            params={"entity": ENTITY, "collections": collection, "limit": 20},
        )
        return [await self.client.record(str(row["id"])) for row in timeline.get("records", [])]

    async def wait_for_patterns(
        self, known_run_ids: set[str], timeout_s: float = 90.0
    ) -> tuple[str, list[dict[str, Any]]]:
        """Wait for the pattern derivation driven by newly ready edges or atoms."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("detecting recurring graph and memory patterns"):
            while True:
                runs = await self.client.runs(
                    entity=ENTITY,
                    processor="pattern_detection",
                    operation="derive",
                    limit=100,
                )
                fresh = next(
                    (run for run in runs.get("runs", []) if str(run["id"]) not in known_run_ids),
                    None,
                )
                if fresh is not None:
                    status = str(fresh.get("status") or "unknown")
                    detail = await self.client.run(str(fresh["id"]))
                    outputs = list(detail.get("outputs", []))
                    if outputs:
                        await self.wait_ready(
                            [str(output["id"]) for output in outputs], "embedding detected patterns"
                        )
                    return status, outputs
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        "pattern derivation did not run — is `memseek worker` running?"
                    )
                await asyncio.sleep(0.75)

    async def wait_for_concepts(
        self, known_run_ids: set[str], timeout_s: float = 90.0
    ) -> tuple[str, list[dict[str, Any]]]:
        """Wait for the bounded static-key concept-index replacement."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("updating the bounded concept index"):
            while True:
                runs = await self.client.runs(
                    entity=ENTITY,
                    processor="concept_synthesis",
                    operation="derive",
                    limit=100,
                )
                fresh = next(
                    (run for run in runs.get("runs", []) if str(run["id"]) not in known_run_ids),
                    None,
                )
                if fresh is not None:
                    status = str(fresh.get("status") or "unknown")
                    detail = await self.client.run(str(fresh["id"]))
                    outputs = list(detail.get("outputs", []))
                    if outputs:
                        await self.wait_ready(
                            [str(output["id"]) for output in outputs], "embedding concept index"
                        )
                    return status, outputs
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        "concept synthesis did not run — is `memseek worker` running?"
                    )
                await asyncio.sleep(0.75)

    async def wait_for_takes(
        self, known_run_ids: set[str], timeout_s: float = 90.0
    ) -> tuple[str, list[dict[str, Any]]]:
        """Wait for the bounded static-key consolidation replacement."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Spinner("consolidating evidence into current takes"):
            while True:
                runs = await self.client.runs(
                    entity=ENTITY,
                    processor="consolidate",
                    operation="derive",
                    limit=100,
                )
                fresh = next(
                    (run for run in runs.get("runs", []) if str(run["id"]) not in known_run_ids),
                    None,
                )
                if fresh is not None:
                    status = str(fresh.get("status") or "unknown")
                    detail = await self.client.run(str(fresh["id"]))
                    outputs = list(detail.get("outputs", []))
                    if outputs:
                        await self.wait_ready(
                            [str(output["id"]) for output in outputs],
                            "embedding consolidated takes",
                        )
                    return status, outputs
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("consolidation did not run — is `memseek worker` running?")
                await asyncio.sleep(0.75)

    async def search(self, query: str) -> dict[str, Any]:
        return await self.client.search(
            query=query,
            collections=["pages", "facts", "atoms", "patterns", "concepts", "takes"],
            entity=ENTITY,
            mode="hybrid",
            k=8,
            include=["text", "collection", "key", "scores"],
            graph_boost={"anchor": DEFAULT_SEED, "depth": 2, "weight": 0.15, "limit": 12},
        )

    async def answer(self, question: str) -> dict[str, Any]:
        return await self.client.answer(
            question=question,
            anchor=DEFAULT_SEED,
            rewrite=True,
            save=True,
        )


def render_graph(graph: dict[str, Any]) -> None:
    paths = graph.get("paths", [])
    citations = graph.get("citations", [])
    print(
        f"\n  {paint('graph_query', BOLD, MAGENTA)}  {len(paths)} path(s), {len(citations)} cited edge(s)"
    )
    for path in paths:
        nodes = path.get("nodes", [])
        print(f"    {paint(' → '.join(str(node) for node in nodes), CYAN)}")
    if graph.get("truncated"):
        note("path limit reached; narrow the view parameters to inspect a smaller walk")
    for edge in citations:
        confidence = float(edge.get("confidence", 0))
        print(
            f"    {paint('↳', GREY)} {edge.get('subject')} "
            f"{paint(str(edge.get('predicate')), BOLD)} {edge.get('object')} "
            f"{paint(f'({confidence:.2f})', GREY)}"
        )


def render_orphans(result: dict[str, Any]) -> None:
    orphans = result.get("orphans", [])
    print(f"\n  {paint('orphan_pages', BOLD, MAGENTA)}  {len(orphans)} isolated page(s)")
    for page in orphans:
        print(f"    {paint(str(page.get('key')), CYAN)}  {page.get('title')}")
    if result.get("truncated"):
        note("orphan report limit reached; this view is intentionally bounded")


def render_facts(record: dict[str, Any]) -> None:
    content = record.get("content") or {}
    facts = content.get("facts") or []
    print(f"\n  {paint('facts/page_facts', BOLD, MAGENTA)}  {len(facts)} declared fact(s)")
    for item in facts:
        print(f"    {paint(str(item.get('page_key')), CYAN)}  {item.get('text')}")
    if content.get("truncated"):
        omitted = content.get("omitted_facts", 0)
        note(f"index is bounded; {omitted} declared fact(s) were omitted")


def render_atoms(atoms: Sequence[dict[str, Any]]) -> None:
    print(f"\n  {paint('atoms', BOLD, MAGENTA)}  {len(atoms)} durable memory atom(s)")
    if not atoms:
        note("no atom was emitted yet; this is expected with LLM_FAKE=1 or transient chat")
        return
    for atom in atoms:
        content = atom.get("content") or {}
        confidence = float(content.get("confidence", 0))
        citations = citation_ids(atom)
        print(
            f"    {paint(str(content.get('kind', 'fact')), BOLD, CYAN)} "
            f"{paint(f'({confidence:.2f})', GREY)}  {content.get('text', '')}"
        )
        print(paint(f"       cites: {', '.join(short(value) for value in citations)}", GREY))


def render_patterns(patterns: Sequence[dict[str, Any]]) -> None:
    print(f"\n  {paint('patterns', BOLD, MAGENTA)}  {len(patterns)} cited observation(s)")
    if not patterns:
        note(
            "no recurring pattern is grounded yet; the detector emits nothing for one-off evidence"
        )
        return
    for pattern in patterns:
        content = pattern.get("content") or {}
        confidence = float(content.get("confidence", 0))
        citations = citation_ids(pattern)
        print(f"    {paint(f'({confidence:.2f})', GREY)}  {content.get('text', '')}")
        print(paint(f"       cites: {', '.join(short(value) for value in citations)}", GREY))


def render_concepts(record: dict[str, Any] | None) -> None:
    if record is None:
        note("concept index is still pending or no durable concept update was justified")
        return
    content = record.get("content") or {}
    concepts = content.get("concepts") or []
    print(f"\n  {paint('concepts/concept_index', BOLD, MAGENTA)}  {len(concepts)} durable theme(s)")
    for concept in concepts:
        confidence = float(concept.get("confidence", 0))
        print(
            f"    {paint(str(concept.get('title', 'untitled')), BOLD, CYAN)} "
            f"{paint(f'({confidence:.2f})', GREY)}  {concept.get('text', '')}"
        )
    if content.get("truncated"):
        note(f"index is bounded; {content.get('omitted_concepts', 0)} concept(s) were omitted")


def render_takes(record: dict[str, Any] | None) -> None:
    if record is None:
        note("take index is still pending or no evidence cluster justified a conclusion")
        return
    content = record.get("content") or {}
    takes = content.get("takes") or []
    print(
        f"\n  {paint('takes/take_index', BOLD, MAGENTA)}  {len(takes)} consolidated conclusion(s)"
    )
    for take in takes:
        confidence = float(take.get("confidence", 0))
        print(
            f"    {paint(str(take.get('title', 'untitled')), BOLD, CYAN)} "
            f"{paint(f'({confidence:.2f})', GREY)}  {take.get('claim', '')}"
        )
        print(
            paint(
                f"       cites: {', '.join(short(value) for value in take.get('citations', []))}",
                GREY,
            )
        )
    if content.get("truncated"):
        note(f"index is bounded; {content.get('omitted_takes', 0)} take(s) were omitted")


def render_search(query: str, result: dict[str, Any]) -> None:
    hits = result.get("hits", [])
    print(f"\n  {paint(f'{len(hits)} hit(s)', BOLD)} for {paint(query, CYAN)}")
    boost = result.get("graph_boost")
    if boost:
        note(
            f"graph boost: {boost.get('anchor')} (depth {boost.get('depth')}, "
            f"weight {boost.get('weight')}) lifted {boost.get('matched_records')} "
            f"hit(s) through {boost.get('edge_count')} cited edge(s)"
        )
    for hit in hits:
        label = f"{hit.get('collection', '?')}"
        if hit.get("key"):
            label += f"/{hit['key']}"
        text = str(hit.get("text") or "")
        print(f"    {paint(label, BOLD, MAGENTA)}  {paint(short(hit.get('id')), GREY)}")
        wrapped(text, indent="       ")


def render_answer(question: str, result: dict[str, Any]) -> None:
    print(f"\n  {paint('answer', BOLD, MAGENTA)}  {paint(question, CYAN)}")
    wrapped(str(result.get("answer", "")))
    retrieval = result.get("retrieval_query")
    if retrieval and retrieval != question:
        print(paint(f"    retrieval rewrite: {retrieval}", GREY))
    citations = result.get("citations") or []
    print(paint(f"    citations: {', '.join(short(value) for value in citations) or '—'}", GREY))
    for gap in result.get("gaps", []):
        print(paint(f"    gap: {gap}", YELLOW))
    if result.get("saved_id"):
        print(paint(f"    saved synthesis: {short(result['saved_id'])}", GREEN))


HELP = """
  graph [seed]       traverse through the named graph_query view (default: people/maya)
  orphans            report pages with no current incoming or outgoing graph edge
  facts               display the deterministic, current page_facts array
  concepts            display the bounded, current concept_index array
  takes               display the bounded, current take_index array
  search <query>      hybrid retrieval across pages, facts, atoms, patterns, concepts, takes + graph boost
  answer <question>   cited synthesis, query rewrite, graph context, and saved result
  remember <text>     store a transcript line and wait for atom extraction
  atoms               list the append-only cited atoms
  patterns            list the append-only cited recurring patterns
  status              redisplay graph, orphans, facts, patterns, concepts, takes, and atoms
  help                show this command list
  quit                leave the tour

  Try: answer What connects Maya, Acme, and Nora?
       remember Maya decided to invite Nora to the Acme board meeting.
"""


async def show_status(demo: GbrainDemo) -> None:
    render_graph(await demo.graph())
    render_orphans(await demo.orphans())
    fact_record = await demo.fact_record()
    if fact_record is not None:
        render_facts(fact_record)
    else:
        note("fact index is still pending")
    render_patterns(await demo.patterns())
    render_concepts(await demo.concept_record())
    render_takes(await demo.take_record())
    render_atoms(await demo.atoms())


async def remember(demo: GbrainDemo, text: str) -> None:
    known_runs = await demo.atom_run_ids()
    known_concept_runs = await demo.concept_run_ids()
    known_consolidate_runs = await demo.consolidate_run_ids()
    transcript_id = await demo.add_transcript(text)
    print(f"  {paint('→', CYAN)} transcript {paint(short(transcript_id), BOLD)} stored")
    await demo.wait_ready([transcript_id], "embedding transcript")
    status, outputs = await demo.wait_for_atoms(known_runs)
    if outputs:
        print(paint(f"  atom run {status}: {len(outputs)} new output(s)", GREEN))
        render_atoms(outputs)
        concept_status, concept_outputs = await demo.wait_for_concepts(known_concept_runs)
        if concept_outputs:
            print(paint(f"  concept run {concept_status}: updated static index", GREEN))
            render_concepts(await demo.concept_record())
        else:
            note(
                f"concept run finished as {concept_status}; the new atom did not justify an index update"
            )
        take_status, take_outputs = await demo.wait_for_takes(known_consolidate_runs)
        if take_outputs:
            print(paint(f"  consolidation run {take_status}: updated static index", GREEN))
            render_takes(await demo.take_record())
        else:
            note(f"consolidation finished as {take_status}; no evidence cluster justified a take")
    elif demo.live_model:
        note(f"atom run finished as {status}; this transcript contained no durable atom")
    else:
        note("LLM_FAKE=1 keeps atom extraction empty; switch to a real provider to see cited atoms")


async def handle(demo: GbrainDemo, line: str) -> bool:
    """Run one command. Returning False ends the terminal session."""

    line = line.strip()
    if not line:
        return True
    verb, _, argument = line.partition(" ")
    lowered = verb.lower()
    argument = argument.strip()
    if lowered in {"quit", "exit", "q"}:
        return False
    if lowered in {"help", "?", "h"}:
        print(HELP)
        return True
    if lowered in {"graph", "g"}:
        render_graph(await demo.graph(argument or DEFAULT_SEED))
        return True
    if lowered in {"orphans", "o"}:
        render_orphans(await demo.orphans())
        return True
    if lowered in {"facts", "f"}:
        record = await demo.fact_record()
        if record is None:
            note("fact index is still pending")
        else:
            render_facts(record)
        return True
    if lowered in {"atoms", "a"}:
        render_atoms(await demo.atoms())
        return True
    if lowered in {"patterns", "p"}:
        render_patterns(await demo.patterns())
        return True
    if lowered in {"concepts", "c"}:
        render_concepts(await demo.concept_record())
        return True
    if lowered in {"takes", "t"}:
        render_takes(await demo.take_record())
        return True
    if lowered in {"status", "s"}:
        await show_status(demo)
        return True
    if lowered == "search":
        if not argument:
            print(paint("  usage: search <query>", YELLOW))
        else:
            render_search(argument, await demo.search(argument))
        return True
    if lowered == "answer":
        if not argument:
            print(paint("  usage: answer <question>", YELLOW))
        elif not demo.live_model:
            note("answer needs a real model; LLM_FAKE=1 cannot produce citation UUIDs")
        else:
            render_answer(argument, await demo.answer(argument))
        return True
    if lowered in {"remember", "say"}:
        if not argument:
            print(paint("  usage: remember <text>", YELLOW))
        else:
            await remember(demo, argument)
        return True
    print(paint(f"  unknown command: {verb!r}; type help", YELLOW))
    return True


async def showcase(demo: GbrainDemo) -> None:
    """Seed the corpus and reveal the model-free surfaces before the prompt."""

    title("ACT I · A SMALL BRAIN", "pages → edges / facts → searchable memory")
    note(f"creating an isolated entity: {ENTITY}")
    known_pattern_runs = await demo.pattern_run_ids()
    page_ids = await demo.seed_pages()
    await demo.wait_ready(page_ids, "embedding source pages")
    render_graph(await demo.wait_graph())
    render_orphans(await demo.orphans())
    render_facts(await demo.wait_facts())
    pattern_status, pattern_outputs = await demo.wait_for_patterns(known_pattern_runs)
    if pattern_outputs:
        print(paint(f"  pattern run {pattern_status}: {len(pattern_outputs)} new output(s)", GREEN))
        render_patterns(pattern_outputs)
    else:
        note(
            f"pattern run finished as {pattern_status}; the current evidence did not establish a recurrence"
        )

    title("ACT I.B · A CONVERSATION LEAVES A TRACE", "transcript → cited atoms")
    await remember(demo, INITIAL_TRANSCRIPT)

    title("ACT I.C · RETRIEVAL", "hybrid search, then optional graph-aware ranking")
    render_search("Maya Acme investment", await demo.search("Maya Acme investment"))
    if demo.live_model:
        title("ACT I.D · ANSWER", "bounded synthesis from visible, cited evidence")
        question = "What connects Maya, Acme, and Nora?"
        render_answer(question, await demo.answer(question))
    else:
        note("LLM_FAKE=1: graph, facts, and retrieval are live; answer/atoms await a real model")


async def interactive(demo: GbrainDemo) -> None:
    title("ACT II · YOUR TURN", "the seeded entity remains isolated; query or add memory")
    print(HELP)
    while True:
        try:
            line = await ainput(paint("\n  gbrain ▸ ", BOLD, BCYAN))
        except EOFError, KeyboardInterrupt:
            print()
            break
        try:
            if not await handle(demo, line):
                break
        except MemseekHTTPError as error:
            print(paint(f"  API error: {error}", RED))
    print(
        paint(
            "\n  The records, edges, facts, atoms, concepts, takes, and saved answers remain auditable.",
            GREY,
        )
    )


async def scripted(demo: GbrainDemo) -> None:
    """A short non-TTY path, matching the runnable examples in this directory."""

    title("ACT II · SCRIPTED TOUR", "stdin is not a TTY")
    commands = ["graph", "orphans", "facts", "concepts", "takes", "search Nora Acme"]
    if demo.live_model:
        commands.append("answer What did Maya found?")
    for command in commands:
        print(f"\n  {paint('▸ ' + command, BOLD, BCYAN)}")
        await handle(demo, command)


async def ensure_workspace() -> str:
    if api_key := os.environ.get("MEMSEEK_API_KEY"):
        return api_key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit("set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL (fresh one)")
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"gbrain-showcase-{RUN}")
    print(paint(f"  created disposable workspace {credential.workspace}", GREY))
    return credential.api_key


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    live_model = not get_settings().llm_fake

    title("GBRAIN ON MEMSEEK", "a graph-aware memory you can talk to")
    provider = paint("real model", BOLD, GREEN) if live_model else paint("LLM_FAKE=1", BOLD, YELLOW)
    print(f"  provider: {provider}   ·   service: {paint(base_url, GREY)}")

    async with MemseekClient(base_url, api_key) as client:
        published = await client.catalog.publish(package=PACKAGE, directory=CATALOG_ROOT)
        package = published.get("package", {})
        print(
            paint(
                f"  published isolated catalog {package.get('name', 'gbrain')}@{package.get('version', '?')}",
                GREY,
            )
        )
        listing = await client._request("GET", "/collections")
        active = {row["name"] for row in listing.get("collections", []) if row.get("active")}
        needed = {
            "pages",
            "edges",
            "facts",
            "atoms",
            "patterns",
            "concepts",
            "takes",
            "syntheses",
            "transcripts",
        }
        missing = sorted(needed - active)
        if missing:
            raise SystemExit(
                "this workspace does not have the gbrain package surfaces: "
                + ", ".join(missing)
                + f". The {PACKAGE} publication did not activate successfully."
            )
        demo = GbrainDemo(client, live_model=live_model)
        await showcase(demo)
        if sys.stdin.isatty():
            await interactive(demo)
        else:
            await scripted(demo)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
    except TimeoutError as error:
        raise SystemExit(str(error)) from error
    except KeyboardInterrupt:
        raise SystemExit(130) from None
