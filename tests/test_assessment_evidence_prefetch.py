from types import SimpleNamespace
from test_reconciliation_agent_runtime import CanonicalChat, fixture
from cortex.capabilities.graph_merge.reconciliation.permissions import ReconciliationRuntime


def test_no_remaining_evidence_hides_read_tool(tmp_path):
    engine,task=fixture(tmp_path,CanonicalChat())
    # Ordinary tasks do not prefetch arbitrary historical evidence.
    engine.session.inputs.task_before=engine.session.current
    engine.session.inputs.active_task=task
    engine.session.inputs.candidate=engine.session.current
    runtime=ReconciliationRuntime(engine.session.inputs,{'task_id':task.id},'graph_assess')
    runtime.evidence_view['id_to_body'].update({i:'provided' for i in runtime.allowed_evidence_ids})
    assert 'read_evidence' not in runtime.available_read_tools({'read_evidence':object()})
