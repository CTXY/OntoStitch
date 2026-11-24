"""Compile explicit local graph edits against an immutable task baseline."""
from types import SimpleNamespace

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.graph.batch import ActiveBatch, BatchResult
from cortex.capabilities.graph_merge.graph.operations import PHYSICAL
from cortex.capabilities.graph_merge.evidence import EvidencePresentation, evidence_sources
from cortex.capabilities.graph_merge.graph.edit_contract import Change, Element, IntegrationProposal
from cortex.capabilities.graph_merge.consolidation.compiler import CompiledPatch
from cortex.capabilities.graph_merge.graph.scope import ProposalScope
from cortex.capabilities.graph_merge.graph.provenance import validate_rewrite_sources
from cortex.capabilities.graph_merge.reconciliation.contract import DraftDocument, validate_combinations
from cortex.capabilities.graph_merge.graph.source_units import PatchContractError
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.schema import GraphNode, GraphEdge, GraphSpace, GraphState
from cortex.schema.models import EdgeType
from cortex.schema.relation_contracts import authoring_schema, merge_relation_contracts


def _fail(code, **details):
    raise PatchContractError(code, **details)


def _union(*groups):
    return tuple(sorted({s for group in groups for s in group}, key=lambda s: s.model_dump_json()))


def _properties(old, updates, unset):
    result = {**old, **updates}
    for field in unset:
        if not field.startswith('properties.') or field.count('.') != 1:
            _fail('INVALID_UNSET', path=field, required_action='Use properties.<key> for optional property removal; omit unchanged fields.')
        key = field.split('.', 1)[1]
        if key in {'sources', 'common_sources', 'overrides', 'reference_origin'}:
            _fail('INTERNAL_WRITE_FIELD', path=field)
        result.pop(key, None)
    return result


def compile_draft(context, base, draft, *, targets, payload, aliases=None):
    old_candidate, old_batch = context.candidate, getattr(context, 'active_batch', None)
    try:
        return _compile(context, base, DraftDocument.model_validate(draft), set(targets), payload or {}, aliases)
    except BaseException:
        context.candidate, context.active_batch = old_candidate, old_batch
        raise


def _compile(context, base, draft, targets, payload, aliases):
    if any(k != v for k, v in (aliases or {}).items()):
        _fail('UNSUPPORTED_NODE_ALIAS', required_action='Express supported local edits; no automatic stage routing or inferred node merge.')
    if draft.before_id != canonical_hash(base): _fail('BEFORE_MISMATCH')
    runtime = getattr(context, 'reconciliation_runtime', None)
    if runtime is not None:
        runtime.validate_submission(draft)
        if draft.task_id != runtime.task_id: _fail('TASK_MISMATCH')
        known = runtime.allowed_evidence_ids
    else:
        if payload.get('task_id') != draft.task_id: _fail('MISSING_CONTROLLER_BINDING')
        known = {e.id for e in context.evidence}
    validate_combinations(draft.changes)
    original_nodes = {n.id:n for n in base.graph.nodes}
    original_edges = {e.id:e for e in base.graph.edges}
    nodes, edges = dict(original_nodes), dict(original_edges)
    changes, scope_edits, audits, locators = [], [], [], {}
    contracts = list(base.graph.relation_contracts)

    def node(ident):
        if ident not in original_nodes: _fail('UNKNOWN_NODE', node_id=ident)
        obj = original_nodes[ident]
        if obj.node_type in PHYSICAL: _fail('PHYSICAL_READONLY', node_id=ident)
        return obj

    def edge(ident):
        if ident not in original_edges: _fail('UNKNOWN_EDGE', edge_id=ident)
        obj = original_edges[ident]
        if original_nodes[obj.from_node_id].node_type in PHYSICAL: _fail('PHYSICAL_READONLY', edge_id=ident)
        return obj

    def evidence(ids):
        if set(ids) - set(known): _fail('UNKNOWN_EVIDENCE', evidence_ids=sorted(set(ids)-set(known)))
        return evidence_sources(context, ids)

    def self_change(kind, ident, reason):
        ref = Element(kind=kind, id=ident)
        for index, previous in enumerate(changes):
            if previous.before == (ref,) and previous.after == (ref,):
                changes[index] = previous.model_copy(update={'explanation':previous.explanation + '; ' + reason})
                return
        changes.append(Change(before=(ref,), after=(ref,), explanation=reason))

    for row in draft.changes:
        if row.kind == 'node_update':
            owner = node(row.node_id)
            if owner.alternatives:
                _fail('CONFLICT_CONTENT_READONLY', owner_id=owner.id,
                    required_action='Keep all competing branch contents unchanged; only justified relation/scope repairs are supported.')
            fields = row.set.model_dump(mode='json', exclude_unset=True)
            props = fields.pop('properties', {})
            nodes[owner.id]=GraphNode.model_validate({**owner.model_dump(),**fields,
                'properties':_properties(owner.properties,props,row.unset),
                'sources':_union(owner.sources,evidence(row.evidence_ids))})
            self_change('node',owner.id,row.reason)
            scope_edits.append({'operation':'update_node','node':{'id':owner.id,'properties':nodes[owner.id].properties}})
        elif row.kind == 'node_create':
            if row.node_id in nodes or row.node_id in original_edges: _fail('ID_EXISTS',object_id=row.node_id)
            nodes[row.node_id]=GraphNode(id=row.node_id,node_type=row.node_type,**row.content.model_dump(),sources=evidence(row.evidence_ids))
            scope_edits.append({'operation':'create_node','node':{'id':row.node_id,'node_type':row.node_type}})
        elif row.kind == 'relation_declare':
            contract=EdgeType.model_validate(row.contract.model_dump())
            if contract.id in authoring_schema(context.schema,base.graph).edge_type_index(): _fail('PREDICATE_ALREADY_INSTALLED',predicate=contract.id)
            contracts.append(contract)

    for row in draft.changes:
        if row.kind == 'edge_update':
            old=edge(row.edge_id)
            fields=row.set.model_dump(mode='json',exclude_unset=True)
            props=fields.pop('properties',{})
            if set(props)&{'sources','branches','reference_origin'}: _fail('INTERNAL_EDGE_FIELDS',edge_id=old.id)
            fields.update(properties=_properties(old.properties,props,row.unset),sources=_union(old.sources,evidence(row.evidence_ids)))
            edges[old.id]=GraphEdge.model_validate({**old.model_dump(),**fields})
            self_change('edge',old.id,row.reason)
            scope_edits.append({'operation':'update_edge','edge':edges[old.id].model_dump(exclude={'sources','reference_origin'})})
        elif row.kind == 'edge_create':
            if row.edge_id in edges or row.edge_id in nodes: _fail('ID_EXISTS',object_id=row.edge_id)
            edges[row.edge_id]=GraphEdge(id=row.edge_id,edge_type=row.edge_type,from_node_id=row.from_node_id,
                to_node_id=row.to_node_id,properties=row.properties,sources=evidence(row.evidence_ids))
            scope_edits.append({'operation':'create_edge','edge':edges[row.edge_id].model_dump(exclude={'sources','reference_origin'})})

    for row in draft.changes:
        if row.kind == 'edge_replace':
            old=edge(row.edge_id)
            successor=edges.get(row.replacement_edge_id)
            if successor is None: _fail('MISSING_EDGE_SUCCESSOR',edge_id=row.replacement_edge_id)
            if nodes[successor.from_node_id].node_type in PHYSICAL: _fail('PHYSICAL_READONLY',edge_id=successor.id)
            edges[successor.id]=successor.model_copy(update={'sources':_union(successor.sources,old.sources)})
            edges.pop(old.id)
            locators[old.id]=successor.id
            changes.append(Change(before=(Element(kind='edge',id=old.id),),after=(Element(kind='edge',id=successor.id),),explanation=row.reason))
        elif row.kind == 'edge_remove':
            old=edge(row.edge_id)
            if runtime is not None:
                for ref in row.refs: runtime.resolve_reference(ref)
            else:
                from cortex.capabilities.graph_merge.reconciliation.projection import validate_reference
                for ref in row.refs: validate_reference(context,base,ref)
            edges.pop(old.id)
            changes.append(Change(before=(Element(kind='edge',id=old.id),),after=(),explanation=row.reason))
            audits.append({'type':'edge_removal','edge_id':old.id,'reason':row.reason,
                'refs':[ref.model_dump(mode='json') for ref in row.refs],
                'before_snapshot':canonical_hash(base),'removed_sources':[s.model_dump(mode='json') for s in old.sources]})

    final=GraphState(graph=GraphSpace(nodes=tuple(nodes.values()),edges=tuple(edges.values()),relation_contracts=merge_relation_contracts(contracts)))
    _check_conflict_preservation(original_nodes,nodes,original_edges,edges)
    writable=authoring_schema(context.schema,final.graph).edge_type_index()
    for row in draft.changes:
        if row.kind not in {'edge_update','edge_create'}: continue
        obj=edges[row.edge_id]
        if nodes[obj.from_node_id].node_type in PHYSICAL: _fail('PHYSICAL_READONLY',edge_id=obj.id)
        if obj.edge_type == 'has_attribute': _fail('ABSTRACT_INSTANCE_PREDICATE',edge_id=obj.id)
        if obj.edge_type not in writable: _fail('UNDECLARED_WRITE_PREDICATE',edge_id=obj.id,predicate=obj.edge_type)
    from cortex.capabilities.graph_merge.graph.validation import validate_local_transition
    final=validate_local_transition(context.schema,base,final)
    _validate_grounding_scopes(final)
    validate_rewrite_sources(SimpleNamespace(raw_nodes=original_nodes,raw_edges=original_edges),
        IntegrationProposal(changes=tuple(changes)),SimpleNamespace(state=final,node_map={},edge_map={}))
    batch=ActiveBatch(context,base,targets=targets)
    batch.seed=batch.preview=base
    context.active_batch,context.candidate=batch,base
    if runtime is not None:
        scope=ProposalScope(context,payload,EvidencePresentation(context))
        scope.visible.update(runtime.visible_node_ids['before'])
        scope.visible.update(runtime.visible_edge_ids['before'])
        scope.visible.update(getattr(context,'task_visible_ids',()))
        admitted=scope.prepare(BatchResult.model_validate({'edits':scope_edits,'changes':[c.model_dump() for c in changes]}))
        batch.allowed.update(admitted)
        if admitted: batch.scope_events.append({'ids':sorted(admitted),'reason':'Explicit local graph edits'})
    batch.preview,batch.result,batch.revision=final,BatchResult(changes=tuple(changes)),1
    batch.edge_aliases={}
    context.candidate=final
    result=CompiledPatch(final,base,(),tuple(changes),{},{},[*batch.scope_events,*audits],batch)
    result.final_edge_aliases=locators
    result.canonical_draft=draft
    return result


def _check_conflict_preservation(before_nodes, after_nodes, before_edges, after_edges):
    """Preserve real conflicts independently of which public edit was used.

    This catches structural and exact-clone bypasses. Paraphrased semantic
    selection still requires the independent Assessment, not hash heuristics.
    """
    created = set(after_nodes) - set(before_nodes)
    for ident, original in before_nodes.items():
        if not original.alternatives:
            continue
        current = after_nodes.get(ident)
        if current is None or not current.alternatives or current.node_type != original.node_type:
            _fail('CONFLICT_RESOLUTION_FORBIDDEN', owner_id=ident,
                required_action='Keep the conflict owner and all competing branches; reconciliation cannot retire or normalize it.')
        old_branches = {branch.alternative_id: branch for branch in original.alternatives}
        new_branches = {branch.alternative_id: branch for branch in current.alternatives}
        if old_branches.keys() != new_branches.keys() or current.conflict_points != original.conflict_points:
            _fail('CONFLICT_STRUCTURE_FROZEN', owner_id=ident)
        if (not set(original.common_sources) <= set(current.common_sources)
                or not set(original.sources) <= set(current.sources)
                or any(not set(branch.sources) <= set(new_branches[key].sources)
                       for key, branch in old_branches.items())):
            _fail('CONFLICT_SOURCE_LOSS', owner_id=ident)
        redirected = set()
        for edge_id, old in before_edges.items():
            new = after_edges.get(edge_id)
            if old.from_node_id == ident and new is not None and new.from_node_id in created:
                redirected.add(new.from_node_id)
            if old.to_node_id == ident and new is not None and new.to_node_id in created:
                redirected.add(new.to_node_id)
            # Changing an edge ID is not permission to select a branch either:
            # retire(old consumer) + create(new consumer) has the same effect.
            if old.from_node_id == ident and (new is None or new.from_node_id != ident):
                redirected.update(edge.from_node_id for edge in after_edges.values()
                    if edge.edge_type == old.edge_type and edge.to_node_id == old.to_node_id
                    and edge.from_node_id in created)
            if old.to_node_id == ident and (new is None or new.to_node_id != ident):
                redirected.update(edge.to_node_id for edge in after_edges.values()
                    if edge.edge_type == old.edge_type and edge.from_node_id == old.from_node_id
                    and edge.to_node_id in created)
        for other_id in redirected:
            other = after_nodes[other_id]
            if other.alternatives or other.node_type != original.node_type:
                continue
            core = {key: value for key, value in other.properties.items() if key != 'relation_metadata'}
            for branch in original.alternatives:
                full = GraphManager.resolve_alternative(original, branch)
                old_core = {key: value for key, value in full.properties.items() if key != 'relation_metadata'}
                if core == old_core and (core or other.description == full.description):
                    _fail('CONFLICT_BRANCH_CLONE_FORBIDDEN', owner_id=ident, node_id=other_id,
                        required_action='Do not redirect a conflict consumer to an ordinary copy of one branch.')


def _validate_grounding_scopes(state):
    nodes = {n.id: n for n in state.graph.nodes}
    for edge in state.graph.edges:
        owner = nodes[edge.from_node_id]
        if edge.edge_type != 'grounded_on' or owner.node_type != 'concept': continue
        scope = set(edge.properties.get('alternative_ids', ()))
        variants = [(None, owner.properties)] if not owner.alternatives else [
            (a.alternative_id, GraphManager.resolve_alternative(owner, a).properties) for a in owner.alternatives
            if not scope or a.alternative_id in scope]
        for alternative, props in variants:
            if edge.to_node_id not in {ref for group in props.get('targets', []) for ref in group}:
                _fail('GROUNDING_BRANCH_TARGET_MISMATCH', owner_id=owner.id, edge_id=edge.id, alternative_id=alternative,
                    required_action='Grounding must be a target of every applicable source branch; narrow scope or repair targets explicitly.')
