"""Shared renewal fixture references and terminal formatting."""

from __future__ import annotations

import os
import secrets
import sys
import textwrap
from pathlib import Path

# --- the account this demo maintains a renewal position for ----------------
RUN = secrets.token_hex(3)
ACCOUNT = "Acme Cloud"
ENTITY = f"account:acme-{RUN}"

CATALOG_ROOT = Path(__file__).with_name("computer_renewal_catalog")
PACKAGE = "computer_renewal_demo@1.0.0"

FAST_COMPUTER = "fast_workspace@1"
RESEARCH_COMPUTER = "research_workspace@1"
ANALYST = "renewal_analyst@2"
EXTRACTOR = "contract_extract@3"
POLICY = "evidence_spine@1"

EVIDENCE = "renewal_evidence"
TERMS = "contract_terms"
RISKS = "renewal_risks"
OBSERVATIONS = "task_observations"
PROPOSALS = "renewal_proposals"

# ---------------------------------------------------------------------------
# Terminal styling. Colors only reach a real TTY, and honor NO_COLOR
# (https://no-color.org) and TERM=dumb, so a pipe or a CI log stays clean.
# ---------------------------------------------------------------------------
_C = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None and os.environ.get("TERM") != "dumb"


def _s(*codes: int) -> str:
    return ("\033[" + ";".join(map(str, codes)) + "m") if _C else ""


RESET, BOLD, DIM = _s(0), _s(1), _s(2)
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN, GREY = (
    _s(31),
    _s(32),
    _s(33),
    _s(34),
    _s(35),
    _s(36),
    _s(90),
)
BCYAN, BGREEN, BYELLOW, BMAG = _s(96), _s(92), _s(93), _s(95)


def paint(text: str, *codes: str) -> str:
    return ("".join(codes) + text + RESET) if _C else text


def rule(char: str = "─", width: int = 76) -> str:
    return paint(char * width, GREY)


def header(title: str, subtitle: str = "") -> None:
    print("\n" + rule("━"))
    line = f"  {paint(title, BOLD, BMAG)}"
    if subtitle:
        line += f"  {paint(subtitle, GREY)}"
    print(line)
    print(rule("━"))


def note(text: str) -> None:
    print(paint(f"  · {text}", GREY))


def short(value: str | None) -> str:
    return value[:8] if value else "—"


def wrap(text: str, width: int = 66) -> list[str]:
    return textwrap.wrap(" ".join(str(text).split()), width=width) or [""]


def block(text: str, *codes: str, lead: str = "      ") -> None:
    for line in wrap(text):
        print(lead + paint(line, *codes))


# ---------------------------------------------------------------------------
# The provider side of the boundary.
#
# `cloudflare/computer-runtime` is the real implementation: a Worker that runs
# Programs in a Dynamic Worker and Agents against Workers AI, inside a
# network-denied durable workspace. This is a stand-in for it — same signed wire
# protocol, same response envelope, same bounds — small enough to read in one
# sitting, so you can watch what Memseek asks a provider for and what it refuses
# to accept back. It sandboxes nothing and calls no model: the two executors
# below are deterministic, which is exactly why the demo is reproducible.
# ---------------------------------------------------------------------------
