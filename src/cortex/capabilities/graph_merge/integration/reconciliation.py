"""Problem-scoped reconciliation; accepted units never follow a failed sibling.

The matched-node path is deliberately independent. These are serial, current-
relative transactions, not a dependency scheduler or a second graph repository.
"""

import json
from uuid import uuid4

from cortex.capabilities.graph_merge.integration.batch import (
    ActiveBatch,
    delta,
)
from cortex.capabilities.graph_merge.integration.compiler import Preview
from cortex.capabilities.graph_merge.integration.context import PHYSICAL
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import (
    IntegrationProposal,
)
from cortex.capabilities.graph_merge.integration.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.integration.reconciliation_scope import (
    _compact_packet,
    _stamp,
    definition_stamp,
    signal_targets,
)
from cortex.capabilities.graph_merge.integration.reconciliation_units import (
    CheckedUnit,
    RepairUnit,
    _combine,
    _footprint,
    group_units,
)
from cortex.capabilities.graph_merge.integration.signals import inspection_signals
from cortex.capabilities.graph_merge.integration.task_context import (
    LocalTask,
    _indexes,
    affected_query_roots,
    build_task_context,
    semantic_review_targets,
)


class ReconciliationController:
    def __init__(self, engine, run_id, *, packet_budget=90000):
        self.engine, self.ctx, self.run_id = engine, engine.ctx, run_id
        self.packet_budget = packet_budget
        self.checked_nodes, self.checked_signals = {}, {}
        self.sequence = 0
        from cortex.capabilities.graph_merge.integration.engine import (
            implementation_version,
        )

        self.version = implementation_version()
        saved = getattr(engine, "reconciliation_coverage", {})
        if saved.get("implementation_version") == self.version:
            self.checked_nodes = dict(saved.get("nodes", {}))
            self.checked_signals = dict(saved.get("signals", {}))
            self.sequence = saved.get("sequence", 0)

    def _save_coverage(self):
        self.engine.reconciliation_coverage = {
            "implementation_version": self.version,
            "nodes": self.checked_nodes,
            "signals": self.checked_signals,
            "sequence": self.sequence,
        }

    def _packet(self, task, state=None, before=None, origins=None):
        state = state or self.engine.current
        payload = _compact_packet(
            build_task_context(
                self.ctx,
                state,
                task,
                origins=origins or self.engine.lineage,
                before=before,
            )
        )
        if task.phase == "reconcile":
            payload["reconciliation_batch"] = True
            payload["inspection_signals"] = [
                *task.checks,
                *[
                    {"id": k, "kind": "node_content", "node_ids": [k]}
                    for k in sorted(task.targets)
                    if k in _indexes(state)[0]
                    and _indexes(state)[0][k].node_type not in PHYSICAL
                    and k not in {check["id"] for check in task.checks}
                    and self.checked_nodes.get(k) != definition_stamp(state, k)
                ],
            ]
        return payload

    def run(self, *, first=False, initial_targets=None):
        engine = self.engine
        nodes, _ = _indexes(engine.current)
        if initial_targets is not None:
            required = set(initial_targets)
        else:
            required = {
                k
                for k, n in nodes.items()
                if n.node_type not in PHYSICAL
                and any(
                    r.kind == "node" and r.id in self.ctx.seeds
                    for r in engine.lineage.origin_refs("node", k)
                )
            }
            for entry in engine.history:
                required.update(entry["delta"]["nodes"]["created"])
                required.update(entry["delta"]["nodes"]["updated"])
        # Every iteration rebuilds against the accepted graph, not a stale queue.
        limit = max(1, len(required)) * (engine.max_rounds + 2)
        for _ in range(limit):
            current = engine.current
            nodes, _edges = _indexes(current)
            pending = {
                k
                for k in required
                if k in nodes
                and nodes[k].node_type not in PHYSICAL
                and self.checked_nodes.get(k) != definition_stamp(current, k)
            }
            signals = inspection_signals(
                self.ctx,
                Preview(current, engine.lineage.node_map, engine.lineage.edge_map, []),
                origins=engine.lineage,
            )
            active = [
                s
                for s in signals
                if signal_targets(s, current) & required
                and self.checked_signals.get(s["id"])
                != _stamp(current, signal_targets(s, current), s)
            ]
            # Organization repairs may retire abstract edges; resolve them
            # before spending calls on predicate wording alone.
            priorities = {
                "binding_contest": 0,
                "related_decisions": 0,
                "source_selection_variants": 0,
                "abstract_predicate": 2,
            }
            active.sort(key=lambda check: priorities.get(check["kind"], 1))
            candidates = [(signal_targets(s, current), [s]) for s in active]
            covered = (
                set().union(*(ids for ids, _ in candidates)) if candidates else set()
            )
            candidates += [({k}, []) for k in sorted(pending - covered)]
            if not candidates:
                return
            ids, checks, payload = set(), [], None
            task_id = f"structure-{self.sequence}"
            # Budget the actual shared packet. An indivisible oversized problem
            # runs alone; branches are never truncated to fit the budget.
            for more, cs in candidates:
                trial = LocalTask(task_id, "reconcile", ids | more, checks=checks + cs)
                packet = self._packet(trial)
                if (
                    ids
                    and len(json.dumps(packet, ensure_ascii=False)) > self.packet_budget
                ):
                    break
                ids, checks, payload = trial.targets, trial.checks, packet
            task = LocalTask(task_id, "reconcile", ids, checks=checks)
            before = current
            completed, impacts = self._execute(task, payload)
            required.update(impacts)
            for key in completed:
                if key in _indexes(engine.current)[0]:
                    self.checked_nodes[key] = definition_stamp(engine.current, key)
            for check in checks:
                self.checked_signals[check["id"]] = _stamp(
                    engine.current, signal_targets(check, engine.current), check
                )
            self.sequence += 1
            # Modified relation endpoints were checked as part of this repair;
            # new signals still get their own explicit conclusion next iteration.
            for key in affected_query_roots(before, engine.current) & set(completed):
                if key in _indexes(engine.current)[0]:
                    self.checked_nodes[key] = definition_stamp(engine.current, key)
            self._save_coverage()
            engine._persist(self.run_id)
        raise IntegrationIncomplete(
            "Reconciliation work budget exhausted; graph not published"
        )

    def _stage(self, base, lineage, task, unit):
        batch = ActiveBatch(self.ctx, base, targets=task.targets)
        batch.submit(unit.patch)
        staged = LineageIndex.restore(self.ctx, lineage.snapshot())
        staged.observe(base, batch.preview, batch.result.changes)
        validate_rewrite_sources(
            self.ctx,
            IntegrationProposal(changes=staged.source_changes(batch.preview)),
            Preview(batch.preview, staged.node_map, staged.edge_map, []),
        )
        return batch, staged

    def _review_ids(self, before, candidate, task, checks):
        impact = semantic_review_targets(before, candidate, task.targets)
        if impact["requires_evidence_check"] and self.engine._needs_evidence_review(
            before, candidate
        ):
            impact = semantic_review_targets(
                before, candidate, task.targets, evidence_changed=True
            )
        old_nodes, old_edges = _indexes(before)
        new_nodes, new_edges = _indexes(candidate)
        ids = set(impact["targets"])
        ids.update(
            k
            for k in delta(before, candidate)["nodes"]["deleted"]
            if old_nodes[k].node_type not in PHYSICAL
        )
        # Review a changed business assertion itself. Unchanged endpoints are
        # context, not invitations to re-audit their entire old descriptions.
        for key in impact["semantic_changed_ids"]:
            for edges in (old_edges, new_edges):
                if key in edges:
                    ids.add(key)
        for check in checks:
            if check["kind"] in {"related_decisions", "source_selection_variants"}:
                ids.update(signal_targets(check, candidate))
        result = ids & (old_edges.keys() | new_edges.keys())
        result |= {
            k
            for k in ids & (old_nodes.keys() | new_nodes.keys())
            if (new_nodes.get(k) or old_nodes[k]).node_type not in PHYSICAL
        }
        return result

    def _execute(self, task, payload):
        engine, ctx = self.engine, self.ctx
        scope = set(task.targets)
        initial_scope = set(scope)
        completed, impacts, unresolved = set(), set(), []
        pending = []
        pending_review_ids = set()
        new_prefix = f"generated::{uuid4().hex}::"
        checks = {s["id"]: s for s in task.checks}

        def expand(ids, reason):
            nodes, edges = _indexes(engine.current)
            if not reason.strip() or not set(ids) <= nodes.keys() | edges.keys():
                raise ValueError(
                    "Extension requires known current objects and a concrete required change"
                )
            if len((scope | set(ids)) - initial_scope) > 40:
                raise ValueError(
                    "Local scope extension exceeded; keep unrelated problems separate"
                )
            scope.update(ids)
            added_checks = []
            for key in ids:
                if (
                    key in nodes
                    and nodes[key].node_type not in PHYSICAL
                    and key not in checks
                ):
                    checks[key] = {"id": key, "kind": "node_content", "node_ids": [key]}
                    added_checks.append(checks[key])
            return {
                "writable_ids": sorted(scope),
                "added_inspection_checks": added_checks,
                "objects": [
                    nodes[k].model_dump(mode="json")
                    if k in nodes
                    else edges[k].model_dump(mode="json")
                    for k in ids
                ],
            }

        for attempt in range(engine.max_rounds + 1):
            base, lineage = engine.current, engine.lineage
            task.targets = set(scope)
            ctx.active_task, ctx.active_batch = task, None
            ctx.extend_task = expand
            ctx.workspace_revision = engine._revision
            ctx.candidate = base
            payload = self._packet(task, before=base)
            payload.update(
                new_id_prefix=new_prefix,
                feedback=unresolved,
            )
            checks = {check["id"]: check for check in payload["inspection_signals"]}
            if attempt:
                payload["retry_units"] = [
                    u.model_dump(mode="json", exclude={"checked_node_ids"})
                    for u in pending
                ]
            staged_units = []

            def validate_proposal(
                result,
                base=base,
                lineage=lineage,
                unresolved=tuple(unresolved),
                pending=tuple(pending),
                payload=payload,
                scope=scope,
                checks=checks,
                pending_review_ids=frozenset(pending_review_ids),
            ):
                nonlocal staged_units
                if result.failure:
                    raise ValueError(result.failure)
                conclusions = [
                    c for u in result.units for c in u.patch.inspection_checks
                ]
                if {c.inspection_id for c in conclusions} != checks.keys() or len(
                    conclusions
                ) != len(checks):
                    found = [c.inspection_id for c in conclusions]
                    raise ValueError(
                        "patch.inspection_checks must answer the supplied registry once; "
                        f"missing={sorted(checks.keys() - set(found))}, "
                        f"unexpected={sorted(set(found) - checks.keys())}, "
                        f"duplicates={sorted(k for k in set(found) if found.count(k) > 1)}"
                    )
                for conclusion in conclusions:
                    if conclusion.conclusion == "failed":
                        raise ValueError(
                            "Inspection incomplete: retrieve required context or report failure"
                        )
                    if (
                        checks[conclusion.inspection_id]["kind"]
                        in {"related_decisions", "source_selection_variants"}
                        and conclusion.decision_relation is None
                    ):
                        raise ValueError(
                            "Decision inspection needs compatible/conditional/conflict/different_decision"
                        )
                units = [
                    CheckedUnit(
                        unit_id=unit.unit_id,
                        patch=unit.patch,
                        checked_node_ids=tuple(
                            c.inspection_id
                            for c in unit.patch.inspection_checks
                            if checks[c.inspection_id]["kind"] == "node_content"
                        ),
                    )
                    for unit in result.units
                ]
                grouped = group_units(units, base, result.coupled_units)
                next_units = []
                local = LocalTask(
                    task.id, task.phase, set(scope), checks=list(checks.values())
                )
                for unit in grouped:
                    batch, staged = self._stage(base, lineage, local, unit)
                    for conclusion in unit.patch.inspection_checks:
                        if conclusion.decision_relation == "conflict" and not any(
                            k in _indexes(batch.preview)[0]
                            and len(_indexes(batch.preview)[0][k].alternatives) > 1
                            for k in conclusion.result_ids
                        ):
                            owners = {
                                k: [a.alternative_id for a in n.alternatives]
                                for k, n in _indexes(batch.preview)[0].items()
                                if n.alternatives
                                and k
                                in signal_targets(
                                    checks[conclusion.inspection_id], batch.preview
                                )
                            }
                            raise ValueError(
                                f"Inspection {conclusion.inspection_id}: result_ids must name current conflict OWNER NODE IDs, not alternative labels. "
                                f"Got {list(conclusion.result_ids)}; local owners and their branches: {owners}. "
                                "Fix the references, not the business decision. Do not dissolve a saved conflict into ordinary rules to avoid this reference error."
                            )
                    if batch.preview == base and any(
                        c.conclusion == "changed" for c in unit.patch.inspection_checks
                    ):
                        raise ValueError(
                            "A changed conclusion requires an actual graph repair"
                        )
                    affected = affected_query_roots(base, batch.preview)
                    changed_ids = {
                        key
                        for values in delta(base, batch.preview).values()
                        for keys in values.values()
                        for key in keys
                    }
                    for conclusion in unit.patch.inspection_checks:
                        check = checks[conclusion.inspection_id]
                        if conclusion.conclusion == "changed" and not (
                            (affected | changed_ids) & signal_targets(check, base)
                        ):
                            raise ValueError(
                                f"Inspection {conclusion.inspection_id} is not repaired by unrelated edits"
                            )
                        if check["kind"] == "abstract_predicate":
                            from cortex.schema.relation_vocabulary import (
                                require_concrete_relation,
                            )

                            for key in check.get("edge_ids", ()):
                                if key in _indexes(batch.preview)[1]:
                                    require_concrete_relation(
                                        _indexes(batch.preview)[1][key].edge_type
                                    )
                    review_ids = self._review_ids(
                        base,
                        batch.preview,
                        local,
                        [checks[c.inspection_id] for c in unit.patch.inspection_checks],
                    )
                    if unresolved and not review_ids:
                        # An empty patch cannot close a rejected semantic unit.
                        review_ids.update(pending_review_ids)
                    next_units.append((unit, batch, staged, review_ids))
                # Validate the combined candidate as well as each independent unit.
                combined = RepairUnit(
                    unit_id="combined", patch=_combine([u for u, *_ in next_units])
                )
                self._stage(base, lineage, local, combined)
                staged_units = next_units

            def transactional_validate(result, validate=validate_proposal, base=base):
                try:
                    validate(result)
                finally:
                    # Unit previews never become the writer's accepted view.
                    ctx.candidate = base

            ctx.output_validator = transactional_validate
            engine.open_tasks = [
                {"id": task.id, "phase": "reconcile", "targets": sorted(scope)}
            ]
            engine._persist(self.run_id)
            proposal = engine.agent.invoke("reconcile", ctx, payload)
            if not staged_units:
                transactional_validate(proposal)
            reviews = self._assess(task, base, staged_units, checks, unresolved)
            rejected, feedback, coupling, repair_refs = [], [], [], set()
            pending_review_ids = set()
            for cross in getattr(reviews, "cross_unit_issues", ()):
                coupling.append(cross.unit_ids)
                feedback.append(cross.issue)
            blocked = {k for group in coupling for k in group}
            by_id = {r.unit_id: r.review for r in getattr(reviews, "results", ())}
            for unit, batch, staged, review_ids in staged_units:
                review = by_id.get(unit.unit_id)
                if unit.unit_id in blocked or (review and review.verdict != "accept"):
                    rejected.append(unit)
                    pending_review_ids.update(review_ids)
                    if review:
                        if review.verdict == "revisit_identity":
                            raise IntegrationIncomplete(
                                "Reconciliation found an identity counterexample"
                            )
                        feedback.extend(review.issues)
                        repair_refs.update(r.id for r in review.issue_refs)
                    continue
                # Restage against earlier accepted siblings. Graph and ledger are
                # accepted together; failed candidates never enter either store.
                local = LocalTask(task.id, "reconcile", set(scope))
                accepted, accepted_lineage = self._stage(
                    engine.current, engine.lineage, local, unit
                )
                before = engine.current
                engine.current, engine.lineage = accepted.preview, accepted_lineage
                engine._mark_reviewed(review_ids)
                engine._revision += 1
                engine.history.append(
                    {
                        "task_id": task.id,
                        "unit_id": unit.unit_id,
                        "phase": "reconcile",
                        "revision": engine._revision,
                        "attempt": attempt,
                        "delta": delta(before, engine.current),
                        "node_review_ids": sorted(
                            review_ids & _indexes(engine.current)[0].keys()
                        ),
                        "edge_review_ids": sorted(
                            review_ids
                            & (
                                _indexes(before)[1].keys()
                                | _indexes(engine.current)[1].keys()
                            )
                        ),
                        "inspection_ids": [
                            c.inspection_id for c in unit.patch.inspection_checks
                        ],
                        "inspection_checks": [
                            c.model_dump(mode="json")
                            for c in unit.patch.inspection_checks
                        ],
                        "checked_node_ids": list(unit.checked_node_ids),
                        "assessment": "accepted" if review else "not_required",
                        "changes": [
                            c.model_dump(mode="json") for c in accepted.result.changes
                        ],
                    }
                )
                completed.update(unit.checked_node_ids)
                for key in set(unit.checked_node_ids) | review_ids:
                    if key in _indexes(engine.current)[0]:
                        self.checked_nodes[key] = definition_stamp(engine.current, key)
                impacts |= affected_query_roots(before, engine.current)
                for c in unit.patch.inspection_checks:
                    check = checks[c.inspection_id]
                    if check["kind"] != "node_content":
                        self.checked_signals[c.inspection_id] = _stamp(
                            engine.current, signal_targets(check, engine.current), check
                        )
                    checks.pop(c.inspection_id, None)
                engine.open_tasks = []
                self._save_coverage()
                engine._persist(self.run_id)
            if not rejected:
                return completed | (impacts & scope), impacts
            pending = group_units(rejected, engine.current, coupling)
            unresolved = feedback
            # Restrict the next invocation to failed units and their actual
            # direct effects, never all successful siblings or earlier history.
            scope = {k for u in pending for k in u.checked_node_ids}
            scope |= {
                k
                for u in pending
                for kind, k in _footprint(u, engine.current)[0]
                if kind in {"node", "edge"}
            }
            scope |= repair_refs
            scope |= pending_review_ids
            nodes, edges = _indexes(engine.current)
            # New objects remain complete in retry_units, not missing accepted
            # targets. Feedback-referenced current objects become routine roots.
            scope &= nodes.keys() | edges.keys()
            task.checks = [c for c in checks.values() if c["kind"] != "node_content"]
        raise IntegrationIncomplete(
            f"{task.id} failed reconciliation assessment; graph not published"
        )

    def _assess(self, task, base, staged_units, checks, feedback):
        from cortex.capabilities.graph_merge.llm.incremental import BatchNodeReview

        selected = [(u, b, lineage, ids) for u, b, lineage, ids in staged_units if ids]
        if not selected:
            return BatchNodeReview(results=())
        union = RepairUnit(unit_id="review", patch=_combine([u for u, *_ in selected]))
        joint, joint_lineage = self._stage(base, self.engine.lineage, task, union)
        ids = set().union(*(ids for *_, ids in selected))
        reviewtask = LocalTask(
            task.id + "-node-review",
            "node_assess",
            ids,
            checks=[
                checks[c.inspection_id]
                for u, *_ in selected
                for c in u.patch.inspection_checks
            ],
            feedback=feedback,
        )
        payload = self._packet(
            reviewtask, joint.preview, before=base, origins=joint_lineage
        )
        payload.pop("reconciliation_batch", None)
        payload["review_units"] = []
        self.ctx.review_candidates = {}
        for unit, batch, lineage, ids in selected:
            local = LocalTask(reviewtask.id, "node_assess", ids)
            packet = self._packet(local, batch.preview, before=base, origins=lineage)
            payload["review_units"].append(
                {
                    "unit_id": unit.unit_id,
                    "reviewed_node_ids": packet["reviewed_node_ids"],
                    "reviewed_deleted_ids": packet["reviewed_deleted_ids"],
                    "reviewed_edge_ids": sorted(
                        ids
                        & (_indexes(base)[1].keys() | _indexes(batch.preview)[1].keys())
                    ),
                    "rationale_review_ids": packet["rationale_review_ids"],
                    "decision_review_ids": packet["decision_review_ids"],
                    "actual_changes": packet["actual_changes"],
                }
            )
            self.ctx.review_candidates[unit.unit_id] = batch.preview
        payload["unit_patches"] = [
            {
                "unit_id": u.unit_id,
                "edits": [
                    {
                        "operation": e.operation,
                        "id": (
                            getattr(e, "node", None) or getattr(e, "edge", None) or {}
                        ).get("id")
                        or getattr(e, "node_id", None)
                        or getattr(e, "edge_id", None),
                    }
                    for e in u.patch.edits
                ],
                "conflict_owners": [c.node_id for c in u.patch.conflicts],
                "changes": [c.model_dump(mode="json") for c in u.patch.changes],
            }
            for u, *_ in selected
        ]
        changed = delta(base, joint.preview)
        payload["accepted_before"] = {
            kind: [
                obj.model_dump(mode="json")
                for obj in getattr(base.graph, kind)
                if obj.id in set(changed[kind]["updated"] + changed[kind]["deleted"])
            ]
            for kind in ("nodes", "edges")
        }
        self.ctx.candidate = joint.preview
        self.ctx.active_batch = None
        self.ctx.output_validator = None
        result = self.engine.agent.invoke("node_assess", self.ctx, payload)
        return result
