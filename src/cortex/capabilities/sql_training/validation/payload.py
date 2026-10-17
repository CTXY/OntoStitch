from __future__ import annotations

from typing import Any

from cortex.capabilities.sql_training.runtime_config import RuntimeValidationConfig
from cortex.capabilities.sql_training.validation.models import GenerationRequest
from cortex.schema import GraphSpace


def build_generation_request(
    config: RuntimeValidationConfig,
    *,
    question: str,
    trace_id: str,
    graph_space: GraphSpace,
    join_paths: tuple[dict[str, Any], ...] = (),
) -> GenerationRequest:
    rules = {
        node.id: node.model_dump(mode="json")
        for node in graph_space.nodes
        if node.node_type == "generation_rule"
    }
    complete_join_paths = []
    for path in join_paths:
        if path.get("kind") != "join":
            continue
        item = dict(path)
        rule_id = item.get("joinability_rule_id")
        if rule_id in rules:
            item["joinability_rule"] = rules[rule_id]
        complete_join_paths.append(item)
    return GenerationRequest(
        trace_id=trace_id,
        question=question,
        dialect=config.dialect,
        graph_space=graph_space,
        join_paths=tuple(complete_join_paths),
        enable_query_execution=config.enable_query_execution,
        max_retry_count=config.max_retry_count,
    )
