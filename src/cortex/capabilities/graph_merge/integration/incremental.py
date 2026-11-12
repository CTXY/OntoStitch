"""Serial two-stage integration over stable workspace IDs.

Identity is distinct from content integration. Local acceptance updates only the
workspace; final IDs, repository normalization and export happen exactly once.
"""

from uuid import uuid4

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.batch import (
    ActiveBatch,
    alias_view,
    delta,
)
from cortex.capabilities.graph_merge.integration.compiler import (
    Preview,
    commit_result,
    identity_map,
)
from cortex.capabilities.graph_merge.integration.context import (
    PHYSICAL,
    IntegrationContext,
)
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import IntegrationProposal
from cortex.capabilities.graph_merge.integration.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.integration.signals import inspection_signals
from cortex.capabilities.graph_merge.integration.task_context import (
    LocalTask,
    affected_query_roots,
    build_task_context,
    semantic_review_targets,
)
from cortex.capabilities.graph_merge.integration.two_stage import matched_batches
from cortex.schema import GraphState
from cortex.schema.relation_vocabulary import require_concrete_relation


def input_fingerprint(context):
    import hashlib
    import json

    data = [
        context.existing.model_dump(mode="json"),
        context.incoming.model_dump(mode="json"),
        context.schema.model_dump(mode="json"),
        sorted(
            {
                (e.id, json.dumps(e.content, ensure_ascii=False, sort_keys=True))
                for e in context.evidence
            }
        ),
    ]
    return hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _nodes(state):
    return {n.id: n for n in state.graph.nodes}


def active_identity_projection(current, aliases, lineage):
    """Project live members only; retain recorded earlier structural rewrites.

    Identity is already Agent-confirmed. Provenance here only verifies why an
    absent member needs no second physical projection; it establishes no match.
    The subsequent Agent still compares the complete immutable matched group.
    """
    nodes = _nodes(current)
    active = {}
    for source, target in aliases.items():
        if target not in nodes:
            raise IntegrationIncomplete(
                f"Matched canonical {target} was retired; rebuild the unpublished plan"
            )
        if source in nodes:
            active[source] = target
        elif not any(
            r.kind == "node" and r.id == source
            for r in lineage.origin_refs("node", target)
        ):
            raise IntegrationIncomplete(
                f"Matched member {source} is absent without a recorded rewrite to {target}"
            )
    return active


def _signal_targets(check, state):
    edges = {e.id: e for e in state.graph.edges}
    keys = set(check.get("node_ids", ())) | set(check.get("edge_ids", ()))
    keys |= {x.get("concept") for x in check.get("members", ())}
    for eid in check.get("edge_ids", ()):
        if eid in edges:
            keys.update((edges[eid].from_node_id, edges[eid].to_node_id))
    if check.get("kind") == "source_selection_variants":
        operands = set(check.get("node_ids", ()))
        keys.update(
            e.from_node_id
            for e in edges.values()
            if e.edge_type == "applies_to" and e.to_node_id in operands
        )
    return {k for k in keys if k in _nodes(state) or k in edges}


def review_batches(state, *, targets=None, checks=(), budget=14000):
    """Bound root definitions, keeping each supplied inspection indivisible.

    Context neighbours are retrieved separately and are not writable by default.
    No domain topic, semantic conclusion or fixed case partition is encoded here.
    """
    nodes = _nodes(state)
    roots = set(
        targets
        if targets is not None
        else (k for k, n in nodes.items() if n.node_type not in PHYSICAL)
    )
    units = [(set(_signal_targets(c, state)), [c]) for c in checks]
    covered = set().union(*(u[0] for u in units)) if units else set()
    units.extend(({k}, []) for k in sorted(roots - covered))
    batches, keys, findings, used = [], set(), [], 0
    for ids, cs in units:
        cost = sum(len(nodes[k].model_dump_json()) for k in ids - keys if k in nodes)
        if keys and used + cost > budget:
            batches.append((keys, findings))
            keys, findings, used = set(), [], 0
        keys |= ids
        findings.extend(cs)
        used += cost
    if keys:
        batches.append((keys, findings))
    return batches


class IncrementalGraphIntegrationEngine:
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
        sql_dialect=None,
        model_call_budget=80,
    ):
        self.agent, self.evidence_resolver = agent, evidence_resolver
        self.max_rounds, self.top_k = max_rounds, top_k
        self.checkpoint_factory, self.runtime_signature = (
            checkpoint_factory,
            runtime_signature,
        )
        self.sql_dialect = sql_dialect
        self.model_call_budget = model_call_budget
        self.history = []
        self.last_session = None

    def merge(self, existing, incoming, schema, *, run_id=None):
        run_id = run_id or uuid4().hex
        GraphManager(schema).validate_state(existing)
        ctx = IntegrationContext(existing, incoming, schema, top_k=self.top_k)
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
        if self.evidence_resolver:
            self.evidence_resolver.clear() if hasattr(
                self.evidence_resolver, "clear"
            ) else None
            evidence = self.evidence_resolver.resolve(sources)
            ctx = IntegrationContext(
                existing, incoming, schema, evidence=evidence, top_k=self.top_k
            )
        ctx.sql_dialect = self.sql_dialect
        if hasattr(self.agent, "max_calls"):
            self.agent.call_budget = {
                "used": 0,
                "limit": self.model_call_budget,
            }
        # All original business objects remain separately available until their
        # complete content transaction is accepted. Only physical IDs align here.
        current = ctx.union_state()
        current, edge_map = alias_view(current, {})
        lineage = LineageIndex(
            ctx, initial_node_map=ctx.physical_map, initial_edge_map=edge_map
        )
        self.history = []
        self.ctx, self.lineage, self.current = ctx, lineage, current
        self._revision = 0
        self._storage_revision = None
        self.open_tasks = []
        self.reviewed_sources = {}
        self.identity_decision = None
        resume = getattr(self, "resume_snapshot", None)
        if resume:
            if resume.get("input_fingerprint") != input_fingerprint(ctx):
                raise ValueError(
                    "Workspace source/schema/evidence fingerprint mismatch"
                )
            self.current = GraphState.model_validate(resume["current"])
            self.lineage = LineageIndex.restore(ctx, resume["lineage"])
            self.history = list(resume["history"])
            self._revision = resume.get("accepted_revision", len(self.history))
            from cortex.schema import SourceReference

            self.reviewed_sources = {
                k: {SourceReference.model_validate(r) for r in refs}
                for k, refs in resume.get("reviewed_sources", {}).items()
            }
            self.identity_decision = resume.get("identity")
        self._persist(run_id)
        seed = getattr(self, "reviewed_node_seed", None)
        if seed:
            if [e.model_dump(mode="json") for e in ctx.evidence] != seed["evidence"]:
                raise ValueError("Source evidence differs from the reviewed candidate")
            from cortex.capabilities.graph_merge.integration.models import (
                IdentityDecision,
            )

            decision = IdentityDecision.model_validate(seed["identity"])
            aliases = identity_map(ctx, decision)
            imported = GraphState.model_validate(seed["preview"])
            lineage.observe(
                current,
                imported,
                IntegrationProposal.model_validate(seed["proposal"]).changes,
                node_aliases=aliases,
                explanation="Explicitly import the reviewed candidate for a controlled comparison",
            )
            self.current = imported
            self._mark_reviewed(set(seed.get("reviewed_node_ids", ())))
            feedback = list(getattr(self, "external_feedback", ()))
            # Feedback references are parsed as handles only, never instructions
            # executed by code. The model decides the requested semantic repair.
            import re

            known = (
                set(_nodes(imported))
                | {e.id for e in imported.graph.edges}
                | set(ctx.raw_nodes)
                | set(ctx.raw_edges)
            )
            targets = {
                k
                for text in feedback
                for k in re.findall(r"input::[ne]\d+", str(text))
                if k in known
            }
            if not targets:
                raise ValueError(
                    "Controlled local repair requires feedback naming existing current handles"
                )
            self._run_task(
                LocalTask("external-review", "reconcile", targets, feedback=feedback),
                run_id,
            )
        elif resume and getattr(self, "external_feedback", None):
            # A reviewed graph needs a local feedback transaction, not another
            # identity/consolidation pass or a scan of every original input.
            if not resume.get("final"):
                raise ValueError("Local-only feedback requires a completed workspace")
            targets = set(getattr(self, "feedback_targets", ()))
            current_ids = set(_nodes(self.current)) | {
                e.id for e in self.current.graph.edges
            }
            if not targets or not targets <= current_ids:
                raise ValueError(
                    "Local resume feedback requires valid current target IDs"
                )
            impacts = self._run_task(
                LocalTask(
                    "external-review",
                    "reconcile",
                    targets,
                    feedback=list(self.external_feedback),
                ),
                run_id,
            )
            if impacts:
                self._reconcile(run_id, initial_targets=impacts)
        else:
            # Identity retrieval is one independent phase. Group choice is never
            # inferred from physical endpoints or candidate scores by code.
            payload = ctx.payload("identity")
            ctx.output_validator = lambda d: identity_map(ctx, d)
            if resume and self.identity_decision:
                from cortex.capabilities.graph_merge.integration.models import (
                    IdentityDecision,
                )

                decision = IdentityDecision.model_validate(self.identity_decision)
            else:
                decision = self.agent.invoke("identity", ctx, payload)
            self.identity_decision = decision.model_dump(mode="json")
            ctx.identity_decision = self.identity_decision
            aliases = identity_map(ctx, decision)
            self._persist(run_id)
            for index, groups in enumerate(
                matched_batches(
                    [g.model_dump(mode="json") for g in decision.groups], ctx.nodes
                )
            ):
                if any(h["task_id"] == f"content-{index}" for h in self.history):
                    continue
                group_aliases = {k: aliases[k] for g in groups for k in g["members"]}
                targets = {aliases[k] for g in groups for k in g["members"]}
                group_aliases = active_identity_projection(
                    self.current, group_aliases, self.lineage
                )
                self._run_task(
                    LocalTask(
                        f"content-{index}", "consolidate", targets, groups=groups
                    ),
                    run_id,
                    aliases=group_aliases,
                )
            # First structural pass covers ALL new/unchecked semantic knowledge,
            # not only topology signals. Later passes reopen actual impacts.
            self._reconcile(run_id, first=True)
        self._publish_checks()
        proposal = IntegrationProposal(
            changes=self.lineage.source_changes(self.current)
        )
        preview = Preview(
            self.current, self.lineage.node_map, self.lineage.edge_map, []
        )
        validate_rewrite_sources(ctx, proposal, preview)
        result = commit_result(ctx, proposal, preview, run_id=run_id)
        self.last_session = {
            "run_id": run_id,
            "revision": self._revision,
            "history": self.history,
            "lineage": self.lineage.snapshot(),
            "preview": self.current.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        }
        self._persist(run_id, final=True)
        return result

    def _run_task(self, task, run_id, *, aliases=None):
        ctx = self.ctx
        before = self.current
        new_id_prefix = f"generated::{uuid4().hex}::"
        batch = ActiveBatch(ctx, before, targets=task.targets, aliases=aliases)
        projected_lineage = LineageIndex.restore(ctx, self.lineage.snapshot())
        # This projection is controller-owned, so fail before any model call if
        # its state assumptions are invalid. The Agent cannot repair this map.
        projected_lineage.observe(
            before, batch.seed, node_aliases=aliases, edge_aliases=batch.edge_aliases
        )
        ctx.active_task = task
        ctx.active_batch = batch
        self.open_tasks = [
            {"id": task.id, "phase": task.phase, "targets": sorted(task.targets)}
        ]
        self._persist(run_id)
        ctx.workspace_revision = self._revision
        extensions = []

        def expand(ids, reason):
            known = {n.id for n in (batch.preview or batch.seed).graph.nodes} | {
                e.id for e in (batch.preview or batch.seed).graph.edges
            }
            aliases_now = {
                **self.lineage.node_map,
                **batch.aliases,
                **self.lineage.edge_map,
                **batch.edge_aliases,
            }
            mapped = [aliases_now.get(k, k) for k in ids]
            archived = [
                k
                for k, m in zip(ids, mapped)
                if m not in known and (k in ctx.raw_nodes or k in ctx.raw_edges)
            ]
            missing = [
                k for k, m in zip(ids, mapped) if m not in known and k not in archived
            ]
            if not reason.strip() or missing:
                raise ValueError(
                    f"Extension needs current IDs and a required-change reason; missing={missing}"
                )
            redirect = {k: m for k, m in zip(ids, mapped) if k != m}
            ids = [m for m in mapped if m in known]
            if len(extensions) >= 3:
                raise ValueError(
                    "Task extension budget exhausted; do not split a coupled semantic decision"
                )
            extensions.append({"ids": ids, "reason": reason})
            task.targets.update(ids)
            batch.targets.update(ids)
            batch.allowed.update(ids)
            batch.allowed.update(
                e.id
                for e in batch.seed.graph.edges
                if e.from_node_id in ids or e.to_node_id in ids
            )
            return {
                "status": "expanded",
                "redirected_aliases": redirect,
                "archived_only": archived,
                "identity_contract": "Confirmed aliases denote one identity. Implementation differences require content reconciliation/complete conflict alternatives, not restoring ordinary competing copies. Archived-only handles are immutable before refs.",
                "writable_ids": sorted(batch.allowed),
                "objects": [
                    n.model_dump(mode="json")
                    for n in (batch.preview or batch.seed).graph.nodes
                    if n.id in ids
                ],
            }

        ctx.extend_task = expand
        checks = {s["id"]: s for s in task.checks}

        def validate(candidate, changes, result):
            found = [c.inspection_id for c in result.inspection_checks]
            if set(found) != set(checks) or len(found) != len(set(found)):
                raise ValueError(
                    f"Conclude each active inspection once: {sorted(checks)}"
                )
            nodes = _nodes(candidate)
            for check in result.inspection_checks:
                if check.conclusion == "failed":
                    raise ValueError("Resolve the named inspection before accepting")
                if (
                    checks[check.inspection_id]["kind"]
                    in {"related_decisions", "source_selection_variants"}
                    and check.decision_relation is None
                ):
                    raise ValueError(
                        "State compatible/conditional/conflict/different_decision"
                    )
                if check.decision_relation == "conflict" and not any(
                    k in nodes and len(nodes[k].alternatives) > 1
                    for k in check.result_ids
                ):
                    raise ValueError(
                        "Conflict is not resolved by listing ordinary competing Rules. Put complete sourced alternatives on the relevant current Rule with conflicts; replace obsolete copies and repair consumers in this batch. Extend the task for any required read-only Rule. A different label cannot substitute for fixing the same decision."
                    )
            # Validate only actual task Rules (including newly created Rules),
            # never unrelated Rules merely seen in an earlier phase.
            rule_ids = {
                k
                for k in delta(before, candidate)["nodes"]["created"]
                + delta(before, candidate)["nodes"]["updated"]
                if k in nodes
                and nodes[k].node_type == "generation_rule"
                and (
                    k not in _nodes(before)
                    or _nodes(before)[k].model_dump(exclude={"sources"})
                    != nodes[k].model_dump(exclude={"sources"})
                )
            }
            if getattr(task, "review_retained_rules", False):
                rule_ids |= {
                    k
                    for k in task.targets
                    if k in nodes and nodes[k].node_type == "generation_rule"
                }
            rule_ids |= {
                k
                for k in delta(before, candidate)["nodes"]["created"]
                if nodes[k].node_type == "generation_rule"
            }
            actual = [r.node_id for r in result.rationale_checks]
            if task.phase == "reconcile" and (
                set(actual) != rule_ids or len(actual) != len(set(actual))
            ):
                raise ValueError(
                    f"Compare each active Rule reason with supplied evidence once: {sorted(rule_ids)}"
                )
            if any(not r.supported for r in result.rationale_checks):
                raise ValueError("Repair unsupported Rule reasons before accepting")
            # Origin ledger checks are staged on a clone, so invalid patches do
            # not pollute the accepted source correspondences.
            staged = LineageIndex.restore(ctx, projected_lineage.snapshot())
            staged.observe(batch.seed, candidate, changes)
            validate_rewrite_sources(
                ctx,
                IntegrationProposal(changes=staged.source_changes(candidate)),
                Preview(candidate, staged.node_map, staged.edge_map, []),
            )
            batch.lineage_preview = staged

        batch.extra_check = validate
        for attempt in range(self.max_rounds + 1):
            ctx.candidate = batch.preview or batch.seed
            payload = build_task_context(
                ctx, ctx.candidate, task, origins=self.lineage, before=before
            )
            payload["new_id_prefix"] = new_id_prefix
            payload["task_scope"]["writable_ids"] = sorted(batch.allowed)
            payload["identity_aliases"] = batch.aliases
            payload["provisional_identity_projection"] = bool(batch.aliases)
            payload["pending_revision"] = batch.revision
            payload["rationale_review_ids"] = (
                sorted(
                    k
                    for k in task.targets
                    if k in _nodes(ctx.candidate)
                    and _nodes(ctx.candidate)[k].node_type == "generation_rule"
                )
                if getattr(task, "review_retained_rules", False)
                else []
            )
            payload["rationale_scope"] = (
                "Check these retained Rules and any Rule meaning you actually change; sources-only maintenance needs no repeated reason review."
            )
            if batch.pending or batch.changes or batch.contracts:
                from cortex.capabilities.graph_merge.integration.submission import (
                    edit_key,
                )

                payload["saved_patch"] = {
                    "edits": [
                        {
                            "kind": edit_key(e)[0],
                            "id": edit_key(e)[1],
                            "operation": e["operation"],
                        }
                        for e in batch.pending.values()
                    ],
                    "changes": batch.changes,
                    "relation_contract_ids": sorted(batch.contracts),
                    "edge_merges": [
                        m.model_dump(mode="json") for m in batch.edge_merges.values()
                    ],
                }
            ctx.output_validator = batch.submit
            result = self.agent.invoke(task.phase, ctx, payload)
            # Injected test agents use the same validation path.
            if batch.result is not result and batch.preview is None:
                batch.submit(result)
            candidate = batch.preview
            impact = semantic_review_targets(before, candidate, task.targets)
            if impact["requires_evidence_check"] and self._needs_evidence_review(
                before, candidate
            ):
                impact = semantic_review_targets(
                    before, candidate, task.targets, evidence_changed=True
                )
            # Node consolidation always receives independent assessment. Graph
            # structure is self-reviewed; semantic/core dependency edits reopen A.
            review_ids = set(impact["targets"])
            review_ids |= {
                key
                for key in delta(before, candidate)["nodes"]["deleted"]
                if _nodes(before)[key].node_type not in PHYSICAL
            }
            for check in task.checks:
                if check["kind"] in {"related_decisions", "source_selection_variants"}:
                    review_ids |= {
                        k
                        for k in _signal_targets(check, candidate)
                        if k in _nodes(candidate)
                        and _nodes(candidate)[k].node_type
                        in {"concept", "generation_rule"}
                    }
            if task.phase == "consolidate":
                review_ids |= {k for k in task.targets if k in _nodes(candidate)}
            if getattr(task, "review_retained_rules", False):
                review_ids |= {
                    k
                    for k in task.targets
                    if k in _nodes(candidate)
                    and _nodes(candidate)[k].node_type == "generation_rule"
                }
            if review_ids:
                reviewtask = LocalTask(
                    task.id + "-node-review",
                    "node_assess",
                    review_ids,
                    groups=task.groups,
                    checks=task.checks,
                    feedback=list(task.feedback),
                )
                reviewpayload = build_task_context(
                    ctx,
                    candidate,
                    reviewtask,
                    origins=batch.lineage_preview,
                    before=before,
                )
                ctx.output_validator = None
                review = self.agent.invoke("node_assess", ctx, reviewpayload)
                if review.verdict != "accept":
                    if review.verdict == "revisit_identity":
                        raise IntegrationIncomplete(
                            "Concrete identity counterexample: rebuild this unpublished input batch; do not guess a reverse split"
                        )
                    task.feedback = list(review.issues)
                    task.targets.update(r.id for r in review.issue_refs)
                    batch.allowed.update(task.targets)
                    continue
            # No model transaction remains open while persistence runs.
            self.lineage = batch.lineage_preview
            self.current = candidate
            self._mark_reviewed(review_ids)
            self._revision += 1
            self.history.append(
                {
                    "task_id": task.id,
                    "phase": task.phase,
                    "revision": self._revision,
                    "delta": delta(before, candidate),
                    "extensions": extensions,
                    "node_review_ids": sorted(review_ids),
                    "changes": [
                        c.model_dump(mode="json") for c in batch.result.changes
                    ],
                }
            )
            self.open_tasks = []
            self._persist(run_id)
            impacts = affected_query_roots(before, candidate)
            if task.phase == "reconcile":
                # The writer has checked its own accepted result. Reopen only
                # effects outside that reviewed scope, not the same patch again.
                impacts -= task.targets | set(
                    delta(before, candidate)["nodes"]["created"]
                )
            return impacts
        raise IntegrationIncomplete(
            f"{task.id} failed independent node assessment; batch not published"
        )

    def _reconcile(self, run_id, *, first=False, initial_targets=None):
        reviewed = set()
        incoming_roots = {
            k
            for k, n in _nodes(self.current).items()
            if n.node_type not in PHYSICAL
            and any(
                r.kind == "node" and r.id in self.ctx.seeds
                for r in self.lineage.origin_refs("node", k)
            )
        }
        if initial_targets is not None:
            incoming_roots = set(initial_targets)
        else:
            for entry in self.history:
                incoming_roots.update(entry["delta"]["nodes"]["created"])
                incoming_roots.update(entry["delta"]["nodes"]["updated"])
        pending = initial_targets
        for cycle in range(self.max_rounds + 1):
            preview = Preview(
                self.current, self.lineage.node_map, self.lineage.edge_map, []
            )
            signals = inspection_signals(self.ctx, preview, origins=self.lineage)
            targets = incoming_roots if cycle == 0 and first else (pending or set())
            checks = [
                s
                for s in signals
                if s["id"] not in reviewed
                and _signal_targets(s, self.current) & targets
            ]
            tasks = review_batches(self.current, targets=targets, checks=checks)
            if not tasks:
                return
            impacts = set()
            for index, (ids, cs) in enumerate(tasks):
                task = LocalTask(
                    f"structure-{cycle}-{index}", "reconcile", ids, checks=cs
                )
                task.review_retained_rules = True
                impacts |= self._run_task(task, run_id)
                reviewed.update(c["id"] for c in cs)
            # A newly introduced pattern still needs an explicit conclusion,
            # even when all its endpoints were part of the writer's own patch.
            scope = incoming_roots | set().union(*(ids for ids, _ in tasks))
            for signal in inspection_signals(
                self.ctx,
                Preview(self.current, self.lineage.node_map, self.lineage.edge_map, []),
                origins=self.lineage,
            ):
                keys = _signal_targets(signal, self.current)
                if signal["id"] not in reviewed and keys & scope:
                    impacts |= keys
            # Reopen affected definitions, including additions to a previously
            # reviewed neighbourhood, without recursively expanding all hubs.
            pending = {
                k
                for k in impacts
                if k in _nodes(self.current)
                and _nodes(self.current)[k].node_type not in PHYSICAL
            }
            if not pending:
                return
            # Checks involving changed endpoints need semantic reconsideration.
            reviewed -= {
                s["id"]
                for s in inspection_signals(
                    self.ctx,
                    Preview(
                        self.current, self.lineage.node_map, self.lineage.edge_map, []
                    ),
                    origins=self.lineage,
                )
                if _signal_targets(s, self.current) & pending
            }
        raise IntegrationIncomplete(
            "Structural reconciliation did not close within the configured budget"
        )

    def _publish_checks(self):
        GraphManager(self.ctx.schema).validate_state(self.current)
        for edge in self.current.graph.edges:
            require_concrete_relation(edge.edge_type)

    def _evidence_keys(self, references):
        import hashlib
        import json

        keys = set()
        for ref in references:
            rows = [e for e in self.ctx.evidence if e.source == ref]
            if not rows:
                keys.add(None)
            for row in rows:
                content = json.dumps(row.content, ensure_ascii=False, sort_keys=True)
                keys.add((row.id, hashlib.sha256(content.encode()).hexdigest()))
        return keys

    def _needs_evidence_review(self, before, after):
        for kind in ("nodes", "edges"):
            old = {x.id: x for x in getattr(before.graph, kind)}
            for obj in getattr(after.graph, kind):
                if obj.id not in old:
                    continue  # New semantic contents already trigger A.
                added = set(obj.sources) - set(old[obj.id].sources)
                removed = set(old[obj.id].sources) - set(obj.sources)
                if removed:
                    return True
                if not added:
                    continue
                owner = obj.id if kind == "nodes" else obj.from_node_id
                seen = self._evidence_keys(self.reviewed_sources.get(owner, set())) - {
                    None
                }
                fresh = self._evidence_keys(added)
                if None in fresh or not fresh <= seen:
                    return True
        return False

    def _mark_reviewed(self, ids):
        nodes = _nodes(self.current)
        for key in ids:
            if key not in nodes:
                continue
            n = nodes[key]
            refs = set(n.sources) | {s for a in n.alternatives for s in a.sources}
            # Node Assessment receives full current core dependencies, so retain
            # the exact evidence references examined for those assertions.
            refs |= {
                s
                for e in self.current.graph.edges
                if e.from_node_id == key
                and e.edge_type in {"grounded_on", "depends_on", "applies_to"}
                for s in e.sources
            }
            self.reviewed_sources[key] = refs

    def _persist(self, run_id, final=False):
        # Optional per-run SQLite journal is separate from the strict final graph
        # repository and stores the graph and ledger in one transaction.
        path = getattr(self, "workspace_path", None)
        if not path:
            return
        from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore

        payload = {
            "current": self.current.model_dump(mode="json"),
            "lineage": self.lineage.snapshot(),
            "history": self.history,
            "open_tasks": self.open_tasks,
            "final": final,
            "accepted_revision": self._revision,
            "input_fingerprint": input_fingerprint(self.ctx),
            "identity": self.identity_decision,
            "reviewed_sources": {
                k: [
                    r.model_dump(mode="json")
                    for r in sorted(refs, key=lambda r: r.model_dump_json())
                ]
                for k, refs in getattr(self, "reviewed_sources", {}).items()
            },
        }
        self._storage_revision = WorkspaceStore(path).save(
            run_id, payload, expected_revision=self._storage_revision
        )
