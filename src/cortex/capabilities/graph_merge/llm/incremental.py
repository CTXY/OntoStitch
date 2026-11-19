"""Bounded tool loop: one patch can submit and finish in the same response."""

import json
from cortex.capabilities.graph_merge.integration.edit_feedback import validation_feedback
from importlib.resources import files
from itertools import count
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai.chat_models.base import OpenAIAPIError, OpenAIConnectionError
from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

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
    # Read old checkpoints, but do not request obsolete source-truth audits.
    rationale_checks: SkipJsonSchema[tuple[RationaleCheck, ...]] = ()
    decision_checks: SkipJsonSchema[tuple[RuleDecisionCheck, ...]] = ()

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
    def __init__(self, llm, *, max_calls=12, trace_sink=None, request_max_characters=210000):
        self.llm, self.max_calls, self.trace_sink = llm, max_calls, trace_sink
        self.call_budget = None
        if request_max_characters < 1:
            raise ValueError("request_max_characters must be positive")
        self.request_max_characters = request_max_characters

    def invoke(self, phase, context, payload):
        if phase in {"reconcile", "graph_assess"}:
            from cortex.capabilities.graph_merge.llm.reconciliation import (
                invoke_relation,
            )

            return invoke_relation(self, phase, context, payload)
        if (phase in {"consolidate", "node_assess"}
                and payload.get("provisional_identity_projection") is False
                and not payload.get("identity_aliases")
                and any(g.get("current_owner") for g in payload.get("matched_groups", []))):
            # An empty *new* alias map does not mean archived members still exist.
            # Each group's explicit current_owner describes the supplied graph.
            payload = dict(payload)
            payload.pop("provisional_identity_projection")
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

        from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
        from cortex.capabilities.graph_merge.integration.proposal_scope import ProposalScope, scope_error
        evidence = EvidencePresentation(context, payload.get("evidence", []))
        scope = None
        if phase == "consolidate" and getattr(context, "active_batch", None):
            scope = ProposalScope(context, payload, evidence)
            context.active_batch.prepare_submission = scope.prepare

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
            """Read only missing context needed by a specific content decision.

            Omitted IDs default to current task roots. next_offset offers another page;
            it is not a request to enumerate all adjacent relations. For an input-view
            comparison, name the relevant archived member IDs explicitly.

            nodes: ids read exact graph objects. table is a PHYSICAL SQL table name/ID,
            never a node type such as concept; column_names selects its exact columns.
            Use table/column_names without ids. To locate a missing semantic operand,
            read its physical column, then relations with that column ID and
            edge_types=['grounded_on'] to find existing semantic bindings.
            relations: ids selects adjacent edges; edge_types filters predicates before
            pagination; omitted, null or [] means no predicate filter. consumers reads reverse content dependencies. contracts reads
            predicate definitions. candidates is identity retrieval, not a name search.
            """
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
                "edge_types": edge_types or None,
            }
            if phase == "node_assess" and mode == "candidates":
                return {"error": "Identity matching is already completed. Complete matched_groups and source members are supplied; assess this candidate. A concrete identity counterexample uses revisit_identity, not a new fuzzy matching pass."}
            result = raw_tools["inspect_graph"].invoke(args)
            if phase in {"node_assess", "consolidate"} and isinstance(result.get("items"), list):
                kept, used = [], 0
                for row in result["items"]:
                    size = len(json.dumps(row, ensure_ascii=False))
                    if kept and used + size > 8000:
                        break
                    kept.append(row)
                    used += size
                if len(kept) < len(result["items"]):
                    result = {**result, "items": kept, "next_offset": offset + len(kept)}
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
            from cortex.capabilities.graph_merge.llm.packets import (
                reference_supplied_records,
                supplied_records,
            )
            if phase in {"consolidate", "node_assess"}:
                from cortex.capabilities.graph_merge.integration.evidence_refs import compact_attribution
                result = compact_attribution(result, context)
            if scope:
                scope.observe(result.get("items", []), view)
            return reference_supplied_records(result, supplied_records(payload))

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
            """Read a missing topic or field schema. Reuse supplied guidance; return field definitions if that part was not supplied."""
            if topic == "conflicts":
                return {
                    "guidance": "Use BatchResult.conflicts with node_id, alternatives and conflict_points. Each alternative is COMPLETE content (name, description, aliases, tags, full properties) plus alternative_id and evidence_ids (existing alternative IDs retain exact sources). Code compacts shared fields losslessly. Do not write overrides, common placeholders, set_conflict edits or independent competing copies. Replace obsolete copies and repair consumers in the same batch."
                }
            guide = payload.get("node_guidance", {})
            if topic in guide or topic in guide.get("types", ()) or topic in guide.get("fields_by_type", {}):
                fields_supplied = topic in guide.get("fields_by_type", {}) or (
                    isinstance(guide.get(topic), dict) and "fields" in guide[topic]
                )
                if topic in {"concept", "metric", "generation_rule", "event"} and not fields_supplied:
                    return {
                        "guidance_already_supplied": True,
                        "location": "node_guidance",
                        "fields": {key: value.model_dump(mode="json") for key, value in context.schema.effective_node_fields(topic).items()},
                    }
                return {
                    "already_supplied": True,
                    "location": "node_guidance",
                    "guidance": "Use the guidance and field definitions already supplied; identical content is not repeated.",
                }
            if phase in {"consolidate", "node_assess"} and topic in {"concept", "metric", "generation_rule", "event"}:
                return {"guidance": files("cortex.capabilities.graph_merge.llm.prompts").joinpath("incremental_content_shared.md").read_text(),
                        "fields": {key: value.model_dump(mode="json") for key, value in context.schema.effective_node_fields(topic).items()}}
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

        @tool
        def read_evidence(ids: list[str]) -> dict:
            """Read missing evidence only; already supplied evidence stays in the conversation."""
            return evidence.read(ids)

        read_tools = [inspect_graph, read_evidence, read_guidance]
        if phase == "consolidate":
            read_tools.append(inspect_pending)
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
        if phase == "consolidate":
            system += (
                "\n\n"
                + files("cortex.capabilities.graph_merge.llm.prompts")
                .joinpath("incremental_writes.md")
                .read_text()
            )
        from cortex.capabilities.graph_merge.llm.packets import action_message, compact_node_packet

        system += "\n\n" + files("cortex.capabilities.graph_merge.llm.prompts").joinpath(
            "incremental_protocol.md"
        ).read_text()

        wire_payload = compact_node_packet(payload) if phase in {"consolidate", "node_assess"} else payload
        trace.record("packet_size", original_characters=len(json.dumps(payload)),
                     supplied_characters=len(json.dumps(wire_payload)))
        messages = [
            SystemMessage(system),
            HumanMessage(
                json.dumps(wire_payload, ensure_ascii=False, separators=(",", ":"))
            ),
        ]
        finished = None
        plain_fallback = False
        seen_reads = set()
        read_names = {
            "inspect_graph",
            "read_evidence",
            "read_guidance",
            "inspect_pending",
        }
        connection_retries = 0
        try:
            for _ in (count() if self.max_calls is None else range(self.max_calls)):
                batch = getattr(context, "active_batch", None)
                has_pending = batch is not None and any(
                    (batch.pending, batch.changes, batch.contracts, batch.edge_merges)
                )
                available_reads = [
                    t for t in read_tools if t.name != "inspect_pending" or has_pending
                ]
                allowed = list(available_reads)
                if not plain_fallback:
                    allowed.append(output)
                try:
                    from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery

                    if phase in {"node_assess", "consolidate"}:
                        from cortex.capabilities.graph_merge.llm.input_budget import check_request
                        check_request(messages, allowed, max_characters=self.request_max_characters)
                    response = invoke_with_recovery(
                        self, allowed, messages, config={"callbacks": [trace]},
                        tool_choice="auto" if plain_fallback else "required",
                    )
                except (OpenAIConnectionError, OpenAIAPIError) as exc:
                    status = getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    transient = isinstance(exc, OpenAIConnectionError) or status in {
                        502,
                        503,
                        504,
                    }
                    if not transient or connection_retries >= 1:
                        raise
                    # No response/tool execution occurred. Retry the exact
                    # conversation once, within both call limits. Callbacks
                    # record each failed/successful attempt separately.
                    connection_retries += 1
                    continue
                messages.append(action_message(response))
                calls = response.tool_calls
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
                                json.dumps({"error": validation_feedback(exc), **scope_error(exc), "repair": f"Return the corrected local {output.__name__}; previously valid pending work remains."}, ensure_ascii=False)
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
                            result = {"status": "validated", "committed": False}
                        elif call["name"] in lookup:
                            revision = getattr(batch, "revision", 0)
                            read_key = (call["name"], json.dumps(call["args"], sort_keys=True), revision)
                            if call["name"] in read_names and read_key in seen_reads:
                                result = {"already_read": True, "guidance": "Use the earlier result of this exact request at the same draft revision. Request different missing objects/fields if needed."}
                            else:
                                result = lookup[call["name"]].invoke(call["args"])
                                if call["name"] in read_names:
                                    seen_reads.add(read_key)
                        else:
                            raise ValueError("Unknown tool")
                    except (ValueError, TypeError) as exc:
                        result = {
                            "status": "rejected",
                            "error": validation_feedback(exc),
                            **scope_error(exc),
                            "repair": ("Correct the structured Review, not the graph. Report concrete preservation/integration defects with issue_refs and actual field paths; accept has no unresolved issues. No per-Rule source audit is required." if phase == "node_assess" else "Replace faulty objects; previously saved pending objects remain. Use remove_edits/remove_changes/remove_edge_merges to withdraw them."),
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
            if (not set(found_decisions) <= expected_decisions or len(found_decisions) != len(set(found_decisions))
                or (value.verdict == "accept" and set(found_decisions) != expected_decisions)):
                raise ValueError(
                    "Check each supplied Rule decision once in decision_checks (not rationale_checks). "
                    f"Missing={sorted(expected_decisions-set(found_decisions))}; unexpected={sorted(set(found_decisions)-expected_decisions)}. "
                    "Each item requires node_id, independent_decisions and issues. Remove duplicate node_ids."
                )
            expected = set(payload.get("rationale_review_ids", ()))
            found = [c.node_id for c in value.rationale_checks]
            if (not set(found) <= expected or len(found) != len(set(found))
                or (value.verdict == "accept" and set(found) != expected)):
                raise ValueError(
                    "Check each supplied Rule reason once in rationale_checks (not decision_checks). "
                    f"Missing={sorted(expected-set(found))}; unexpected={sorted(set(found)-expected)}. "
                    "Each item requires node_id, supported, explanation and unsupported_claims. Keep the decision_checks separately; remove duplicate node_ids."
                )
            if value.verdict != "accept" and not value.issue_refs:
                raise ValueError("A rejecting review must identify concrete affected objects in issue_refs")
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
        if phase == "node_assess":
            return  # A review is never submitted to the graph-edit validator.
        validator = getattr(context, "output_validator", None)
        if validator:
            validator(value)
