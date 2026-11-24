from copy import deepcopy
from types import SimpleNamespace
from graph_merge_fixtures import catalog, concept, state
from cortex.capabilities.graph_merge.models import LocalTask
from cortex.capabilities.graph_merge.consolidation.task_context import _pending_identity_consolidations
from cortex.capabilities.graph_merge.llm.packets import compact_node_packet


def test_only_exact_live_confirmed_future_identity_groups_are_exposed():
    groups=[{'members':['A','B'],'meaning':'Name attribute','reason':'Same declared referent.'},
            {'members':['C','D'],'meaning':'Card record','reason':'Same declared referent.'},
            {'members':['retired','E'],'meaning':'Rewritten group','reason':'Requires explicit projection.'},
            {'members':['F','G'],'meaning':'Unrelated group','reason':'No supplied member.'}]
    ctx=SimpleNamespace(identity_decision={'groups':groups})
    current=state([*catalog(), *[concept(k,'a') for k in ['A','B','C','D','E','F','G']]])
    before=current.model_dump(mode='json'); original=deepcopy(groups)
    packet={'candidate_nodes':[{'id':k} for k in ['A','C','D','E']]}
    task=LocalTask('content-0-node-review','node_assess',{'A','C'},groups=[groups[1]])
    _pending_identity_consolidations(ctx,current,packet,task)
    assert packet['pending_identity_consolidations']==[groups[0]]
    assert groups==original and current.model_dump(mode='json')==before
    assert 'different-referent' in packet['pending_identity_consolidations_scope']
    assert compact_node_packet(packet)['pending_identity_consolidations']==[groups[0]]


def test_no_pending_hint_for_completed_retired_or_current_task_groups():
    group={'members':['A','B'],'meaning':'One referent','reason':'Confirmed.'}
    ctx=SimpleNamespace(identity_decision={'groups':[group]})
    packet={'candidate_nodes':[{'id':'A'}]}
    _pending_identity_consolidations(ctx,state([*catalog(), concept('A','a')]),packet,LocalTask('later','node_assess',{'A'}))
    assert 'pending_identity_consolidations' not in packet
    _pending_identity_consolidations(ctx,state([*catalog(), concept('A','a'),concept('B','a')]),packet,
                                    LocalTask('active','node_assess',{'A'},groups=[group]))
    assert 'pending_identity_consolidations' not in packet
