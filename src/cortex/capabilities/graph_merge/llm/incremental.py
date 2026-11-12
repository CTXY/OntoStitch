"""Bounded tool loop: one patch can submit and finish in the same response."""

import json
from importlib.resources import files
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import Field, model_validator

from cortex.capabilities.graph_merge.integration.batch import BatchResult
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    Model,
    RationaleCheck,
    ReviewReference,
)
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace


class RuleDecisionCheck(Model):
    node_id: str
    independent_decisions: tuple[str, ...] = Field(min_length=1)
    issues: tuple[str, ...] = ()


class NodeReview(Model):
    verdict: str = Field(pattern="^(accept|revise|revisit_identity)$")
    issues: tuple[str, ...] = ()
    issue_refs: tuple[ReviewReference, ...] = ()
    rationale_checks: tuple[RationaleCheck, ...] = ()
    decision_checks: tuple[RuleDecisionCheck, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.verdict != "accept" and not self.issues:
            raise ValueError("A revision needs concrete issues and affected issue_refs")
        if self.verdict == "accept" and (
            self.issues
            or self.issue_refs
            or any(not r.supported for r in self.rationale_checks)
            or any(
                len(c.independent_decisions) != 1 or c.issues
                for c in self.decision_checks
            )
        ):
            raise ValueError("Acceptance cannot contain unresolved issues")
        return self


class IncrementalIntegrationAgent:
    def __init__(self, llm, *, max_calls=12, trace_sink=None):
        self.llm, self.max_calls, self.trace_sink = llm, max_calls, trace_sink
        self.call_budget = None

    def invoke(self, phase, context, payload):
        output = (
            IdentityDecision
            if phase == "identity"
            else NodeReview
            if phase == "node_assess"
            else BatchResult
        )
        from cortex.infrastructure.artifacts.graph_merge_trace import (
            FileSystemGraphMergeTraceSink,
        )

        call_sink = (
            FileSystemGraphMergeTraceSink(
                self.trace_sink.path.with_name("model_calls.jsonl")
            )
            if hasattr(self.trace_sink, "path")
            else self.trace_sink
        )
        trace = EpisodeTrace(call_sink, phase, budget=self.call_budget)
        raw_tools = {t.name: t for t in build_tools(context)}
        roots = payload.get("task_scope", {}).get(
            "current_targets", payload.get("incoming_seeds", ())
        )
        if context.candidate is None:
            context.candidate = context.union_state()

        @tool
        def inspect_graph(
            mode: Literal["nodes", "relations", "consumers", "candidates", "contracts"],
            ids: list[str] | None = None,
            view: Literal["input", "candidate"] = "candidate",
            offset: int = 0,
            limit: int = 30,
            table: str | None = None,
            column_names: list[str] | None = None,
            edge_types: list[str] | None = None,
        ) -> dict:
            """Read complete nodes/relations/consumers/candidates/contracts in the stated view. Default scope is this task, never earlier read history. Paginate next_offset."""
            selection = ids
            if selection is None and table is None:
                selection = (
                    list(roots)
                    if mode != "contracts"
                    else sorted(
                        {e["edge_type"] for e in payload.get("candidate_relations", [])}
                    )
                )
            args = {
                "mode": mode,
                "ids": selection,
                "view": view,
                "offset": offset,
                "limit": limit,
                "table": table,
                "column_names": column_names,
                "edge_types": edge_types,
            }
            result = raw_tools["inspect_graph"].invoke(args)
            if mode == "relations" and selection:
                index = (
                    context.nodes
                    if view == "input"
                    else {n.id: n for n in context.candidate.graph.nodes}
                )
                edges = (
                    context.edges
                    if view == "input"
                    else {e.id: e for e in context.candidate.graph.edges}
                )
                result["missing_ids"] = sorted(
                    set(selection) - index.keys() - edges.keys()
                )
            result.update(
                view=view,
                revision=getattr(context, "workspace_revision", 0),
                complete=result.get("next_offset") is None,
            )
            return result

        @tool
        def extend_task(ids: list[str], reason: str) -> dict:
            """Request necessary current objects for THIS atomic change. Name the required semantic change. Independent issues belong to later tasks."""
            expand = getattr(context, "extend_task", None)
            if expand is None:
                return {"error": "This read-only phase cannot extend write scope"}
            return expand(ids, reason)

        @tool
        def read_guidance(
            topic: Literal[
                "concept",
                "metric",
                "event",
                "generation_rule",
                "relations",
                "conflicts",
                "operations",
            ],
        ) -> dict:
            """Read only a missing contract/topic. Preloaded node_guidance already contains the corresponding full fields and requirements."""
            if topic == "conflicts":
                return {
                    "guidance": "Use BatchResult.conflicts with node_id, alternatives and conflict_points. Each alternative is COMPLETE content (name, description, aliases, tags, full properties) plus alternative_id and its actual sources. Code compacts shared fields losslessly. Do not write overrides, common placeholders, set_conflict edits or independent competing copies. Replace obsolete copies and repair consumers in the same batch."
                }
            if topic in payload.get("node_guidance", {}):
                return {
                    "already_supplied": True,
                    "location": f"node_guidance.{topic}",
                    "guidance": "Use the full guidance and fields already in the task packet; identical content is not repeated.",
                }
            return raw_tools["read_guidance"].invoke({"topic": topic})

        @tool
        def inspect_pending(ids: list[str] | None = None) -> dict:
            """Read saved pending objects/changes by ID, including invalid objects absent from candidate. No IDs returns a small directory; pending is not a validated graph."""
            batch = getattr(context, "active_batch", None)
            if batch is None:
                return {"error": "No active batch"}
            from cortex.capabilities.graph_merge.integration.submission import edit_key

            result = {
                "revision": batch.revision,
                "view": "pending",
                "candidate_available": batch.preview is not None,
            }
            if not ids:
                result["edits"] = [
                    {
                        "kind": edit_key(e)[0],
                        "id": edit_key(e)[1],
                        "operation": e["operation"],
                    }
                    for e in batch.pending.values()
                ]
                result["relation_contract_ids"] = list(batch.contracts)
            else:
                result["edits"] = [
                    e for e in batch.pending.values() if edit_key(e)[1] in ids
                ]
                result["changes"] = [
                    c
                    for c in batch.changes
                    if any(r["id"] in ids for r in c["before"] + c["after"])
                ]
                result["relation_contracts"] = [
                    c.model_dump(mode="json")
                    for k, c in batch.contracts.items()
                    if k in ids
                ]
                result["edge_merges"] = [
                    m.model_dump(mode="json")
                    for m in batch.edge_merges.values()
                    if m.survivor_id in ids or set(m.source_ids) & set(ids)
                ]
            return result

        read_tools = [inspect_graph, raw_tools["read_evidence"], read_guidance]
        if phase in {"consolidate", "reconcile"}:
            read_tools.extend([extend_task, inspect_pending])
        lookup = {t.name: t for t in read_tools}
        prompt_name = (
            "integration_identity.md"
            if phase == "identity"
            else f"incremental_{phase}.md"
        )
        system = (
            files("cortex.capabilities.graph_merge.llm.prompts")
            .joinpath(prompt_name)
            .read_text()
        )
        if phase in {"consolidate", "reconcile"}:
            system += (
                "\n\n"
                + files("cortex.capabilities.graph_merge.llm.prompts")
                .joinpath("incremental_writes.md")
                .read_text()
            )
        messages = [
            SystemMessage(system),
            HumanMessage(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            ),
        ]
        finished = None
        plain_fallback = False
        read_rounds = 0
        read_limit = 3 if phase == "node_assess" else 4
        read_names = {
            "inspect_graph",
            "read_evidence",
            "read_guidance",
            "inspect_pending",
        }
        budget_announced = False
        try:
            for _ in range(self.max_calls):
                batch = getattr(context, "active_batch", None)
                has_pending = batch is not None and any(
                    (batch.pending, batch.changes, batch.contracts, batch.edge_merges)
                )
                available_reads = [
                    t for t in read_tools if t.name != "inspect_pending" or has_pending
                ]
                if read_rounds >= read_limit:
                    if not budget_announced:
                        messages.append(
                            HumanMessage(
                                "Read budget reached. Use the supplied definitions/evidence and saved results to submit the supported local result now. Do not repeat exploratory reads. If essential evidence is unavailable, report failure rather than inventing it."
                            )
                        )
                        budget_announced = True
                    allowed = (
                        [extend_task] if phase in {"consolidate", "reconcile"} else []
                    )
                    if not plain_fallback:
                        allowed.append(output)
                else:
                    allowed = list(available_reads)
                    if not plain_fallback:
                        allowed.append(output)
                bound = (
                    self.llm.bind_tools(allowed, tool_choice="auto")
                    if allowed
                    else self.llm
                )
                response = bound.invoke(messages, config={"callbacks": [trace]})
                messages.append(response)
                calls = response.tool_calls
                reads_allowed = read_rounds < read_limit
                if any(call["name"] in read_names for call in calls):
                    read_rounds += 1
                fallback_notice = None
                if response.invalid_tool_calls:
                    for invalid in response.invalid_tool_calls:
                        messages.append(
                            ToolMessage(
                                "Malformed tool JSON. No edits were applied. Finish with a valid plain JSON object in assistant content matching the result schema; do not wrap it in arguments or probe the output tool.",
                                tool_call_id=invalid["id"],
                            )
                        )
                    # If valid calls accompany malformed ones, respond to every
                    # tool ID below before sending another provider request.
                    if not plain_fallback:
                        bound = self.llm.bind_tools(read_tools, tool_choice="auto")
                        fallback_notice = HumanMessage(
                            "Transport fallback: return your final result as plain JSON, no Markdown fences or arguments wrapper. Result schema: "
                            + json.dumps(output.model_json_schema(), ensure_ascii=False)
                        )
                        plain_fallback = True
                    if not calls:
                        if fallback_notice:
                            messages.append(fallback_notice)
                        continue
                if not calls:
                    try:
                        value = output.model_validate(json.loads(response.content))
                        self._validate(value, context, payload, phase)
                        finished = value
                        return value
                    except (ValueError, TypeError) as exc:
                        messages.append(
                            HumanMessage(
                                f"Output rejected: {exc}. Submit only replacements for the affected objects; pending objects remain saved. Use {output.__name__} or valid complete JSON."
                            )
                        )
                    continue
                completed = None
                for call in calls:
                    try:
                        if call["name"] == output.__name__:
                            if len(calls) != 1:
                                raise ValueError(
                                    "Read first; submit completion alone after using the read results"
                                )
                            value = output.model_validate(call["args"])
                            self._validate(value, context, payload, phase)
                            completed = value
                            result = {"status": "accepted"}
                        elif call["name"] in lookup:
                            if call["name"] in read_names and not reads_allowed:
                                raise ValueError(
                                    "Read budget exhausted; submit a supported result or explicit failure"
                                )
                            result = lookup[call["name"]].invoke(call["args"])
                        else:
                            raise ValueError("Unknown tool")
                    except (ValueError, TypeError) as exc:
                        result = {
                            "status": "rejected",
                            "error": str(exc),
                            "repair": "Replace faulty objects; previously saved pending objects remain. Use remove_edits/remove_changes/remove_edge_merges to withdraw them.",
                        }
                    if self.trace_sink:
                        self.trace_sink.record(
                            {
                                "event_type": "tool_result",
                                "component": f"integration_{phase}",
                                "name": call["name"],
                                "arguments": call["args"],
                                "result": result,
                            }
                        )
                    messages.append(
                        ToolMessage(
                            json.dumps(result, ensure_ascii=False, default=str),
                            tool_call_id=call["id"],
                        )
                    )
                if fallback_notice:
                    messages.append(fallback_notice)
                if completed is not None:
                    finished = completed
                    return completed
            raise RuntimeError(
                f"{phase} exhausted {self.max_calls} model turns; workspace not published"
            )
        finally:
            if self.trace_sink:
                self.trace_sink.record(
                    {
                        "event_type": "agent_episode",
                        "component": f"integration_{phase}",
                        "task_id": payload.get("task_id"),
                        "calls": trace.calls,
                        "input_payload": payload,
                        "parsed_response": finished.model_dump(mode="json")
                        if finished
                        else None,
                    }
                )

    @staticmethod
    def _validate(value, context, payload, phase):
        if phase == "node_assess":
            expected_decisions = set(payload.get("decision_review_ids", ()))
            found_decisions = [c.node_id for c in value.decision_checks]
            if set(found_decisions) != expected_decisions or len(
                found_decisions
            ) != len(set(found_decisions)):
                raise ValueError(
                    f"Check each supplied Rule decision once: {sorted(expected_decisions)}"
                )
            expected = set(payload.get("rationale_review_ids", ()))
            found = [c.node_id for c in value.rationale_checks]
            if set(found) != expected or len(found) != len(set(found)):
                raise ValueError(
                    f"Check each supplied Rule reason once: {sorted(expected)}"
                )
            from cortex.schema.graph import alternative_content

            current_nodes = (
                {n.id: n for n in context.candidate.graph.nodes}
                if context.candidate is not None
                else {}
            )
            for check in value.rationale_checks:
                if check.supported:
                    continue
                if check.node_id not in current_nodes:
                    raise ValueError(f"Missing current reason target: {check.node_id}")
                node = current_nodes[check.node_id]
                contents = [node] + [
                    alternative_content(node, a) for a in node.alternatives
                ]
                reasons = [str(c.properties.get("reason", "")) for c in contents]
                normalized = [" ".join(text.split()) for text in reasons]
                if not check.unsupported_claims or any(
                    not quote.strip()
                    or not any(" ".join(quote.split()) in text for text in normalized)
                    for quote in check.unsupported_claims
                ):
                    raise ValueError(
                        f"Unsupported claims must quote actual CURRENT reason text for {check.node_id}, "
                        f"not an archived predecessor. Current reason texts: {reasons}"
                    )
        if getattr(value, "failure", None):
            from cortex.capabilities.graph_merge.integration.engine import (
                IntegrationIncomplete,
            )

            raise IntegrationIncomplete(value.failure)
        validator = getattr(context, "output_validator", None)
        if validator:
            validator(value)
