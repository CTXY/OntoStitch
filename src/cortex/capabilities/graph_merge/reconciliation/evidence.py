"""Source evidence ports and a cache scoped to one merge invocation."""

from __future__ import annotations

from typing import Protocol

from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import SourceReference


class EvidenceResolver(Protocol):
    def resolve(
        self, sources: tuple[SourceReference, ...]
    ) -> tuple[EvidenceItem, ...]: ...


class CachedEvidenceResolver:
    def __init__(self, resolver: EvidenceResolver):
        self.resolver = resolver
        self._cache: dict[SourceReference, EvidenceItem] = {}

    def clear(self) -> None:
        self._cache.clear()

    def resolve(self, sources: tuple[SourceReference, ...]) -> tuple[EvidenceItem, ...]:
        missing = tuple(dict.fromkeys(s for s in sources if s not in self._cache))
        if missing:
            self._cache.update(
                {item.source: item for item in self.resolver.resolve(missing)}
            )
        return tuple(self._cache[s] for s in sources if s in self._cache)


def resolve_group_evidence(group, resolver):
    sources = tuple(
        dict.fromkeys(s for member in group.members for s in member.sources)
    )
    evidence = resolver.resolve(sources) if resolver is not None and sources else ()
    by_source = {item.source: item for item in evidence}
    return {
        member.ref: tuple(by_source[s] for s in member.sources if s in by_source)
        for member in group.members
    }


def evidence_complete(members, evidence_by_ref) -> bool:
    return all(
        member.sources
        and {item.source for item in evidence_by_ref.get(member.ref, ())}
        == set(member.sources)
        and all(
            item.content.get("question") and item.content.get("sql")
            for item in evidence_by_ref[member.ref]
        )
        for member in members
    )
