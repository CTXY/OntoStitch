"""Matched-node consolidation, independent node review, then graph reconciliation.

Stable source handles and one cumulative edit buffer survive all small submissions.
Only the final strict graph is published. No domain-specific decisions live here.
"""

import json
from contextlib import nullcontext

from langgraph.graph import END, START, StateGraph

from cortex.application.merge_graph import GraphMergeResult
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.assessment import assessment_view
from cortex.capabilities.graph_merge.integration.compiler import (
    Preview,
    commit_result,
    compile_proposal,
    identity_map,
    mapped_base,
)
from cortex.capabilities.graph_merge.integration.context import (
    PHYSICAL,
    IntegrationContext,
)
from cortex.capabilities.graph_merge.integration.engine import (
    GraphIntegrationEngine,
    IntegrationIncomplete,
    RunState,
    implementation_version,
)
from cortex.capabilities.graph_merge.integration.models import (
    IdentityDecision,
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.integration.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.integration.submission import ProposalBuffer
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import GraphSpace, GraphState


def matched_batches(groups, nodes, budget=16000):
    batches, batch, used = [], [], 0
    for group in groups:
        size = sum(len(nodes[k].model_dump_json()) for k in group["members"])
        if batch and used + size > budget:
            batches.append(batch)
            batch, used = [], 0
        batch.append(group)
        used += size
    if batch:
        batches.append(batch)
    return batches


def graph_issues(context, decision, proposal):
    try:
        preview = compile_proposal(context, decision, proposal)
        validate_rewrite_sources(context, proposal, preview)
        return []
    except (ValueError, TypeError, KeyError) as exc:
        return [str(exc)]


class TwoStageState(RunState, total=False):
    batches: list
    batch_index: int
    node_review_ids: list[str]
    return_phase: str
    reused_node_stage: dict
    content_attempts: int


class TwoStageGraphIntegrationEngine(GraphIntegrationEngine):
    def __init__(self, *args, sql_dialect=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sql_dialect = sql_dialect

    def merge(self, existing, incoming, schema, *, run_id=None):
        from uuid import uuid4

        run_id = run_id or uuid4().hex
        GraphManager(schema).validate_state(existing)
        if hasattr(self.agent, "max_calls"):
            self.agent.call_budget = {
                "used": 0,
                "limit": self.agent.max_calls * (self.max_rounds + 2),
            }
        request = {
            "existing": existing.model_dump(mode="json"),
            "incoming": incoming.model_dump(mode="json"),
            "schema": schema.model_dump(mode="json"),
            "version": implementation_version(),
            "runtime_signature": self.runtime_signature,
            "max_rounds": self.max_rounds,
            "top_k": self.top_k,
        }

        request["sql_dialect"] = self.sql_dialect
        request["external_feedback"] = getattr(self, "external_feedback", [])
        request["pending_seed"] = getattr(self, "pending_seed", None)
        seed = getattr(self, "reviewed_node_seed", None)
        if seed:
            import hashlib

            request["node_seed_hash"] = hashlib.sha256(
                json.dumps(seed, sort_keys=True).encode()
            ).hexdigest()

        def context(state):
            ctx = IntegrationContext(
                existing,
                incoming,
                schema,
                evidence=tuple(
                    EvidenceItem.model_validate(e) for e in state.get("evidence", [])
                ),
                top_k=self.top_k,
            )
            ctx.read_ids.update(state.get("read_ids", []))
            ctx.sql_dialect = self.sql_dialect
            return ctx

        def prepare(state):
            ctx = context(state)
            sources = tuple(
                dict.fromkeys(
                    [
                        s
                        for n in ctx.raw_nodes.values()
                        for s in (
                            *n.sources,
                            *(s for a in n.alternatives for s in a.sources),
                        )
                    ]
                    + [s for e in ctx.raw_edges.values() for s in e.sources]
                )
            )
            if hasattr(self.evidence_resolver, "clear"):
                self.evidence_resolver.clear()
            evidence = (
                self.evidence_resolver.resolve(sources)
                if self.evidence_resolver
                else ()
            )
            if seed:
                fresh_evidence = [e.model_dump(mode="json") for e in evidence]
                if fresh_evidence != seed["evidence"]:
                    raise ValueError(
                        "Source evidence differs from the reviewed node stage"
                    )
                return {
                    k: seed[k]
                    for k in [
                        "identity",
                        "proposal",
                        "preview",
                        "review",
                        "read_ids",
                        "evidence",
                    ]
                } | {
                    "attempts": 0,
                    "feedback": [],
                    "node_review_ids": [],
                    "return_phase": "consolidate",
                    "next_phase": "reconcile",
                    "reused_node_stage": {
                        "origin": seed["origin"],
                        "run_id": seed["run_id"],
                        "not_new_model_calls": True,
                    },
                }
            return {
                "evidence": [e.model_dump(mode="json") for e in evidence],
                "proposal": IntegrationProposal().model_dump(mode="json"),
                "read_ids": [],
                "batch_index": 0,
                "attempts": 0,
                "feedback": [],
                "node_review_ids": [],
                "next_phase": "identity",
            }

        def identify(state):
            ctx = context(state)
            decision = self.agent.invoke("identity", ctx, ctx.payload("identity"))
            mapping = identity_map(ctx, decision)
            return {
                "identity": decision.model_dump(mode="json"),
                "batches": matched_batches(
                    [g.model_dump(mode="json") for g in decision.groups], ctx.nodes
                ),
                "node_review_ids": sorted(
                    {mapping[k] for g in decision.groups for k in g.members}
                ),
                "read_ids": sorted(ctx.read_ids),
                "next_phase": "consolidate" if decision.groups else "reconcile",
            }

        def setup(state):
            ctx = context(state)
            decision = IdentityDecision.model_validate(state["identity"])
            proposal = IntegrationProposal.model_validate(state["proposal"])
            nodes, edges, mapping, edge_map = mapped_base(ctx, decision)
            if proposal.edits or proposal.changes:
                preview = compile_proposal(ctx, decision, proposal, strict=False)
            else:
                preview = Preview(
                    GraphState(
                        graph=GraphSpace(
                            nodes=tuple(nodes.values()),
                            edges=tuple(edges.values()),
                            relation_contracts=ctx.contracts,
                        )
                    ),
                    mapping,
                    edge_map,
                    [],
                )
            ctx.candidate = preview.state
            return ctx, decision, proposal, preview

        def run_writer(state, phase, groups=None):
            ctx, decision, previous, preview = setup(state)
            buffer = ProposalBuffer(ctx, decision, previous)
            pending_seed = getattr(self, "pending_seed", None)
            if phase == "reconcile" and state.get("attempts", 0) == 0 and pending_seed:
                buffer.pending = pending_seed["proposal"]
                buffer.revision = pending_seed["revision"]
            # Empty and earlier structurally incomplete states are not successful submissions.
            ctx.proposal_buffer = buffer
            trace_sink = getattr(self.agent, "trace_sink", None)
            if hasattr(trace_sink, "path"):
                buffer.artifact_dir = trace_sink.path.parent
            payload = ctx.payload(phase)
            scope = (
                {k for g in groups for k in g["members"]}
                if groups is not None
                else None
            )
            if scope is not None:
                payload["nodes"] = [n for n in payload["nodes"] if n["id"] in scope]
            payload.update(
                identity=state["identity"],
                identity_map={
                    k: v
                    for k, v in preview.node_map.items()
                    if k in ctx.nodes and ctx.nodes[k].node_type not in PHYSICAL
                },
                matched_groups=groups,
                proposal_mode="buffered",
                pending_revision=buffer.revision,
                feedback=state.get("feedback", [])
                + getattr(self, "external_feedback", []),
                prior_changes=previous.model_dump(mode="json")["changes"],
            )
            if phase == "reconcile":
                payload.update(assessment_view(ctx, decision, preview))
                payload["graph_issues"] = graph_issues(ctx, decision, previous)
                from cortex.capabilities.graph_merge.integration.signals import (
                    inspection_signals,
                )

                payload["inspection_signals"] = inspection_signals(ctx, preview)
                if state.get("review"):
                    payload["node_review"] = state["review"]
            else:
                selected = {preview.node_map[k] for k in scope}
                # A node task receives its complete groups, not the batch's unrelated
                # seed/candidate directories or Identity distinctions about other Rules.
                from cortex.capabilities.graph_merge.integration.context import (
                    references,
                )
                from cortex.capabilities.graph_merge.integration.packing import (
                    bounded_items,
                )

                incident = [
                    e
                    for e in ctx.edges.values()
                    if e.from_node_id in scope or e.to_node_id in scope
                ]
                endpoints = {
                    k for e in incident for k in (e.from_node_id, e.to_node_id)
                }
                physical = {k for k in endpoints if ctx.nodes[k].node_type in PHYSICAL}
                physical |= {
                    k
                    for nid in scope
                    for k in references(ctx.nodes[nid])
                    if k in ctx.nodes and ctx.nodes[k].node_type in PHYSICAL
                }
                payload = {
                    "phase": phase,
                    "matched_groups": groups,
                    "nodes": [
                        ctx.nodes[k].model_dump(mode="json") for k in sorted(scope)
                    ],
                    "candidate_nodes": [
                        n.model_dump(mode="json")
                        for n in preview.state.graph.nodes
                        if n.id in selected
                    ],
                    "identity_map": {k: preview.node_map[k] for k in scope},
                    "relations": [e.model_dump(mode="json") for e in incident],
                    "neighbor_directory": [
                        ctx.summary(k) for k in sorted(endpoints - scope - physical)
                    ],
                    "physical_facts": bounded_items(
                        [
                            ctx.nodes[k].model_dump(mode="json", exclude_defaults=True)
                            for k in sorted(physical)
                        ],
                        budget=8000,
                    ),
                    "evidence": ctx.evidence_rows(),
                    "proposal_mode": "buffered",
                    "pending_revision": buffer.revision,
                    "feedback": state.get("feedback", []),
                    "prior_changes": [
                        c.model_dump(mode="json")
                        for c in previous.changes
                        if any(e.id in scope for e in c.before)
                    ],
                }
            if phase == "reconcile" and pending_seed and state.get("attempts", 0) == 0:
                from cortex.capabilities.graph_merge.integration.submission import (
                    content_errors,
                    edit_key,
                )

                payload["pending_revision"] = buffer.revision
                payload["pending_edit_ids"] = [
                    edit_key(e)[1] for e in buffer.pending["edits"]
                ]
                payload["pending_repairs"] = content_errors(
                    buffer.pending["edits"], ctx.schema
                )
                payload["pending_notice"] = (
                    "Saved pending edits are not candidate view. Inspect pending objects before replacing them; repair only remaining errors. write_conflict builds valid compact nodes from complete alternatives."
                )
            # Current accepted edits are available by ID, not repeatedly reprinted in prompts.
            outcome = self.agent.invoke(phase, ctx, payload)
            if outcome.verdict != "complete":
                raise IntegrationIncomplete(outcome.explanation)
            result = buffer.accepted
            new_preview = buffer.preview
            if hasattr(buffer, "artifact_dir"):
                from cortex.infrastructure.artifacts.graph_html import render_graph_html

                directory = buffer.artifact_dir
                (directory / "current_graph_state.json").write_text(
                    new_preview.state.model_dump_json(indent=2)
                )
                (directory / "current_graph.html").write_text(
                    render_graph_html(
                        new_preview.state.graph,
                        schema,
                        title="Current merge · not yet published",
                    )
                )
            old_nodes = {n.id: n for n in preview.state.graph.nodes}
            changed = {
                n.id
                for n in new_preview.state.graph.nodes
                if n.node_type not in PHYSICAL and old_nodes.get(n.id) != n
            }
            # Formula/branch-scoped dependency changes can change meaning without editing text.
            old_edges = {e.id: e for e in preview.state.graph.edges}
            for edge in new_preview.state.graph.edges:
                if old_edges.get(edge.id) != edge and edge.edge_type in {
                    "grounded_on",
                    "depends_on",
                    "applies_to",
                }:
                    changed.add(edge.from_node_id)
            changed &= {
                n.id
                for n in new_preview.state.graph.nodes
                if n.node_type not in PHYSICAL
            }
            if phase == "reconcile":
                from cortex.capabilities.graph_merge.integration.signals import (
                    inspection_signals,
                )

                decision_signals = [
                    s
                    for s in inspection_signals(ctx, new_preview)
                    if s["kind"] in {"related_decisions", "source_selection_variants"}
                ]
                subjects = {
                    k for signal in decision_signals for k in signal.get("node_ids", [])
                }
                changed |= subjects
                changed |= {
                    e.from_node_id
                    for e in new_preview.state.graph.edges
                    if e.edge_type == "applies_to" and e.to_node_id in subjects
                }

            return {
                "proposal": result.model_dump(mode="json"),
                "preview": new_preview.state.model_dump(mode="json"),
                "read_ids": sorted(ctx.read_ids),
                "node_review_ids": sorted(
                    (set(state.get("node_review_ids", [])) | changed)
                    & {n.id for n in new_preview.state.graph.nodes}
                ),
                "feedback": [],
            }

        def consolidate(state):
            index = state["batch_index"]
            result = run_writer(state, "consolidate", state["batches"][index])
            index += 1
            result.update(
                batch_index=index,
                next_phase="consolidate"
                if index < len(state["batches"])
                else "node_assess",
                return_phase="consolidate",
            )
            return result

        def node_assess(state):
            ctx, decision, proposal, preview = setup(state)
            payload = ctx.payload("node_assess")
            ids = set(state.get("node_review_ids", []))
            view = assessment_view(ctx, decision, preview)
            view["candidate_nodes"] = [
                n for n in view["candidate_nodes"] if n["id"] in ids
            ]
            view["rationale_review_ids"] = sorted(
                n["id"]
                for n in view["candidate_nodes"]
                if n["node_type"] == "generation_rule"
            )
            payload.update(
                view,
                identity=state["identity"],
                proposal={"changes": state["proposal"]["changes"]},
                reviewed_node_ids=sorted(ids),
            )
            previous_review = state.get("review") or {}
            if previous_review.get("issues"):
                payload["previous_review_feedback"] = {
                    "issues": previous_review["issues"],
                    "issue_refs": previous_review.get("issue_refs", []),
                }
            source_ids = {
                key for key in ctx.raw_nodes if preview.node_map.get(key, key) in ids
            }
            for change in proposal.changes:
                if any(
                    e.kind == "node" and preview.node_map.get(e.id, e.id) in ids
                    for e in change.after
                ):
                    source_ids.update(e.id for e in change.before if e.kind == "node")
            payload["nodes"] = [
                ctx.nodes[k].model_dump(mode="json")
                for k in sorted(source_ids)
                if k in ctx.nodes
            ]
            for field in [
                "incoming_seeds",
                "candidate_ids",
                "binding_contests",
                "endpoint_directory",
            ]:
                payload.pop(field, None)
            payload["identity"] = {
                "groups": [
                    g
                    for g in state["identity"]["groups"]
                    if set(g["members"]) & source_ids
                ]
            }
            if state.get("return_phase") == "reconcile":
                from cortex.capabilities.graph_merge.integration.signals import (
                    inspection_signals,
                )

                payload["shared_decision_checks"] = [
                    s
                    for s in inspection_signals(ctx, preview)
                    if s["kind"] in {"related_decisions", "source_selection_variants"}
                ]

            review = self.agent.invoke("node_assess", ctx, payload)
            if review.verdict == "revisit_identity":
                raise IntegrationIncomplete(
                    "Concrete identity correction requires reconstruction before publication: "
                    + review.decision_check
                )
            if review.verdict != "accept":
                if state.get("return_phase") == "consolidate":
                    attempts = state.get("content_attempts", 0) + 1
                    if attempts > self.max_rounds:
                        raise IntegrationIncomplete(
                            f"Node consolidation repair budget exhausted: {review.issues}"
                        )
                    mapping = identity_map(ctx, decision)
                    affected = {
                        ref.id for ref in review.issue_refs if ref.kind == "node"
                    } or ids
                    groups = [
                        g.model_dump(mode="json")
                        for g in decision.groups
                        if any(mapping[k] in affected for k in g.members)
                    ]
                    groups = groups or [
                        g.model_dump(mode="json") for g in decision.groups
                    ]
                    return {
                        "review": review.model_dump(mode="json"),
                        "feedback": list(review.issues),
                        "next_phase": "consolidate",
                        "read_ids": sorted(ctx.read_ids),
                        "batches": matched_batches(groups, ctx.nodes),
                        "batch_index": 0,
                        "content_attempts": attempts,
                    }
                return {
                    "review": review.model_dump(mode="json"),
                    "feedback": list(review.issues),
                    "next_phase": "reconcile",
                    "read_ids": sorted(ctx.read_ids),
                }
            return {
                "review": review.model_dump(mode="json"),
                "feedback": [],
                "node_review_ids": [],
                "read_ids": sorted(ctx.read_ids),
                "next_phase": "commit"
                if state.get("return_phase") == "reconcile"
                else "reconcile",
            }

        def reconcile(state):
            count = state.get("attempts", 0) + 1
            if count > self.max_rounds:
                raise IntegrationIncomplete(
                    f"Reconciliation budget exhausted: {state.get('feedback')}"
                )
            result = run_writer(state, "reconcile")
            ctx = context({**state, **result})
            decision = IdentityDecision.model_validate(state["identity"])
            issues = graph_issues(
                ctx, decision, IntegrationProposal.model_validate(result["proposal"])
            )
            result.update(attempts=count, return_phase="reconcile")
            if issues:
                result.update(feedback=issues, next_phase="reconcile")
            else:
                result["next_phase"] = (
                    "node_assess" if result["node_review_ids"] else "commit"
                )
            return result

        def commit(state):
            ctx, decision, proposal, _ = setup(state)
            preview = compile_proposal(ctx, decision, proposal)
            validate_rewrite_sources(ctx, proposal, preview)
            return {
                "result": commit_result(
                    ctx, proposal, preview, run_id=run_id
                ).model_dump(mode="json")
            }

        builder = StateGraph(TwoStageState)
        for name, fn in [
            ("prepare", prepare),
            ("identity", identify),
            ("consolidate", consolidate),
            ("node_assess", node_assess),
            ("reconcile", reconcile),
            ("commit", commit),
        ]:
            builder.add_node(name, fn)
        builder.add_edge(START, "prepare")
        for name in ["prepare", "identity", "consolidate", "node_assess", "reconcile"]:
            builder.add_conditional_edges(name, lambda s: s["next_phase"])
        builder.add_edge("commit", END)
        factory = self.checkpoint_factory or (lambda: nullcontext(None))
        with factory() as saver:
            graph = builder.compile(checkpointer=saver, name="two_stage_ontology_merge")
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": 100}
            saved = graph.get_state(config) if saver else None
            if saved and saved.values and saved.values.get("request") != request:
                raise ValueError("Cannot resume with changed code, config or inputs")
            result = (
                saved.values
                if saved and saved.values and not saved.next
                else graph.invoke(
                    None if saved and saved.values else {"request": request},
                    config=config,
                )
            )
        return GraphMergeResult.model_validate(result["result"])
