"""Resolve explicit evidence references without reconstructing provenance in an LLM."""

from __future__ import annotations

import json
from typing import Any, Protocol

from cortex.capabilities.graph_merge.models import Model
from cortex.schema import SourceReference


class EvidenceItem(Model):
    id: str
    source: SourceReference
    case_id: str | None
    content: dict[str, Any]


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


class EvidencePresentation:
    """Conversation-local body deduplication; attribution stays on each assertion."""

    def __init__(self, context, supplied=()):
        self.contents, self.by_source = {}, {}
        for evidence in context.evidence:
            if (
                evidence.id in self.contents
                and self.contents[evidence.id] != evidence.content
            ):
                raise ValueError(f"Conflicting content for evidence ID {evidence.id}")
            self.contents[evidence.id] = evidence.content
            self.by_source.setdefault(evidence.source, set()).add(evidence.id)
        self.seen = {
            key for row in supplied for key in row.get("evidence_ids", [row["id"]])
        }

    def ids_for(self, sources):
        return sorted(
            {key for source in sources for key in self.by_source.get(source, ())}
        )

    def read(self, ids, *, refresh=False):
        requested = set(ids)
        groups = {}
        for key in sorted(requested if refresh else requested - self.seen):
            if key in self.contents:
                signature = json.dumps(
                    self.contents[key], sort_keys=True, ensure_ascii=False
                )
                groups.setdefault(signature, []).append(key)
        previous = (
            {}
            if refresh
            else {
                json.dumps(self.contents[key], sort_keys=True, ensure_ascii=False): key
                for key in sorted(self.seen)
                if key in self.contents
            }
        )
        rows = []
        for signature, keys in groups.items():
            row = {"id": keys[0]}
            if signature in previous:
                row["same_content_as"] = previous[signature]
            else:
                row["content"] = self.contents[keys[0]]
            if len(keys) > 1:
                row["evidence_ids"] = keys
            rows.append(row)
        result = {
            "items": rows,
            "already_supplied": [] if refresh else sorted(requested & self.seen),
            "missing_ids": sorted(requested - self.contents.keys()),
        }
        self.seen.update(requested & self.contents.keys())
        return result

    def append(self, result, ids):
        """New bodies are always at the end of a graph/context response."""
        rows = self.read(ids)["items"]
        if rows:
            result["evidence"] = rows
        return result


def evidence_sources(context, ids):
    unknown = set(ids) - {e.id for e in context.evidence}
    if unknown:
        raise ValueError(
            f"Unknown evidence_ids: {sorted(unknown)}; use displayed evidence IDs"
        )
    return tuple(dict.fromkeys(e.source for e in context.evidence if e.id in ids))


def compact_attribution(value, context):
    """Use the same source encoding in initial packets and on-demand reads."""
    from cortex.schema import SourceReference

    index = {}
    for evidence in context.evidence:
        index.setdefault(evidence.source.model_dump_json(), set()).add(evidence.id)

    def walk(item):
        if isinstance(item, list):
            return [walk(row) for row in item]
        if not isinstance(item, dict):
            return item
        attributed = "sources" in item and any(
            key in item for key in ("node_type", "edge_type", "alternative_id")
        )
        result = {
            key: val if key in {"properties", "content", "overrides"} else walk(val)
            for key, val in item.items()
            if key not in {"sources", "common_sources"} or not attributed
        }
        if attributed:
            result["evidence_ids"] = sorted(
                {
                    ident
                    for ref in item["sources"]
                    for ident in index.get(
                        SourceReference.model_validate(ref).model_dump_json(), ()
                    )
                }
            )
            if item.get("common_sources"):
                result["common_evidence_ids"] = sorted(
                    {
                        ident
                        for ref in item["common_sources"]
                        for ident in index.get(
                            SourceReference.model_validate(ref).model_dump_json(), ()
                        )
                    }
                )
        return result

    return walk(value)
