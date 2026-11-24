from copy import deepcopy
import pytest
from cortex.capabilities.graph_merge.reconciliation.contract import apply_edit_shape, DraftEditError


def change(ident, kind='edge_remove'):
    row={'change_id':ident,'kind':kind,'edge_id':'E','reason':'Preserve the supported assertion.'}
    if kind=='edge_remove':row['refs']=[{'view':'before','kind':'edge','id':'E'}]
    else:row['replacement_edge_id']='successor'
    return row


def test_atomic_withdrawal_replacement_and_empty_edit():
    draft={'task_id':'T','before_id':'B','version':0}
    first=apply_edit_shape(draft,{'put_changes':[change('c1')]})
    with pytest.raises(DraftEditError) as error:
        apply_edit_shape(first,{'put_changes':[change('c2','edge_replace')]})
    assert error.value.details['change_ids']==['c1','c2']
    revised=apply_edit_shape(first,{'remove_changes':['c1'],'put_changes':[change('c2','edge_replace')]})
    assert [c.change_id for c in revised.changes]==['c2']
    assert apply_edit_shape(revised,{'put_changes':[],'remove_changes':[]})==revised
    assert not apply_edit_shape(revised,{'remove_changes':['c2']}).changes


def test_no_silent_overwrite_and_identical_add_is_idempotent():
    first=apply_edit_shape({'task_id':'T','before_id':'B','version':0},{'put_changes':[change('c1')]})
    assert apply_edit_shape(first,{'put_changes':[change('c1')]})==first
    with pytest.raises(DraftEditError,match='Withdraw'):
        apply_edit_shape(first,{'put_changes':[change('c1','edge_replace')]})
