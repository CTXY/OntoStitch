"""Checkpointed relation coordination inside one unpublished node transaction."""
from __future__ import annotations

from copy import deepcopy

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.runtime.errors import IntegrationIncomplete
from cortex.capabilities.graph_merge.consolidation.compiler import CompiledPatch, MergePatch, compile_patch
from cortex.capabilities.graph_merge.reconciliation.context import relation_context
from cortex.capabilities.graph_merge.consolidation.review import Review, validate_review
from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def _compose(error, repair):
    """Keep aliases attached to the transition in which their targets exist."""
    result = CompiledPatch(state=repair.state, integrated=error.integrated_state,
        integration_changes=tuple(error.integration_changes),
        changes=(*repair.integration_changes, *repair.changes),
        node_aliases=dict(error.node_aliases), edge_aliases=dict(error.edge_aliases),
        extensions=list(repair.extensions), batch=repair.batch)
    result.coupled_relation_node_aliases = dict(repair.node_aliases)
    live_edges = {edge.id for edge in repair.state.graph.edges}
    # The generic compiler carries identity mappings for every seed edge. A
    # retirement is a Change disposition, not an alias to a vanished object.
    # Passing retired e->e to LineageIndex rejects before it can read Change.
    result.coupled_relation_edge_aliases = {key: target for key, target in repair.edge_aliases.items()
        if key != target or target in live_edges}
    locators = dict(getattr(repair, 'final_edge_aliases', repair.edge_aliases))
    # An explicit one-edge successor is useful for locating retained knowledge,
    # but it does not assert semantic identity and must not enter lineage aliases.
    for change in repair.changes:
        if (len(change.before) == len(change.after) == 1
                and change.before[0].kind == change.after[0].kind == 'edge'
                and change.after[0].id in live_edges):
            locators[change.before[0].id] = change.after[0].id
    result.final_edge_aliases = {key: locators.get(value, value)
                                for key, value in error.edge_aliases.items()}
    result.final_edge_aliases.update(locators)
    return result


def coordinate_node_relations(engine, task, base, error, *, saved=None,
                              save_record, run_id=''):
    """Repair and independently assess a node draft's necessary relations.

    No accepted graph, outer node draft or node recovery binding is overwritten.
    A saved valid relation Patch is recompiled and hash checked, never regenerated.
    Rejected assessments feed back to the same local Writer; every draft remains checkpointed.
    """
    ctx = engine.ctx
    canonical_relation = getattr(engine.agent, 'reconciliation_contract', False)
    from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument, AssessmentRecord
    draft_model = DraftDocument if canonical_relation else MergePatch
    review_model = AssessmentRecord if canonical_relation else Review
    intermediate = error.integrated_state
    intermediate_hash = canonical_hash(intermediate)
    node_draft_id = getattr(ctx, 'task_draft_id', None)
    if not node_draft_id:
        raise ValueError('Relation coordination requires a bound current node draft')
    binding = {'version': 2 if canonical_relation else 1, 'draft_id': node_draft_id,
               'intermediate_hash': intermediate_hash, 'base_hash': canonical_hash(base)}
    if canonical_relation:
        from cortex.capabilities.graph_merge.runtime.state import build_contract_versions
        versions = getattr(engine, 'contract_versions', None) or build_contract_versions(ctx.schema)
        binding['policy'] = canonical_hash(versions.to_dict())
    if saved is not None:
        if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in binding.items()):
            raise ValueError('Saved relation coordination addresses a different node draft or intermediate graph')
        record = deepcopy(saved)
    else:
        record = {**binding, 'stage': 'writing', 'run_id': run_id}
    original = dict(ctx.__dict__)
    recovery_load = original.get('load_task_recovery')
    recovery_store = original.get('record_task_recovery')
    prefix = 'node-relation:' + node_draft_id + ':'
    related = set(error.owner_ids)
    original_edges = {edge.id: edge for edge in base.graph.edges}
    intermediate_edges = {edge.id: edge for edge in intermediate.graph.edges}
    raw_edge_ids = {key for row in error.followups for key in row.get('edge_ids', [])}
    related.update(error.edge_aliases.get(key, key) for key in raw_edge_ids
                   if error.edge_aliases.get(key, key) in intermediate_edges)
    # Stable original edge IDs remain data, not claims that the edge survived.
    original_nodes = {node.id: node for node in base.graph.nodes}
    diagnostics, original_targets, target_map = [], {}, {}
    from cortex.capabilities.graph_merge.evidence import EvidencePresentation
    evidence = EvidencePresentation(ctx)
    for row in error.diagnostics:
        diagnostic = deepcopy(row)
        before_target = diagnostic.pop('before_target', None)
        # The live target is already a complete focus in relation_context;
        # provide its ID, not another duplicate full conflict graph.
        diagnostic.pop('candidate_target', None)
        diagnostic.pop('original_edge_id',None)
        diagnostic.pop('before_target_id',None)
        if before_target is not None:
            diagnostic['evidence_ids']=evidence.ids_for(original_nodes[before_target['id']].sources)
        diagnostics.append(diagnostic)
    obligations = {'owner_ids': list(error.owner_ids),
        'diagnostics': diagnostics, 'relation_followups': list(error.followups),
        'original_targets': [original_targets[key] for key in sorted(original_targets)],
        'target_owner_map': target_map,
        'original_edges': [original_edges[key].model_dump(mode='json', exclude={'sources'})
                           for key in sorted(raw_edge_ids) if key in original_edges],
        'edge_aliases': {key: error.edge_aliases.get(key, key) for key in sorted(raw_edge_ids)},
        'instruction': 'Close every supplied relation obligation before acceptance. Preserve the node draft meaning and original branch restrictions; alternative_ids scopes the source only. Retired original IDs locate knowledge, not live edge copies.'}
    kinds = {'metric_operand_adjacency': 'metric_direct_inputs',
             
             'incoming_target_conflict_scope': 'target_alternative_scope'}
    checks = [{'id': 'node-obligation:' + canonical_hash(row)[:24],
        'kind': kinds.get(row.get('code'), 'node_relation_followup'), 'mode': 'required',
        'node_ids': sorted(error.owner_ids),
        'edge_ids': sorted({error.edge_aliases.get(key, key) for key in raw_edge_ids}),
        'observed': row} for row in diagnostics]
    checks.extend({'id': 'node-followup:' + canonical_hash(row)[:24],
        'kind': 'node_relation_followup', 'mode': 'required', 'node_ids': sorted(error.owner_ids),
        'edge_ids': list(row.get('edge_ids', [])), 'observed': row} for row in error.followups)
    if not checks:
        raise ValueError('Node relation coordination needs explicit required obligations')
    relation_task = LocalTask(task.id + '-node-relations-' + node_draft_id[:12],
        'reconcile', related, checks=checks)

    def preserving_local(callback, *args):
        # The outer callback persists TaskState and calls sync_draft_context.
        # Keep that durable state change, but restore this nested model context.
        local = dict(ctx.__dict__)
        try:
            return callback(*args)
        finally:
            ctx.__dict__.clear()
            ctx.__dict__.update(local)

    def save():
        preserving_local(save_record, deepcopy(record))

    def draft_context(raw):
        ctx.active_draft = deepcopy(raw)
        ctx.task_draft_id = canonical_hash([relation_task.id, intermediate_hash, raw])
        ctx.task_draft_errors = ()
        ctx.task_draft_errors_draft_id = None
        ctx.task_candidate_draft_id = None

    def record_draft(raw):
        draft_context(raw)
        if record.get('attempted_patch') != raw:
            for key in ('preview', 'patch', 'patch_hash', 'candidate_hash', 'review', 'review_candidate_hash'):
                record.pop(key, None)
            record['stage'] = 'writing'
        record['attempted_patch'] = deepcopy(raw)
        record['relation_draft_id'] = ctx.task_draft_id
        save()

    def record_error(failure):
        from cortex.capabilities.graph_merge.llm.agent import failure_feedback
        feedback = failure_feedback(failure)
        ctx.task_draft_errors = (feedback,)
        ctx.task_draft_errors_draft_id = ctx.task_draft_id
        record['compile_issues'] = [feedback]
        save()

    def compile_value(value, *, persist=True):
        value = draft_model.model_validate(value)
        raw = value.model_dump(mode='json', exclude_unset=True)
        draft_context(raw)
        # Crucially call the compiler directly, not the outer node hook.
        if canonical_relation:
            from cortex.capabilities.graph_merge.reconciliation.agent import prepare_runtime
            from cortex.capabilities.graph_merge.reconciliation.compiler import compile_draft
            prepare_runtime(engine.agent, 'reconcile', ctx, payload, draft=value)
            repair = compile_draft(ctx, intermediate, value, targets=related, payload=payload)
        else:
            repair = compile_patch(ctx, intermediate, value, targets=related, payload=payload)
        GraphManager(ctx.schema).validate_state(repair.state)
        ctx.candidate = repair.state
        ctx.last_compiled_patch = repair
        ctx.task_candidate_draft_id = ctx.task_draft_id
        if persist:
            record.update({'patch': raw, 'patch_hash': canonical_hash(raw),
                'candidate_hash': canonical_hash(repair.state), 'stage': 'assessing'})
            record.pop('compile_issues', None)
            save()
        return repair

    compiled_cache = {}

    def preview_binding(value):
        return canonical_hash({'before':intermediate_hash,
            'draft':value.model_dump(mode='json',exclude_unset=True),
            'evidence':[row.model_dump(mode='json') for row in ctx.evidence],
            'schema':ctx.schema.model_dump(mode='json'), 'policy':binding.get('policy')})

    def cached_preview(value):
        value=draft_model.model_validate(value)
        key=preview_binding(value)
        saved_preview=record.get('preview')
        if saved_preview is None or saved_preview['binding']!=key:
            return None
        if key not in compiled_cache:
            repair=compile_value(value,persist=False)
            if canonical_hash(repair.state)!=saved_preview['candidate_hash']:
                raise ValueError('Saved relation preview no longer compiles to the exact candidate')
            compiled_cache[key]=repair
        return compiled_cache[key]

    def preview_draft(value):
        value=draft_model.model_validate(value)
        repair=cached_preview(value)
        if repair is None:
            try:
                repair=compile_value(value,persist=False)
            except (ValueError,TypeError,KeyError) as failure:
                record_error(failure)
                raise
            key=preview_binding(value)
            compiled_cache[key]=repair
            raw=value.model_dump(mode='json',exclude_unset=True)
            record.update(attempted_patch=raw,preview={'binding':key,'candidate_hash':canonical_hash(repair.state)},
                stage='compiled_unsubmitted')
            record.pop('compile_issues',None)
            save()
        ctx.candidate,ctx.last_compiled_patch=repair.state,repair
        return repair

    def record_complete_draft_action(raw):
        # Save draft and completion intent together, including an empty version zero.
        draft_context(raw)
        if record.get('attempted_patch') != raw:
            for key in ('preview','patch','patch_hash','candidate_hash','review','review_candidate_hash'):
                record.pop(key,None)
        record.update(attempted_patch=deepcopy(raw), relation_draft_id=ctx.task_draft_id,
            complete_draft_action=canonical_hash(raw))
        save()

    def has_pending_draft_action(value):
        return record.get('complete_draft_action')==canonical_hash(value.model_dump(mode='json',exclude_unset=True))

    def accept_compiled_draft(value):
        value=draft_model.model_validate(value)
        repair=cached_preview(value)
        if repair is None:
            raise ValueError('DRAFT_NOT_COMPILED: compile the exact local draft before submission')
        raw=value.model_dump(mode='json',exclude_unset=True)
        record.update(patch=raw,patch_hash=canonical_hash(raw),candidate_hash=canonical_hash(repair.state),stage='assessing')
        record.pop('complete_draft_action',None)
        save()
        return repair

    try:
        ctx.task_before = intermediate
        ctx.task_identity_groups = []
        ctx.node_local_compile = False
        ctx.active_task = relation_task
        ctx.task_visible_ids = set(related)
        ctx.read_ids = set(original.get('read_ids', ()))
        ctx.task_relevant_facts = ()
        ctx.task_semantic_issues = ()
        ctx.task_semantic_issue_basis = ()
        ctx.task_required_context = ()
        ctx.task_draft_errors = ()
        ctx.task_draft_errors_draft_id = None
        ctx.task_candidate_draft_id = None
        ctx.task_draft_id = None
        ctx.active_draft = None

        ctx.record_task_draft, ctx.record_task_error = record_draft, record_error
        if recovery_load:
            ctx.load_task_recovery = lambda key: preserving_local(recovery_load, prefix + key)
        if recovery_store:
            ctx.record_task_recovery = lambda key, value: preserving_local(recovery_store, prefix + key, value)
        if getattr(engine.agent, 'relation_merger_contract', False):
            from cortex.capabilities.graph_merge.reconciliation.relation_merger import consolidate_relations, relation_groups
            groups = relation_groups(intermediate, focus_ids=related)
            if groups:
                def save_merger(checkpoint):
                    record['relation_merger'] = checkpoint
                    save()
                preview = consolidate_relations(engine.agent, ctx, intermediate,
                    checkpoint=record.get('relation_merger'), save=save_merger,
                    batch_size=getattr(engine.agent, 'relation_merger_batch_size', 10),
                    validate=False, focus_ids=related)
                proposed = [{**group, 'description': preview.checkpoint['results'][group['group_id']]}
                            for group in groups]
                checks.append({'id': 'node-relation-content:' + canonical_hash(proposed)[:20],
                    'kind': 'node_relation_followup', 'mode': 'required',
                    'node_ids': sorted({ident for group in groups for ident in (group['from_node_id'], group['to_node_id'])}),
                    'edge_ids': sorted({ident for group in groups for ident in group['member_edge_ids']}),
                    'observed': {'proposed_relation_merges': proposed},
                    'required_outcome': 'Apply the normalized relation descriptions and consolidation in this same transaction while preserving all supported business knowledge and fixed branch scopes.'})
        payload = relation_context(ctx, intermediate, relation_task)
        payload['node_relation_obligations'] = {key:value for key,value in obligations.items() if key not in {'original_edges','original_targets'}}
        ctx.compile_task_patch = compile_value
        if canonical_relation:
            ctx.preview_task_draft,ctx.accept_compiled_draft=preview_draft,accept_compiled_draft
            ctx.record_complete_draft_action=record_complete_draft_action
            ctx.has_pending_draft_action=has_pending_draft_action
            ctx.get_task_compilation=cached_preview
        ctx.output_validator = compile_value
        save()
        for _ in range(8):
            ctx.active_task=relation_task
            ctx.task_semantic_issues=tuple(record.get('semantic_issues',()))
            if record.get('semantic_issues'):
                payload['semantic_issues']=record['semantic_issues']
            if record.get('patch') is not None:
                raw=record['patch']
                if record.get('patch_hash')!=canonical_hash(raw):
                    raise ValueError('Saved relation draft hash mismatch')
                repair=compile_value(draft_model.model_validate(raw),persist=False)
                if record.get('candidate_hash')!=canonical_hash(repair.state):
                    raise ValueError('Saved relation draft no longer compiles to its candidate')
            else:
                if record.get('attempted_patch') is not None:
                    payload['draft']=record['attempted_patch']
                    payload['draft_id']=record.get('relation_draft_id')
                    if record.get('compile_issues'):
                        payload['issues']=record['compile_issues']
                value=engine.agent.invoke('reconcile',ctx,payload)
                raw=value.model_dump(mode='json',exclude_unset=True)
                if record.get('patch')!=raw:
                    repair=compile_value(value)
                else:
                    repair=ctx.last_compiled_patch
            candidate_hash=canonical_hash(repair.state)
            review_task=LocalTask(relation_task.id+'-review','graph_assess',related,
                checks=checks,feedback=[{'kind':'node_relation_obligations'}])
            ctx.active_task=review_task
            review_payload=relation_context(ctx,repair.state,review_task,before=intermediate,assessment=True)
            review_payload['node_relation_obligations']={key:value for key,value in obligations.items() if key not in {'original_edges','original_targets'}}
            if canonical_relation:
                from cortex.capabilities.graph_merge.reconciliation.agent import prepare_runtime
                prepare_runtime(engine.agent,'graph_assess',ctx,review_payload,fresh=True)
            if record.get('review') is not None:
                if record.get('review_candidate_hash')!=candidate_hash:
                    raise ValueError('Saved relation assessment addresses another candidate')
                review=validate_review(review_model.model_validate(record['review']),ctx,before=intermediate,candidate=repair.state)
            else:
                ctx.output_validator=lambda review:validate_review(review,ctx,before=intermediate,candidate=repair.state)
                review=engine.agent.invoke('graph_assess',ctx,review_payload)
                review=validate_review(review,ctx,before=intermediate,candidate=repair.state)
                record.update(review=review.model_dump(mode='json'),review_candidate_hash=candidate_hash,
                    stage='accepted' if review.verdict=='accept' else 'assessment_rejected')
                save()
            if review.verdict=='accept':
                break
            # Keep feedback inside this unpublished transaction. No stage routing
            # and no loss of the old candidate or diagnostics in the checkpoint.
            signature=canonical_hash({'candidate':candidate_hash,'review':review.model_dump(mode='json')})
            previous=record.get('feedback_gate',{})
            repeats=previous.get('occurrences',0)+1 if previous.get('signature')==signature else 1
            record['feedback_gate']={'signature':signature,'occurrences':repeats}
            record.setdefault('assessment_history',[]).append(deepcopy(record['review']))
            record['semantic_issues']=[issue.model_dump(mode='json') for issue in review.issues]
            if repeats>=2:
                save()
                raise IntegrationIncomplete('Unchanged local relation candidate and feedback repeated; checkpoint retained')
            for key in ('patch','patch_hash','review','review_candidate_hash'):
                record.pop(key,None)
            record['stage']='writing'
            save()
        else:
            raise IntegrationIncomplete('Local relation feedback exceeded eight revisions; checkpoint retained')
        record['stage'] = 'accepted'
        save()
        return _compose(error, repair), deepcopy(record)
    finally:
        ctx.__dict__.clear()
        ctx.__dict__.update(original)
