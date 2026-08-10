"""Terminal handoff to the browser workspace explorer.

The explorer is a read-only debugging console. The browser link carries the
API URL but deliberately never carries the workspace bearer key: URLs leak
into history, logs, and referrer headers.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import urlencode


def _terminal_link(label: str, url: str) -> str:
    """Use OSC 8 where available; other terminals still show a usable URL."""

    if sys.stdout.isatty() and os.environ.get("TERM") not in {None, "dumb"}:
        return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"
    return url


def print_workspace_explorer(*, api_url: str, api_key: str) -> None:
    """Print the connection facts after an example selects a workspace."""

    explorer_base = os.environ.get(
        "MEMSEEK_EXPLORER_URL",
        "http://localhost:4321/showcase/workspace-explorer/index.html",
    )
    separator = "&" if "?" in explorer_base else "?"
    explorer_url = explorer_base + separator + urlencode({"api_url": api_url})
    print("\n  WORKSPACE EXPLORER")
    print(f"  Open:    {_terminal_link(explorer_url, explorer_url)}")
    print(f"  API URL: {api_url}")
    print(f"  API key: {api_key}")
    print(
        "  The key is not included in the link. Paste it into the explorer; "
        "the browser keeps it only in memory."
    )
