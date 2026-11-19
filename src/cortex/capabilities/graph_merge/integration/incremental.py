"""Serial two-stage integration over stable workspace IDs.

Identity is distinct from content integration. Local acceptance updates only the
workspace; final IDs, repository normalization and export happen exactly once.
"""

from uuid import uuid4

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.integration.batch import (
    alias_view,
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
    build_task_context,
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



def identity_task_projection(current, aliases, lineage):
    """Keep ordinary aliases; route rewritten groups to explicit Agent review.

    Provenance locates current assertions, but does not establish a new identity.
    A stale group therefore receives no mechanical aliases. The Writer and its
    independent reviewer compare complete archived groups with all live outputs.
    """
    try:
        active = active_identity_projection(current, aliases, lineage)
        return active, set(active.values()), []
    except IntegrationIncomplete:
        nodes = _nodes(current)
        origins = {
            key: {r.id for r in lineage.origin_refs("node", key) if r.kind == "node"}
            for key in nodes
        }
        locations = {}
        for source in sorted(set(aliases) | set(aliases.values())):
            live = {key for key, refs in origins.items() if source in refs}
            if source in nodes:
                live.add(source)
            if not live:
                raise  # An unrepresented/discarded source needs a new plan.
            locations[source] = sorted(live)
        import json

        feedback = [
            "An earlier accepted rewrite changed the live objects of this archived "
            "matched group. These provenance locations do not assert new identity: "
            + json.dumps(locations, sort_keys=True)
            + ". Compare complete current contents with the archived group. Preserve "
            "accepted rewrites and make explicit sourced edits/dispositions for any "
            "remaining integration. A group already represented by one live object "
            "needs content review without recreating retired copies. If the current "
            "meanings reveal an identity counterexample, report it."
        ]
        return {}, set().union(*(set(v) for v in locations.values())), feedback



def _signal_targets(check, state):
    edges = {e.id: e for e in state.graph.edges}
    keys = set(check.get('node_ids', ())) | set(check.get('edge_ids', ()))
    keys |= {x.get('concept') for x in check.get('members', ())}
    for eid in check.get('edge_ids', ()):
        if eid in edges:
            keys.update((edges[eid].from_node_id, edges[eid].to_node_id))
    if check.get('kind') == 'source_selection_variants':
        operands = set(check.get('node_ids', ()))
        keys.update(e.from_node_id for e in edges.values()
                    if e.edge_type == 'applies_to' and e.to_node_id in operands)
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
        node_context_max_characters=110000,
    ):
        self.agent, self.evidence_resolver = agent, evidence_resolver
        self.max_rounds, self.top_k = max_rounds, top_k
        self.checkpoint_factory, self.runtime_signature = (
            checkpoint_factory,
            runtime_signature,
        )
        self.sql_dialect = sql_dialect
        self.model_call_budget = model_call_budget
        if node_context_max_characters < 1:
            raise ValueError("node_context_max_characters must be positive")
        self.node_context_max_characters = node_context_max_characters
        self.history = []
        self.last_session = None
        self.relation_coverage = set()

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
            self.agent.call_budget = (None if self.model_call_budget is None else {
                "used": 0,
                "limit": self.model_call_budget,
            })
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
        self.identity_read_ids = None
        self.content_plan = None
        self.relation_coverage = set()
        self.reconciliation_complete = False
        self.pending_relation_work = None
        self.pending_review = None
        from cortex.capabilities.graph_merge.integration.task_state import (
            build_contract_versions, validate_checkpoint_contract, TaskState,
        )
        self.contract_versions = build_contract_versions(schema)
        self.task_state = None
        self.checkpoint_migration = None
        resume = getattr(self, "resume_snapshot", None)
        if resume:
            validate_checkpoint_contract(resume, self.contract_versions)
            self.checkpoint_migration = resume.get('migration')
            if resume.get("input_fingerprint") != input_fingerprint(ctx):
                raise ValueError(
                    "Workspace source/schema/evidence fingerprint mismatch"
                )
            path = getattr(self, "workspace_path", None)
            if path:
                from cortex.capabilities.graph_merge.integration.workspace import (
                    WorkspaceStore,
                )

                if WorkspaceStore(path).load(run_id) is not None:
                    self._storage_revision = resume["revision"]
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
            identity_reads = resume.get("identity_read_ids")
            if identity_reads is None and self.identity_decision and getattr(self, "identity_trace_path", None):
                from cortex.capabilities.graph_merge.integration.identity_reads import identity_reads_from_trace
                identity_reads = identity_reads_from_trace(self.identity_trace_path, self.identity_decision)
            if identity_reads is not None:
                if not set(identity_reads) <= set(ctx.nodes):
                    raise ValueError("Saved identity reads reference nodes outside the original input")
                ctx.read_ids.update(identity_reads)
                self.identity_read_ids = sorted(identity_reads)
            self.content_plan = resume.get("content_plan")
            self.relation_coverage = set(resume.get("relation_coverage", ()))
            self.reconciliation_complete = resume.get("reconciliation_complete", False)
            self.pending_relation_work = resume.get("pending_relation_work")
            self.pending_review = resume.get("pending_review")
            self.open_tasks = list(resume.get('open_tasks', []))
            if resume.get('task_state'):
                restored_task = TaskState.from_dict(resume['task_state'])
                self.task_state = restored_task if restored_task.stage != 'committed' else None
            normalized, edge_aliases = alias_view(self.current, {})
            if normalized != self.current:
                self.lineage.observe(
                    self.current, normalized, edge_aliases=edge_aliases,
                    explanation="Canonicalize duplicate assertions using declared physical JOIN identity",
                )
                self.current = normalized
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
            self.identity_read_ids = sorted(ctx.read_ids & set(ctx.nodes))
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
            phase = getattr(self, "feedback_phase", "reconcile")
            if phase not in {"consolidate", "reconcile"}:
                raise ValueError("Local feedback phase must be consolidate or reconcile")
            impacts = self._run_task(
                LocalTask(
                    "external-review",
                    phase,
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
            self.identity_read_ids = sorted(ctx.read_ids & set(ctx.nodes))
            self._persist(run_id)
            def input_size(groups):
                import json
                from cortex.capabilities.graph_merge.integration.node_context import compact_task_context
                from cortex.capabilities.graph_merge.llm.packets import compact_node_packet
                targets = set()
                for group in groups:
                    _projection, live, _ = identity_task_projection(
                        self.current, {k: aliases[k] for k in group["members"]}, self.lineage
                    )
                    targets.update(live)
                candidate = self.current
                task = LocalTask("input-preflight", "consolidate", targets, groups=groups)
                packet = build_task_context(ctx, candidate, task, origins=self.lineage, before=self.current)
                packet = compact_task_context(packet, ctx, candidate, targets, groups, for_review=False)
                return len(json.dumps(compact_node_packet(packet), ensure_ascii=False))

            if self.content_plan is None:
                groups = [g.model_dump(mode="json") for g in decision.groups]
                started = any(h["task_id"].startswith("content-") for h in self.history) or bool(
                    self.pending_review and self.pending_review.get("phase") == "consolidate"
                )
                # Legacy checkpoints keep their existing task numbering. New
                # plans measure complete transport context and survive resumes.
                self.content_plan = (matched_batches(groups, ctx.nodes) if started else
                    matched_batches(groups, ctx.nodes, budget=self.node_context_max_characters, measure=input_size))
                self._persist(run_id)
            for index, groups in enumerate(self.content_plan):
                if any(h["task_id"] == f"content-{index}" for h in self.history):
                    continue
                group_aliases, targets, rewrite_feedback = {}, set(), []
                for group in groups:
                    projection, live_targets, feedback = identity_task_projection(
                        self.current, {k: aliases[k] for k in group["members"]}, self.lineage
                    )
                    group_aliases.update(projection)
                    targets.update(live_targets)
                    rewrite_feedback.extend(feedback)
                self._run_task(
                    LocalTask(
                        f"content-{index}", "consolidate", targets, groups=groups,
                        feedback=rewrite_feedback,
                    ),
                    run_id,
                    aliases=group_aliases,
                )
            # First structural pass covers ALL new/unchecked semantic knowledge,
            # not only topology signals. Later passes reopen actual impacts.
            if not (resume and resume.get("reconciliation_complete")):
                self._reconcile(run_id, first=True)
                self.reconciliation_complete = True
                self._persist(run_id)
        self._validate_for_publication(run_id)
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
        from cortex.capabilities.graph_merge.integration.task_runner import run_task
        return run_task(self, task, run_id, aliases=aliases)

    def _reconcile(self, run_id, *, first=False, initial_targets=None):
        reviewed = set()
        saved_task = getattr(self, 'task_state', None)
        if saved_task is not None and saved_task.phase == 'reconcile':
            definitions = [r for r in self.open_tasks if r['id'] == saved_task.task_id]
            if len(definitions) != 1:
                raise ValueError('Unfinished relation task has no exact saved definition')
            row = definitions[0]
            resumed_task = LocalTask(row['id'], row['phase'], set(row['targets']),
                groups=row.get('groups', []), checks=row.get('checks', []), feedback=row.get('feedback', []))
            self._run_task(resumed_task, run_id)
            from cortex.capabilities.graph_merge.integration.relation_context import relation_signature
            self.relation_coverage.add(relation_signature(self.ctx, self.current, resumed_task))
            self._persist(run_id)
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
                from cortex.capabilities.graph_merge.integration.relation_context import (
                    relation_signature,
                )

                signature = relation_signature(self.ctx, self.current, task)
                if signature in getattr(self, "relation_coverage", set()):
                    reviewed.update(c["id"] for c in cs)
                    continue
                impacts |= self._run_task(task, run_id)
                self.relation_coverage.add(relation_signature(self.ctx, self.current, task))
                self._persist(run_id)
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

    def _validate_for_publication(self, run_id):
        """Route strict, localized contract errors back to the existing Writer.

        No second model review for a no-op and no deterministic semantic fix.
        Accepted local work is retained; only validator-named objects are opened.
        """
        from cortex.capabilities.graph_merge.integration.validation_repair import (
            validation_targets,
        )

        for attempt in range(self.max_rounds + 1):
            try:
                self._publish_checks()
                return
            except GraphRejected as error:
                if attempt == self.max_rounds:
                    raise
                targets = validation_targets(self.current, error.issues)
                if not targets:
                    raise
                before = self.current
                self._run_task(LocalTask(
                    f"contract-repair-{self._revision}", "reconcile", targets,
                    feedback=[i.model_dump(mode="json") for i in error.issues],
                ), run_id)
                if self.current == before:
                    raise error

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
            "identity_read_ids": getattr(self, "identity_read_ids", None),
            "content_plan": getattr(self, "content_plan", None),
            "relation_coverage": sorted(getattr(self, "relation_coverage", ())),
            "reconciliation_complete": getattr(self, "reconciliation_complete", False),
            "pending_relation_work": getattr(self, "pending_relation_work", None),
            "pending_review": getattr(self, "pending_review", None),
            "contract_versions": self.contract_versions.to_dict() if getattr(self, 'contract_versions', None) else None,
            "task_state": self.task_state.to_dict() if getattr(self, 'task_state', None) else None,
            **({'migration': self.checkpoint_migration} if getattr(self, 'checkpoint_migration', None) else {}),
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
