"""A fresh execution frame keeps the direct Writer's missing-intent semantics."""
from langchain_core.messages import AIMessage
from graph_merge_chat import ScriptedModel
from graph_merge_fixtures import catalog, concept, edge, state

from cortex.capabilities.graph_merge.consolidation.task_context import LocalTask
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
from cortex.capabilities.graph_merge.runtime.context import fork_task_context
from cortex.schema import load_root_schema


def test_fresh_fork_has_no_completion_intent_and_requires_the_writer():
    before = state([*catalog(), concept('A', 'a')], [edge('EA', 'A', 'a')])
    inputs = IntegrationContext(before, state(), load_root_schema())
    task = LocalTask('direct-writer', 'reconcile', {'A'}, checks=[{
        'id': 'retain-binding', 'kind': 'node_relation_followup', 'mode': 'required',
        'node_ids': ['A'], 'edge_ids': ['EA'],
        'required_outcome': 'Retain this complete evidenced binding.',
    }])
    context = fork_task_context(inputs, before=before, task=task)
    model = ScriptedModel([AIMessage(content='', tool_calls=[{
        'id': 'consider-current', 'name': 'edit_draft', 'args': {},
    }])])
    assert not context.has_pending_draft_action(None)
    draft = IncrementalIntegrationAgent(model).invoke('reconcile', context, {'task_id': task.id})
    assert draft.task_id == task.id and not draft.changes
    assert len(model.requests) == 1
    assert context.candidate == before
    assert not inputs.tool_log and inputs.candidate is None
