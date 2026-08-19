"""A toy "Generative Agents" (Park et al., UIST '23) simulation on Memseek.

The paper's agent architecture maps onto the shipped catalog like this:

  memory stream       -> immutable ``main`` records (observation/chat events)
  importance (1-10)   -> the shipped ``importance`` LLM score processor
  relevance           -> embedding similarity / lexical match (hybrid search)
  recency             -> exponential decay term in the rank expression
  retrieval score     -> ``normalize(relevance) + normalize(importance) + decay(recency)``
  reflection          -> the shipped ``reflection`` derivation (cited insights;
                         accumulator trigger at 150 summed importance, as in the paper)
  agent summary       -> the shipped ``profile`` derivation (cited keyed beliefs:
                         role, preferences, commitments, open_threads, timeline)
  contradiction       -> the shipped ``contradiction`` YAML derivation over keyed
                         facts, emitting public ``relations/contradiction`` events
  plans               -> keyed ``plans`` records authored by the application
  calendar            -> ``calendar_events`` records authored by the application
  prompt assembly     -> the ``daily_agent_prompt`` live artifact (profile +
                         calendar + relevant memory, with a manifest of input IDs)

Everything else -- simulation time, who meets whom, dialogue, planning, acting
-- is deliberately application code, per the ownership boundary in the README:
Memseek stores observations, plans, and dialogue; it is not the simulator. So
the application owns its *own* LLM calls for the generative half: this script
borrows Memseek's configured provider seam (the same ``conf/models.yaml``
aliases the worker uses -- see ``Author``) to write in-character dialogue,
re-plan overnight, and answer interview questions in a character's own voice.

The simulation runs three game days, building toward the provenance graph:

  day 1     agents wake with seed memories, plans, and calendars; meet; and gossip.
            Each relayed memory stores a ``derived_from`` edge back to the fact it
            came from, so diffusion is a traversable graph, not just similar text
  overnight the ``reflection`` derivation turns each agent's day into cited insight,
            and every insight is opened up to the importance-scored memories it stands on
  day 2     agents revise plans from those reflections, meet again, throw the party
  measure   a paper-style interview measures how far each fact diffused, dawn -> day 2
  distill   the ``profile`` derivation turns each agent's stream into a cited profile,
            which the ``daily_agent_prompt`` artifact assembles for the next prompt
  glassbox  one belief is opened to its roots -- the run that wrote it (resolved model,
            hash-committed prompt/response, token cost, config/contract hashes) and every
            importance-scored event it cited; a belief cannot cite evidence it never saw
  day 3     Sam changes his mind and withdraws; the contradiction derivation flags it
            and profile re-derivation reconciles HIS beliefs — but the town still holds
            yesterday's "Sam is running". Sam carries the correction from door to door,
            and each listener reconciles in turn, so the fix diffuses like the party did
  pointtime the same mayoral belief is replayed AS OF three instants — end of day 2, day 3
            morning, end of day 3 — reconstructed from the immutable version ledger. At
            day 3 morning Sam believes he has withdrawn while Klaus, queried at the very
            same instant, still believes Sam is running: belief is per-agent and
            reconstructable at any point in time, never a shared global fact
  graph     one corrected belief is tracked all the way *down* its ``derived_from`` edges
            to the immutable observations it rests on, each hop an importance-scored atom:
            history tracked down, not reconstructed (lineage stays within an agent, so
            erasing one resident never reaches into another's memories)
  audit     the changed belief's full version history is replayed oldest -> newest --
            point-in-time proof of what the agent believed at each step, and exactly which
            run and evidence set put it there

Run it against a local stack with a real provider (see
docs/generative-agents-example.md and docs/skill-maintenance.md):

    make database && source .env.sh
    export PROVIDER_OPENAI_COMPAT_API_KEY=sk-...   # or OPENAI_API_KEY
    # point conf/models.yaml aliases at models your account can call
    uv run memseek migrate
    uv run uvicorn memseek.api:app &              # terminal A
    uv run memseek worker &                       # terminal B
    uv run python examples/generative_agents_toy.py

With DATABASE_URL set the script creates its own disposable workspace; set
MEMSEEK_API_KEY instead to reuse an existing one.

Against a real provider the shipped ``importance`` score processor judges each
memory, retrieval ranks on those ``scores.importance`` values, embeddings are
semantic, reflection emits insights that cite real evidence UUIDs, and the
dialogue, plans, and interview answers are all model-written and grounded in
retrieved memory.

Set ``LLM_FAKE=1`` for a deterministic offline run instead: the fake provider
reads the ``[importance=N]`` markers back out as scores and returns hash-based
embeddings, so retrieval still ranks on importance but leans on lexical match
rather than semantics; the application-side generation falls back to plain
templates. Reflection output requires cited UUIDs, which the fake provider
cannot invent, so the reflection run fails validation with an auditable error.
The script reports whichever outcome honestly.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from _catalog import publish_reference_catalog
from _workspace_explorer import print_workspace_explorer

from memseek.config import Settings, get_settings
from memseek.definitions import DefinitionCatalog, load_definition_catalog
from memseek.llm.registry import TEXT_OUTPUT, LLMTransportError
from memseek.llm.runtime import complete
from memseek.sdk import MemseekClient, MemseekHTTPError

RUN = secrets.token_hex(3)
# Start on a UTC midnight so game labels, calendar timestamps, and prose such
# as "5pm" all describe the same instant. Keeping it three days in the past
# also means every event is eligible for the recency rank expression.
SIM_START = (datetime.now(UTC) - timedelta(days=3)).replace(
    hour=0, minute=0, second=0, microsecond=0
)
PARTY_HOUR = 41.0  # Day 2, 17:00

ISABELLA = f"smallville-{RUN}:isabella"
KLAUS = f"smallville-{RUN}:klaus"
SAM = f"smallville-{RUN}:sam"
NAMES = {ISABELLA: "Isabella Rodriguez", KLAUS: "Klaus Mueller", SAM: "Sam Moore"}
AGENTS = (ISABELLA, KLAUS, SAM)


# ---------------------------------------------------------------------------
# Terminal styling. Colors are emitted only to a real TTY and can be disabled
# with NO_COLOR (https://no-color.org) or TERM=dumb, so piping to a file or CI
# log stays clean. Same approach as examples/sdk_crm_profile_smoke.py.
# ---------------------------------------------------------------------------
_USE_COLOR = (
    sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
)


def _sgr(*codes: int) -> str:
    return "\033[" + ";".join(str(c) for c in codes) + "m" if _USE_COLOR else ""


RESET = _sgr(0)
BOLD = _sgr(1)
DIM = _sgr(2)
RED = _sgr(31)
GREEN = _sgr(32)
YELLOW = _sgr(33)
BLUE = _sgr(34)
MAGENTA = _sgr(35)
CYAN = _sgr(36)
GREY = _sgr(90)


def paint(text: str, *codes: str) -> str:
    """Wrap ``text`` in the given SGR codes (a no-op when color is disabled)."""

    return ("".join(codes) + text + RESET) if _USE_COLOR else text


# A stable signature color per agent, so the transcript reads like a cast list.
AGENT_COLOR = {ISABELLA: MAGENTA, KLAUS: BLUE, SAM: YELLOW}


def who(entity: str) -> str:
    """An agent's name in its signature color."""

    return paint(NAMES[entity], BOLD, AGENT_COLOR.get(entity, ""))


def run_state(state: object) -> str:
    """A job/run state word, colored by outcome (done/dead/other)."""

    text = str(state)
    color = GREEN if text == "done" else RED if text == "dead" else YELLOW
    return paint(text, color)


def note(text: str, indent: int = 2) -> None:
    """A dimmed, indented aside explaining what the next step does."""

    print(paint(f"{' ' * indent}· {text}", GREY))


class Status:
    """A live single-line spinner for the polling waits — worker enrichment,
    derivation jobs, and contradiction emission. These block for tens of seconds
    against a real provider, so the spinner shows the run is alive and how long
    it has waited. It animates in a background task and only draws to a real TTY
    (``_USE_COLOR``); otherwise it is a silent no-op. Update ``label`` any time
    and the next frame reflects it."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._task: asyncio.Task[None] | None = None
        self._start = 0.0

    async def _run(self) -> None:
        frame = 0
        while True:
            elapsed = asyncio.get_running_loop().time() - self._start
            glyph = paint(self.FRAMES[frame % len(self.FRAMES)], CYAN)
            clock_s = paint(f"({elapsed:4.1f}s)", GREY)
            sys.stdout.write(f"\r{glyph} {self.label} {clock_s}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Status:
        if _USE_COLOR:
            self._start = asyncio.get_running_loop().time()
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")  # erase the spinner line
            sys.stdout.flush()


# The paper's retrieval score (section 4.1): equally weighted, min-max
# normalized relevance + importance + recency. The shipped default decays
# recency over ``last_accessed`` (hours since a memory was last retrieved,
# like the paper); this toy compresses game days into ``occurred_at``
# timestamps, so it passes an inline rank whose decay follows game time.
PAPER_RANK = [
    "sum",
    [
        ["product", 1.0, ["normalize", ["max", [["similarity"], ["text_match"]]]]],
        ["product", 1.0, ["normalize", ["score", "importance"]]],
        ["product", 1.0, ["decay", ["age_hours", "occurred_at"], {"midpoint": 24, "exponent": 1}]],
    ],
]


def game_time(hour: float) -> str:
    return game_moment(hour).isoformat()


def game_moment(hour: float) -> datetime:
    """The absolute UTC instant represented by a game-clock hour."""

    return SIM_START + timedelta(hours=hour)


def clock(hour: float) -> str:
    """Render a game hour as ``Day N HH:MM`` for the transcript."""

    day, rem = divmod(hour, 24)
    return f"Day {int(day) + 1} {int(rem):02d}:{int((rem % 1) * 60):02d}"


def memory(
    entity: str,
    text: str,
    importance: int,
    hour: float,
    *,
    type_: str = "observation",
    tag: str = "",
    parents: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One immutable memory-stream record.

    A real provider ignores the trailing ``[importance=N]`` marker and scores
    the sentence itself; the fake provider reads the marker back as the score,
    which is what keeps an ``LLM_FAKE=1`` run deterministic.

    ``parents`` records this memory's ``derived_from`` lineage -- the exact
    prior record(s) it was built from. Memseek stores that as the provenance
    edge (citations are ``derived_from`` minus the writing run), computes each
    record's ``depth`` from it, and lets it be walked as a graph. A relayed
    fact points back at the memory it came from, so gossip becomes traceable
    rather than merely look-alike text."""

    record = {
        "collection": "main",
        "entity": entity,
        "type": type_,
        "text": f"{text} [importance={importance}]",
        "occurred_at": game_time(hour),
        "dedupe_key": f"gatoy-{RUN}:{entity}:{tag or text[:48]}",
    }
    if parents:
        record["derived_from"] = list(parents)
    return record


def seed_memories() -> list[dict[str, Any]]:
    """Paper section 3.1: one description per agent, split into seed memories."""

    return [
        memory(
            ISABELLA,
            "Isabella Rodriguez runs Hobbs Cafe and loves making customers feel welcome.",
            6,
            0,
        ),
        memory(
            ISABELLA,
            "Isabella Rodriguez is planning a Valentine's Day party at Hobbs Cafe "
            f"on {game_moment(PARTY_HOUR):%Y-%m-%d} from 5pm to 7pm and wants to invite everyone.",
            9,
            0.1,
        ),
        memory(
            ISABELLA,
            "Isabella Rodriguez is close friends with Klaus Mueller, a regular customer.",
            5,
            0.2,
        ),
        memory(
            KLAUS,
            "Klaus Mueller is a sociology student writing a research paper on gentrification in low-income communities.",
            7,
            0,
        ),
        memory(
            KLAUS,
            "Klaus Mueller studies at the library most days and takes lunch at Hobbs Cafe.",
            4,
            0.1,
        ),
        memory(SAM, "Sam Moore has decided to run for mayor in the upcoming local election.", 9, 0),
        memory(
            SAM,
            "Sam Moore has been involved in local politics for years and wants to bring new ideas to the community.",
            6,
            0.1,
        ),
    ]


SEED_PLANS = {
    ISABELLA: "Open Hobbs Cafe at 8am, serve customers, decorate for the Valentine's Day party in the afternoon, and invite everyone who stops by.",
    KLAUS: "Read at the library in the morning, lunch at Hobbs Cafe at noon, then keep drafting the gentrification paper until evening.",
    SAM: "Walk through Johnson Park in the morning, then talk to neighbors about the mayoral campaign for the rest of the day.",
}


def plan_record(entity: str, text: str, hour: float) -> dict[str, Any]:
    """Paper section 4.3: a broad-strokes plan, stored as keyed current state.

    The ``plans`` collection is keyed, so re-writing key ``today`` versions the
    plan in place -- yesterday's plan is superseded, not duplicated."""

    return {
        "collection": "plans",
        "entity": entity,
        "type": "plan",
        "key": "today",
        "text": f"{NAMES[entity]}'s plan: {text}",
        "occurred_at": game_time(hour),
        "dedupe_key": f"gatoy-{RUN}:{entity}:plan-{int(hour)}",
    }


def calendar_event(
    entity: str, title: str, start_hour: float, end_hour: float, attendees: list[str], *, tag: str
) -> dict[str, Any]:
    """One scheduled event in the ``calendar_events`` collection.

    Calendar-specific fields live under ``content`` (the ingest envelope forbids
    unknown top-level keys); the collection's ``text_projection`` fills in the
    searchable ``text`` from them, so this omits it."""

    return {
        "collection": "calendar_events",
        "entity": entity,
        "type": "event",
        "content": {
            "title": title,
            "starts_at": game_time(start_hour),
            "ends_at": game_time(end_hour),
            "attendees": attendees,
        },
        "occurred_at": game_time(start_hour),
        "dedupe_key": f"gatoy-{RUN}:{entity}:cal-{tag}",
    }


def calendar_events() -> list[dict[str, Any]]:
    """Each agent's day-2 schedule, so the prompt artifact's calendar block has
    something to render. The party is on everyone's calendar; the rest is
    per-agent."""

    guests = [NAMES[a] for a in AGENTS]
    events = [
        calendar_event(
            entity,
            "Valentine's Day party at Hobbs Cafe",
            PARTY_HOUR,
            PARTY_HOUR + 2,
            guests,
            tag="party",
        )
        for entity in AGENTS
    ]
    events += [
        calendar_event(
            ISABELLA, "Decorate Hobbs Cafe for the party", 33, 35, [NAMES[ISABELLA]], tag="prep"
        ),
        calendar_event(KLAUS, "Library research block", 31, 35, [NAMES[KLAUS]], tag="library"),
        calendar_event(
            SAM, "Doorknock in Johnson Park for the campaign", 30, 34, [NAMES[SAM]], tag="campaign"
        ),
    ]
    return events


# The environment schedule is application state: (game hour, speaker, listener).
DAY1_MEETINGS = [
    (9.0, ISABELLA, KLAUS),
    (10.5, SAM, ISABELLA),
    (12.0, KLAUS, SAM),
    (14.0, SAM, KLAUS),
    (16.0, KLAUS, ISABELLA),
]
DAY2_MEETINGS = [
    (32.0, ISABELLA, SAM),
    (34.0, KLAUS, SAM),
    (36.0, SAM, ISABELLA),
]
# Day 3: Sam has changed his mind overnight and now carries the correction to a
# town that already "knows" he is running -- the withdrawal has to diffuse the
# same way the party did, one conversation at a time.
DAY3_MEETINGS = [
    (50.0, SAM, KLAUS),
    (52.0, SAM, ISABELLA),
]

INTERVIEW = [
    ("Did you know there is a Valentine's Day party?", "valentine"),
    ("Do you know who is running for mayor?", "mayor"),
]


class Author:
    """The paper's *simulator* half, expressed as the application's own LLM.

    Dialogue, planning, and interview answers are application responsibilities
    (the README's ownership boundary), so the app makes its own model calls --
    it does not smuggle them into a Memseek processor. It borrows Memseek's
    configured provider seam (``memseek.llm.runtime.complete`` over the same
    ``conf/models.yaml`` aliases the worker resolves) so the example needs no
    separate model config. Under ``LLM_FAKE=1`` -- or any provider error -- each
    method falls back to a plain deterministic template, so the run still reads
    cleanly offline."""

    def __init__(self, settings: Settings, catalog: DefinitionCatalog) -> None:
        self.settings = settings
        self.catalog = catalog
        self.calls = 0

    @property
    def live(self) -> bool:
        return not self.settings.llm_fake

    @staticmethod
    def _persona(entity: str) -> str:
        """Self-identity preamble. Memories are stored in the third person and
        name the agent, so a character will happily say "Isabella's party" about
        its own party unless it is told that the name in its memories is itself."""

        name = NAMES[entity]
        return (
            f"You ARE {name}. Your memories are written in the third person and "
            f'refer to you by name: whenever a memory says "{name}", that is you. '
            "Speak in the first person and never refer to yourself by name in the "
            f'third person -- say "my party", not "{name}\'s party"; "I", not "{name}".'
        )

    async def _say(self, system: str, prompt: str, *, fallback: str, max_tokens: int) -> str:
        if not self.live:
            return fallback
        try:
            resolved = await complete(
                self.settings,
                self.catalog,
                "cheap",
                system,
                prompt,
                output=TEXT_OUTPUT,
                max_output_tokens=max_tokens,
            )
        except LLMTransportError:
            return fallback
        self.calls += 1
        text = " ".join(resolved.completion.text.split()).strip("\"'“”")
        return text or fallback

    async def utterance(
        self, hour: float, speaker: str, listener: str, headline: str, memories: list[str]
    ) -> str:
        """One in-character line grounded in the speaker's retrieved memories."""

        recalled = "\n".join(f"- {m}" for m in memories) or "- (nothing in particular)"
        system = (
            self._persona(speaker)
            + " You are a warm, natural resident of a small town. Reply in one or "
            "two spoken sentences, saying only what your memories support -- never "
            "invent facts. The simulation's current time is "
            f"{clock(hour)} ({game_time(hour)}). Treat dates and times in memories as "
            "hard facts, and use relative language from that current time: do not call "
            "an event tomorrow when it is today or has already happened."
        )
        prompt = (
            f"You run into {NAMES[listener]}.\n"
            f"Your relevant memories right now:\n{recalled}\n\n"
            f"The most important thing on your mind is: {headline}\n\n"
            f"Greet {NAMES[listener]} and pass along what matters most. "
            "Return only the spoken line."
        )
        return await self._say(system, prompt, fallback=headline, max_tokens=120)

    async def replan(
        self, entity: str, yesterday: str, insights: list[str], *, day_start_hour: float
    ) -> str:
        """Tomorrow's plan, revised from the agent's overnight reflections."""

        learned = "\n".join(f"- {i}" for i in insights) or "- (no new insights)"
        system = (
            "You plan a character's day in one concise, concrete sentence, in the "
            "third person, grounded in what they now know."
        )
        prompt = (
            f"{NAMES[entity]}'s plan yesterday was:\n{yesterday}\n\n"
            f"Overnight, {NAMES[entity]} reflected and concluded:\n{learned}\n\n"
            f"Write {NAMES[entity]}'s revised plan for {clock(day_start_hour)} "
            f"({game_time(day_start_hour)}) in one sentence."
        )
        return await self._say(system, prompt, fallback=yesterday, max_tokens=120)

    async def answer(self, entity: str, question: str, evidence: str) -> str:
        """Answer an interview question in the character's voice, from one memory."""

        fallback = f"Yes — {evidence}"
        system = (
            self._persona(entity)
            + " Answer the question in one first-person sentence, using only the "
            "memory provided. If it does not answer the question, say you haven't "
            "heard about it."
        )
        prompt = f"A memory you hold: {evidence}\nQuestion: {question}\nYour answer:"
        return await self._say(system, prompt, fallback=fallback, max_tokens=80)


class Simulation:
    def __init__(self, client: MemseekClient, author: Author) -> None:
        self.client = client
        self.author = author

    async def ingest(self, records: list[dict[str, Any]]) -> list[str]:
        result = await self.client.records.ingest_many(records)
        ids = [row["id"] for row in result.get("inserted", [])]
        ids += [row["id"] for row in result.get("duplicates", [])]
        return ids

    async def wait_ready(self, record_ids: list[str], timeout_s: float = 90.0) -> None:
        """Search and triggers only see enriched rows; wait for the worker."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        pending = list(record_ids)
        async with Status("") as spin:
            while pending:
                spin.label = f"enriching {len(pending)} record(s) via the worker"
                record_id = pending[-1]
                try:
                    detail = await self.client.record(record_id)
                except MemseekHTTPError as error:
                    # A batch write is committed before the API returns its IDs,
                    # but a split deployment can briefly route this read to a
                    # replica that has not caught up. Treat that as a polling
                    # condition; a persistent 404 gets a useful diagnosis below.
                    if error.status_code != 404:
                        raise
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError(
                            f"record {record_id} was accepted for enrichment but remained "
                            "unreadable; verify the API and worker use the same database"
                        ) from error
                    spin.label = f"waiting for record {record_id[:8]} to become readable"
                    await asyncio.sleep(0.4)
                    continue
                if detail.get("enriched_at") or detail.get("ready"):
                    pending.pop()
                    continue
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        "records were not enriched in time; is `memseek worker` running?"
                    )
                await asyncio.sleep(0.4)

    async def retrieve(self, entity: str, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Paper-style retrieval over the agent's memory stream and reflections.

        ``scores`` is requested so the dialogue policy can rank on the real
        ``importance`` the pipeline computed, not on the inline marker."""

        response = await self.client.search(
            query=query,
            collections=["main", "reflections"],
            entity=entity,
            mode="hybrid",
            k=k,
            rank=PAPER_RANK,
            include=["text", "collection", "entity", "scores"],
        )
        return response.get("hits", [])

    async def top_memories(self, entity: str, k: int = 3) -> list[dict[str, Any]]:
        """The agent's highest-signal memories (importance + recency), for a card."""

        response = await self.client.search(
            query="what matters most today",
            collections=["main"],
            entity=entity,
            mode="recent",
            k=k,
            include=["text", "scores", "type"],
        )
        return response.get("hits", [])

    async def converse(self, hour: float, speaker: str, listener: str) -> str:
        """Dialogue turn: retrieve what the speaker would bring up, let the
        Author voice it, and write both participants' view of the exchange.

        Retrieval, the "share the most important thing" policy, and the Author
        are all application-owned; Memseek serves and stores the memories.

        The spoken ``line`` is *presentation* -- it goes to the transcript. What
        gets stored and diffused is the clean, third-person ``fact``, not the
        first-person utterance. That distinction matters: a stored utterance like
        "I wanted to invite you to our party" carries pronouns bound to this
        exchange, and relaying it verbatim collapses them ("you" becomes the next
        listener), so the fact would mutate on every hop. Storing the proposition
        instead keeps diffusion faithful -- each participant records *their own*
        memory of the fact, and the listener's higher importance is why a fact
        just heard tends to win the next retrieval and hop onward.

        The speaker's outgoing statement records a ``derived_from`` edge back to
        the memory the fact actually came from (``best``) -- its own, since a
        speaker only retrieves its own stream -- so a statement is traceable to
        the belief behind it. The listener's memory is deliberately *independent*:
        Klaus's memory of what Sam said is Klaus's own record, not a derivative of
        Sam's, so it survives Sam's erasure (the ownership boundary the ERASURE
        section relies on) and it does not model one agent as having read another
        agent's mind. Lineage stays within an agent; provenance is honest."""

        async with Status(f"{who(speaker)} recalls memories and speaks to {who(listener)}"):
            hits = await self.retrieve(
                speaker,
                f"the most important recent news {NAMES[speaker]} should share with {NAMES[listener]}",
                k=4,
            )
            best = max(hits, key=hit_importance, default=None)
            fact = core_fact(best["text"]) if best else "nothing much is new."
            memories = [strip_marker(core_fact(hit["text"])) for hit in hits[:4]]
            line = await self.author.utterance(hour, speaker, listener, fact, memories)
        # Same-entity lineage: the speaker's statement derives from its own source
        # memory (older sequence, as the provenance graph requires). The listener's
        # memory takes no parent -- it is theirs, not derived from the speaker.
        source = (best["id"],) if best else ()
        ids = await self.ingest(
            [
                memory(
                    speaker,
                    f"{NAMES[speaker]} told {NAMES[listener]}: {fact}",
                    3,
                    hour,
                    type_="chat",
                    tag=f"told-{listener}-{hour}",
                    parents=source,
                ),
                memory(
                    listener,
                    f"{NAMES[listener]} heard from {NAMES[speaker]}: {fact}",
                    8,
                    hour,
                    type_="chat",
                    tag=f"heard-{speaker}-{hour}",
                ),
            ]
        )
        await self.wait_ready(ids)
        return line

    async def run_derivation(
        self, entity: str, processor: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Enqueue one derive run for an entity, wait for it to finish, and wait
        for its cited output records to become searchable — they carry
        embeddings, so search and the prompt artifact cannot see them until the
        worker enriches them. Returns the job status and the newest run's output
        records (each with ``key``, ``content.text``, and ``citations``).

        Running a derivation explicitly stands in for the accumulator trigger,
        which in production fires reflection/profile by itself once an entity
        crosses the paper's importance threshold; the toy's two days never
        accumulate that much."""

        job = await self.client._request(
            "POST", f"/processors/{processor}/run", json={"entity": entity}
        )
        job_id = job["job_id"]
        deadline = asyncio.get_running_loop().time() + 120.0
        status: dict[str, Any] = {}
        async with Status(f"running {processor} derivation for {who(entity)}") as spin:
            while True:
                status = await self.client._request("GET", f"/jobs/{job_id}")
                spin.label = f"{processor} derivation for {who(entity)} — {status.get('state')}"
                if status.get("state") in {"done", "dead"}:
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(1.0)

        runs = await self.client.runs(entity=entity, processor=processor, operation="derive")
        outputs: list[dict[str, Any]] = []
        for run in runs.get("runs", []):
            if run.get("output_count"):
                detail = await self.client._request("GET", f"/runs/{run['id']}")
                outputs = detail.get("outputs", [])
                break  # newest run with outputs
        if outputs:
            await self.wait_ready([row["id"] for row in outputs])
        return status, outputs

    async def profile_beliefs(self, entity: str) -> dict[str, dict[str, Any]]:
        """Current keyed profile beliefs for an entity, keyed by belief key."""

        doc = await self.client._request(
            "GET", "/document", params={"entity": entity, "collections": "profiles"}
        )
        return {belief["key"]: belief for belief in doc.get("beliefs", [])}

    async def contradictions(self, entity: str, subject_id: str) -> list[dict[str, Any]]:
        """Read public contradiction events for one changed keyed record.

        The YAML derivation writes each conflict to the public ``relations``
        collection. The compact timeline row omits the relation body, so this
        re-reads each edge in full."""

        timeline = await self.client._request(
            "GET",
            "/timeline",
            params={
                "entity": entity,
                "collections": "relations",
                "types": "contradiction",
                "limit": 25,
            },
        )
        edges: list[dict[str, Any]] = []
        for row in timeline.get("records", []):
            detail = await self.client._request("GET", f"/records/{row['id']}")
            content = detail.get("content") or {}
            if content.get("subject_id") == subject_id:
                edges.append(detail)
        return edges

    async def await_contradiction(
        self, entity: str, subject_id: str, timeout_s: float = 90.0
    ) -> list[dict[str, Any]]:
        """The derivation rides keyed-record readiness, so poll for its output."""

        deadline = asyncio.get_running_loop().time() + timeout_s
        async with Status(f"waiting for the contradiction derivation on {who(entity)}"):
            while True:
                edges = await self.contradictions(entity, subject_id)
                if edges or asyncio.get_running_loop().time() >= deadline:
                    return edges
                await asyncio.sleep(1.5)

    async def interview(self, entity: str, question: str, needle: str) -> tuple[bool, str]:
        """Paper section 7.1: answer from retrieved memory, never from thin air."""

        hits = await self.retrieve(entity, question, k=5)
        for hit in hits:
            if needle in hit["text"].lower():
                return True, strip_marker(hit["text"])
        return False, "no supporting memory retrieved"

    async def run_content(self, run_id: str | None) -> dict[str, Any]:
        """The persisted content of one audited run: trigger, keyed divergence,
        timing — everything needed to explain what the run did."""

        if not run_id:
            return {}
        detail = await self.client.run(run_id)
        return detail.get("run", {}).get("content", {}) or {}

    async def trace_belief_change(
        self,
        entity: str,
        before: dict[str, dict[str, Any]],
        after: dict[str, dict[str, Any]],
        keyword: str,
    ) -> list[str]:
        """Trace every belief touching ``keyword`` from its old state to its new one.

        The contradiction event is the *alarm*; this is the *receipt*. For each
        affected belief it prints before → after text, the run that rewrote it
        (with how that run's keyed divergence classified the change), the
        citation delta, and dereferences the *new* citation so the concrete event
        that drove the change — Sam's withdrawal observation — is shown in full.
        That is the whole point: a belief that shifted is auditable all the way
        down to the immutable record and the derivation that folded it in.

        Returns the belief keys shown to have changed/removed, so the caller can
        assert the reconciliation moved a belief and replay its full history."""

        keys = sorted(
            {
                key
                for beliefs in (before, after)
                for key, belief in beliefs.items()
                if keyword in (belief.get("text") or "").lower()
            }
        )
        # The reconciling run is the newest ok profile derivation; its keyed
        # divergence names exactly how each belief key moved this run.
        runs = await self.client.runs(entity=entity, processor="profile", operation="derive")
        newest = next((r for r in runs.get("runs", []) if r.get("status") == "ok"), None)
        recon = await self.run_content(newest["id"]) if newest else {}
        reasons = recon.get("trigger_reasons") or []
        arrow = paint("→", CYAN)

        moved: list[str] = []
        for key in keys:
            b0 = before.get(key)
            b1 = after.get(key)
            change = divergence_change(recon, key) or classify_change(b0, b1)
            if change in {"changed", "removed"}:
                moved.append(key)
            color = RED if change == "removed" else GREEN if change == "changed" else GREY
            print(
                f"\n  {paint('belief', BOLD)}[{paint(key, BOLD, CYAN)}] "
                f"{paint(change, BOLD, color)}"
            )

            run0 = (b0 or {}).get("run_id")
            run1 = (b1 or {}).get("run_id") or (newest or {}).get("id")
            trigger = paint("[" + ", ".join(reasons) + "]", CYAN) if reasons else paint("[—]", GREY)
            print(
                f"    {paint('run', GREY)}       {short(run0)} {arrow} {paint(short(run1), BOLD)}"
                f"   trigger {trigger}"
            )

            cites0 = {str(c) for c in (b0 or {}).get("citations") or []}
            cites1 = {str(c) for c in (b1 or {}).get("citations") or []}
            added = sorted(cites1 - cites0)
            delta = paint(f"(+{len(added)})", GREEN if added else GREY)
            print(f"    {paint('citations', GREY)} {len(cites0)} {arrow} {len(cites1)}  {delta}")
            for cid in added:
                try:
                    record = await self.client.record(cid)
                except MemseekHTTPError:
                    continue
                print(f"    {paint('←', CYAN)} now cites {short(cid)}  {evidence_line(record)}")

            b0_text = strip_marker(b0["text"]) if b0 else "(no such belief yet)"
            b1_text = strip_marker(b1["text"]) if b1 else "(belief retracted)"
            print(f"    {paint('before:', GREY)} {paint(b0_text, YELLOW)}")
            print(f"    {paint('after: ', GREY)} {paint(b1_text, GREEN)}")
        return moved

    async def glass_box(self, entity: str) -> bool:
        """Open one belief all the way up: the derivation that produced it and
        every concrete event it stands on.

        This is the investor line made concrete — a belief is not an opaque
        model output, it is a *glass box*. It carries the id of the run that
        wrote it; that run records the exact model it called, the token cost,
        and content-addressed hashes of the prompt and response (auditable
        without leaking either); and every citation dereferences to an immutable,
        importance-scored event the model actually saw. The runner refuses any
        citation the model did not see, so a belief cannot cite evidence that
        does not exist. Nothing here is reconstructed after the fact — it is the
        provenance the substrate recorded at write time."""

        beliefs = await self.profile_beliefs(entity)
        # The flagship belief is the best-grounded one: most citations, then the
        # richest text. A belief with no run or citations can't be opened up.
        candidates = [b for b in beliefs.values() if b.get("run_id") and (b.get("citations") or [])]
        if not candidates:
            print(paint("  (no cited belief to open — a real provider grounds these)", YELLOW))
            return False
        belief = max(
            candidates, key=lambda b: (len(b.get("citations") or []), len(b.get("text", "")))
        )
        key = str(belief.get("key"))
        cites = [str(c) for c in belief.get("citations") or []]

        print(
            f"\n  {paint('belief', BOLD)}[{paint(key, BOLD, CYAN)}]  {strip_marker(belief['text'])}"
        )

        # (1) the derivation that produced it — the reproducibility receipt.
        content = await self.run_content(belief.get("run_id"))
        reasons = content.get("trigger_reasons") or []
        trigger = paint("[" + ", ".join(reasons) + "]", CYAN) if reasons else paint("[—]", GREY)
        call = next((c for c in content.get("model_calls") or [] if c.get("resolved")), {})
        usage = content.get("usage") or {}
        engine = content.get("engine_version")
        run_short = paint(short(belief.get("run_id")), BOLD)
        ms = paint(f"{content.get('ms', 0)}ms", GREY)
        print(f"    {paint('produced by run', GREY)} {run_short}   trigger {trigger}   {ms}")
        if call:
            tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            est = " ~est" if usage.get("estimated") else ""
            model = paint(str(call.get("resolved")), BOLD)
            print(f"    {paint('model', GREY)}  {model}   {paint(f'{tokens} tokens{est}', GREY)}")
            note_hash = paint("(content-addressed, reproducible)", GREY)
            print(
                f"    {paint('committed', GREY)}  prompt#{short(call.get('prompt_sha256'))}"
                f"  response#{short(call.get('response_sha256'))}   {note_hash}"
            )
        engine_suffix = f"  {paint(str(engine), GREY)}" if engine else ""
        print(
            f"    {paint('pinned', GREY)}  config#{short(content.get('config_hash'))}"
            f"  contract#{short(content.get('contract_hash'))}{engine_suffix}"
        )

        # (2) the concrete, importance-scored events it stands on.
        print(f"    {paint(f'grounded in {len(cites)} cited event(s):', GREY)}")
        shown = 0
        for cid in cites:
            try:
                record = await self.client.record(cid)
            except MemseekHTTPError:
                continue
            shown += 1
            score = (record.get("scores") or {}).get("importance")
            meter = f"{bar(float(score))} " if score is not None else ""
            print(f"    {paint('•', CYAN)} {short(cid)}  {meter}{evidence_line(record)}")
        return shown > 0

    async def belief_timeline(self, entity: str, key: str) -> int:
        """Replay one belief's entire version history, oldest → newest.

        Regulators, auditors, and debuggers all ask the same question: *what did
        the system believe at time T, and why?* Because every keyed belief is an
        immutable version carrying its run and its citations, the answer is a
        replayable ledger — each version names the derivation that wrote it and
        the evidence it cited, so any past state reconstructs exactly."""

        history = await self.client.document_history(entity=entity, collection="profiles", key=key)
        versions = list(reversed(history.get("versions") or []))
        if not versions:
            print(paint(f"  (no recorded history for '{key}')", YELLOW))
            return 0
        arrow = paint("→", CYAN)
        for index, version in enumerate(versions):
            last = index == len(versions) - 1
            marker = paint("●", GREEN) if last else paint("○", GREY)
            state = "retracted" if version.get("tombstone") else str(version.get("status"))
            stamp = str(version.get("created_at", ""))[:19].replace("T", " ")
            n_cites = len(version.get("citations") or [])
            head = (
                f"  {marker} {paint(f'v{index + 1}', BOLD)} {paint(stamp, GREY)}  "
                f"run {short(version.get('run_id'))}  "
                f"{paint(f'[{n_cites} cited]', GREY)}  {paint(state, GREY)}"
            )
            print(head)
            text = strip_marker((version.get("content") or {}).get("text") or "")
            color = GREEN if last else YELLOW
            print(f"      {paint(text, color)}")
            if not last:
                print(f"      {arrow}")
        return len(versions)

    async def belief_key_mentioning(self, entity: str, keyword: str) -> str | None:
        """The current profile belief key whose text mentions ``keyword``.

        The model may file the mayoral fact under ``role``, ``open_threads``, or
        ``timeline``, so the point-in-time and provenance views find the key by
        content rather than assuming a fixed one."""

        beliefs = await self.profile_beliefs(entity)
        for key in sorted(beliefs):
            if keyword in (beliefs[key].get("text") or "").lower():
                return key
        return None

    async def deepest_memory(self, entity: str, hint: str, k: int = 12) -> str | None:
        """The entity's memory with the longest provenance chain (max ``depth``).

        The richest root for a downward graph walk. Used as the offline (or
        ungrounded) fallback, where there are no derived beliefs to root at but
        the app-authored ``derived_from`` lineage of the memory stream still
        forms a real, walkable graph."""

        response = await self.client.search(
            query=hint,
            collections=["main"],
            entity=entity,
            mode="hybrid",
            k=k,
            rank=PAPER_RANK,
            include=["text", "depth"],
        )
        hits = response.get("hits", [])
        if not hits:
            return None
        return max(hits, key=lambda h: h.get("depth", 0))["id"]

    async def belief_as_of(
        self, entity: str, key: str, at: datetime, *, collection: str = "profiles"
    ) -> dict[str, Any] | None:
        """Reconstruct the belief version that was ACTIVE at wall-clock ``at``.

        This is the point-in-time question every auditor asks — *what did the
        agent believe at time T?* — answered from the ledger, not a cache.
        ``/document/history`` returns every version of a key newest-first, each
        carrying its ``created_at``, the run that wrote it, and its citations, so
        the state at any past instant is simply the newest version created at or
        before that instant. Nothing is reconstructed after the fact; it is
        replayed from immutable versions."""

        history = await self.client.document_history(entity=entity, collection=collection, key=key)
        for version in history.get("versions") or []:  # newest first
            created = _parse_ts(version.get("created_at"))
            if created is not None and created <= at:
                return version
        return None

    async def provenance_tree(
        self,
        root_id: str,
        *,
        max_depth: int = 4,
        _depth: int = 0,
        _seen: set[str] | None = None,
    ) -> int:
        """Walk the provenance DAG downward from one node to its roots.

        Every record names the exact parents it was built from in
        ``derived_from``: for a derivation output that is ``[run, ...cited
        events]``; for a relayed memory it is the source memory the fact came
        from. Following those edges turns any downstream belief into a tree that
        bottoms out at origin observations — across agents — each an immutable,
        importance-scored atom. This is the provenance graph the whole example
        keeps pointing at: history you can track *down*, not reconstruct."""

        seen = _seen if _seen is not None else set()
        if root_id in seen or _depth > max_depth:
            return 0
        seen.add(root_id)
        try:
            record = await self.client.record(root_id)
        except MemseekHTTPError:
            return 0

        indent = "  " + "   " * _depth
        branch = paint("●", CYAN) if _depth == 0 else paint("└─", GREY)
        score = (record.get("scores") or {}).get("importance")
        meter = f"{bar(float(score))} " if score is not None else ""
        actor = NAMES.get(record.get("entity", ""), record.get("entity") or "—")
        depth_tag = paint(f"[depth {record.get('depth', 0)}]", GREY)
        print(
            f"{indent}{branch} {short(root_id)} {depth_tag} {meter}"
            f"{paint(actor, BOLD)}  {evidence_line(record)}"
        )

        shown = 1
        run_id = record.get("run_id")
        parents = [p for p in record.get("derived_from") or [] if p != run_id]
        for parent in parents:
            shown += await self.provenance_tree(
                parent, max_depth=max_depth, _depth=_depth + 1, _seen=seen
            )
        return shown

    async def trace_reflection(self, outputs: list[dict[str, Any]]) -> int:
        """Ground each reflection insight in the concrete memories it was drawn from.

        A reflection is the agent's *own* synthesis — the paper's higher-level
        insight, not a raw observation — but it is not free-floating. The
        derivation forces every insight to cite the specific memories it stands
        on, and the runner rejects any citation the model did not actually
        retrieve. So even a synthesized conclusion opens up into the
        importance-scored events behind it, exactly like a profile belief:
        nothing the agent "concluded" is unfalsifiable."""

        shown = 0
        for row in outputs:
            cites = [str(c) for c in row.get("citations") or []]
            tag = paint(f"[{len(cites)} cited]", GREY)
            print(f"    {paint('✦', CYAN)} {paint(output_text(row), GREEN)}  {tag}")
            for cid in cites:
                try:
                    record = await self.client.record(cid)
                except MemseekHTTPError:
                    continue
                shown += 1
                score = (record.get("scores") or {}).get("importance")
                meter = f"{bar(float(score))} " if score is not None else ""
                print(f"        {paint('└', GREY)} {short(cid)}  {meter}{evidence_line(record)}")
        return shown


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an API timestamp into a timezone-aware datetime for as-of compares.

    History rows serialize ``created_at`` as ISO-8601; normalize a trailing
    ``Z`` and treat a naive value as UTC so point-in-time comparisons never
    trip over an offset-naive/aware mismatch."""

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def strip_marker(text: str) -> str:
    return text.split(" [importance=")[0]


def output_text(row: dict[str, Any]) -> str:
    """The rendered text of a derivation output record."""

    return strip_marker((row.get("content") or {}).get("text") or "")


def importance_of(text: str) -> int:
    match = re.search(r"\[importance=(\d+)\]", text)
    return int(match.group(1)) if match else 0


def hit_importance(hit: dict[str, Any]) -> float:
    """Rank a retrieved memory by the importance the pipeline actually scored.

    The ``importance`` processor writes ``scores.importance`` for every record
    in both modes -- a real provider judges the sentence, the fake provider
    reads the inline ``[importance=N]`` marker back out -- so that score is the
    authority. The text fallback only covers a hit that carries no score at all
    (e.g. a row not yet enriched)."""

    real = (hit.get("scores") or {}).get("importance")
    return float(real) if real is not None else float(importance_of(hit["text"]))


def core_fact(text: str) -> str:
    """Unwrap '<name> told/heard from <name>: ...' chat framing so a relayed
    fact is passed on as the fact itself, not as nested hearsay."""

    return re.sub(r"^(?:.*?(?:told|heard from) [^:]+: )+", "", strip_marker(text))


def short(record_id: str | None) -> str:
    """The first 8 hex of a run/record id — enough to follow it in a transcript."""

    return record_id[:8] if record_id else "—"


def divergence_change(run_content: dict[str, Any], key: str) -> str | None:
    """How the deriving run classified one belief key: added/changed/removed.

    Every derive run records keyed ``divergence`` against the prior active head,
    so this is the run's own account of what it did to the belief — not a guess
    reconstructed by diffing text."""

    manifest = run_content.get("candidate_set") or {}
    for entry in manifest.get("divergence") or []:
        if entry.get("key") == key:
            return str(entry.get("change"))
    return None


def classify_change(before: dict[str, Any] | None, after: dict[str, Any] | None) -> str:
    """Fallback classification when a run left no keyed divergence entry."""

    if before is None and after is not None:
        return "added"
    if before is not None and after is None:
        return "removed"
    if (
        before
        and after
        and strip_marker(before.get("text", "")) != strip_marker(after.get("text", ""))
    ):
        return "changed"
    return "unchanged"


def evidence_line(record: dict[str, Any]) -> str:
    """A one-line view of a dereferenced source record: what it is, and its text.

    This is what turns a belief's citation id into the concrete event behind it
    — the atom the belief was (re)built from."""

    content = record.get("content") or {}
    tag = "/".join(part for part in (record.get("collection"), record.get("type")) if part)
    return f"{paint(tag, CYAN)}  {strip_marker(str(content.get('text', '')).strip())}"


def bar(score: float, width: int = 10) -> str:
    """A tiny importance meter, 1-10 -> filled cells, colored by band."""

    filled = max(0, min(width, round(score / 10 * width)))
    color = GREEN if score >= 7 else YELLOW if score >= 4 else RED
    return paint("█" * filled, color) + paint("░" * (width - filled), GREY)


def show(title: str, body: str = "") -> None:
    rule = paint("━" * 68, GREY)
    print(f"\n{rule}\n  {paint(title, BOLD, CYAN)}\n{rule}")
    if body:
        print(paint(f"  {body}", DIM))


async def ensure_workspace() -> str:
    """Return a bearer key: MEMSEEK_API_KEY if set, else create a fresh
    disposable workspace directly in DATABASE_URL (what the
    ``memseek create-workspace`` CLI does). The key is printed nowhere and
    lives only for this run; erase or drop the test database to retire it."""

    api_key = os.environ.get("MEMSEEK_API_KEY")
    if api_key:
        return api_key
    if not os.environ.get("DATABASE_URL"):
        raise SystemExit(
            "set MEMSEEK_API_KEY (existing workspace) or DATABASE_URL "
            "(to create a disposable one); see docs/generative-agents-example.md"
        )
    from memseek.auth import create_workspace
    from memseek.db import pool_lifespan

    async with pool_lifespan(get_settings()) as pool:
        credential = await create_workspace(pool, f"gatoy-{RUN}")
    print(paint(f"created disposable workspace {credential.workspace}", GREY))
    return credential.api_key


async def play(sim: Simulation, meetings: list[tuple[float, str, str]]) -> None:
    """Run a block of scheduled meetings and print the dialogue transcript."""

    for hour, speaker, listener in meetings:
        line = await sim.converse(hour, speaker, listener)
        print(f"\n  {paint(clock(hour), GREY)}  {who(speaker)} {paint('→', CYAN)} {who(listener)}")
        print(f"    {paint('“' + line + '”', AGENT_COLOR.get(speaker, ''))}")


async def measure(sim: Simulation, agents: tuple[str, ...]) -> dict[str, dict[str, bool]]:
    """Who can answer each interview question from their own retrieved memory."""

    result: dict[str, dict[str, bool]] = {}
    for question, needle in INTERVIEW:
        result[question] = {
            entity: (await sim.interview(entity, question, needle))[0] for entity in agents
        }
    return result


def scoreboard(
    baseline: dict[str, dict[str, bool]],
    final: dict[str, dict[str, bool]],
    agents: tuple[str, ...],
) -> None:
    """Show each fact's diffusion as day-1 -> day-2 marks (● knows, ○ doesn't)."""

    def mark(known: bool) -> str:
        return paint("●", GREEN) if known else paint("○", GREY)

    for question, _ in INTERVIEW:
        d1 = sum(baseline[question].values())
        d2 = sum(final[question].values())
        print(f"\n  {paint(question, BOLD)}")
        for entity in agents:
            moved = (
                paint("  (learned it)", GREEN)
                if final[question][entity] and not baseline[question][entity]
                else ""
            )
            arrow = paint("→", CYAN)
            print(
                f"    {mark(baseline[question][entity])} {arrow} "
                f"{mark(final[question][entity])}  {who(entity)}{moved}"
            )
        print(paint(f"    diffusion: {d1}/{len(agents)} → {d2}/{len(agents)} agents know", DIM))


async def main() -> None:
    api_key = await ensure_workspace()
    base_url = os.environ.get("MEMSEEK_BASE_URL", "http://127.0.0.1:8000")
    print_workspace_explorer(api_url=base_url, api_key=api_key)
    settings = get_settings()
    author = Author(settings, load_definition_catalog(settings))

    async with MemseekClient(base_url, api_key) as client:
        try:
            await publish_reference_catalog(client)
        except MemseekHTTPError as error:
            payload = error.payload
            if (
                error.status_code == 409
                and isinstance(payload, dict)
                and payload.get("error") == "catalog_incompatible"
            ):
                raise SystemExit(
                    "this workspace already contains records from a different catalog; "
                    "unset MEMSEEK_API_KEY and set DATABASE_URL so this example can "
                    "create a fresh workspace; see docs/generative-agents-example.md"
                ) from error
            raise
        catalog = await client._request("GET", "/collections")
        active = {c["name"] for c in catalog.get("collections", []) if c.get("active")}
        if not {"main", "reflections", "plans"} <= active:
            raise SystemExit(
                "this workspace's catalog does not expose the shipped 'main'/"
                "'reflections'/'plans' collections (a custom package replaced the "
                "bootstrap catalog). Unset MEMSEEK_API_KEY so the script creates "
                "a fresh workspace on the shipped catalog."
            )
        sim = Simulation(client, author)
        mode = "real provider" if author.live else "LLM_FAKE=1 (deterministic offline)"

        show("SMALLVILLE", f"three agents, three days, one Valentine's party — run {RUN}")
        mode_color = GREEN if author.live else YELLOW
        print(f"  provider mode: {paint(mode, BOLD, mode_color)}")
        print("  cast: " + "   ".join(who(a) for a in AGENTS))
        note("seeding memories, plans, and calendars, then waiting for the worker to enrich them")
        ids = await sim.ingest(seed_memories())
        await sim.ingest([plan_record(e, SEED_PLANS[e], 7) for e in AGENTS])
        # calendar_events need no enrichment, so they are searchable on insert
        # (no wait_ready) and feed the prompt artifact's calendar block.
        await sim.ingest(calendar_events())
        await sim.wait_ready(ids)
        print("\n  day-1 plans:")
        for entity in AGENTS:
            print(f"    {who(entity)}: {paint(SEED_PLANS[entity], GREY)}")

        # Measure diffusion at dawn, before anyone has talked: each fact lives
        # only in its originator's memory. The closing scoreboard compares this
        # against the same interview after two days of gossip.
        baseline = await measure(sim, AGENTS)

        show("DAY ONE", "agents cross paths and pass along what's on their minds")
        await play(sim, DAY1_MEETINGS)

        show(
            "OVERNIGHT REFLECTION",
            "the reflection derivation turns the day into insight — each grounded in cited memories",
        )
        note("even a synthesized insight opens up: every one cites the concrete memories behind it")
        insights: dict[str, list[str]] = {}
        for entity in AGENTS:
            status, outputs = await sim.run_derivation(entity, "reflection")
            insights[entity] = [output_text(o) for o in outputs]
            print(f"\n  {who(entity)}  {paint('run', GREY)} {run_state(status.get('state'))}")
            if outputs:
                await sim.trace_reflection(outputs)
            elif author.live:
                print(paint("    (no insight cleared citation validation this run)", YELLOW))
            else:
                print(
                    paint(
                        "    (LLM_FAKE=1 cannot cite evidence UUIDs — use a real provider)", YELLOW
                    )
                )

        show("RE-PLAN", "each agent revises tomorrow from what it learned overnight")
        revised: dict[str, str] = {}
        for entity in AGENTS:
            plan = await author.replan(
                entity, SEED_PLANS[entity], insights[entity], day_start_hour=24
            )
            revised[entity] = plan
            await sim.ingest([plan_record(entity, plan, 30)])
            print(f"\n  {who(entity)}")
            print(f"    {paint('was:', GREY)} {paint(SEED_PLANS[entity], GREY)}")
            print(f"    {paint('now:', GREY)} {paint(plan, GREEN)}")

        show("DAY TWO", "better-informed agents meet again, then the party happens")
        await play(sim, DAY2_MEETINGS)
        party = await sim.ingest(
            [
                memory(
                    entity,
                    f"{NAMES[entity]} was at Isabella's Valentine's Day party at Hobbs Cafe; "
                    "the cafe was warm and full of neighbors.",
                    8,
                    PARTY_HOUR,
                    tag="party",
                )
                for entity in AGENTS
            ]
        )
        await sim.wait_ready(party)
        headline = paint("the Valentine's Day party fills Hobbs Cafe", BOLD, MAGENTA)
        print(f"\n  {paint(clock(PARTY_HOUR), GREY)}  🎉 {headline}")

        show("END OF DAY TWO", "each agent's most salient memories, by scored importance")
        for entity in AGENTS:
            print(f"\n  {who(entity)}")
            for hit in await sim.top_memories(entity, k=3):
                score = hit_importance(hit)
                print(
                    f"    {bar(score)} {paint(f'{score:>4.1f}', BOLD)}  {strip_marker(hit['text'])}"
                )

        show("DIFFUSION", "how far each fact traveled, dawn → after two days (● knows, ○ doesn't)")
        final = await measure(sim, AGENTS)
        scoreboard(baseline, final, AGENTS)

        show("IN THEIR OWN WORDS", "agents answer the interview from retrieved memory")
        no_memory = paint("(hasn't heard anything about that)", GREY)
        for question, needle in INTERVIEW:
            print(f"\n  {paint('Q: ' + question, BOLD)}")
            for entity in AGENTS:
                known, evidence = await sim.interview(entity, question, needle)
                if known:
                    voice = await author.answer(entity, question, core_fact(evidence))
                    print(f"    {who(entity)}: {voice}")
                else:
                    print(f"    {who(entity)}: {no_memory}")

        show("PROFILE", "the profile derivation distills each agent into cited keyed beliefs")
        for entity in AGENTS:
            status, outputs = await sim.run_derivation(entity, "profile")
            print(f"\n  {who(entity)}  {paint('run', GREY)} {run_state(status.get('state'))}")
            if outputs:
                for row in sorted(outputs, key=lambda r: r.get("key") or ""):
                    cited = paint(f"[{len(row.get('citations') or [])} cited]", GREY)
                    key = paint(str(row.get("key")), BOLD, CYAN)
                    print(f"    {key}: {output_text(row)}  {cited}")
            elif author.live:
                print(paint("    (no profile keys cleared citation validation this run)", YELLOW))
            else:
                print(
                    paint(
                        "    (LLM_FAKE=1 cannot cite evidence UUIDs — use a real provider)", YELLOW
                    )
                )

        # Anchor for the point-in-time replay: the town's beliefs as they stand at
        # the close of Day 2, before Sam changes his mind. Everyone "knows" Sam is
        # running; that shared belief is exactly what Day 3 has to correct.
        ckpt_day2 = datetime.now(UTC)
        note(
            f"checkpoint — end of Day 2: the whole town believes Sam is running ({ckpt_day2:%H:%M:%S})"
        )

        show(
            "GLASS BOX",
            "open one belief to its roots — the derivation that wrote it and the events it stands on",
        )
        note(
            "a belief is not an opaque model output: it carries the run that produced it, that "
            "run's hash-committed model call, and the immutable, importance-scored evidence it cited"
        )
        await sim.glass_box(SAM)

        show(
            "DAY THREE",
            "Sam changes his mind — and the correction has to catch up to a town that already 'knows'",
        )
        before_sam = await sim.profile_beliefs(SAM)
        reversal = await sim.ingest(
            [
                # The immutable observation is new evidence for the profile
                # derivation. The keyed plan below is current state for the
                # contradiction derivation. Keeping both roles explicit avoids
                # making a derived relation event the profile's source of truth.
                memory(
                    SAM,
                    "Sam Moore has decided NOT to run for mayor after all and is "
                    "withdrawing from the race.",
                    9,
                    48,
                    tag="withdraw-observation",
                ),
                {
                    "collection": "plans",
                    "entity": SAM,
                    "type": "plan",
                    "key": "campaign_status",
                    "text": (
                        "Sam Moore has decided NOT to run for mayor after all and is "
                        "withdrawing from the race."
                    ),
                    "occurred_at": game_time(48),
                    "dedupe_key": f"gatoy-{RUN}:{SAM}:withdraw-plan",
                },
            ]
        )
        campaign_status_id = reversal[1]
        await sim.wait_ready(reversal)
        reversal_line = paint('"I am withdrawing from the mayoral race."', AGENT_COLOR[SAM])
        print(f"\n  {paint(clock(48), GREY)}  {who(SAM)}: {reversal_line}")

        # The normal `contradiction` derivation compares the new keyed plan with
        # Sam's current keyed facts and emits a public `relations/contradiction`
        # event, so poll for it after readiness.
        edges = await sim.await_contradiction(SAM, campaign_status_id)
        for edge in edges:
            c = edge["content"]
            head = paint(f"⚡ contradicts  (confidence {c.get('confidence')})", BOLD, RED)
            print(f"\n  {head}  {c.get('text')}")
            print(paint(f"     {c.get('explanation')}", GREY))
        if not edges:
            if author.live:
                print(
                    "\n  (no contradiction event appeared — inspect the contradiction run and\n"
                    "   its explicit YAML prompt to see whether the model found a direct conflict)"
                )
            else:
                print(
                    "\n  (LLM_FAKE=1 cannot judge contradictions — run against a real provider\n"
                    "   to see the public relation event)"
                )

        # Reconcile is the application's response to the flag: re-run the profile
        # derivation so Sam's beliefs reflect the immutable withdrawal observation.
        # The model may file the mayoral fact under role, commitments, or
        # open_threads, so the trace follows every mayor-mentioning belief rather
        # than assuming a fixed key — and shows each one moving, with the run and
        # the concrete event behind it.
        note("reconcile — re-deriving Sam's profile so HIS beliefs fold in the withdrawal")
        await sim.run_derivation(SAM, "profile")
        after_sam = await sim.profile_beliefs(SAM)
        sam_moved = await sim.trace_belief_change(SAM, before_sam, after_sam, "mayor")
        if not sam_moved and author.live:
            print(paint("\n  (no mayoral belief moved for Sam this run)", YELLOW))

        # Point-in-time anchor: Sam now believes he has withdrawn, but he has told
        # no one. Klaus and Isabella still hold yesterday's "Sam is running". This
        # instant — one agent corrected, the rest stale — is what the replay below
        # reconstructs, proving belief is per-agent, not a shared global fact.
        ckpt_morning = datetime.now(UTC)
        note(
            f"checkpoint — Day 3 morning: Sam has withdrawn, but only Sam knows "
            f"({ckpt_morning:%H:%M:%S})"
        )

        show(
            "THE CORRECTION SPREADS",
            "Sam tells the town he's out; each listener records it — with a lineage edge back to the source",
        )
        note(
            "each relayed memory stores derived_from pointing at the fact it came from, so the gossip "
            "becomes a traversable provenance graph — not just look-alike text"
        )
        await play(sim, DAY3_MEETINGS)
        before_klaus = await sim.profile_beliefs(KLAUS)
        note(
            "the listeners reconcile too — re-deriving Klaus's and Isabella's profiles on the correction"
        )
        for entity in (KLAUS, ISABELLA):
            await sim.run_derivation(entity, "profile")
        after_klaus = await sim.profile_beliefs(KLAUS)
        klaus_moved = await sim.trace_belief_change(KLAUS, before_klaus, after_klaus, "mayor")
        ckpt_evening = datetime.now(UTC)
        note(
            f"checkpoint — end of Day 3: the correction has reached the town ({ckpt_evening:%H:%M:%S})"
        )

        show(
            "POINT IN TIME",
            "what did each agent believe about the mayoral race, and when — replayed from the version ledger",
        )
        note(
            "nothing is cached: each row is the belief version that was ACTIVE at that instant, picked "
            "from the immutable per-key history — with the run and cited-evidence count behind it"
        )
        stamps = [
            ("end of Day 2 ", ckpt_day2),
            ("Day 3 morning", ckpt_morning),
            ("end of Day 3 ", ckpt_evening),
        ]
        for entity in (SAM, KLAUS):
            key = await sim.belief_key_mentioning(entity, "mayor")
            if not key:
                print(f"\n  {who(entity)}  {paint('(no mayoral belief formed this run)', YELLOW)}")
                continue
            print(f"\n  {who(entity)}  belief[{paint(key, BOLD, CYAN)}]")
            for label, at in stamps:
                version = await sim.belief_as_of(entity, key, at)
                if version is None:
                    print(f"    {paint(label, GREY)}  {paint('· (no belief yet)', GREY)}")
                    continue
                text = strip_marker((version.get("content") or {}).get("text") or "")
                run = short(version.get("run_id"))
                ncite = len(version.get("citations") or [])
                tag = (
                    paint("retracted", RED)
                    if version.get("tombstone")
                    else paint(f"{ncite} cited", GREY)
                )
                colour = GREEN if at is ckpt_evening else YELLOW
                print(f"    {paint(label, GREY)}  {paint('run ' + run, GREY)} [{tag}]")
                print(f"      {paint(text, colour)}")
        note(
            "at Day 3 morning Sam already believed he had withdrawn, while Klaus — queried at the very "
            "same instant — still believed Sam was running; belief is per-agent and time-reconstructable"
        )

        show(
            "PROVENANCE GRAPH",
            "track one belief all the way down — from the claim to the immutable observations it rests on",
        )
        note(
            "every record names the exact parents it was built from; following those derived_from edges "
            "bottoms out at immutable, importance-scored atoms — history tracked down, not reconstructed"
        )
        prov_key = await sim.belief_key_mentioning(KLAUS, "mayor")
        root = (after_klaus.get(prov_key) or {}).get("id") if prov_key else None
        root_desc = f"Klaus's current belief[{prov_key}] about the mayoral race" if root else ""
        if not root:
            # Offline (or ungrounded) fallback: no derived belief to root at, but
            # the memory stream's app-authored lineage still forms a walkable graph.
            root = await sim.deepest_memory(KLAUS, "Sam Moore running for mayor")
            root_desc = "Klaus's deepest-lineage memory of the mayoral news"
        if root:
            print(f"\n  rooted at {paint(root_desc, BOLD)}:")
            shown = await sim.provenance_tree(root)
            note(
                f"walked {shown} node(s) to the roots — each dereferenced from its immutable record"
            )
        else:
            print(paint("\n  (nothing to root the graph at this run)", YELLOW))

        moved = klaus_moved or sam_moved
        moved_entity = KLAUS if klaus_moved else SAM
        if moved:
            show(
                "AUDIT TRAIL",
                f"replay {NAMES[moved_entity]}'s changed belief across runs — point-in-time: what, when, and why",
            )
            note(
                "every keyed belief is an immutable version carrying its run and its citations, so "
                "any past state — and the evidence behind it — reconstructs exactly, on demand"
            )
            await sim.belief_timeline(moved_entity, moved[0])

        show(
            "PROMPT ARTIFACT", "daily_agent_prompt render for Isabella — profile, calendar, memory"
        )
        artifact = await client.render_artifact(
            "daily_agent_prompt",
            entity=SAM,
            task="Running for mayor",
            start=game_time(0),
            end=game_time(48),
        )
        rendered = artifact.get("rendered") or artifact.get("content") or json.dumps(artifact)
        text = rendered if isinstance(rendered, str) else json.dumps(rendered, indent=2)
        for line in text.splitlines():
            print(paint(f"  {line}", GREY))
        manifest = artifact.get("manifest") or {}
        if manifest:
            inputs = manifest.get("input_record_ids") or []
            note(
                f"manifest: {len(inputs)} exact input records, "
                f"content hash {manifest.get('rendered_sha256')}"
            )

        show("ERASURE", "Sam leaves town: POST /erase retires his provenance graph")
        erased = await client._request("POST", "/erase", json={"entity": SAM})
        count = paint(str(erased.get("deleted_count")), BOLD, RED)
        print(
            f"  deleted {count} records "
            + paint(
                f"(affected entities: {erased.get('affected_entity_count')}, "
                f"audit record {erased.get('erasure_record_id')})",
                GREY,
            )
        )
        gone = await client._request("GET", "/timeline", params={"entity": SAM, "limit": 5})
        remaining = len(gone.get("records", []))
        tally = paint(str(remaining), GREEN if remaining == 0 else RED)
        print(f"  {who(SAM)}'s timeline after erasure: {tally} records")
        known, evidence = await sim.interview(SAM, INTERVIEW[1][0], INTERVIEW[1][1])
        verdict = paint("yes", RED) if known else paint("no", GREEN)
        print(f"  re-interviewing Sam about the mayor: {verdict} — {paint(evidence, GREY)}")
        note("other agents still remember what Sam told them; their records are their own")

        if author.live:
            note(
                f"{author.calls} application-side LLM calls for dialogue, plans, answers", indent=2
            )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except MemseekHTTPError as error:
        raise SystemExit(f"memseek API error: {error}") from error
