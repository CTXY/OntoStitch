"""Production relation-only agent: compact view, selective reads, partial edits."""
import json
from cortex.capabilities.graph_merge.integration.edit_feedback import validation_feedback
from cortex.capabilities.graph_merge.integration.proposal_repair import ProposalRepair, Correction
from importlib.resources import files
from itertools import count
from typing import Annotated, Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import model_validator

from cortex.capabilities.graph_merge.integration.models import Model, ReviewReference
from cortex.capabilities.graph_merge.integration.relation_edits import (
    RelationPatch,
    submit_patch,
)
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.packets import action_message
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery


class RelationReview(Model):
    verdict: Literal["accept", "revise"]
    issues: tuple[str, ...] = ()
    issue_refs: tuple[ReviewReference, ...] = ()

    @model_validator(mode="after")
    def coherent(self):
        if self.verdict == "accept" and (self.issues or self.issue_refs):
            raise ValueError("accept requires empty issues and issue_refs")
        if self.verdict == "revise" and (not self.issues or not self.issue_refs):
            raise ValueError("revise requires concrete issues and nonempty issue_refs identifying the affected node, edge or contract; fields are optional for structural issues")
        return self


def invoke_relation(agent, phase, context, payload):
    payload = dict(payload)
    if "evidence" in payload:
        payload["evidence"] = payload.pop("evidence")
    writer = phase == "reconcile"
    prompt_root = files("cortex.capabilities.graph_merge.llm.prompts")
    system = prompt_root.joinpath("incremental_relation_shared.md").read_text()
    system += "\n\n" + prompt_root.joinpath(
        "incremental_relation_write.md" if writer else "incremental_relation_assess.md"
    ).read_text()
    system += "\n\n" + prompt_root.joinpath("incremental_protocol.md").read_text()
    messages = [SystemMessage(system), HumanMessage(json.dumps(payload, ensure_ascii=False))]
    raw_tools = {t.name: t for t in build_tools(context)}
    from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
    from cortex.capabilities.graph_merge.integration.proposal_scope import ProposalScope, scope_error
    from cortex.capabilities.graph_merge.llm.relation_reads import RelationReads
    evidence = EvidencePresentation(context, payload.get("evidence", []))
    scope = ProposalScope(context, payload, evidence) if writer else None
    if writer:
        context.active_batch.prepare_submission = scope.prepare
    reader = RelationReads(context, evidence, scope)
    finished = None
    proposal_repair = ProposalRepair()
    guidance_read = {}
    seen_reads = set()
    trace = EpisodeTrace(agent.trace_sink, phase, budget=agent.call_budget)
    # Model call traces use the same file layout as the established runner.
    if hasattr(agent.trace_sink, "path"):
        from cortex.infrastructure.artifacts.graph_merge_trace import (
            FileSystemGraphMergeTraceSink,
        )
        trace.sink = FileSystemGraphMergeTraceSink(agent.trace_sink.path.with_name("model_calls.jsonl"))

    @tool
    def inspect_graph(mode: Literal["items", "relations", "consumers", "contracts"], ids: list[str],
                      fields: Annotated[list[str] | None,
                          "Exact stored paths, e.g. ['properties.targets'], ['properties.definition'], "
                          "['properties.relation_metadata'], or ['name', 'description']. "
                          "Bare 'targets' and whole 'properties' are not selectors. "
                          "Use ['content'] or ['alternatives'] only for necessary reconstruction."] = None, view: Literal["candidate", "input"] = "candidate",
                      offset: int = 0, limit: int = 30,
                      direction: Literal["incoming", "outgoing", "both"] = "both",
                      edge_types: list[str] | None = None) -> dict:
        """Read missing graph context. items selects exact objects/real field paths; relations selects adjacent edges; consumers selects declared references; contracts selects predicate IDs. Omitted fields use a light view. Read content/alternatives only for necessary reconstruction. Reads do not grant unrestricted write access."""
        return reader.read(mode, ids, fields, view, offset, limit, direction, edge_types)

    @tool
    def inspect_items(ids: list[str], fields: list[str]) -> dict:
        """Legacy replay adapter; new conversations use inspect_graph."""
        return reader.read("items", ids, fields)

    @tool
    def read_evidence(ids: list[str]) -> dict:
        """Read missing evidence bodies in one batch; already supplied bodies are not repeated."""
        return evidence.read(ids)

    @tool
    def read_guidance(topic: Literal["concept", "metric", "generation_rule", "event", "operations"]) -> dict:
        """Read required creation/formula/intent details only for a necessary node reconstruction."""
        if topic in guidance_read:
            return guidance_read[topic]
        result = raw_tools["read_guidance"].invoke({"topic": topic})
        guidance_read[topic] = result
        return result

    def apply_proposal(arguments):
        nonlocal finished
        raw = proposal_repair.prepare(arguments)
        patch = RelationPatch.model_validate(raw)
        previous = context.active_batch.preview or context.active_batch.seed
        finished = submit_patch(context, patch)
        from cortex.capabilities.graph_merge.integration.edit_feedback import changed_fields
        changes = changed_fields(previous, context.active_batch.preview)
        return {"status": "validated", "proposal_applied": "candidate_only", "committed": False,
                "assessment_pending": True, "changes": changes,
                "created_ids": [c["id"] for c in changes if c.get("operation") == "created"]}

    @tool
    def submit_changes(patch: RelationPatch | None = None, corrections: list[Correction] | None = None) -> dict:
        """Final output: supply exactly one of patch or corrections. First use a complete patch. After rejection, corrections edit the saved, UNAPPLIED patch using JSON Pointer (add/replace/remove/move), so do not resend valid parts. Example: corrections=[{op:'move',from_path:'/new_edges',path:'/create_edges'}]. New edges use create_edges with id/evidence_ids; existing edge support uses edge_updates with id/add_evidence. Nothing from a rejected patch is created in the graph. A fully valid result ends the Writer and awaits Assessment; no extra finish tool."""
        return apply_proposal({"patch": patch.model_dump(exclude_unset=True, mode="json") if patch else None,
                               "corrections": [c.model_dump(exclude_unset=True) for c in corrections] if corrections else None})

    tools = [inspect_graph, read_evidence, read_guidance]
    if writer:
        tools += [submit_changes]
    else:
        tools += [RelationReview]
    lookup = {t.name: t for t in tools if hasattr(t, "name")}
    lookup["inspect_items"] = inspect_items  # Old stored tool calls only; not advertised.
    result = None
    plain_fallback = False
    try:
        for step in (count() if agent.max_calls is None else range(agent.max_calls)):
            final_call = agent.max_calls is not None and step == agent.max_calls - 1
            available = ([submit_changes] if writer else [RelationReview]) if final_call else tools
            if plain_fallback:
                available = [] if final_call else [inspect_graph, read_evidence, read_guidance]
            if final_call:
                messages.append(HumanMessage(
                    "This is the reserved completion call; no further reads. Submit the complete supported result from the supplied view and completed reads. "
                    + ("Use failure if a necessary safe decision remains impossible; do not hide unresolved required repairs in an empty success." if writer
                       else "Return the bounded assessment of actual changes, with concrete defects only, not speculative missing facts.")))
            from cortex.capabilities.graph_merge.llm.input_budget import check_request
            check_request(messages, available)
            response = invoke_with_recovery(
                agent, available, messages, config={"callbacks": [trace]},
                tool_choice="auto" if plain_fallback else "required")
            messages.append(action_message(response))
            fallback_notice = None
            if response.invalid_tool_calls:
                # Every provider tool ID needs a response before another request,
                # even when all calls failed JSON parsing. Do not execute a final
                # Patch bundled with malformed companion calls.
                for call in response.invalid_tool_calls:
                    messages.append(ToolMessage(
                        "Malformed arguments; no changes from this call were applied. Return the complete result as valid plain JSON.",
                        tool_call_id=call["id"]))
                if not plain_fallback:
                    schema = RelationPatch if writer else RelationReview
                    fallback_notice = HumanMessage(
                        "Transport fallback: return the final result as plain JSON, no Markdown fences, arguments wrapper or patch wrapper. "
                        "Do not call the result tool again. Result schema: "
                        + json.dumps(schema.model_json_schema(), ensure_ascii=False))
                    plain_fallback = True
                if not response.tool_calls:
                    if fallback_notice:
                        messages.append(fallback_notice)
                    continue
            if not response.tool_calls:
                try:
                    if writer:
                        raw = json.loads(response.content)
                        apply_proposal(raw if "corrections" in raw else {"patch": raw})
                        result = finished
                    else:
                        result = RelationReview.model_validate_json(response.content)
                    return result
                except (ValueError, TypeError) as exc:
                    messages.append(HumanMessage(json.dumps({"error": validation_feedback(exc), **scope_error(exc), **(proposal_repair.feedback(exc, context) if writer else {"repair": "Return the corrected Review."})}, ensure_ascii=False)))
                    continue
            for call in response.tool_calls:
                try:
                    if final_call and call["name"] != ("submit_changes" if writer else "RelationReview"):
                        raise ValueError("Reserved completion call does not permit additional reads")
                    if call["name"] in {"submit_changes", "RelationReview"} and (len(response.tool_calls) != 1 or response.invalid_tool_calls):
                        raise ValueError("Read first; submit alone using the read results")
                    if call["name"] == "RelationReview":
                        result = RelationReview.model_validate(call["args"])
                        tool_result = {"status": "accepted"}
                    else:
                        key = (call["name"], json.dumps(call["args"], sort_keys=True))
                        if call["name"] == "submit_changes":
                            tool_result = apply_proposal(call["args"])
                        elif call["name"] in {"inspect_graph", "inspect_items", "read_evidence", "read_guidance"} and key in seen_reads:
                            tool_result = {"already_supplied": True, "guidance": "Use the earlier result; other missing objects or fields remain readable."}
                        else:
                            tool_result = lookup[call["name"]].invoke(call["args"])
                            seen_reads.add(key)
                    trace.record("tool_result", name=call["name"],
                                 arguments=call["args"], result=tool_result)
                    messages.append(ToolMessage(json.dumps(tool_result, ensure_ascii=False), tool_call_id=call["id"]))
                    if finished is not None:
                        result = finished
                        return result
                    if result is not None:
                        return result
                except (ValueError, TypeError, KeyError) as exc:
                    tool_result = {"status": "rejected", "error": validation_feedback(exc),
                                   **scope_error(exc), **(proposal_repair.feedback(exc, context) if call["name"] == "submit_changes" else {"repair": "Correct the reported read/Review arguments; the graph is unchanged."})}
                    trace.record("tool_result", name=call["name"], arguments=call["args"], result=tool_result)
                    messages.append(ToolMessage(json.dumps(tool_result, ensure_ascii=False), tool_call_id=call["id"]))
            if fallback_notice:
                messages.append(fallback_notice)
        raise ValueError(f"{phase} exceeded local call budget")
    finally:
        if agent.trace_sink:
            agent.trace_sink.record({"event_type": "agent_episode",
                "component": f"integration_{phase}", "task_id": payload["task_id"],
                "input_payload": payload, "calls": trace.calls,
                "parsed_response": result.model_dump(mode="json") if result else None})
