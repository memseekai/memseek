"""Advanced renewal desk: streamed journal, recall, attach, fork, and inspection.

Run against the configured stack: uv run python examples/computer_renewal_advanced.py
"""

import asyncio

from _computer_renewal_support import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
