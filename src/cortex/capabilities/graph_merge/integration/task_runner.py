"""One fixed-base local transaction, independently reviewed before acceptance."""
from copy import deepcopy
from dataclasses import replace

from cortex.capabilities.graph_merge.integration.batch import ActiveBatch, BatchResult, delta
from cortex.capabilities.graph_merge.integration.compiler import Preview
from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.lineage import LineageIndex
from cortex.capabilities.graph_merge.integration.models import IntegrationProposal
from cortex.capabilities.graph_merge.integration.patch import MergePatch, CompiledPatch, compile_patch, identity_obligations
from cortex.capabilities.graph_merge.integration.provenance import validate_rewrite_sources
from cortex.capabilities.graph_merge.integration.review import Review, validate_review
from cortex.capabilities.graph_merge.integration.task_context import LocalTask, build_task_context, affected_query_roots, semantic_review_targets
from cortex.capabilities.graph_merge.integration.task_state import TaskState, canonical_hash, build_contract_versions
from cortex.schema import GraphState


def review_basis(context, review, base, candidate):
    """Pin prior Review premises without relabelling old candidate as current."""
    from cortex.application.ontology_graph import GraphManager
    from cortex.capabilities.graph_merge.integration.evidence_refs import compact_attribution
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
        if obj is None:
            continue  # A needs_context reference may not be available yet.
        value = obj.model_dump(mode='json')
        if ref.kind == 'node' and obj.alternatives:
            value['alternatives'] = [{'alternative_id': a.alternative_id,
                'content': GraphManager.resolve_alternative(obj, a).model_dump(mode='json'),
                'sources': [s.model_dump(mode='json') for s in a.sources]} for a in obj.alternatives]
        if ref.kind != 'contract':
            value = compact_attribution([value], context)[0]
        records.append({'view': ref.view, 'kind': ref.kind, 'id': ref.id, 'record': value})
    return ({'candidate_hash': canonical_hash(candidate), 'records': records},) if records else ()


def run_task(engine, task, run_id, *, aliases=None):
    ctx, base = engine.ctx, engine.current
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
    ctx.task_visible_ids = set()
    ctx.active_draft = state.draft
    ctx.task_draft_errors = state.issues
    engine.open_tasks = [{'id': task.id, 'phase': task.phase, 'targets': sorted(task.targets),
                          'groups': task.groups, 'checks': task.checks, 'feedback': task.feedback,
                          'aliases': aliases or {}}]
    compiled = None
    last_review = None
    rejected_hash = state.candidate_hash if state.review and state.review.get('verdict') == 'revise' else None
    rejected_issues = list(state.issues)
    payload = {}

    def persist():
        engine.task_state = state
        engine.open_tasks = [{'id': task.id, 'phase': task.phase, 'targets': sorted(task.targets),
                              'groups': task.groups, 'checks': task.checks, 'feedback': task.feedback,
                              'aliases': aliases or {}}]
        engine._persist(run_id)

    def record_draft(raw):
        nonlocal state
        if raw != state.draft:
            state = state.with_draft(raw)
        ctx.active_draft = deepcopy(raw)
        ctx.task_draft_errors = ()
        persist()

    def record_error(error):
        nonlocal state
        from cortex.capabilities.graph_merge.llm.task_agent import failure_feedback
        state = state.with_issues([failure_feedback(error)], stage='validating')
        ctx.task_draft_errors = state.issues
        persist()

    def compile_value(value, *, save=True):
        nonlocal state, compiled
        raw = value.model_dump(mode='json', exclude_unset=True)
        if save:
            record_draft({'backend_batch': raw} if isinstance(value, BatchResult) else raw)
        old_candidate, old_batch = ctx.candidate, getattr(ctx, 'active_batch', None)
        try:
            if isinstance(value, MergePatch):
                result = compile_patch(ctx, base, value, targets=task.targets, aliases=aliases, payload=payload)
            elif isinstance(value, BatchResult):
                # Mechanical injected backends share the transaction gate, not
                # an additional model-visible production output protocol.
                backend = ActiveBatch(ctx, base, targets=task.targets, aliases=aliases)
                ctx.active_batch = backend
                ctx.candidate = backend.seed
                from cortex.capabilities.graph_merge.integration.proposal_scope import ProposalScope
                from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
                scope = ProposalScope(ctx, payload, EvidencePresentation(ctx, payload.get('evidence', [])))
                scope.visible.update(getattr(ctx, 'task_visible_ids', ()))
                backend.prepare_submission = scope.prepare
                backend.submit_atomic(value)
                result = CompiledPatch(backend.preview, backend.seed, (), backend.result.changes,
                    aliases or {}, backend.edge_aliases, backend.scope_events, backend)
            else:
                raise TypeError('Writer must return a MergePatch')
            staged = LineageIndex.restore(ctx, engine.lineage.snapshot())
            staged.observe(base, result.integrated, result.integration_changes,
                           node_aliases=result.node_aliases, edge_aliases=result.edge_aliases)
            staged.observe(result.integrated, result.state, result.changes)
            validate_rewrite_sources(ctx, IntegrationProposal(changes=staged.source_changes(result.state)),
                                    Preview(result.state, staged.node_map, staged.edge_map, []))
            result.batch.lineage_preview = staged
            compiled = result
            state = state.with_candidate(result.state)
            ctx.candidate = result.state
            ctx.last_compiled_patch = result
            if save:
                persist()
            return result
        except Exception as error:
            ctx.candidate, ctx.active_batch = old_candidate, old_batch
            from cortex.capabilities.graph_merge.integration.proposal_scope import scope_error
            required = scope_error(error).get('required_context') or {}
            ctx.task_visible_ids |= {r['id'] for key in ('nodes', 'edges') for r in required.get(key, [])}
            if isinstance(error, (ValueError, TypeError, KeyError)):
                record_error(error)
            raise

    ctx.record_task_draft, ctx.record_task_error = record_draft, record_error
    ctx.compile_task_patch = compile_value
    # Injected deterministic agents exercise the same compiler/commit gate.
    ctx.output_validator = compile_value
    ctx.candidate = GraphState.model_validate(state.last_valid_candidate) if state.last_valid_candidate else base
    ctx.active_batch = ActiveBatch(ctx, base, targets=task.targets)
    persist()
    for attempt in range(engine.max_rounds + 1):
        current = GraphState.model_validate(state.last_valid_candidate) if state.last_valid_candidate else base
        ctx.candidate = current
        if task.phase == 'reconcile':
            from cortex.capabilities.graph_merge.integration.relation_context import relation_context
            payload = relation_context(ctx, current, task)
        else:
            from cortex.capabilities.graph_merge.integration.node_context import compact_task_context
            payload = build_task_context(ctx, current, task, origins=engine.lineage, before=base)
            payload = compact_task_context(payload, ctx, current, task.targets, task.groups, for_review=False)
        payload['task'] = {'id': task.id, 'phase': task.phase, 'base': 'before',
                           'draft_semantics': 'complete-replacement', 'stage': state.stage}
        payload['identity_plan'] = identity_obligations(base, aliases)
        from cortex.capabilities.graph_merge.integration.source_units import source_unit_manifest
        node_ids = {n.id for n in base.graph.nodes if n.node_type not in {'table', 'column', 'dimension_value'}}
        payload['source_unit_manifest'] = source_unit_manifest(base, ctx.task_identity_groups,
                                                              node_ids.intersection(task.targets))
        if state.draft is not None:
            payload['draft'] = state.draft
        if state.issues:
            payload['issues'] = list(state.issues)
        if state.semantic_issues:
            payload['semantic_issues'] = list(state.semantic_issues)
            payload['semantic_issue_basis'] = list(state.semantic_issue_basis)
            if payload.get('feedback') == list(state.semantic_issues):
                payload.pop('feedback')
        ctx.task_semantic_issues = state.semantic_issues
        ctx.task_semantic_issue_basis = state.semantic_issue_basis
        ctx.task_visible_ids = {r['id'] for key in ('nodes', 'candidate_nodes', 'edges', 'candidate_relations')
                                for r in payload.get(key, [])}
        needs_compile = state.draft is not None and state.stage in {'assessing', 'ready_to_commit'}
        if needs_compile:
            # Recompilation verifies saved state; it never reruns the Writer.
            raw = state.draft
            if 'backend_batch' in raw:
                if getattr(engine.agent, 'contract_version', None):
                    raise ValueError('A deterministic backend draft is not a model Patch')
                value = BatchResult.model_validate(raw['backend_batch'])
            else:
                value = MergePatch.model_validate(raw)
            previous_state = state
            compile_value(value, save=False)
            if previous_state.candidate_hash != state.candidate_hash:
                state = previous_state
                ctx.candidate = current
                raise ValueError('Saved draft no longer compiles to the saved candidate')
            state = previous_state
        else:
            ctx.output_validator = compile_value
            value = engine.agent.invoke(task.phase, ctx, payload)
            if not (compiled is not None and ctx.last_compiled_patch is compiled and state.stage == 'assessing'):
                compile_value(value)
        candidate = compiled.state
        ctx.output_validator = None
        review = None
        if rejected_hash == state.candidate_hash:
            state = state.with_issues(rejected_issues)
            persist()
            raise IntegrationIncomplete('Unchanged rejected candidate; prior assessment remains valid')
        if state.stage == 'ready_to_commit':
            review = Review.model_validate(state.review)
        elif candidate != base or identity_obligations(base, aliases) or (task.phase == 'consolidate' and task.feedback):
            review_phase = 'graph_assess' if task.phase == 'reconcile' else 'node_assess'
            changes = delta(base, candidate)['nodes']
            impact = semantic_review_targets(base, candidate, task.targets)
            assessed_ids = set(task.targets) | set(impact['targets']) | set(changes['created']) | set(changes['updated']) | set(changes['deleted'])
            context_requests = set()
            while True:
                ctx.task_required_context = state.required_context
                review_task = LocalTask(task.id + '-review', review_phase, assessed_ids,
                                        groups=task.groups, checks=task.checks)
                if review_phase == 'graph_assess':
                    ctx.task_partial_semantic_findings = ()
                    from cortex.capabilities.graph_merge.integration.relation_context import relation_context
                    review_payload = relation_context(ctx, candidate, review_task, before=base, assessment=True)
                    if state.required_context:
                        review_payload['required_context'] = list(state.required_context)
                    if state.semantic_issues:
                        review_payload['semantic_issues'] = list(state.semantic_issues)
                        review_payload['semantic_issue_basis'] = list(state.semantic_issue_basis)
                    review = engine.agent.invoke(review_phase, ctx, review_payload)
                else:
                    from cortex.capabilities.graph_merge.integration.node_review import assess_nodes
                    review = assess_nodes(engine.agent, ctx, candidate, review_task,
                        origins=compiled.batch.lineage_preview, before=base,
                        earlier_findings=state.semantic_issues,
                        max_characters=engine.node_context_max_characters)
                review = validate_review(review, ctx, before=base, candidate=candidate)
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
                from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
                from cortex.capabilities.graph_merge.llm.relation_reads import RelationReads
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
                        supplied.append({'request': ref.model_dump(mode='json'), 'result': answer})
                combined = {canonical_hash(row): row for row in (*state.required_context, *supplied)}
                state = replace(state, required_context=tuple(combined.values()))
                persist()

        else:
            review = validate_review(Review(verdict='accept'), ctx, before=base, candidate=candidate)
            state = state.with_review(review, candidate_hash=state.candidate_hash)
            persist()
        if review.verdict != 'accept':
            if review.verdict == 'revisit_identity':
                raise IntegrationIncomplete('Concrete identity counterexample; rebuild this unpublished plan')
            key = (state.candidate_hash, tuple(issue.model_dump_json() for issue in review.issues))
            if key == last_review:
                raise IntegrationIncomplete('Unchanged candidate and unresolved issues; task checkpoint retained')
            last_review = key
            if review.verdict == 'revise':
                rejected_hash, rejected_issues = state.candidate_hash, list(state.issues)
            task.feedback = [issue.model_dump(mode='json') for issue in review.issues]
            persist()
            continue
        backup = {key: getattr(engine, key, None) for key in
                  ('lineage', 'current', '_revision', '_storage_revision', 'open_tasks',
                   'pending_relation_work', 'pending_review')}
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
                'node_review_ids': sorted(review_ids), 'assessment': ('graph_assess' if task.phase == 'reconcile' else 'node_assess') if candidate != base else 'skipped_no_change',
                'changes': [c.model_dump(mode='json') for c in (*compiled.integration_changes, *compiled.changes)]})
            engine.open_tasks = []
            engine.pending_relation_work = engine.pending_review = None
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
