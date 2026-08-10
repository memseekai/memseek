"""The four-layer agent memory, driven entirely through the MCP tool surface.

This is the MCP-native counterpart to ``examples/agent_memory.py``. That script
uses the Python SDK, which can reach every route in the service. This one is
restricted to the seven tools ``mcp/agent_memory.yaml`` declares, because that
is all a connected coding agent ever gets:

    remember        ingest   append one message                      WRITES
    context         artifact the bounded prompt for one request
    recall          view     one search fused across all three layers
    standing_rules  view     exact rules at or above a priority floor
    replay_session  view     one conversation, in the order it happened
    record          record   open one record by a cited id
    answer          answer   a cited synthesis over this agent's memory

The point of running it is to watch a memory get *built and used* without the
agent ever touching a Memseek route directly. It writes with ``remember``, waits
while the worker climbs L0 → L1 → L2 → L3 on its own, then asks for the context
of a request it must refuse — and follows a citation back down to the sentence
that caused the refusal, one ``record`` call at a time.

Run it against a local stack with a real provider (``LLM_FAKE=1`` cannot invent
citation UUIDs, so extraction honestly no-ops and the layers above L0 stay
empty):

    docker compose up -d --wait                      # or: make quickstart
    export MEMSEEK_URL=http://127.0.0.1:8000
    export MEMSEEK_API_KEY=$(cat .memseek/api_key)
    export OPENAI_API_KEY=sk-...                     # conf/models.yaml api_key_env
    uv run python examples/mcp_agent_memory.py

Piping stdin runs a short scripted tour instead of the prompt, which makes this
a convenient smoke check of the whole declared surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

RUN = os.environ.get("MCP_MEMORY_RUN_ID") or secrets.token_hex(3)
AGENT = f"agent.alice-{RUN}"
SESSION_ONE = "s1-platform-review"
SESSION_TWO = "s2-mobile-slip"

_TTY = (
    sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"
)


def _style(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _TTY else ""


RESET, BOLD, DIM = _style(0), _style(1), _style(2)
RED, GREEN, YELLOW, MAGENTA, CYAN, GREY = (
    _style(31),
    _style(32),
    _style(33),
    _style(95),
    _style(36),
    _style(90),
)
BCYAN = _style(96)


def paint(value: str, *styles: str) -> str:
    return ("".join(styles) + value + RESET) if _TTY else value


def rule(char: str = "─", width: int = 78) -> str:
    return paint(char * width, GREY)


def title(heading: str, detail: str = "") -> None:
    print(f"\n{rule('━')}")
    line = f"  {paint(heading, BOLD, BCYAN)}"
    if detail:
        line += paint(f"   {detail}", GREY)
    print(line)
    print(rule("━"))


def note(message: str) -> None:
    print(paint(f"  · {message}", GREY))


def warn(message: str) -> None:
    print(paint(f"  ! {message}", YELLOW))


def wrapped(message: str, *, indent: str = "      ", width: int = 72) -> None:
    for line in textwrap.wrap(message, width=width) or [""]:
        print(indent + line)


def short(value: object | None) -> str:
    return str(value)[:8] if value else "—"


def clip(value: str, width: int) -> str:
    return value if len(value) <= width else value[: width - 1].rstrip() + "…"


async def ainput(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


class Spinner:
    """A wait indicator for worker-owned work, as in the SDK walkthrough."""

    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._task: asyncio.Task[None] | None = None

    async def _animate(self) -> None:
        started = asyncio.get_running_loop().time()
        frame = 0
        while True:
            elapsed = asyncio.get_running_loop().time() - started
            glyph = paint(self.FRAMES[frame % len(self.FRAMES)], CYAN)
            sys.stdout.write(f"\r  {glyph} {self.label} {paint(f'({elapsed:4.1f}s)', GREY)}\033[K")
            sys.stdout.flush()
            frame += 1
            await asyncio.sleep(0.1)

    async def __aenter__(self) -> Spinner:
        if _TTY:
            self._task = asyncio.create_task(self._animate())
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()


# Anchored, not relative to now(): a dedupe key makes a rerun with the same
# MCP_MEMORY_RUN_ID a true replay, and that only holds if the record it names is
# byte-identical. Deriving these from now() made the second run a 409 conflict.
BASE = datetime(2026, 7, 30, 9, 0, tzinfo=UTC)

CONVERSATION: tuple[dict[str, Any], ...] = (
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE,
        "text": (
            "For context on how we build: every new Node service goes out on Fastify now. "
            "The billing API is the one exception — it's still Express because the mobile "
            "app depends on its old response format, so don't migrate billing yet."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE + timedelta(minutes=2),
        "text": (
            "One hard rule: never deploy billing changes to production without my explicit "
            "approval. Writing the code is fine, shipping it is not."
        ),
    },
    {
        "session": SESSION_ONE,
        "role": "user",
        "at": BASE + timedelta(minutes=4),
        "text": (
            "Before we remove any compatibility path I want telemetry showing the old fields "
            "have zero traffic. I've been burned by 'nobody uses that' twice this year."
        ),
    },
    {
        "session": SESSION_TWO,
        "role": "user",
        "at": BASE + timedelta(days=6),
        "text": (
            "Update from mobile: the legacy-field removal slipped to release 8, expected in "
            "October. Billing has to stay compatible until then."
        ),
    },
)

REQUEST = "Rewrite billing in Fastify and deploy it tonight."


class Tools:
    """The declared MCP surface, and nothing else.

    Every method here is one ``tools/call``. There is deliberately no escape
    hatch to the HTTP API: if a thing cannot be done with a declared tool, this
    walkthrough cannot do it either, which is the property being demonstrated.
    """

    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.available: dict[str, Any] = {}
        self._sequence = 0

    async def discover(self) -> dict[str, Any]:
        listed = await self.session.list_tools()
        self.available = {tool.name: tool for tool in listed.tools}
        return self.available

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in self.available:
            raise SystemExit(
                f"the package does not declare a {name!r} tool; "
                f"it declares: {', '.join(sorted(self.available))}"
            )
        result = await self.session.call_tool(name, arguments)
        if result.is_error:
            text = result.content[0].text if result.content else "unknown MCP error"
            raise RuntimeError(f"{name}: {text}")
        return dict(result.structured_content or {})

    # -- write ---------------------------------------------------------------

    async def remember(self, message: dict[str, Any], ordinal: int) -> str:
        self._sequence += 1
        written = await self.call(
            "remember",
            {
                "entity": AGENT,
                "type": "message",
                "text": message["text"],
                "occurred_at": message["at"].isoformat(),
                "content": {
                    "text": message["text"],
                    "role": message["role"],
                    "session_id": message["session"],
                    "ordinal": ordinal,
                },
                "dedupe_key": f"mcp:{RUN}:{self._sequence}",
            },
        )
        rows = list(written.get("inserted") or []) + list(written.get("duplicates") or [])
        return str(rows[0]["id"]) if rows else "—"

    # -- read ----------------------------------------------------------------

    async def rules(self, floor: float = 80) -> list[dict[str, Any]]:
        result = await self.call("standing_rules", {"entity": AGENT, "min_priority": floor})
        return list(result.get("hits") or [])

    async def recall(self, task: str) -> list[dict[str, Any]]:
        result = await self.call("recall", {"entity": AGENT, "task": task})
        return list(result.get("hits") or [])

    async def replay(self, session_id: str) -> list[dict[str, Any]]:
        result = await self.call("replay_session", {"entity": AGENT, "session": session_id})
        return list(result.get("hits") or [])

    async def record(self, record_id: str) -> dict[str, Any]:
        return await self.call("record", {"id": record_id})

    async def context(self, task: str) -> dict[str, Any]:
        return await self.call("context", {"entity": AGENT, "task": task, "skill": f"skill.{RUN}"})

    async def answer(self, question: str) -> dict[str, Any]:
        return await self.call("answer", {"question": question, "entities": [AGENT]})


async def act_write(tools: Tools) -> None:
    title("ACT I · WRITE", "the agent's own tool call is the only way in")
    note(f"entity {AGENT}")
    for ordinal, message in enumerate(CONVERSATION):
        record_id = await tools.remember(message, ordinal)
        print(
            f"    {paint('⏺ remember', MAGENTA)} {paint(short(record_id), BOLD)} "
            + paint(message["session"], GREY)
        )
        wrapped(message["text"], indent="      ")
    print(paint(f"\n  {len(CONVERSATION)} message(s) written through MCP", GREEN))
    note("`remember` is the one declared tool that writes, and it only appends")


async def act_cascade(tools: Tools) -> bool:
    """Wait for the worker to climb the ladder, watching only through MCP."""

    title("ACT II · THE CASCADE", "nothing here is scheduled by this script")
    note("the worker extracts claims, consolidates scenes, and distils persona on its own")
    deadline = asyncio.get_running_loop().time() + 240
    async with Spinner("waiting for the worker to extract claims") as spinner:
        while True:
            found = await tools.rules(0)
            if found:
                spinner.label = f"{len(found)} claim(s) visible"
                await asyncio.sleep(0.6)
                return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(1.5)


def render_rules(rows: list[dict[str, Any]], floor: float) -> None:
    if not rows:
        note(f"no standing rule at priority ≥ {floor:g}")
        return
    for hit in rows:
        fields = hit.get("fields") or {}
        priority = float(fields.get("priority") or 0)
        stamp = paint(f"p{priority:>3.0f}", BOLD, RED if priority >= 90 else YELLOW)
        print(f"    {stamp}  {paint(clip(str(hit.get('text') or ''), 96), BOLD)}")


async def act_recall(tools: Tools) -> None:
    title("ACT III · RECALL", "the request the memory has to push back on")
    print(f"    {paint('request', BOLD, BCYAN)}  {paint(REQUEST, BOLD)}")

    print(paint("\n  ⏺ standing_rules — exact, no relevance guessing:", BOLD))
    render_rules(await tools.rules(80), 80)

    print(paint("\n  ⏺ recall — one search fused across the layers:", BOLD))
    for hit in (await tools.recall(REQUEST))[:6]:
        origin = str(hit.get("collection") or "?")
        colour = {"persona": MAGENTA, "scenes": CYAN, "memories": GREEN}.get(origin, GREY)
        print(
            f"    {paint(origin.ljust(9), colour)} {paint(short(hit.get('id')), BOLD)} "
            + paint(clip(str(hit.get("text") or ""), 82), colour)
        )

    print(paint("\n  ⏺ context — the bounded prompt an agent would actually receive:", BOLD))
    rendered = await tools.context(REQUEST)
    manifest = rendered.get("manifest") or {}
    for name, block in (manifest.get("blocks") or {}).items():
        state = (
            paint("omitted", YELLOW)
            if block.get("omitted")
            else paint("truncated", YELLOW)
            if block.get("truncated")
            else paint("packed", GREEN)
        )
        print(
            f"    {paint(str(name).ljust(13), BOLD)} {state}  "
            + paint(f"{block.get('tokens')} tokens · {len(block.get('ids') or [])} record(s)", GREY)
        )
    print(paint(f"    total {manifest.get('tokens')} tokens", GREY))
    print(rule())
    for line in str(rendered.get("rendered") or "").splitlines():
        print("  " + paint(line, GREY))
    print(rule())


async def act_glass_box(tools: Tools) -> None:
    """Follow a citation down, one `record` call per hop — the agent's own path."""

    title("ACT IV · GLASS BOX", "follow a citation down with nothing but `record`")
    hits = await tools.recall(REQUEST)
    if not hits:
        warn("nothing to open yet")
        return
    seen: set[str] = set()
    current = str(hits[0].get("id"))
    depth = 0
    while current and current not in seen and depth < 5:
        seen.add(current)
        try:
            record = await tools.record(current)
        except RuntimeError as error:
            warn(str(error))
            return
        collection = str(record.get("collection") or "?")
        label = {"messages": "L0", "memories": "L1", "scenes": "L2", "persona": "L3"}.get(
            collection, "  "
        )
        indent = "    " + "   " * depth
        elbow = paint("└─ ", GREY) if depth else ""
        print(
            f"{indent}{elbow}{paint(label, BOLD, CYAN)} {paint(short(current), BOLD)} "
            + paint(collection, GREY)
        )
        wrapped(str((record.get("content") or {}).get("text") or ""), indent=indent + "       ")
        parents = [
            str(parent)
            for parent in (record.get("derived_from") or [])
            if str(parent) != str(record.get("run_id"))
        ]
        current = parents[0] if parents else ""
        depth += 1
    note("every hop was a stored citation, validated when the claim was written")


HELP = f"""
  {paint("remember", BOLD, BCYAN)} <text>  write a message, then watch the cascade
  {paint("recall", BOLD, BCYAN)} <task>    fused search across the layers
  {paint("context", BOLD, BCYAN)} <task>   the bounded prompt for a request
  {paint("rules", BOLD, BCYAN)} [floor]    standing rules at or above a floor
  {paint("replay", BOLD, BCYAN)} [session] one conversation, exactly as it happened
  {paint("trace", BOLD, BCYAN)} <id>       open a record by its cited id
  {paint("answer", BOLD, BCYAN)} <q>       a cited answer from this memory
  {paint("tools", BOLD, BCYAN)}            what this agent is allowed to do
  {paint("help", BOLD, BCYAN)}  ·  {paint("quit", BOLD, BCYAN)}
"""


async def handle(tools: Tools, line: str, live: dict[str, int]) -> bool:
    line = line.strip()
    if not line:
        return True
    verb, _, argument = line.partition(" ")
    verb, argument = verb.lower(), argument.strip()

    if verb in {"quit", "exit", "q"}:
        return False
    if verb in {"help", "?"}:
        print(HELP)
        return True
    if verb == "tools":
        for name, tool in sorted(tools.available.items()):
            writes = getattr(tool.annotations, "read_only_hint", True) is False
            mark = paint("WRITES", BOLD, RED) if writes else paint("read", GREEN)
            print(
                f"    {paint(name.ljust(16), BOLD)} {mark}  {paint(tool.description or '', GREY)}"
            )
        return True
    if verb == "remember":
        if not argument:
            warn("usage: remember <text>")
            return True
        live["ordinal"] += 1
        record_id = await tools.remember(
            {
                "session": f"s3-live-{RUN}",
                "role": "user",
                "at": datetime.now(UTC),
                "text": argument,
            },
            live["ordinal"],
        )
        print(f"  {paint('⏺ remember', MAGENTA)} {paint(short(record_id), BOLD)} written")
        async with Spinner("waiting for the worker to fold it in"):
            await asyncio.sleep(8)
        render_rules(await tools.rules(80), 80)
        return True
    if verb == "rules":
        floor = float(argument) if argument else 80.0
        render_rules(await tools.rules(floor), floor)
        return True
    if verb == "recall":
        for hit in (await tools.recall(argument or REQUEST))[:10]:
            print(
                f"    {paint(str(hit.get('collection') or '?').ljust(9), GREY)} "
                f"{paint(short(hit.get('id')), BOLD)} " + clip(str(hit.get("text") or ""), 96)
            )
        return True
    if verb == "context":
        rendered = await tools.context(argument or REQUEST)
        print(rule())
        for text_line in str(rendered.get("rendered") or "").splitlines():
            print("  " + paint(text_line, GREY))
        print(rule())
        return True
    if verb == "replay":
        for hit in await tools.replay(argument or SESSION_ONE):
            fields = hit.get("fields") or {}
            print(
                f"    {paint(str(fields.get('ordinal')).rjust(3), GREY)} "
                f"{paint(str(fields.get('role') or '?').rjust(9), BCYAN, BOLD)}  "
                + paint(clip(str(hit.get("text") or ""), 110), GREY)
            )
        return True
    if verb == "trace":
        if not argument:
            warn("usage: trace <record id>")
            return True
        try:
            record = await tools.record(argument)
        except RuntimeError as error:
            warn(str(error))
            return True
        print(json.dumps(record, indent=2, default=str)[:1200])
        return True
    if verb == "answer":
        if not argument:
            warn("usage: answer <question>")
            return True
        try:
            result = await tools.answer(argument)
        except RuntimeError as error:
            warn(str(error))
            return True
        wrapped(str(result.get("answer") or ""), indent="    ")
        cited = ", ".join(short(item) for item in (result.get("citations") or [])[:8])
        print(paint(f"    cites {cited}", GREY))
        return True
    warn(f"unknown command {verb!r}; type help")
    return True


async def walkthrough(session: ClientSession) -> None:
    """Everything the connected agent can do, in the order it would do it."""

    init = await session.initialize()
    print(
        f"  connected to {paint(init.server_info.name, BOLD)} "
        + paint(f"(protocol {init.protocol_version})", GREY)
    )
    tools = Tools(session)
    await tools.discover()
    print()
    for name, tool in sorted(tools.available.items()):
        writes = getattr(tool.annotations, "read_only_hint", True) is False
        mark = paint("WRITES", BOLD, RED) if writes else paint("read  ", GREEN)
        print(f"    {mark}  {paint(name, BOLD)}")
    if "remember" not in tools.available:
        raise SystemExit(
            "this package declares no ingest tool — publish examples/agent_memory_catalog"
        )

    await act_write(tools)
    if not await act_cascade(tools):
        warn("no claims appeared within the timeout")
        note("is `memseek worker` running, and is a real model configured?")
        note("LLM_FAKE=1 cannot cite record UUIDs, so extraction honestly no-ops")
    else:
        await act_recall(tools)
        await act_glass_box(tools)

    live = {"ordinal": 100}
    if sys.stdin.isatty():
        title("ACT V · YOUR TURN", "everything below goes through the same seven tools")
        print(HELP)
        while True:
            try:
                line = await ainput(paint("\n  mcp ▸ ", BOLD, BCYAN))
            except EOFError, KeyboardInterrupt:
                print()
                break
            try:
                if not await handle(tools, line, live):
                    break
            except RuntimeError as error:
                print(paint(f"  tool error: {error}", RED))
    else:
        title("ACT V · SCRIPTED TOUR", "stdin is not a TTY")
        for command in ("tools", "rules 0", f"replay {SESSION_ONE}", "recall billing"):
            print(f"\n  {paint('▸ ' + command, BOLD, BCYAN)}")
            await handle(tools, command, live)


async def main() -> None:
    url = os.environ.get("MEMSEEK_URL")
    key = os.environ.get("MEMSEEK_API_KEY")
    if not url or not key:
        raise SystemExit("set MEMSEEK_URL and MEMSEEK_API_KEY (see .memseek/api_key)")

    title("AGENT MEMORY OVER MCP", "seven declared tools, and no other route in")
    print(f"  service: {paint(url, GREY)}   ·   stdio bridge: {paint('memseek mcp', GREY)}")

    params = StdioServerParameters(command="memseek", args=["mcp"], env=dict(os.environ))
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await walkthrough(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
