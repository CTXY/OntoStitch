"""Four business nodes, two agent roles, one bounded semantic revision loop."""

from __future__ import annotations

import hashlib
import json
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cortex.application.ontology_graph import GraphRejected
from cortex.capabilities.graph_merge.llm.agents import RoleInvocationError
from cortex.capabilities.graph_merge.models import ReconciledRepresentation
from cortex.capabilities.graph_merge.reconciliation.evidence import evidence_complete
from cortex.capabilities.graph_merge.reconciliation.materialization import (
    StableCanonicalIdFactory,
    materialize,
)
from cortex.capabilities.graph_merge.reconciliation.models import (
    ConsolidationProposal,
    preserved_proposal,
)
from cortex.capabilities.graph_merge.reconciliation.validation import (
    review_passes,
    validate_proposal,
    validate_review,
)
from cortex.capabilities.graph_merge.transactions import preview_group
from cortex.capabilities.graph_merge.workflow.context import GroupContext


class GroupState(TypedDict, total=False):
    context: dict
    proposal: dict | None
    review: dict | None
    validation_feedback: str | None
    round: int
    model_calls: int
    proposal_valid: bool
    review_valid: bool
    accepted: bool
    stop_reason: str
    previous_failure: str
    result: dict
    candidate_graph: dict | None


def build_group_workflow(
    consolidator,
    assessor,
    *,
    max_rounds=3,
    max_calls=18,
    checkpointer=None,
    id_factory=None,
):
    id_factory = id_factory or StableCanonicalIdFactory()

    def context(state):
        return GroupContext.model_validate(state["context"])

    def can_continue(state):
        return state.get("model_calls", 0) < max_calls and not state.get("stop_reason")

    def prepare(state):
        failure = ""
        try:
            ctx = context(state)
            ctx.payload()
            if not any(
                evidence_complete((m,), ctx.evidence_by_ref())
                for m in ctx.group.members
            ):
                failure = "evidence_unavailable"
        except ValueError:
            failure = "context_limit"
        return {
            "round": 0,
            "model_calls": 0,
            "accepted": False,
            "proposal": None,
            "review": None,
            "validation_feedback": None,
            "stop_reason": failure,
        }

    def consolidate(state, config):
        ctx = context(state)
        calls = 0
        proposal = state.get("proposal")
        error = None
        try:
            invocation = consolidator.invoke(
                ctx,
                proposal=proposal,
                feedback=state.get("validation_feedback") or state.get("review"),
                remaining_calls=max_calls - state["model_calls"],
                config=config,
            )
            calls = invocation.calls
            proposal = invocation.output.model_dump(mode="json")
            validate_proposal(
                invocation.output,
                ctx.group,
                ctx.schema_definition,
                ctx.nodes,
                ctx.evidence_by_ref(),
            )
            candidate = (
                None
                if invocation.output.identity_mismatch
                else preview_group(
                    ctx, _proposal_result(ctx, invocation.output, id_factory)
                )
            )
            candidate_graph = (
                candidate.model_dump(mode="json") if candidate is not None else None
            )
        except RoleInvocationError as exc:
            calls, error = exc.calls, str(exc)
        except (ValueError, TypeError) as exc:
            error = (
                json.dumps(exc.feedback(), ensure_ascii=False)
                if isinstance(exc, GraphRejected)
                else str(exc)
            )
        return {
            "proposal": proposal,
            "proposal_valid": error is None,
            "round": state["round"] + 1,
            "model_calls": state["model_calls"] + max(1, calls),
            "validation_feedback": error,
            "review": None,
            "review_valid": False,
            "candidate_graph": candidate_graph if error is None else None,
        }

    def after_consolidate(state):
        if state["proposal_valid"]:
            return "assess" if can_continue(state) else "finalize"
        return (
            "consolidate"
            if can_continue(state) and state["round"] < max_rounds
            else "finalize"
        )

    def assess(state, config):
        ctx = context(state)
        if state.get("candidate_graph") is not None:
            from cortex.schema import GraphState

            ctx = ctx.model_copy(
                update={
                    "candidate_state": GraphState.model_validate(
                        state["candidate_graph"]
                    )
                }
            )
        proposal = ConsolidationProposal.model_validate(state["proposal"])
        calls = 0
        review = None
        error = None
        accepted = False
        try:
            invocation = assessor.invoke(
                ctx,
                proposal=state["proposal"],
                feedback=state.get("validation_feedback"),
                remaining_calls=max_calls - state["model_calls"],
                config=config,
            )
            calls = invocation.calls
            review = invocation.output.model_dump(mode="json")
            validate_review(invocation.output, proposal, ctx.evidence_by_ref())
            scope_changes = ctx.relation_scope_changes()
            if set(invocation.output.approved_relation_scope_changes) - set(
                scope_changes
            ):
                raise ValueError(
                    "Relationship scope approvals must name supplied scope-change edge IDs"
                )
            accepted = review_passes(invocation.output, proposal, ctx.evidence_by_ref())
            if accepted and set(
                invocation.output.approved_relation_scope_changes
            ) != set(scope_changes):
                raise ValueError(
                    f"Explicit relationship scope expansion requires separate approval of {list(scope_changes)}; otherwise revise or report unknown"
                )
        except RoleInvocationError as exc:
            calls, error = exc.calls, str(exc)
        except (ValueError, TypeError) as exc:
            error = str(exc)
            accepted = False
        updates = {
            "review": review,
            "review_valid": error is None,
            "accepted": accepted,
            "validation_feedback": error,
            "model_calls": state["model_calls"] + max(1, calls),
        }
        if review is not None and error is None and not accepted:
            failure = hashlib.sha256(
                json.dumps([state["proposal"], review], sort_keys=True).encode()
            ).hexdigest()
            updates.update(previous_failure=failure)
            if state.get("previous_failure") == failure:
                updates["stop_reason"] = "repeated_failed_proposal"
        return updates

    def after_assess(state):
        if state["accepted"] or not can_continue(state):
            return "finalize"
        if not state["review_valid"]:
            return "assess"
        return "consolidate" if state["round"] < max_rounds else "finalize"

    def finalize(state):
        ctx = context(state)
        proposal = (
            ConsolidationProposal.model_validate(state["proposal"])
            if state["accepted"]
            else preserved_proposal(ctx.group)
        )
        by_ref = {m.ref: m for m in ctx.group.members}
        outcomes = tuple(
            ReconciledRepresentation(
                content=c.content, supporting_refs=c.supporting_refs
            )
            for c in proposal.candidates
        ) + tuple(
            ReconciledRepresentation(
                content=by_ref[ref].content, supporting_refs=(ref,)
            )
            for ref in proposal.unresolved_refs
        )
        stop = (
            "accepted"
            if state["accepted"] and not proposal.unresolved_refs
            else "partial_preservation"
            if state["accepted"] and proposal.candidates
            else state.get("stop_reason") or "preserved_unresolved"
        )
        audit = {
            "round": state["round"],
            "model_calls": state["model_calls"],
            "stop_reason": stop,
            "proposal": state.get("proposal"),
            "review": state.get("review"),
            "validation_feedback": state.get("validation_feedback"),
            "identity_mismatch": proposal.identity_mismatch
            if state["accepted"]
            else None,
        }
        result = materialize(
            id_factory,
            ctx.group.existing_node_id or id_factory.new_node_id(ctx.group),
            ctx.group,
            outcomes,
            proposal.difference_points,
            ctx.evidence_by_ref(),
            ["CONSOLIDATION_PRESERVED_UNRESOLVED"] if proposal.unresolved_refs else [],
        )
        conflict = result.conflict
        if conflict is not None:
            conflict = conflict.model_copy(
                update={
                    "consolidation_status": "unresolved"
                    if proposal.unresolved_refs
                    else "semantic_conflict",
                    "preservation_reasons": (stop,) if proposal.unresolved_refs else (),
                }
            )
        return {
            "stop_reason": stop,
            "result": {
                "node": (
                    result.node.with_conflict(conflict)
                    if conflict
                    else result.node.without_conflict()
                ).model_dump(mode="json"),
                "diagnostic_codes": list(result.diagnostic_codes),
                "consolidation": audit,
            },
        }

    graph = StateGraph(GroupState)
    for name, node in (
        ("prepare", prepare),
        ("consolidate", consolidate),
        ("assess", assess),
        ("finalize", finalize),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "prepare")
    graph.add_conditional_edges(
        "prepare", lambda state: "finalize" if state["stop_reason"] else "consolidate"
    )
    graph.add_conditional_edges("consolidate", after_consolidate)
    graph.add_conditional_edges("assess", after_assess)
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer, name="merge_group")


def _proposal_result(ctx, proposal, id_factory):
    by_ref = {m.ref: m for m in ctx.group.members}
    outcomes = tuple(
        ReconciledRepresentation(content=c.content, supporting_refs=c.supporting_refs)
        for c in proposal.candidates
    ) + tuple(
        ReconciledRepresentation(content=by_ref[ref].content, supporting_refs=(ref,))
        for ref in proposal.unresolved_refs
    )
    return materialize(
        id_factory,
        ctx.group.existing_node_id or id_factory.new_node_id(ctx.group),
        ctx.group,
        outcomes,
        proposal.difference_points,
        ctx.evidence_by_ref(),
        (),
    )
