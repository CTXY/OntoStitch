"""Creation and source inheritance obey the same atomic relation contract."""
from types import SimpleNamespace

import pytest

from test_graph_integration import catalog, concept, edge, state
from cortex.capabilities.graph_merge.graph.batch import ActiveBatch
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.graph.edit_contract import Element
from cortex.capabilities.graph_merge.graph.provenance import SourceCoverageError, validate_rewrite_sources
from cortex.capabilities.graph_merge.graph.edits import NewEdge, RelationPatch, submit_patch
from cortex.capabilities.graph_merge.models import EvidenceItem
from cortex.schema import SourceReference, load_root_schema


def setup():
    old_source = SourceReference(source_id='original', case_id='first', locator='QA', chunk_id='part')
    new_source = SourceReference(source_id='additional', case_id='second', locator='QA')
    original = state([*catalog(), concept('A', 'a').model_copy(update={'sources': (new_source,)}), concept('B', 'b')],
                     [edge('old', 'A', 'B', 'has_feature').model_copy(update={'sources': (old_source,)})])
    ctx = IntegrationContext(state(), original, load_root_schema())
    base = ctx.union_state()
    a, b, old = ctx.input_node_map['A'], ctx.input_node_map['B'], ctx.input_edge_map['old']
    ctx.candidate = base
    ctx.active_batch = ActiveBatch(ctx, base, targets={a, b})
    def check_sources(candidate, changes, result):
        validate_rewrite_sources(ctx, SimpleNamespace(changes=changes),
            SimpleNamespace(state=candidate, node_map={}, edge_map={}))
    ctx.active_batch.extra_check = check_sources
    ctx.evidence = (EvidenceItem(id='Q_new', source=new_source, case_id='second', content={'question':'additional evidence'}),)
    return ctx, base, a, b, old, old_source, new_source


def creation(a, b, key='new'):
    return {'id':key, 'edge_type':'has_feature', 'from_node_id':a, 'to_node_id':b,
            'properties':{'description':'The same feature relationship, expressed precisely.'}, 'evidence_ids':['Q_new']}


def test_creation_schema_has_no_update_only_evidence_field():
    props = NewEdge.model_json_schema()['properties']
    assert 'add_evidence' not in props and 'explanation' not in props
    assert 'id' in NewEdge.model_json_schema()['required']
    with pytest.raises(ValueError, match='add_evidence'):
        NewEdge.model_validate({**creation('a','b'), 'add_evidence':['Q_new']})


def test_new_edge_inherits_exact_source_from_declared_single_predecessor():
    ctx, base, a, b, old, before_source, added_source = setup()
    assert not any(e.id == 'new' for e in base.graph.edges)
    submit_patch(ctx, RelationPatch(create_edges=[creation(a,b)],
        retirements=[{'before':[{'kind':'edge','id':old}], 'after':[{'kind':'edge','id':'new'}],
                      'explanation':'This single edge replaces the original assertion without losing its meaning.'}],
        explanation='Replace the assertion with one precise successor.'))
    candidate = ctx.active_batch.preview
    new = next(e for e in candidate.graph.edges if e.id == 'new')
    assert set(new.sources) == {before_source, added_source}
    assert not any(e.id == old for e in candidate.graph.edges)
    assert any(e.id == old for e in base.graph.edges)


def test_rejected_creation_leaves_no_new_object_or_pending_fragment():
    ctx, base, a, b, *_ = setup()
    with pytest.raises(ValueError):
        submit_patch(ctx, RelationPatch(create_edges=[creation(a,'missing')], explanation='Invalid endpoint.'))
    assert ctx.active_batch.preview is None and not ctx.active_batch.pending
    assert not any(e.id == 'new' for e in ctx.candidate.graph.edges)


def test_multiple_successors_do_not_receive_broadcast_sources():
    ctx, base, a, b, old, *_ = setup()
    with pytest.raises(SourceCoverageError):
        submit_patch(ctx, RelationPatch(create_edges=[creation(a,b,'n1'),creation(a,b,'n2')],
            retirements=[{'before':[{'kind':'edge','id':old}], 'after':[{'kind':'edge','id':'n1'},{'kind':'edge','id':'n2'}],
                          'explanation':'Two separate successor assertions require explicit source allocation.'}],
            explanation='Split an assertion.'))
    assert ctx.active_batch.preview is None and not ctx.active_batch.pending


def test_direct_source_loss_exposes_same_structured_error():
    ctx, base, a, b, old, source, *_ = setup()
    original = ctx.raw_edges[old]
    dropped = original.model_copy(update={'sources': ()})
    preview = SimpleNamespace(state=base.model_copy(update={'graph':base.graph.model_copy(update={
        'edges':tuple(dropped if e.id==old else e for e in base.graph.edges)})}), node_map={}, edge_map={})
    with pytest.raises(SourceCoverageError) as error:
        validate_rewrite_sources(ctx, SimpleNamespace(changes=()), preview)
    assert error.value.before == (Element(kind='edge',id=old),)
    assert error.value.after == (Element(kind='edge',id=old),)
    assert error.value.missing_sources == {source}


def test_branch_retirement_cannot_copy_all_owner_sources_to_new_edge():
    ctx, base, a, b, old, *_ = setup()
    with pytest.raises(ValueError, match='Use conflicts to change complete alternatives'):
        submit_patch(ctx, RelationPatch(create_edges=[creation(a,b)],
            retirements=[{'before':[{'kind':'edge','id':old,'alternative_id':'branch'}],
                          'after':[{'kind':'edge','id':'new'}], 'explanation':'A branch is not the full assertion.'}],
            explanation='Do not copy all owner sources for a branch.'))
    assert ctx.active_batch.preview is None and not ctx.active_batch.pending
