"""One fixed-base local transaction, independently reviewed before acceptance."""
from copy import deepcopy
from dataclasses import replace

from cortex.capabilities.graph_merge.integration.batch import (
    ActiveBatch,
    BatchResult,
    delta,
)
from cortex.capabilities.graph_merge.integration.compiler import Preview
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import IntegrationProposal
from cortex.capabilities.graph_merge.integration.patch import (
    CompiledPatch,
    MergePatch,
    compile_patch,
    identity_obligations,
)
from cortex.capabilities.graph_merge.integration.provenance import (
    validate_rewrite_sources,
)
from cortex.capabilities.graph_merge.integration.review import Review, validate_review
from cortex.capabilities.graph_merge.integration.task_context import (
    LocalTask,
    affected_query_roots,
    build_task_context,
    semantic_review_targets,
)
from cortex.capabilities.graph_merge.integration.task_state import (
    TaskState,
    build_contract_versions,
    canonical_hash,
)
from cortex.schema import GraphState


def review_basis(context, review, base, candidate):
    """Pin prior Review premises without relabelling old candidate as current."""
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.graph_merge.integration.evidence_refs import (
        compact_attribution,
    )
    from cortex.capabilities.graph_merge.integration.review import graph_view
    records = []
    refs = dict.fromkeys(ref for issue in review.issues for ref in (*issue.refs, *issue.basis))
    for ref in refs:
        nodes, edges, graph = graph_view(context, ref.view, before=base, candidate=candidate)
        if ref.kind == 'contract':
            from cortex.schema.relation_contracts import effective_schema
            obj = effective_schema(context.schema, graph).edge_type_index().get(ref.id)
        else:
            obj = (nodes if ref.kind == 'node' else edges).get(ref.id)
        if obj is None:continue
        value=obj.model_dump(mode='json')
        if ref.kind=='node' and obj.alternatives:
            value['alternatives']=[{'alternative_id':a.alternative_id,
                'content':GraphManager.resolve_alternative(obj,a).model_dump(mode='json'),
                'sources':[s.model_dump(mode='json') for s in a.sources]} for a in obj.alternatives]
        if ref.kind!='contract':value=compact_attribution([value],context)[0]
        records.append({'view':ref.view,'kind':ref.kind,'id':ref.id,'record':value})
    return ({'candidate_hash': canonical_hash(candidate), 'records': records},) if records else ()


def accepted_signal_checks(context, candidate, checks):
    """Bind detector-owned membership to the independently accepted AFTER.

    A repair may remove some edges while the same triangle remains. Persisting
    the trigger's old edge list makes the very next unchanged scan look new.
    Explicit requirements and node-stage obligations are never removed here.
    """
    from cortex.capabilities.graph_merge.integration.signals import inspection_signals
    from cortex.capabilities.graph_merge.integration.signal_premises import normalize_signal
    from cortex.capabilities.graph_merge.llm.reconciliation_prompts import SIGNALS
    if not any(check.get('kind') in SIGNALS for check in checks):
        return deepcopy(checks)
    detected = {row['id']: row for row in inspection_signals(
        context, Preview(candidate, {}, {}, []))}
    result = []
    for check in checks:
        current = detected.get(check.get('id'))
        if check.get('kind') in SIGNALS and current is not None:
            if current['kind'] != check['kind']:
                raise ValueError('Accepted signal identity changed kind')
            # Keep controller/operator requirements not owned by the detector.
            result.append(normalize_signal({**deepcopy(check), **deepcopy(current)}))
        else:
            # Disappeared signals remain historical receipts, not new tasks;
            # immutable explicit obligations keep their exact requirements.
            result.append(deepcopy(check))
    return result


def run_task(engine, task, run_id, *, aliases=None):
    ctx, base = engine.ctx, engine.current
    node_local = task.phase == 'consolidate' and getattr(engine.agent, 'node_local_contract', False)
    canonical_relation = task.phase == 'reconcile' and getattr(engine.agent, 'reconciliation_contract', False)
    from cortex.capabilities.graph_merge.integration.reconciliation_contract import DraftDocument, AssessmentRecord
    review_model = AssessmentRecord if canonical_relation else Review
    versions = getattr(engine, 'contract_versions', None) or build_contract_versions(ctx.schema)
    engine.contract_versions = versions
    saved = getattr(engine, 'task_state', None)
    if isinstance(saved, dict):
        saved = TaskState.from_dict(saved)
    if saved is not None and saved.task_id == task.id and saved.phase == task.phase:
        if saved.base_hash != canonical_hash(base) or saved.versions != versions:
            raise ValueError('Unfinished task has a different accepted base or contract')
        state = saved
    elif saved is not None and saved.stage != 'committed':
        raise ValueError('A different unfinished task must be resumed or explicitly migrated first')
    else:
        state = TaskState.capture(base, task_id=task.id, phase=task.phase, versions=versions)
    engine.task_state = state
    if state.stage == 'blocked':
        raise IntegrationIncomplete('Blocked task requires explicit context repair; Writer was not restarted')
    ctx.task_before, ctx.active_task = base, task
    ctx.task_identity_groups = identity_obligations(base, aliases)
    ctx.node_local_compile = node_local
    ctx.node_writer_followups = ()
    ctx.node_assessment_followups = ()
    ctx.node_local_raw = None
    ctx.node_structural_diagnostics = ()
    if node_local and len(ctx.task_identity_groups) != 1:
        raise ValueError('Node-local runtime requires one complete live identity group; migrate the saved content plan explicitly')
    ctx.task_visible_ids = set()
    def sync_draft_context():
        ctx.active_draft = deepcopy(state.draft)
        ctx.task_draft_id = state.draft_id
        ctx.task_draft_errors = state.issues
        ctx.task_draft_errors_draft_id = state.issues_draft_id
        ctx.task_candidate_draft_id = state.candidate_draft_id

    sync_draft_context()
    engine.open_tasks = [{'id': task.id, 'phase': task.phase, 'targets': sorted(task.targets),
                          'groups': task.groups, 'checks': task.checks, 'feedback': task.feedback,
                          'aliases': aliases or {}}]
    compiled = None
    payload = {}

    def guard_node_structural_review(review):
        """An Assessment cannot authorize a known-invalid pending self-edge."""
        diagnostics = getattr(ctx, 'node_structural_diagnostics', ())
        if not node_local or review.verdict != 'accept' or not diagnostics:
            return review
        return Review.model_validate({'verdict': 'revise', 'issues': [
            {'refs': [{'kind': 'edge', 'id': row['edge_id'], 'view': 'candidate'}],
             'basis': [{'kind': 'edge', 'id': row['edge_id'], 'view': 'before'}],
             'problem': 'Controller structural check: this identity integration maps both endpoints '
                 f"of {row['edge_id']} to {row['owner_id']}, leaving a schema-forbidden is_a self-relation. "
                 'The pending candidate cannot be accepted while this relation remains.',
             'required_outcome': row.get('required_outcome') or
                 'Delete the relation only if its assertion is erroneous or redundant and preserve '
                 'its supported knowledge and provenance; otherwise keep the mistaken group member '
                 'as an unchanged standalone node and revise the integration.'}
            for row in diagnostics]})

    def persist():
        sync_draft_context()
        engine.task_state = state
        engine.open_tasks = [{'id': task.id, 'phase': task.phase, 'targets': sorted(task.targets),
                              'groups': task.groups, 'checks': task.checks, 'feedback': task.feedback,
                              'aliases': aliases or {}}]
        engine._persist(run_id)

    def record_draft(raw):
        nonlocal state
        state = state.with_draft(raw)
        sync_draft_context()
        persist()

    def record_error(error):
        nonlocal state
        from cortex.capabilities.graph_merge.llm.task_agent import failure_feedback
        feedback = failure_feedback(error)
        feedback.setdefault('stage', getattr(ctx, 'task_failure_stage', 'patch_compile'))
        state = state.with_issues([feedback], stage='validating')
        sync_draft_context()
        persist()

    def load_task_recovery(key):
        return deepcopy(state.agent_recovery.get(key))

    def record_task_recovery(key, record):
        nonlocal state
        state = state.with_agent_recovery(key, record)
        persist()

    def compilation_binding(value):
        evidence = sorted((row.model_dump(mode='json') for row in ctx.evidence),
                          key=lambda row: (row['id'], canonical_hash(row)))
        return canonical_hash({'before': state.base_hash, 'schema': ctx.schema.model_dump(mode='json'),
            'draft': value.model_dump(mode='json', exclude_unset=True),
            'evidence': evidence, 'versions': state.versions.to_dict()})

    def compile_value(value, *, save=True, preview=False):
        nonlocal state, compiled
        raw = (deepcopy(ctx.node_local_raw) if node_local and ctx.node_local_raw is not None
               else value.model_dump(mode='json', exclude_unset=True))
        if save:
            record_draft({'backend_batch': raw} if isinstance(value, BatchResult) else raw)
        if node_local:
            from cortex.capabilities.graph_merge.llm.node_contract import normalize_node_result
            from cortex.capabilities.graph_merge.llm.node_context import build_node_packet
            local_packet = build_node_packet(ctx, {**payload, 'phase': 'consolidate'}, before=base)
            value, writer_followups = normalize_node_result('writer', raw, payload=local_packet, base=base)
            ctx.node_writer_followups = writer_followups
            state = replace(state, relation_followups=tuple(writer_followups))
        old_candidate, old_batch = ctx.candidate, getattr(ctx, 'active_batch', None)
        old_diagnostics = getattr(ctx, 'node_structural_diagnostics', ())
        coupled_attempt = False
        try:
            if isinstance(value, DraftDocument):
                if not canonical_relation:
                    raise TypeError('Canonical relation drafts belong to reconciliation tasks')
                from cortex.capabilities.graph_merge.integration.reconciliation_compile import compile_draft
                from cortex.capabilities.graph_merge.llm.reconciliation_agent import prepare_runtime
                prepare_runtime(engine.agent, 'reconcile', ctx, payload, draft=value)
                result = compile_draft(ctx, base, value, targets=task.targets, aliases=aliases, payload=payload)
            elif isinstance(value, MergePatch):
                try:
                    result = compile_patch(ctx, base, value, targets=task.targets, aliases=aliases, payload=payload)
                except Exception as error:
                    from cortex.capabilities.graph_merge.integration.node_relations import NodeRelationCoordinationRequired
                    if not node_local or not isinstance(error, NodeRelationCoordinationRequired):
                        raise
                    coupled_attempt = True
                    from cortex.capabilities.graph_merge.integration.node_relation_repair import coordinate_node_relations
                    def save_repair(record):
                        nonlocal state
                        state = replace(state, coupled_relation={'draft_id': state.draft_id, **record})
                        persist()
                    result, _ = coordinate_node_relations(engine, task, base, error,
                        saved=state.coupled_relation, save_record=save_repair, run_id=run_id)
            elif isinstance(value, BatchResult):
                # Mechanical injected backends share the transaction gate, not
                # an additional model-visible production output protocol.
                backend = ActiveBatch(ctx, base, targets=task.targets, aliases=aliases)
                ctx.active_batch = backend
                ctx.candidate = backend.seed
                from cortex.capabilities.graph_merge.integration.evidence_refs import (
                    EvidencePresentation,
                )
                from cortex.capabilities.graph_merge.integration.proposal_scope import (
                    ProposalScope,
                )
                scope = ProposalScope(ctx, payload, EvidencePresentation(ctx, payload.get('evidence', [])))
                scope.visible.update(getattr(ctx, 'task_visible_ids', ()))
                backend.prepare_submission = scope.prepare
                backend.submit_atomic(value)
                result = CompiledPatch(backend.preview, backend.seed, (), backend.result.changes,
                    aliases or {}, backend.edge_aliases, backend.scope_events, backend)
            else:
                raise TypeError('Writer must return a MergePatch')
            ctx.node_structural_diagnostics = tuple(getattr(result.batch, 'node_structural_diagnostics', ()))
            staged = LineageIndex.restore(ctx, engine.lineage.snapshot())
            staged.observe(base, result.integrated, result.integration_changes,
                           node_aliases=result.node_aliases, edge_aliases=result.edge_aliases)
            staged.observe(result.integrated, result.state, result.changes,
                node_aliases=getattr(result, 'coupled_relation_node_aliases', None),
                edge_aliases=getattr(result, 'coupled_relation_edge_aliases', None))
            validate_rewrite_sources(ctx, IntegrationProposal(changes=staged.source_changes(result.state)),
                                    Preview(result.state, staged.node_map, staged.edge_map, []))
            result.batch.lineage_preview = staged
            compiled = result
            state = state.with_candidate(result.state, submitted=not preview,
                compilation_binding=compilation_binding(value) if canonical_relation else None)
            sync_draft_context()
            ctx.candidate = result.state
            ctx.last_compiled_patch = result
            if save:
                persist()
            return result
        except Exception as error:
            ctx.candidate, ctx.active_batch = old_candidate, old_batch
            ctx.node_structural_diagnostics = old_diagnostics
            if coupled_attempt and isinstance(error, (ValueError, TypeError, KeyError)):
                from cortex.capabilities.graph_merge.llm.task_agent import failure_feedback
                state = replace(state, coupled_relation={**(state.coupled_relation or {}),
                    'draft_id': state.draft_id, 'controller_error': failure_feedback(error)})
                persist()
                raise IntegrationIncomplete('Node relation coordination or its provenance validation stopped; retained the node draft and relation checkpoint without restarting Node Writer') from error
            from cortex.capabilities.graph_merge.integration.proposal_scope import (
                scope_error,
            )
            required = scope_error(error).get('required_context') or {}
            ctx.task_visible_ids |= {r['id'] for key in ('nodes', 'edges') for r in required.get(key, [])}
            if isinstance(error, (ValueError, TypeError, KeyError)):
                record_error(error)
            raise

    def get_task_compilation(value):
        """Restore only an already compiled exact head, without invoking a model."""
        nonlocal state, compiled
        if (not canonical_relation or state.stage not in {'compiled_unsubmitted', 'assessing'}
                or state.draft is None or state.last_valid_candidate is None
                or state.candidate_draft_id != state.draft_id
                or DraftDocument.model_validate(state.draft) != DraftDocument.model_validate(value)
                or state.compilation_binding != compilation_binding(value)):
            return None
        if compiled is not None and canonical_hash(compiled.state) == state.candidate_hash:
            return compiled
        previous = state
        old_candidate, old_batch = ctx.candidate, getattr(ctx, 'active_batch', None)
        old_compiled = getattr(ctx, 'last_compiled_patch', None)
        try:
            restored = compile_value(value, save=False, preview=True)
            if canonical_hash(restored.state) != previous.candidate_hash:
                raise ValueError('Saved preview no longer compiles to its bound candidate')
        except BaseException:
            state = previous
            sync_draft_context()
            ctx.candidate, ctx.active_batch = old_candidate, old_batch
            ctx.last_compiled_patch = old_compiled
            compiled = None
            persist()
            raise
        # Mechanical reconstruction must not grant submission or drop feedback.
        state = previous
        sync_draft_context()
        return restored

    def preview_task_draft(value):
        restored = get_task_compilation(value)
        return restored if restored is not None else compile_value(value, preview=True)

    def record_complete_draft_action(raw):
        nonlocal state
        state = state.with_draft(raw)
        state = state.with_agent_recovery('complete_draft_action', {'draft_id':state.draft_id})
        persist()

    def has_pending_draft_action(value):
        intent=state.agent_recovery.get('complete_draft_action')
        return bool(intent and intent.get('draft_id')==state.draft_id
            and state.draft is not None and DraftDocument.model_validate(state.draft)==value)

    def accept_compiled_draft(value):
        nonlocal state
        result = get_task_compilation(value)
        if result is None:
            raise ValueError('DRAFT_NOT_COMPILED: edit and compile the current complete draft before submission')
        state = state.with_assessment_handoff().with_agent_recovery('complete_draft_action', None)
        persist()
        return result

    ctx.record_task_draft, ctx.record_task_error = record_draft, record_error
    ctx.load_task_recovery, ctx.record_task_recovery = load_task_recovery, record_task_recovery
    ctx.compile_task_patch = compile_value
    ctx.preview_task_draft, ctx.accept_compiled_draft = preview_task_draft, accept_compiled_draft
    ctx.record_complete_draft_action = record_complete_draft_action
    ctx.has_pending_draft_action = has_pending_draft_action
    ctx.get_task_compilation = get_task_compilation
    # Injected deterministic agents exercise the same compiler/commit gate.
    ctx.output_validator = compile_value
    ctx.candidate = GraphState.model_validate(state.last_valid_candidate) if state.last_valid_candidate else base
    ctx.active_batch = ActiveBatch(ctx, base, targets=task.targets)
    persist()
    for attempt in range(engine.max_rounds + 1):
        current = GraphState.model_validate(state.last_valid_candidate) if state.last_valid_candidate else base
        ctx.candidate = current
        if task.phase == 'reconcile':
            from cortex.capabilities.graph_merge.integration.relation_context import (
                relation_context,
            )
            payload = relation_context(ctx, current, task)
        else:
            from cortex.capabilities.graph_merge.integration.node_context import (
                compact_task_context,
            )
            payload = build_task_context(ctx, current, task, origins=engine.lineage, before=base)
            payload = compact_task_context(payload, ctx, current, task.targets, task.groups, for_review=False)
        payload['task'] = {'id': task.id, 'phase': task.phase, 'base': 'before',
                           'draft_semantics': 'canonical-versioned-edits' if canonical_relation else 'complete-replacement',
                           'stage': state.stage}
        if state.last_valid_candidate is not None:
            payload['task']['candidate_draft_id'] = state.candidate_draft_id
            payload['task']['candidate_matches_draft'] = state.candidate_draft_id == state.draft_id
        payload['identity_plan'] = identity_obligations(base, aliases)
        from cortex.capabilities.graph_merge.integration.source_units import (
            source_unit_manifest,
        )
        node_ids = {n.id for n in base.graph.nodes if n.node_type not in {'table', 'column', 'dimension_value'}}
        payload['source_unit_manifest'] = source_unit_manifest(base, ctx.task_identity_groups,
                                                              node_ids.intersection(task.targets))
        if state.draft is not None:
            payload['draft'] = state.draft
        if state.issues:
            payload['issues'] = list(state.issues)
            payload['issues_draft_id'] = state.issues_draft_id
        if state.draft_id is not None:
            payload['draft_id'] = state.draft_id
        if state.semantic_issues:
            payload['semantic_issues'] = list(state.semantic_issues)
            payload['semantic_issue_basis'] = list(state.semantic_issue_basis)
            if payload.get('feedback') == list(state.semantic_issues):
                payload.pop('feedback')
        ctx.task_semantic_issues = state.semantic_issues
        ctx.task_semantic_issue_basis = state.semantic_issue_basis
        ctx.task_visible_ids = {r['id'] for key in ('nodes', 'candidate_nodes', 'edges', 'candidate_relations')
                                for r in payload.get(key, [])}
        needs_compile = state.draft is not None and (state.stage in {'assessing', 'ready_to_commit'}
            or (node_local and state.coupled_relation is not None and state.coupled_relation.get('draft_id') == state.draft_id))
        if needs_compile:
            # Recompilation verifies saved state; it never reruns the Writer.
            raw = state.draft
            if 'backend_batch' in raw:
                if getattr(engine.agent, 'contract_version', None):
                    raise ValueError('A deterministic backend draft is not a model Patch')
                value = BatchResult.model_validate(raw['backend_batch'])
            elif not node_local:
                value = (DraftDocument if canonical_relation else MergePatch).model_validate(raw)
            if node_local:
                ctx.node_local_raw = raw
                from cortex.capabilities.graph_merge.llm.node_contract import normalize_node_result
                from cortex.capabilities.graph_merge.llm.node_context import build_node_packet
                local_packet = build_node_packet(ctx, {'task_id': task.id, 'phase': 'consolidate'}, before=base)
                value, _ = normalize_node_result('writer', raw, payload=local_packet, base=base)
            previous_state = state
            # Old checkpoints did not persist every read-visible object. A
            # saved, previously compiled draft may request that exact scope
            # again. Restore only compiler-provided visibility, then prove the
            # complete result still equals the saved candidate before review.
            from cortex.capabilities.graph_merge.integration.proposal_scope import (
                ScopeContextRequired,
            )
            restored_scope = set()
            for scope_attempt in range(3):
                try:
                    compile_value(value, save=False)
                    break
                except ScopeContextRequired as error:
                    ids = {row['id'] for key in ('nodes', 'edges') for row in error.context.get(key, [])}
                    if not ids - restored_scope or scope_attempt == 2:
                        raise
                    restored_scope.update(ids)
            if previous_state.candidate_hash is not None and previous_state.candidate_hash != state.candidate_hash:
                state = previous_state
                sync_draft_context()
                ctx.candidate = current
                raise ValueError('Saved draft no longer compiles to the saved candidate')
            # Legacy v3 checkpoints may lack a draft/candidate binding. Only
            # successful recompilation to the exact saved graph can establish
            # that binding; never infer it from the candidate hash alone.
            if previous_state.candidate_hash is not None:
                state = replace(previous_state, candidate_draft_id=state.candidate_draft_id)
            if restored_scope:
                from cortex.capabilities.graph_merge.integration.evidence_refs import (
                    EvidencePresentation,
                )
                from cortex.capabilities.graph_merge.llm.relation_reads import (
                    RelationReads,
                )
                present = {obj.id for objects in (compiled.state.graph.nodes, compiled.state.graph.edges) for obj in objects}
                reader = RelationReads(ctx, EvidencePresentation(ctx), before=base)
                supplied = []
                for view, ids in (('candidate', restored_scope & present), ('before', restored_scope - present)):
                    ordered = sorted(ids)
                    for offset in range(0, len(ordered), 100):
                        selected = ordered[offset:offset + 100]
                        answer = reader.read('items', selected, view=view, limit=100)
                        if answer.get('read_errors') or not answer.get('complete'):
                            raise ValueError('Saved-candidate scope could not be restored completely')
                        supplied.append({'request': {'kind': 'checkpoint_scope', 'view': view, 'ids': selected},
                            'result': answer, 'requested_for_candidate_hash': state.candidate_hash})
                state = replace(state, required_context=(*state.required_context, *supplied))
            sync_draft_context()
            if state != previous_state:
                persist()
        else:
            ctx.output_validator = compile_value
            value = engine.agent.invoke(task.phase, ctx, payload)
            if not (compiled is not None and ctx.last_compiled_patch is compiled and state.stage == 'assessing'):
                compile_value(value)
        candidate = compiled.state
        ctx.output_validator = None
        review = None
        if state.stage == 'ready_to_commit':
            review = review_model.model_validate(state.review)
        elif canonical_relation or candidate != base or identity_obligations(base, aliases) or task.feedback:
            review_phase = 'graph_assess' if task.phase == 'reconcile' else 'node_assess'
            changes = delta(base, candidate)['nodes']
            impact = semantic_review_targets(base, candidate, task.targets)
            assessed_ids = set(task.targets) | set(impact['targets']) | set(changes['created']) | set(changes['updated']) | set(changes['deleted'])
            context_requests = set()
            while True:
                from cortex.capabilities.graph_merge.integration.incremental import (
                    input_fingerprint,
                )
                from cortex.capabilities.graph_merge.llm.packets import (
                    compact_node_packet,
                    premise_fingerprint,
                )

                review_policy = canonical_hash({'prompts': versions.prompts, 'tools': versions.tools,
                                                'review_policy': versions.review_policy})
                # This is a cache provenance check, not an extra presented
                # premise: never reuse observed reads from changed inputs.
                source_fingerprint = input_fingerprint(ctx)
                previous_gate = state.agent_recovery.get('assessment_gate', {})
                eligible_gate = previous_gate if (
                    previous_gate.get('source_fingerprint') == source_fingerprint
                    and previous_gate.get('base_hash') == state.base_hash
                    and previous_gate.get('candidate_hash') == state.candidate_hash
                    and previous_gate.get('policy') == review_policy
                    and previous_gate.get('phase') == review_phase
                    and previous_gate.get('last_review', {}).get('verdict') == 'revise') else None
                ctx.task_required_context = state.required_context
                ctx.task_semantic_issues = state.semantic_issues
                ctx.task_semantic_issue_basis = state.semantic_issue_basis
                review_task = LocalTask(task.id + '-review', review_phase, assessed_ids,
                                        groups=task.groups, checks=task.checks, feedback=task.feedback)
                if review_phase == 'graph_assess':
                    ctx.task_partial_semantic_findings = ()
                    from cortex.capabilities.graph_merge.integration.relation_context import (
                        relation_context,
                    )
                    review_payload = relation_context(ctx, candidate, review_task, before=base, assessment=True)
                    if state.required_context:
                        review_payload['required_context'] = list(state.required_context)
                    if state.semantic_issues:
                        review_payload['semantic_issues'] = list(state.semantic_issues)
                        review_payload['semantic_issue_basis'] = list(state.semantic_issue_basis)
                    known_packets = eligible_gate.get('actual_packets', []) if eligible_gate else []
                    known_reads = known_packets[0]['reads'] if len(known_packets) == 1 else ()
                    if canonical_relation:
                        from cortex.capabilities.graph_merge.llm.reconciliation_agent import prepare_runtime
                        prepared = prepare_runtime(engine.agent, review_phase, ctx, review_payload, fresh=True)
                        ctx.canonical_assessment_facts = prepared.premise_hash(policy=review_policy)
                        if eligible_gate and ctx.canonical_assessment_facts == eligible_gate.get('premise_fingerprint'):
                            raise IntegrationIncomplete('Unchanged canonical candidate, assessment policy and visible facts; '
                                'retained the rejected candidate without rerunning Assessment')
                    cached = (not canonical_relation and eligible_gate and len(known_packets) == 1 and premise_fingerprint(
                        review_payload, reads=known_reads, policy=review_policy) == eligible_gate.get('premise_fingerprint'))
                    ctx.task_assessment_cache_reused = bool(cached)
                    ctx.task_relevant_facts = deepcopy(known_reads) if cached else ()
                    review = (review_model.model_validate(eligible_gate['last_review']) if cached
                              else engine.agent.invoke(review_phase, ctx, review_payload))
                    ctx.task_assessment_premises = [{'payload': compact_node_packet(review_payload),
                        'reads': deepcopy(getattr(ctx, 'task_relevant_facts', ()))}]
                else:
                    from cortex.capabilities.graph_merge.integration.node_review import (
                        assess_nodes,
                    )
                    review = assess_nodes(engine.agent, ctx, candidate, review_task,
                        origins=compiled.batch.lineage_preview, before=base,
                        earlier_findings=state.semantic_issues,
                        max_characters=engine.node_context_max_characters,
                        previous_gate=eligible_gate, policy=review_policy)
                review = validate_review(review, ctx, before=base, candidate=candidate)
                review = validate_review(guard_node_structural_review(review), ctx,
                                        before=base, candidate=candidate)
                if node_local:
                    combined = {canonical_hash(row): row for row in (*state.relation_followups,
                        *getattr(ctx, 'node_assessment_followups', ()))}
                    state = replace(state, relation_followups=tuple(combined.values()))
                if review.verdict in {'revise', 'revisit_identity'}:
                    state = replace(state, semantic_issue_basis=review_basis(ctx, review, base, candidate))
                elif review.verdict == 'needs_context' and getattr(ctx, 'task_partial_semantic_findings', ()):
                    partial = ctx.task_partial_semantic_findings
                    combined = {canonical_hash(row): row for row in (*state.semantic_issues,
                                *(issue.model_dump(mode='json') for issue in partial))}
                    basis = review_basis(ctx, Review(verdict='revise', issues=partial), base, candidate)
                    state = replace(state, semantic_issues=tuple(combined.values()),
                                    semantic_issue_basis=(*state.semantic_issue_basis, *basis))
                state = state.with_review(review, candidate_hash=state.candidate_hash)
                persist()
                if review.verdict != 'needs_context':
                    break
                if canonical_relation:
                    # The canonical roles already have precise, scoped context
                    # requests. An unavailable premise is not an implicit grant
                    # for the legacy full-graph reader to expand their scope.
                    state = state.with_issues(list(state.issues), stage='blocked')
                    persist()
                    raise IntegrationIncomplete('Canonical Assessment requires an unavailable decisive premise; '
                        'retained the candidate, issues and authorized local context')
                if node_local:
                    # Both node roles may read evidence only. Do not turn a
                    # missing premise into an implicit graph-inspection grant.
                    state = state.with_issues(list(state.issues), stage='blocked')
                    persist()
                    raise IntegrationIncomplete('Node Assessment requires a concrete premise unavailable in its complete local packet; checkpoint retained without expanding graph permissions')
                from cortex.capabilities.graph_merge.integration.evidence_refs import (
                    EvidencePresentation,
                )
                from cortex.capabilities.graph_merge.llm.relation_reads import (
                    RelationReads,
                )
                signature = tuple(issue.model_dump_json() for issue in review.issues)
                if signature in context_requests:
                    state = state.with_issues(list(state.issues), stage='blocked')
                    persist()
                    raise IntegrationIncomplete('Required assessment context was already supplied; candidate retained')
                context_requests.add(signature)
                reader = RelationReads(ctx, EvidencePresentation(ctx), before=base)
                supplied = []
                for issue in review.issues:
                    for ref in issue.refs:
                        answer = reader.read('contracts' if ref.kind == 'contract' else 'items', [ref.id],
                                             fields=list(ref.fields) or None, view=ref.view)
                        if answer.get('missing_ids') or answer.get('read_errors'):
                            state = state.with_issues([{'context_unavailable': answer}], stage='blocked')
                            persist()
                            raise IntegrationIncomplete('Required assessment context is unavailable in the selected snapshot')
                        supplied.append({'request': ref.model_dump(mode='json'), 'result': answer,
                                         'requested_for_candidate_hash': state.candidate_hash})
                combined = {canonical_hash(row): row for row in (*state.required_context, *supplied)}
                state = replace(state, required_context=tuple(combined.values()))
                persist()

        else:
            review = validate_review(Review(verdict='accept'), ctx, before=base, candidate=candidate)
            state = state.with_review(review, candidate_hash=state.candidate_hash)
            persist()
        # Restored ready_to_commit checkpoints take this path too: a cached
        # acceptance never bypasses the current structural publication gate.
        guarded = guard_node_structural_review(review)
        if guarded is not review:
            review = validate_review(guarded, ctx, before=base, candidate=candidate)
            state = replace(state, stage='assessing',
                            semantic_issue_basis=review_basis(ctx, review, base, candidate))
            state = state.with_review(review, candidate_hash=state.candidate_hash)
            task.feedback = [issue.model_dump(mode='json') for issue in review.issues]
            persist()
            # This is the restored-acceptance defense, not another Assessment
            # episode. Return the deterministic structural feedback to Writer.
            continue
        if review.verdict != 'accept':
            if review.verdict == 'revisit_identity':
                raise IntegrationIncomplete('Concrete identity counterexample; rebuild this unpublished plan')
            if review_phase in {'graph_assess', 'node_assess'}:
                from cortex.capabilities.graph_merge.llm.packets import (
                    premise_fingerprint,
                )
                policy = review_policy
                if review_phase == 'graph_assess':
                    premises = (ctx.reconciliation_runtime.premise_hash(policy=policy) if canonical_relation
                        else premise_fingerprint(review_payload,
                            reads=getattr(ctx, 'task_relevant_facts', ()), policy=policy))
                else:
                    # Every actual node-review slice contributes its own
                    # visible facts, not only the final slice's read memory.
                    premises = canonical_hash(sorted({premise_fingerprint(
                        packet['payload'], reads=packet['reads'], policy=policy)
                        for packet in ctx.task_assessment_premises}))
                signature = canonical_hash({'candidate': state.candidate_hash, 'premises': premises,
                    'verdict': review.verdict, 'issues': [issue.model_dump(mode='json') for issue in review.issues]})
                previous = state.agent_recovery.get('assessment_gate', {})
                repeats = previous.get('occurrences', 0) + 1 if previous.get('signature') == signature else 1
                state = state.with_agent_recovery('assessment_gate', {
                    'signature': signature, 'candidate_hash': state.candidate_hash,
                    'premise_fingerprint': premises, 'policy': policy, 'occurrences': repeats,
                    'phase': review_phase, 'base_hash': state.base_hash,
                    'source_fingerprint': source_fingerprint,
                    'actual_packets': deepcopy(ctx.task_assessment_premises),
                    'last_review': review.model_dump(mode='json'),
                    'cache_reused': getattr(ctx, 'task_assessment_cache_reused', False)})
                persist()
                if repeats >= 2:
                    raise IntegrationIncomplete('Unchanged candidate, assessment issues and supplied facts; '
                                                'task checkpoint retained')
            task.feedback = [issue.model_dump(mode='json') for issue in review.issues]
            persist()
            continue
        if node_local:
            # Unlike a validation-only preview, an accepted graph must satisfy
            # the real schema. This also protects restored accepted reviews.
            from cortex.schema.validation import validate_graph_state
            validate_graph_state(candidate, ctx.schema)
        backup = {key: getattr(engine, key, None) for key in
                  ('lineage', 'current', '_revision', '_storage_revision', 'open_tasks',
                   'pending_relation_work', 'pending_review', 'relation_followups')}
        backup['signal_receipts'] = deepcopy(getattr(engine, 'signal_receipts', []))
        backup['history'] = list(engine.history)
        backup['reviewed_sources'] = deepcopy(engine.reviewed_sources)
        try:
            engine.lineage = compiled.batch.lineage_preview
            engine.current = candidate
            review_ids = set(semantic_review_targets(base, candidate, task.targets)['targets']) | set(task.targets)
            engine._mark_reviewed(review_ids)
            engine._revision += 1
            engine.history.append({'task_id': task.id, 'phase': task.phase, 'revision': engine._revision,
                'delta': delta(base, candidate), 'extensions': compiled.extensions,
                'node_review_ids': sorted(review_ids), 'assessment': ('graph_assess' if task.phase == 'reconcile' else 'node_assess') if (canonical_relation or candidate != base or identity_obligations(base, aliases) or task.feedback) else 'skipped_no_change',
                'changes': [c.model_dump(mode='json') for c in (*compiled.integration_changes, *compiled.changes)]})
            if canonical_relation:
                from cortex.capabilities.graph_merge.integration.signal_receipts import policy_hash, record_acceptance
                from cortex.capabilities.graph_merge.integration.signal_premises import normalize_signal
                accepted_checks = accepted_signal_checks(ctx, candidate, task.checks)
                accepted_task = replace(task, checks=accepted_checks)
                engine.history[-1].update(candidate_hash=canonical_hash(candidate),
                    reconciliation_receipt_policy_hash=policy_hash(engine),
                    reconciliation_check_hashes=[canonical_hash(normalize_signal(c)) for c in accepted_checks])
                if accepted_checks != task.checks:
                    engine.history[-1]['reconciliation_trigger_check_hashes'] = [
                        canonical_hash(normalize_signal(c)) for c in task.checks]
                # Save accepted-after receipts in the SAME transaction as graph,
                # lineage and history, including accepted empty drafts.
                record_acceptance(engine, accepted_task, state, engine.history[-1])
            engine.open_tasks = []
            engine.pending_relation_work = engine.pending_review = None
            if node_local and state.relation_followups:
                queue = list(getattr(engine, 'relation_followups', ()))
                for row in state.relation_followups:
                    item = {'task_id': task.id, 'draft_id': state.draft_id,
                        'candidate_hash': state.candidate_hash,
                        'owner_ids': [group['owner_id'] for group in ctx.task_identity_groups],
                        'edge_aliases': {key: getattr(compiled, 'final_edge_aliases', compiled.edge_aliases).get(key, key) for key in row['edge_ids']}, **row}
                    item['id'] = canonical_hash(item)
                    if not any(old['id'] == item['id'] for old in queue):
                        queue.append(item)
                engine.relation_followups = queue
            engine.task_state = replace(state, stage='committed')
            engine._persist(run_id)
        except BaseException:
            for key, old in backup.items():
                setattr(engine, key, old)
            engine.task_state = state
            raise
        engine.task_state = None
        ctx.compile_task_patch = ctx.output_validator = None
        impacts = affected_query_roots(base, candidate)
        if task.phase == 'reconcile':
            impacts -= task.targets | set(delta(base, candidate)['nodes']['created'])
        return impacts
    raise IntegrationIncomplete(f'{task.id} failed independent assessment; accepted graph unchanged')
