"""Rebuild the committed OFAC snapshot from the live SDN list.

Run from the repository root:

    uv run python -m sentinel.tools.refresh_sanctions [output.json]
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from sentinel.tools.sanctions import DEFAULT_SNAPSHOT, refresh_snapshot


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_SNAPSHOT
    snapshot = asyncio.run(refresh_snapshot(path))
    print(
        f"wrote {len(snapshot.addresses)} EVM addresses "
        f"(SDN published {snapshot.publish_date}) to {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
