"""Serial two-stage integration over stable workspace IDs.

Identity is distinct from content integration. Local acceptance updates only the
workspace; final IDs, repository normalization and export happen exactly once.
"""

from uuid import uuid4

from cortex.application.ontology_graph import GraphManager, GraphRejected
from cortex.capabilities.graph_merge.graph.edit_contract import IntegrationProposal
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.capabilities.graph_merge.graph.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.graph.publication import Preview, commit_result
from cortex.capabilities.graph_merge.identity.contract import identity_map
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.planning import (
    _nodes,
    _signal_targets,
    identity_task_projection,
    review_batches,
)
from cortex.capabilities.graph_merge.reconciliation.inspection import inspection_signals
from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete
from cortex.capabilities.graph_merge.runtime.session import MergeSession
from cortex.schema.relation_vocabulary import require_concrete_relation


class IncrementalGraphIntegrationEngine:
    manages_input_namespaces = True

    def __init__(
        self,
        agent,
        *,
        evidence_resolver=None,
        workspace_path=None,
        max_rounds=3,
        top_k=5,
        sql_dialect=None,
        model_call_budget=80,
        node_context_max_characters=110000,
    ):
        self.agent, self.evidence_resolver = agent, evidence_resolver
        self.max_rounds, self.top_k = max_rounds, top_k
        self.workspace_path = workspace_path
        self.sql_dialect = sql_dialect
        self.model_call_budget = model_call_budget
        if node_context_max_characters < 1:
            raise ValueError("node_context_max_characters must be positive")
        self.node_context_max_characters = node_context_max_characters
        self.session = MergeSession()
        self.execution = None
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
            self.agent.call_budget = (
                None
                if self.model_call_budget is None
                else {
                    "used": 0,
                    "limit": self.model_call_budget,
                }
            )
        from cortex.capabilities.graph_merge.runtime.contracts import (
            build_contract_versions,
        )

        versions = build_contract_versions(schema)
        self.session = MergeSession.start(ctx, versions=versions)
        self.execution = None
        resume = getattr(self, "resume_snapshot", None)
        if resume:
            storage_revision = None
            if self.workspace_path:
                from cortex.capabilities.graph_merge.runtime.workspace import (
                    WorkspaceStore,
                )

                if WorkspaceStore(self.workspace_path).load(run_id) is not None:
                    storage_revision = resume["revision"]
            self.session.restore(
                resume, versions=versions, storage_revision=storage_revision
            )
        self._persist(run_id)
        if resume and getattr(self, "external_feedback", None):
            # A reviewed graph needs a local feedback transaction, not another
            # identity/consolidation pass or a scan of every original input.
            if not resume.get("final"):
                raise ValueError("Local-only feedback requires a completed workspace")
            targets = set(getattr(self, "feedback_targets", ()))
            current_ids = set(_nodes(self.session.current)) | {
                e.id for e in self.session.current.graph.edges
            }
            if not targets or not targets <= current_ids:
                raise ValueError(
                    "Local resume feedback requires valid current target IDs"
                )
            phase = getattr(self, "feedback_phase", "reconcile")
            if phase not in {"consolidate", "reconcile"}:
                raise ValueError(
                    "Local feedback phase must be consolidate or reconcile"
                )
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
            if resume and self.session.identity_decision:
                from cortex.capabilities.graph_merge.identity.contract import (
                    IdentityDecision,
                )

                decision = IdentityDecision.model_validate(
                    self.session.identity_decision
                )
            else:
                decision = self.agent.invoke("identity", ctx, payload)
            self.session.identity_decision = decision.model_dump(mode="json")
            ctx.identity_decision = self.session.identity_decision
            aliases = identity_map(ctx, decision)
            self.session.identity_read_ids = sorted(ctx.read_ids & set(ctx.nodes))
            self._persist(run_id)
            if self.session.content_plan is None:
                groups = [g.model_dump(mode="json") for g in decision.groups]
                self.session.content_plan = [[group] for group in groups]
                self._persist(run_id)
            if any(len(groups) != 1 for groups in self.session.content_plan):
                raise ValueError(
                    "Saved multi-group content plan requires explicit migration; completed task IDs were preserved"
                )
            for index, groups in enumerate(self.session.content_plan):
                if any(
                    h["task_id"] == f"content-{index}" for h in self.session.history
                ):
                    continue
                group_aliases, targets, rewrite_feedback = {}, set(), []
                for group in groups:
                    projection, live_targets, feedback = identity_task_projection(
                        self.session.current,
                        {k: aliases[k] for k in group["members"]},
                        self.session.lineage,
                    )
                    group_aliases.update(projection)
                    targets.update(live_targets)
                    rewrite_feedback.extend(feedback)
                self._run_task(
                    LocalTask(
                        f"content-{index}",
                        "consolidate",
                        targets,
                        groups=groups,
                        feedback=rewrite_feedback,
                    ),
                    run_id,
                    aliases=group_aliases,
                )
            # First structural pass covers ALL new/unchecked semantic knowledge,
            # not only topology signals. Later passes reopen actual impacts.
            if (
                not (resume and resume.get("reconciliation_complete"))
                or self.session.relation_followups
                or (
                    self.session.task_state is not None
                    and self.session.task_state.phase == "reconcile"
                )
            ):
                self._reconcile(run_id, first=True)
                self.session.reconciliation_complete = True
                self._persist(run_id)
        self._validate_for_publication(run_id)
        proposal = IntegrationProposal(
            changes=self.session.lineage.source_changes(self.session.current)
        )
        preview = Preview(
            self.session.current,
            self.session.lineage.node_map,
            self.session.lineage.edge_map,
            [],
        )
        validate_rewrite_sources(ctx, proposal, preview)
        result = commit_result(ctx, proposal, preview, run_id=run_id)
        self.last_session = {
            "run_id": run_id,
            "revision": self.session.revision,
            "history": self.session.history,
            "lineage": self.session.lineage.snapshot(),
            "preview": self.session.current.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
        }
        self._persist(run_id, final=True)
        return result

    def _run_task(self, task, run_id, *, aliases=None):
        from cortex.capabilities.graph_merge.runtime.task import run_task

        return run_task(self, task, run_id, aliases=aliases)

    def _reconcile(self, run_id, *, first=False, initial_targets=None):
        from cortex.capabilities.graph_merge.reconciliation.followups import (
            drain_relation_followups,
        )
        from cortex.capabilities.graph_merge.reconciliation.receipts import (
            is_accepted,
            stable_task_id,
        )

        canonical = getattr(self.agent, "reconciliation_contract", False)
        reviewed = set()
        saved_task = getattr(self.session, "task_state", None)
        if saved_task is not None and saved_task.phase == "reconcile":
            definitions = [
                r for r in self.session.open_tasks if r["id"] == saved_task.task_id
            ]
            if len(definitions) != 1:
                raise ValueError(
                    "Unfinished relation task has no exact saved definition"
                )
            row = definitions[0]
            resumed_task = LocalTask(
                row["id"],
                row["phase"],
                set(row["targets"]),
                groups=row.get("groups", []),
                checks=row.get("checks", []),
                feedback=row.get("feedback", []),
            )
            self._run_task(resumed_task, run_id)
            from cortex.capabilities.graph_merge.reconciliation.context import (
                relation_signature,
            )

            if not canonical:
                self.session.relation_coverage.add(
                    relation_signature(
                        self.session.inputs, self.session.current, resumed_task
                    )
                )
            self._persist(run_id)
        from cortex.capabilities.graph_merge.runtime.relation_stage import (
            run_relation_merger,
        )

        run_relation_merger(self, run_id)
        drain_relation_followups(self, run_id)
        incoming_roots = {
            k
            for k, n in _nodes(self.session.current).items()
            if n.node_type not in PHYSICAL
            and any(
                r.kind == "node" and r.id in self.session.inputs.seeds
                for r in self.session.lineage.origin_refs("node", k)
            )
        }
        if initial_targets is not None:
            incoming_roots = set(initial_targets)
        else:
            for entry in self.session.history:
                incoming_roots.update(entry["delta"]["nodes"]["created"])
                incoming_roots.update(entry["delta"]["nodes"]["updated"])
        pending = initial_targets
        for cycle in range(self.max_rounds + 1):
            preview = Preview(
                self.session.current,
                self.session.lineage.node_map,
                self.session.lineage.edge_map,
                [],
            )
            signals = inspection_signals(
                self.session.inputs, preview, origins=self.session.lineage
            )
            targets = incoming_roots if cycle == 0 and first else (pending or set())
            checks = [
                s
                for s in signals
                if (
                    not is_accepted((self).session, s)
                    if canonical
                    else s["id"] not in reviewed
                )
                and _signal_targets(s, self.session.current) & targets
            ]
            tasks = review_batches(
                self.session.current,
                targets=targets,
                checks=checks,
                signals_only=getattr(self.agent, "reconciliation_contract", False),
            )
            if not tasks:
                return
            impacts = set()
            checks_graph = self.session.current
            live_checks = {s["id"]: s for s in signals}
            for index, (ids, cs) in enumerate(tasks):
                if canonical:
                    # A preceding accepted task can close or change a queued
                    # signal. Refresh only after the graph actually changes.
                    if self.session.current != checks_graph:
                        live_checks = {
                            s["id"]: s
                            for s in inspection_signals(
                                self.session.inputs,
                                Preview(
                                    self.session.current,
                                    self.session.lineage.node_map,
                                    self.session.lineage.edge_map,
                                    [],
                                ),
                                origins=self.session.lineage,
                            )
                        }
                        checks_graph = self.session.current
                    cs = [live_checks[c["id"]] for c in cs if c["id"] in live_checks]
                    # Earlier tasks may have accepted a shared check or changed
                    # its premises after this batch list was constructed.
                    cs = [c for c in cs if not is_accepted((self).session, c)]
                    if not cs:
                        continue
                    ids = set().union(
                        *(_signal_targets(c, self.session.current) for c in cs)
                    )
                task = LocalTask(
                    stable_task_id((self).session, cs)
                    if canonical
                    else f"structure-{cycle}-{index}",
                    "reconcile",
                    ids,
                    checks=cs,
                )
                from cortex.capabilities.graph_merge.reconciliation.context import (
                    relation_signature,
                )

                signature = (
                    None
                    if canonical
                    else relation_signature(
                        self.session.inputs, self.session.current, task
                    )
                )
                if not canonical and signature in getattr(
                    self.session, "relation_coverage", set()
                ):
                    reviewed.update(c["id"] for c in cs)
                    continue
                impacts |= self._run_task(task, run_id)
                if not canonical:
                    self.session.relation_coverage.add(
                        relation_signature(
                            self.session.inputs, self.session.current, task
                        )
                    )
                self._persist(run_id)
                reviewed.update(c["id"] for c in cs)
            # A newly introduced pattern still needs an explicit conclusion,
            # even when all its endpoints were part of the writer's own patch.
            scope = incoming_roots | set().union(*(ids for ids, _ in tasks))
            for signal in inspection_signals(
                self.session.inputs,
                Preview(
                    self.session.current,
                    self.session.lineage.node_map,
                    self.session.lineage.edge_map,
                    [],
                ),
                origins=self.session.lineage,
            ):
                keys = _signal_targets(signal, self.session.current)
                if (
                    not is_accepted((self).session, signal)
                    if canonical
                    else signal["id"] not in reviewed
                ) and keys & scope:
                    impacts |= keys
            # Reopen affected definitions, including additions to a previously
            # reviewed neighbourhood, without recursively expanding all hubs.
            pending = {
                k
                for k in impacts
                if k in _nodes(self.session.current)
                and _nodes(self.session.current)[k].node_type not in PHYSICAL
            }
            if not pending:
                return
            # Checks involving changed endpoints need semantic reconsideration.
            reviewed -= {
                s["id"]
                for s in inspection_signals(
                    self.session.inputs,
                    Preview(
                        self.session.current,
                        self.session.lineage.node_map,
                        self.session.lineage.edge_map,
                        [],
                    ),
                    origins=self.session.lineage,
                )
                if not canonical and _signal_targets(s, self.session.current) & pending
            }
        raise IntegrationIncomplete(
            "Structural reconciliation did not close within the configured budget"
        )

    def _publish_checks(self):
        if getattr(self.session, "relation_followups", ()):
            raise IntegrationIncomplete(
                "Final graph still has unresolved node relation obligations"
            )
        GraphManager(self.session.inputs.schema).validate_state(self.session.current)
        for edge in self.session.current.graph.edges:
            require_concrete_relation(edge.edge_type)

    def _validate_for_publication(self, run_id):
        """Route strict, localized contract errors back to the existing Writer.

        No second model review for a no-op and no deterministic semantic fix.
        Accepted local work is retained; only validator-named objects are opened.
        """
        from cortex.capabilities.graph_merge.graph.repair import (
            validation_targets,
        )
        from cortex.capabilities.graph_merge.reconciliation.followups import (
            drain_relation_followups,
        )

        drain_relation_followups(self, run_id)

        for attempt in range(self.max_rounds + 1):
            try:
                self._publish_checks()
                return
            except GraphRejected as error:
                if attempt == self.max_rounds:
                    raise
                targets = validation_targets(self.session.current, error.issues)
                if not targets:
                    raise
                before = self.session.current
                self._run_task(
                    LocalTask(
                        f"contract-repair-{self.session.revision}",
                        "reconcile",
                        targets,
                        feedback=[i.model_dump(mode="json") for i in error.issues],
                    ),
                    run_id,
                )
                if self.session.current == before:
                    raise error

    def _mark_reviewed(self, ids):
        self.session.mark_reviewed(ids)

    def _persist(self, run_id, final=False):
        self.session.save(self.workspace_path, run_id, final=final)
