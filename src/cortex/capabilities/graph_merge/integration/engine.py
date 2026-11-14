"""Checkpointed five-stage orchestration. Only a reviewed whole patch is committed."""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.integration.compiler import (
    commit_result,
    compile_proposal,
    identity_map,
    mapped_base,
)
from cortex.capabilities.graph_merge.integration.context import (
    PHYSICAL,
    IntegrationContext,
)
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.integration.revision import (
    compose_revision,
    graph_delta,
)
from cortex.capabilities.graph_merge.integration.scope import focused_revision
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.infrastructure.progress import emit_progress
from cortex.schema import GraphSpace, GraphState, SchemaDefinition


class IntegrationIncomplete(RuntimeError):
    """No graph or mappings are published after incomplete/failed review."""


class RunState(TypedDict, total=False):
    request: dict
    evidence: list[dict]
    identity: dict
    proposal: dict
    review: dict
    preview: dict
    valid_proposal: dict | None
    failed_patch: dict | None
    pending_patch: dict
    reviewed_preview: dict | None
    feedback: list[str]
    read_ids: list[str]
    attempts: int
    identity_attempts: int
    next_phase: str
    result: dict


def implementation_version():
    root = Path(__file__).parent
    paths = [
        *root.glob("*.py"),
        root.parent / "llm/integration.py",
        root.parent / "llm/incremental.py",
        root.parent / "llm/reconciliation.py",
        *root.parent.glob("llm/prompts/incremental_*.md"),
        root.parent / "llm/submission.py",
        *root.parent.glob("llm/prompts/integration_*.md"),
        root.parent / "transactions.py",
        root.parent.parent / "sql_training/llm/prompts/asset_contract.py",
        root.parent.parent / "sql_training/llm/generation_rule_taxonomy.py",
        root.parent.parent.parent / "application/ontology_graph.py",
        root.parent.parent.parent / "schema/references.py",
        root.parent.parent.parent / "schema/relation_metadata.py",
    ]
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(paths))).hexdigest()


class GraphIntegrationEngine:
    manages_input_namespaces = True

    def __init__(
        self,
        agent,
        *,
        evidence_resolver=None,
        checkpoint_factory=None,
        max_rounds=3,
        top_k=5,
        runtime_signature="",
    ):
        self.agent, self.evidence_resolver = agent, evidence_resolver
        self.checkpoint_factory = checkpoint_factory
        self.max_rounds, self.top_k = max_rounds, top_k
        self.runtime_signature = runtime_signature

    def merge(self, existing, incoming, schema, *, run_id=None):
        run_id = run_id or uuid4().hex
        GraphManager(schema).validate_state(existing)
        # Context validates fragments after source namespacing and local-contract
        # isolation. Shared original IDs across snapshots are legal here.
        request = {
            "existing": existing.model_dump(mode="json"),
            "incoming": incoming.model_dump(mode="json"),
            "schema": schema.model_dump(mode="json"),
            "version": implementation_version(),
            "max_rounds": self.max_rounds,
            "top_k": self.top_k,
            "runtime_signature": self.runtime_signature,
        }

        def context(state):
            req = state["request"]
            ctx = IntegrationContext(
                GraphState.model_validate(req["existing"]),
                GraphState.model_validate(req["incoming"]),
                SchemaDefinition.model_validate(req["schema"]),
                evidence=tuple(
                    EvidenceItem.model_validate(x) for x in state.get("evidence", ())
                ),
                top_k=self.top_k,
            )
            ctx.read_ids.update(state.get("read_ids", ()))
            return ctx

        def prepare(state):
            emit_progress("phase", phase="integration_prepare")
            ctx = context(state)
            sources = tuple(
                dict.fromkeys(
                    [s for n in ctx.raw_nodes.values() for s in n.sources]
                    + [s for e in ctx.raw_edges.values() for s in e.sources]
                    + [
                        s
                        for n in ctx.raw_nodes.values()
                        for a in n.alternatives
                        for s in a.sources
                    ]
                )
            )
            if hasattr(self.evidence_resolver, "clear"):
                self.evidence_resolver.clear()
            evidence = (
                self.evidence_resolver.resolve(sources)
                if self.evidence_resolver
                else ()
            )
            return {
                "evidence": [e.model_dump(mode="json") for e in evidence],
                "attempts": 0,
                "identity_attempts": 0,
                "feedback": [],
                "read_ids": [],
            }

        def decide_identity(state):
            emit_progress("phase", phase="integration_identity")
            count = state.get("identity_attempts", 0) + 1
            if count > self.max_rounds + 1:
                raise IntegrationIncomplete(
                    f"Identity did not converge: {state.get('feedback')}"
                )
            ctx = context(state)
            payload = ctx.payload("identity")
            payload["feedback"] = state.get("feedback", [])
            try:
                decision = self.agent.invoke("identity", ctx, payload)
                identity_map(ctx, decision)
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 402, 403}:
                    raise  # Provider access/balance failures are not semantic revision feedback.
                return {
                    "identity_attempts": count,
                    "feedback": [f"{type(exc).__name__}: {exc}"],
                    "read_ids": sorted(ctx.read_ids),
                    "next_phase": "identity",
                }
            return {
                "identity": decision.model_dump(mode="json"),
                "valid_proposal": None,
                "failed_patch": None,
                "reviewed_preview": None,
                "review": None,
                "feedback": [],
                "identity_attempts": count,
                "read_ids": sorted(ctx.read_ids),
                "next_phase": "propose",
            }

        def propose(state):
            emit_progress("phase", phase="integration_propose")
            count = state.get("attempts", 0) + 1
            if count > self.max_rounds:
                raise IntegrationIncomplete(
                    f"Integration not accepted after {self.max_rounds} proposals: {state.get('feedback')}"
                )
            ctx = context(state)
            payload = ctx.payload("propose")
            decision = IdentityDecision.model_validate(state["identity"])
            nodes, edges, mapping, _ = mapped_base(ctx, decision)
            ctx.candidate = GraphState(
                graph=GraphSpace(
                    nodes=tuple(nodes.values()),
                    edges=tuple(edges.values()),
                    relation_contracts=ctx.contracts,
                )
            )
            payload.update(
                identity=state["identity"],
                identity_map={
                    k: v
                    for k, v in mapping.items()
                    if k in ctx.nodes and ctx.nodes[k].node_type not in PHYSICAL
                },
                feedback=state.get("feedback", []),
            )
            previous = preview = None
            payload["proposal_mode"] = "base"
            if state.get("valid_proposal"):
                previous = IntegrationProposal.model_validate(state["valid_proposal"])
                preview = compile_proposal(ctx, decision, previous)
                ctx.candidate = preview.state
                payload.update(
                    proposal_mode="delta",
                    prior_changes=state["valid_proposal"]["changes"],
                    **assessment_view(ctx, decision, preview),
                )
            if previous is not None and not state.get("failed_patch"):
                focused = focused_revision(
                    ctx,
                    preview,
                    previous,
                    (state.get("review") or {}).get("issue_refs", []),
                )
                if focused is not None:
                    payload = {
                        "phase": "propose",
                        "proposal_mode": "delta",
                        "feedback": state.get("feedback", []),
                        **focused,
                    }
            if state.get("failed_patch"):
                payload["failed_patch"] = state["failed_patch"]
            proposal = None
            try:
                proposal = self.agent.invoke("propose", ctx, payload)
                pending_patch = proposal.model_dump(mode="json")
                if (
                    previous is not None
                    and not proposal.identity_feedback
                    and not proposal.representation_error
                ):
                    proposal = compose_revision(
                        ctx, decision, previous, preview, proposal
                    )
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 402, 403}:
                    raise  # Provider access/balance failures are not semantic revision feedback.
                return {
                    "attempts": count,
                    "feedback": [f"{type(exc).__name__}: {exc}"],
                    "read_ids": sorted(ctx.read_ids),
                    "next_phase": "propose",
                    "failed_patch": proposal.model_dump(mode="json")
                    if proposal
                    else state.get("failed_patch"),
                }
            if proposal.representation_error:
                raise IntegrationIncomplete(proposal.representation_error)
            return {
                "proposal": proposal.model_dump(mode="json"),
                "pending_patch": pending_patch,
                "failed_patch": None,
                "attempts": count,
                "read_ids": sorted(ctx.read_ids),
                "feedback": [proposal.identity_feedback]
                if proposal.identity_feedback
                else [],
                "next_phase": "identity" if proposal.identity_feedback else "validate",
            }

        def validate(state):
            emit_progress("phase", phase="integration_validate")
            ctx = context(state)
            ctx.payload("validate")
            try:
                preview = compile_proposal(
                    ctx,
                    IdentityDecision.model_validate(state["identity"]),
                    IntegrationProposal.model_validate(state["proposal"]),
                )
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 402, 403}:
                    raise  # Provider access/balance failures are not semantic revision feedback.
                return {
                    "feedback": [f"{type(exc).__name__}: {exc}"],
                    "failed_patch": state["pending_patch"],
                    "next_phase": "propose",
                }
            # Refresh against full source index when merged aliases/definitions reveal nonlocal candidates.
            refreshed = set()
            created_matches = {}
            ctx.candidate = preview.state
            for node in preview.state.graph.nodes:
                if node.node_type in PHYSICAL or node == ctx.nodes.get(node.id):
                    continue
                matches = {x["id"] for x in ctx.candidates(node.id, seed=node)["items"]}
                if node.id in ctx.nodes:
                    refreshed.update(matches)
                elif matches - ctx.read_ids:
                    created_matches[node.id] = sorted(matches)
            unseen = refreshed - ctx.read_ids
            new_unseen = {
                key for ids in created_matches.values() for key in ids
            } - ctx.read_ids
            if unseen or new_unseen:
                feedback = []
                if unseen:
                    feedback.append(
                        f"New source candidate definitions revealed by integration; compare in identity before acceptance: {sorted(unseen)}"
                    )
                if created_matches:
                    feedback.append(
                        f"Proposer must compare newly created nodes to these source candidates and reuse existing identities where appropriate (temporary created IDs are not source identity members): {created_matches}"
                    )
                return {
                    "valid_proposal": state["proposal"],
                    "read_ids": sorted(ctx.read_ids | unseen | new_unseen),
                    "next_phase": "identity" if unseen else "propose",
                    "feedback": feedback,
                }
            return {
                "preview": preview.state.model_dump(mode="json"),
                "valid_proposal": state["proposal"],
                "next_phase": "assess",
            }

        def assess(state):
            emit_progress("phase", phase="integration_assess")
            ctx = context(state)
            payload = ctx.payload("assess")
            proposal = IntegrationProposal.model_validate(state["proposal"])
            decision = IdentityDecision.model_validate(state["identity"])
            preview = compile_proposal(ctx, decision, proposal)
            ctx.candidate = preview.state
            payload.update(
                identity=state["identity"],
                proposal={"changes": state["proposal"]["changes"]},
                identity_map={
                    k: v
                    for k, v in preview.node_map.items()
                    if k in ctx.nodes and ctx.nodes[k].node_type not in PHYSICAL
                },
                impacts=preview.impacts,
                **assessment_view(ctx, decision, preview),
            )
            if state.get("reviewed_preview"):
                payload["previous_review"] = state["review"]
                payload["changes_since_review"] = graph_delta(
                    GraphState.model_validate(state["reviewed_preview"]), preview.state
                )
            try:
                review = self.agent.invoke("assess", ctx, payload)
            except Exception as exc:
                if getattr(exc, "status_code", None) in {401, 402, 403}:
                    raise  # Provider access/balance failures are not semantic revision feedback.
                return {
                    "feedback": [f"Assessment failed: {type(exc).__name__}: {exc}"],
                    "next_phase": "propose",
                }
            phase = {
                "accept": "commit",
                "revise": "propose",
                "revisit_identity": "identity",
            }[review.verdict]
            return {
                "review": review.model_dump(mode="json"),
                "reviewed_preview": preview.state.model_dump(mode="json"),
                "next_phase": phase,
                "read_ids": sorted(ctx.read_ids),
                "feedback": list(review.issues)
                or (
                    [
                        review.decision_check,
                        review.quality_check,
                        review.preservation_check,
                    ]
                    if phase != "commit"
                    else []
                ),
            }

        def commit(state):
            emit_progress("phase", phase="integration_commit")
            ctx = context(state)
            ctx.payload("commit")
            proposal = IntegrationProposal.model_validate(state["proposal"])
            preview = compile_proposal(
                ctx, IdentityDecision.model_validate(state["identity"]), proposal
            )
            result = commit_result(ctx, proposal, preview, run_id=run_id)
            return {"result": result.model_dump(mode="json")}

        builder = StateGraph(RunState)
        for name, function in (
            ("prepare", prepare),
            ("identity", decide_identity),
            ("propose", propose),
            ("validate", validate),
            ("assess", assess),
            ("commit", commit),
        ):
            builder.add_node(name, function)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "identity")
        for name in ("identity", "propose", "validate", "assess"):
            builder.add_conditional_edges(name, lambda state: state["next_phase"])
        builder.add_edge("commit", END)
        factory = self.checkpoint_factory or (lambda: nullcontext(None))
        with factory() as saver:
            graph = builder.compile(
                checkpointer=saver, name="ontology_graph_integration"
            )
            config = {
                "configurable": {"thread_id": run_id},
                "recursion_limit": 20 + 10 * self.max_rounds,
            }
            saved = graph.get_state(config) if saver else None
            if saved and saved.values and saved.values.get("request") != request:
                raise ValueError(
                    "Cannot resume integration with changed code, prompts, config or graph inputs"
                )
            result = (
                saved.values
                if saved and saved.values and not saved.next
                else graph.invoke(
                    None if saved and saved.values else {"request": request},
                    config=config,
                )
            )
        parsed = GraphMergeResult.model_validate(result["result"])
        GraphManager(schema).validate_state(parsed.state)
        return parsed
