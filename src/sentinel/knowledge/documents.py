"""Load fraud-pattern markdown documents and split them into embeddable chunks.

Each document in `data/patterns/` has this shape:

    # Title

    ## Description
    ...
    ## On-chain indicators
    - bullet
    ## Example signals
    - bullet
    ## References
    - https://...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sentinel.tools.paths import DATA_DIR

DEFAULT_PATTERNS_DIR = DATA_DIR / "patterns"

REQUIRED_SECTIONS = ("Description", "On-chain indicators", "Example signals", "References")

_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


@dataclass(frozen=True)
class PatternSource:
    """A parsed pattern document, before any scoring."""

    doc_id: str
    title: str
    content: str
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def indicators(self) -> list[str]:
        return _bullets(self.sections.get("On-chain indicators", ""))

    @property
    def signals(self) -> list[str]:
        return _bullets(self.sections.get("Example signals", ""))

    @property
    def references(self) -> list[str]:
        return _bullets(self.sections.get("References", ""))


@dataclass(frozen=True)
class Chunk:
    doc_id: str
    chunk_id: str
    text: str


def _bullets(text: str) -> list[str]:
    return [m.group(1) for line in text.splitlines() if (m := _BULLET.match(line))]


def parse_pattern_markdown(doc_id: str, text: str) -> PatternSource:
    title = ""
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    if not title:
        raise ValueError(f"pattern doc {doc_id!r} has no '# Title' line")
    return PatternSource(
        doc_id=doc_id,
        title=title,
        content=text.strip(),
        sections={name: "\n".join(lines).strip() for name, lines in sections.items()},
    )


def load_pattern_docs(directory: Path = DEFAULT_PATTERNS_DIR) -> list[PatternSource]:
    """Load every `*.md` in `directory`, sorted by file name; doc_id is the file stem."""
    docs = [
        parse_pattern_markdown(path.stem, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.md"))
    ]
    if not docs:
        raise FileNotFoundError(f"no pattern documents in {directory}")
    return docs


def chunk_document(doc: PatternSource, max_chars: int = 1500) -> list[Chunk]:
    """Split a document into chunks for embedding.

    References are left out (URLs add noise to embeddings). Short documents become a
    single chunk; longer ones get one chunk per section, each prefixed with the title so
    every chunk stands on its own.
    """
    parts = [(name, body) for name, body in doc.sections.items() if name != "References" and body]
    whole = "\n\n".join([doc.title, *(f"{name}\n{body}" for name, body in parts)])
    if len(whole) <= max_chars:
        return [Chunk(doc.doc_id, f"{doc.doc_id}#0", whole)]
    return [
        Chunk(doc.doc_id, f"{doc.doc_id}#{i}", f"{doc.title}\n\n{name}\n{body}"[:max_chars])
        for i, (name, body) in enumerate(parts)
    ]
