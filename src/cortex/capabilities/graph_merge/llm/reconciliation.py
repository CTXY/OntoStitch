"""Production relation-only agent: compact view, selective reads, partial edits."""
import json
from importlib.resources import files
from itertools import count
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import model_validator

from cortex.capabilities.graph_merge.integration.models import Model, ReviewReference
from cortex.capabilities.graph_merge.integration.relation_context import (
    edge_view,
    node_view,
)
from cortex.capabilities.graph_merge.integration.relation_edits import (
    RelationPatch,
    submit_patch,
)
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace


class RelationReview(Model):
    verdict: Literal["accept", "revise"]
    issues: tuple[str, ...] = ()
    issue_refs: tuple[ReviewReference, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        if self.verdict == "accept" and (self.issues or self.issue_refs):
            raise ValueError("accept requires empty issues and issue_refs")
        if self.verdict == "revise" and not self.issues:
            raise ValueError("revise needs concrete issues")
        return self


def invoke_relation(agent, phase, context, payload):
    writer = phase == "reconcile"
    prompt_root = files("cortex.capabilities.graph_merge.llm.prompts")
    system = prompt_root.joinpath("incremental_relation_shared.md").read_text()
    system += "\n\n" + prompt_root.joinpath(
        "incremental_relation_write.md" if writer else "incremental_relation_assess.md"
    ).read_text()
    messages = [SystemMessage(system), HumanMessage(json.dumps(payload, ensure_ascii=False))]
    raw_tools = {t.name: t for t in build_tools(context)}
    state = context.candidate
    nodes = {n.id: n for n in state.graph.nodes}
    edges = {e.id: e for e in state.graph.edges}
    evidence_ids = {}
    for ev in context.evidence:
        evidence_ids.setdefault(ev.source, []).append(ev.id)
    finished = None
    trace = EpisodeTrace(agent.trace_sink, phase, budget=agent.call_budget)
    # Model call traces use the same file layout as the established runner.
    if hasattr(agent.trace_sink, "path"):
        from cortex.infrastructure.artifacts.graph_merge_trace import (
            FileSystemGraphMergeTraceSink,
        )
        trace.sink = FileSystemGraphMergeTraceSink(agent.trace_sink.path.with_name("model_calls.jsonl"))

    @tool
    def inspect_items(ids: list[str], fields: list[str]) -> dict:
        """Read named missing fields, not complete objects. Node content: targets/expression/action/condition as applicable; Concept: relation_metadata; edge: description/branches. Returns valid items plus per-object errors so one missing ID/field does not discard successful reads."""
        blocked = {"*", "properties", "sources", "source_metadata", "table_schema"}
        errors = [{"field": f, "error": "Request a named missing field, not complete metadata"}
                  for f in fields if f in blocked]
        rows = []
        for key in ids:
            obj = nodes.get(key) or edges.get(key)
            if obj is None:
                errors.append({"id": key, "error": "Not current; use displayed current IDs"})
                continue
            raw = obj.model_dump(mode="json")
            semantic = node_view(obj) if key in nodes else edge_view(obj, evidence_ids)
            if key in edges:
                semantic.setdefault("branches", [])
                semantic["alternative_ids"] = semantic["branches"]
            elif obj.node_type == "concept":
                semantic.setdefault("relation_metadata", [])
            row = {"id": key}
            for field in fields:
                if field in blocked:
                    continue
                if field in semantic:
                    row[field] = semantic[field]
                elif field in raw:
                    row[field] = raw[field]
                elif field in obj.properties:
                    row[field] = obj.properties[field]
                else:
                    if field == "relation_metadata":
                        errors.append({"id": key, "field": field, "error": "Concept-only field; use edge descriptions for other types"})
                        continue
                    declared = context.schema.effective_node_fields(obj.node_type) if key in nodes else {}
                    if field not in declared:
                        errors.append({"id": key, "field": field, "error": "Field does not apply to this object", "available_fields": sorted((semantic.keys() | obj.properties.keys()) - blocked)})
                        continue
                    row[field] = None
            rows.append(row)
        return {"items": rows, **({"read_errors": errors} if errors else {})}

    @tool
    def read_evidence(ids: list[str]) -> dict:
        """Read evidence absent from the initial view; supplied evidence is not repeated."""
        supplied = {e["id"] for e in payload.get("evidence", [])}
        missing = sorted(set(ids) - supplied)
        return {"already_supplied": sorted(set(ids) & supplied),
                "items": [{"id": e.id, "content": e.content}
                          for e in context.evidence if e.id in missing]}

    @tool
    def read_guidance(topic: Literal["concept", "metric", "generation_rule", "event", "operations"]) -> dict:
        """Read required creation/formula/intent details only for a necessary node reconstruction."""
        return raw_tools["read_guidance"].invoke({"topic": topic})

    @tool
    def extend_task(ids: list[str], reason: str) -> dict:
        """Extend writable scope only for a necessary coupled repair; a read alone grants no permission."""
        result = context.extend_task(ids, reason)
        return {k: v for k, v in result.items() if k != "objects"}

    @tool
    def submit_changes(patch: RelationPatch) -> dict:
        """Submit the complete intended local change, using changed fields only. Unmentioned content and sources stay unchanged. Use retirements to couple deletion and before/after provenance; use edge_merges for equivalent assertions. A valid submission ends this task directly."""
        nonlocal finished
        finished = submit_patch(context, patch)
        return {"status": "accepted"}

    tools = [inspect_items, read_evidence, read_guidance]
    if writer:
        tools += [extend_task, submit_changes]
    else:
        tools += [RelationReview]
    lookup = {t.name: t for t in tools if hasattr(t, "name")}
    result = None
    try:
        for step in (count() if agent.max_calls is None else range(agent.max_calls)):
            final_call = agent.max_calls is not None and step == agent.max_calls - 1
            available = ([submit_changes] if writer else [RelationReview]) if final_call else tools
            if final_call:
                messages.append(HumanMessage(
                    "This is the reserved completion call; no further reads. Submit the complete supported result from the supplied view and completed reads. "
                    + ("Use failure if a necessary safe decision remains impossible; do not hide unresolved required repairs in an empty success." if writer
                       else "Return the bounded assessment of actual changes, with concrete defects only, not speculative missing facts.")))
            response = agent.llm.bind_tools(available, tool_choice="auto").invoke(
                messages, config={"callbacks": [trace]})
            messages.append(response)
            if not response.tool_calls:
                try:
                    if writer:
                        result = submit_patch(context, RelationPatch.model_validate_json(response.content))
                    else:
                        result = RelationReview.model_validate_json(response.content)
                    return result
                except (ValueError, TypeError) as exc:
                    messages.append(HumanMessage(f"Invalid result: {exc}. Return the complete corrected result."))
                    continue
            for call in response.tool_calls:
                try:
                    if final_call and call["name"] != ("submit_changes" if writer else "RelationReview"):
                        raise ValueError("Reserved completion call does not permit additional reads or scope extensions")
                    if call["name"] in {"submit_changes", "RelationReview"} and len(response.tool_calls) != 1:
                        raise ValueError("Read first; submit alone using the read results")
                    if call["name"] == "RelationReview":
                        result = RelationReview.model_validate(call["args"])
                        tool_result = {"status": "accepted"}
                    else:
                        tool_result = lookup[call["name"]].invoke(call["args"])
                    trace.record("tool_result", name=call["name"],
                                 arguments=call["args"], result=tool_result)
                    messages.append(ToolMessage(json.dumps(tool_result, ensure_ascii=False), tool_call_id=call["id"]))
                    if finished is not None:
                        result = finished
                        return result
                    if result is not None:
                        return result
                except (ValueError, TypeError, KeyError) as exc:
                    tool_result = {"status": "rejected", "error": str(exc),
                                   "repair": "Rejected proposal saved no changes. Resubmit the complete corrected proposal, including all intended edits and retirements. The last valid candidate remains unchanged."}
                    trace.record("tool_result", name=call["name"], arguments=call["args"], result=tool_result)
                    messages.append(ToolMessage(json.dumps(tool_result, ensure_ascii=False), tool_call_id=call["id"]))
            if response.invalid_tool_calls:
                for call in response.invalid_tool_calls:
                    messages.append(ToolMessage("Malformed arguments: use valid complete JSON for the intended tool.", tool_call_id=call["id"]))
        raise ValueError(f"{phase} exceeded local call budget")
    finally:
        if agent.trace_sink:
            agent.trace_sink.record({"event_type": "agent_episode",
                "component": f"integration_{phase}", "task_id": payload["task_id"],
                "input_payload": payload, "calls": trace.calls,
                "parsed_response": result.model_dump(mode="json") if result else None})
