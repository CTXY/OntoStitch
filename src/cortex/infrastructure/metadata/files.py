"""Read supplied text files into a deduplicated collection of source records."""

import hashlib
from collections.abc import Iterable
from pathlib import Path

from cortex.capabilities.metadata.models import SourceRecord
from cortex.infrastructure.bird_descriptions import decode_description


def read_initial_files(
    paths: Iterable[str | Path] = (), *, max_file_bytes: int = 2_000_000
) -> list[SourceRecord]:
    sources: dict[str, SourceRecord] = {}
    for supplied in paths:
        root = Path(supplied).expanduser().resolve(strict=True)
        paths_in_root = (
            sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink())
            if root.is_dir()
            else [root]
        )
        for path in paths_in_root:
            if path.suffix.lower() not in {
                ".csv",
                ".json",
                ".jsonl",
                ".yaml",
                ".yml",
                ".md",
                ".txt",
            }:
                raise ValueError(
                    f"unsupported initial file type: {path}; convert to text"
                )
            if path.stat().st_size > max_file_bytes:
                raise ValueError(f"initial file exceeds {max_file_bytes} bytes: {path}")
            raw = path.read_bytes()
            digest = hashlib.sha256(str(path).encode() + raw).hexdigest()[:24]
            source = SourceRecord(
                id=f"file:{digest}",
                kind="initial_file",
                locator=str(path),
                content=decode_description(raw),
            )
            sources[source.id] = source
    return list(sources.values())
