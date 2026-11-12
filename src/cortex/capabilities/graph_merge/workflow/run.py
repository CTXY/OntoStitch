"""Checkpoint fixed evidence, matching, and each accepted graph transaction."""

from __future__ import annotations

import hashlib
import json
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from cortex.application.merge_graph import GraphMergeResult
from cortex.capabilities.graph_merge.models import EvidenceItem, IdentityGroupingResult
from cortex.infrastructure.progress import emit_progress
from cortex.schema import GraphState, SchemaDefinition


class MergeRunState(TypedDict, total=False):
    request: dict
    grouping: dict
    evidence: list[dict]
    result: dict
    assembly: dict


def run_merge(operator, existing, incoming, schema, *, saver=None, run_id=None):
    run_id = run_id or uuid4().hex
    request = {
        "existing": existing.model_dump(mode="json"),
        "incoming": incoming.model_dump(mode="json"),
        "schema": schema.model_dump(mode="json"),
        "workflow_version": operator.workflow_version,
    }
    fingerprint = hashlib.sha256(
        json.dumps(request, sort_keys=True).encode()
    ).hexdigest()

    def match(state):
        emit_progress("phase", phase="matching_nodes")
        req = state["request"]
        old, new = (
            GraphState.model_validate(req["existing"]),
            GraphState.model_validate(req["incoming"]),
        )
        definition = SchemaDefinition.model_validate(req["schema"])
        sources = tuple(
            dict.fromkeys(
                [s for n in (*old.graph.nodes, *new.graph.nodes) for s in n.sources]
                + [
                    s
                    for c in old.conflict_nodes
                    for a in c.alternatives
                    for s in a.sources
                ]
            )
        )
        resolver = operator.node_reconciler.evidence_resolver
        try:
            if hasattr(resolver, "clear"):
                resolver.clear()
            evidence = resolver.resolve(sources) if resolver and sources else ()
        except Exception:  # noqa: BLE001 - missing evidence must preserve the original definitions
            evidence = ()  # The group proposal boundary preserves unsupported originals.
        grouping = operator.group_nodes(old, new, definition)
        return {
            "grouping": grouping.model_dump(mode="json"),
            "evidence": [e.model_dump(mode="json") for e in evidence],
        }

    def prepare(state):
        emit_progress("phase", phase="preparing_merge")
        req = state["request"]
        return {
            "assembly": operator.begin_merge(
                GraphState.model_validate(req["existing"]),
                GraphState.model_validate(req["incoming"]),
                SchemaDefinition.model_validate(req["schema"]),
                IdentityGroupingResult.model_validate(state["grouping"]),
            )
        }

    def integrate(state):
        req = state["request"]
        emit_progress("merge_progress", remaining=len(state["assembly"]["remaining"]))
        return {
            "assembly": operator.advance_merge(
                state["assembly"],
                GraphState.model_validate(req["existing"]),
                GraphState.model_validate(req["incoming"]),
                SchemaDefinition.model_validate(req["schema"]),
                evidence_items=tuple(
                    EvidenceItem.model_validate(e) for e in state["evidence"]
                ),
                checkpointer=saver,
                run_id=run_id,
            )
        }

    def finalize(state):
        emit_progress("phase", phase="finalizing_merge")
        req = state["request"]
        result = operator.finish_merge(
            state["assembly"],
            GraphState.model_validate(req["incoming"]),
            SchemaDefinition.model_validate(req["schema"]),
        )
        return {
            "result": result.model_copy(update={"run_id": run_id}).model_dump(
                mode="json"
            )
        }

    builder = StateGraph(MergeRunState)
    builder.add_node("match_nodes", match)
    builder.add_node("prepare_graph", prepare)
    builder.add_node("integrate_group", integrate)
    builder.add_node("finalize_graph", finalize)
    builder.add_edge(START, "match_nodes")
    builder.add_edge("match_nodes", "prepare_graph")
    for source in ("prepare_graph", "integrate_group"):
        builder.add_conditional_edges(
            source,
            lambda state: (
                "integrate_group"
                if state["assembly"]["remaining"]
                else "finalize_graph"
            ),
        )
    builder.add_edge("finalize_graph", END)
    graph = builder.compile(checkpointer=saver, name="graph_merge")
    config = {
        "recursion_limit": 32
        + 8 * (len(existing.graph.nodes) + len(incoming.graph.nodes)),
        "configurable": {"thread_id": run_id},
        "metadata": {
            "thread_id": run_id,
            "merge_run_id": run_id,
            "input_fingerprint": fingerprint,
        },
    }
    saved = graph.get_state(config) if saver is not None else None
    if saved and saved.values and saved.values.get("request") != request:
        raise ValueError(
            "Cannot resume merge: inputs, configuration or prompts changed"
        )
    try:
        output = (
            saved.values
            if saved and saved.values and not saved.next
            else graph.invoke(
                None if saved and saved.values else {"request": request},
                config=config,
                **({"durability": "sync"} if saver is not None else {}),
            )
        )
    except BaseException as exc:
        exc.add_note(f"Graph Merge checkpoint run_id={run_id}")
        raise
    return GraphMergeResult.model_validate(output["result"])
