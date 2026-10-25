"""Default locations of committed data and of the git-ignored download cache.

Both assume the repository layout (editable install). Every adapter also accepts
explicit paths, so nothing depends on these defaults.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
CACHE_DIR = REPO_ROOT / ".cache" / "sentinel"
