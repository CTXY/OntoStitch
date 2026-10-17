from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from cortex.application.online_context import QueryExample
from cortex.schema import GraphSpace

_SEMANTIC_TYPES = {"concept", "metric", "generation_rule"}
_START_MARKER = "<!-- cortex-query-examples:start -->"
_END_MARKER = "<!-- cortex-query-examples:end -->"


@dataclass(frozen=True)
class QueryExampleSelection:
    examples: tuple[QueryExample, ...]
    missing_source_ids: tuple[str, ...] = ()


def select_query_examples(
    *,
    graph: GraphSpace,
    relevant_asset_ids: set[str],
    example_index: Mapping[str, QueryExample],
    top_k: int,
) -> QueryExampleSelection:
    """Rank source Cases by how many final relevant semantic assets cite them."""
    if top_k < 0:
        raise ValueError("query example top_k must be non-negative")
    if top_k == 0 or not relevant_asset_ids:
        return QueryExampleSelection(())

    support = Counter[str]()
    matched_assets: dict[str, set[str]] = defaultdict(set)
    for node in graph.nodes:
        if node.id not in relevant_asset_ids or node.node_type not in _SEMANTIC_TYPES:
            continue
        source_ids = {
            source_id
            for source in node.sources
            if (source_id := ((source.case_id or "").strip() or source.source_id.strip()))
        }
        for source_id in source_ids:
            support[source_id] += 1
            matched_assets[source_id].add(node.id)

    missing = tuple(
        sorted(
            source_id
            for source_id in support
            if source_id not in example_index or not example_index[source_id].question.strip()
        )
    )
    ranked = sorted(
        (
            (source_id, count)
            for source_id, count in support.items()
            if source_id in example_index and example_index[source_id].question.strip()
        ),
        key=lambda item: (-item[1], item[0]),
    )[:top_k]
    return QueryExampleSelection(
        examples=tuple(
            example_index[source_id].model_copy(
                update={
                    "source_id": source_id,
                    "support_count": count,
                    "matched_asset_ids": tuple(sorted(matched_assets[source_id])),
                }
            )
            for source_id, count in ranked
        ),
        missing_source_ids=missing,
    )


def render_query_examples(
    content: str,
    examples: tuple[QueryExample, ...],
    *,
    chinese: bool,
) -> str:
    """Replace the marked query-example section, keeping repeated backfills idempotent."""
    content = _without_query_example_section(content).rstrip()
    if not examples:
        return content
    if chinese:
        heading = "历史 Query Examples（仅作参考）："
        note = (
            "这些历史 examples 供你参考。若历史 example 与已确认的 user guidance、"
            "当前问题、task evidence、数据库 schema 或可用字段冲突，应以后者为准。"
        )
    else:
        heading = "Historical query examples (reference only):"
        note = (
            "These historical examples are provided for reference. If a historical example "
            "conflicts with resolved user guidance, the current question, task evidence, "
            "database schema, or available columns, follow those higher-priority sources instead."
        )
    lines = [_START_MARKER, heading, note]
    for example in examples:
        lines.append(
            f"- [{example.source_id}] ({example.support_count} supporting assets) "
            f"Question: {example.question}"
        )
        if example.sql:
            lines.append(f"  SQL: {example.sql}")
    lines.append(_END_MARKER)
    return content + "\n\n" + "\n".join(lines)


def _without_query_example_section(content: str) -> str:
    start = content.find(_START_MARKER)
    if start < 0:
        return content
    end = content.find(_END_MARKER, start)
    if end < 0:
        return content
    return content[:start] + content[end + len(_END_MARKER) :]


__all__ = [
    "QueryExampleSelection",
    "render_query_examples",
    "select_query_examples",
]
