"""The deployed node path has narrow tools, real compilation and durable gates."""
import json
from copy import deepcopy
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from test_graph_integration import catalog, concept, edge, state
from test_merge_task_review_boundary import engine_for

from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
from cortex.capabilities.graph_merge.integration.relation_followups import drain_relation_followups
from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.integration.task_state import TaskState, canonical_hash
from cortex.capabilities.graph_merge.integration.workspace import WorkspaceStore
from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent


class NodeChat:
    def __init__(self, *, invalid_first=False, followup=False, interrupt_assessment=False):
        self.requests = []
        self.invalid_first = invalid_first
        self.followup = followup
        self.interrupt_assessment = interrupt_assessment

    def bind(self, **kwargs):
        self.output_options=kwargs
        return self

    def bind_tools(self, tools, **kwargs):
        self.tools = [convert_to_openai_tool(tool) for tool in tools]
        return self

    def invoke(self, messages, config=None):
        self.requests.append((deepcopy(messages), deepcopy(self.tools)))
        names = {tool['function']['name'] for tool in self.tools}
        local = 'node integration Writer' in messages[0].content or 'independent node Assessment' in messages[0].content
        if local:
            assert names <= {'read_evidence', 'submit_patch', 'Review'}
            fixed = json.loads(messages[2].content.split('\n', 1)[1])
            assert 'consumer_constraints' not in fixed
            assert isinstance(fixed.get('relations', fixed.get('before_relations')), list)
            assert 'incoming' not in fixed
            if 'node integration Writer' in messages[0].content:
                if self.invalid_first:
                    self.invalid_first = False
                    return AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'bad',
                        'args': {'edge_updates': [], 'explanation': 'Forbidden graph edit'}}])
                group = fixed['group']
                content = {key: deepcopy(group['members'][0][key]) for key in ('name', 'description', 'aliases', 'tags', 'properties')}
                content['properties']['targets'] = [['a'], ['b']]
                body = {'integrate_groups': [{'owner_id': group['owner_id'], 'member_ids': group['member_ids'],
                    'content': content, 'source_routes': [{'source_unit_id': unit['unit_id'],
                        'destination': {'kind': 'result'}, 'explanation': 'Both matching roles survive'}
                        for unit in fixed['source_unit_manifest'][0]['source_units']],
                    'explanation': 'Compatible representations'}], 'explanation': 'Merge this group'}
                if self.followup:
                    body['relation_followups'] = [{'edge_ids': ['e-A'], 'problem': 'Check retained representation relation',
                        'required_outcome': 'Both distinct endpoint roles survive'}]
                return AIMessage(content='', tool_calls=[{'name': 'submit_patch', 'id': 'merge', 'args': body}])
            if self.interrupt_assessment:
                self.interrupt_assessment = False
                raise RuntimeError('Interrupted before independent assessment')
            return AIMessage(content='', tool_calls=[{'name': 'Review', 'id': 'review', 'args': {'verdict': 'accept', 'issues': []}}])
        if 'edit_draft' in names:
            return AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'submit','args':{}}])
        return AIMessage(content=json.dumps({'explanation':'Actual relations preserve the supplied outcome and sources.','feedback':[]}))


def fixture(tmp_path, chat):
    base = state([*catalog(), concept('A', 'a'), concept('B', 'b')],
                 [edge('e-A', 'A', 'a'), edge('e-B', 'B', 'b')])
    engine, _ = engine_for(base, IncrementalIntegrationAgent(chat, max_calls=5), tmp_path / 'local.sqlite')
    chat.engine = engine
    task = LocalTask('one-group', 'consolidate', {'A', 'B'}, groups=[{'members': ['A', 'B'], 'reason': 'Confirmed same business object'}])
    return engine, task


def test_real_node_agent_compiles_narrow_result_and_keeps_distinct_edges(tmp_path):
    chat = NodeChat()
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    assert len(chat.requests) == 2
    relations = [edge for edge in engine.current.graph.edges if edge.from_node_id == 'A']
    assert len(relations) == 2
    assert {edge.to_node_id for edge in relations} == {'a', 'b'}
    assert engine.history[0]['assessment'] == 'node_assess'
    assert {tool['function']['name'] for tool in chat.requests[0][1]} == {'read_evidence', 'submit_patch'}
    assert {tool['function']['name'] for tool in chat.requests[1][1]} == {'read_evidence', 'Review'}


def test_schema_error_survives_retry_without_changing_fixed_prefix(tmp_path):
    chat = NodeChat(invalid_first=True)
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    before, after = chat.requests[0][0], chat.requests[1][0]
    assert before[:3] == after[:3]
    assert 'edge_updates' in after[-1].content and 'output_schema' in after[-1].content
    assert len(after) == 4


def test_compiled_checkpoint_resumes_assessment_without_writer_replay(tmp_path):
    chat = NodeChat(interrupt_assessment=True)
    engine, task = fixture(tmp_path, chat)
    with pytest.raises(RuntimeError, match='Interrupted'):
        engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    saved = WorkspaceStore(engine.workspace_path).load('node')
    assert saved['task_state']['stage'] == 'assessing'
    assert not saved['history']
    engine.task_state = TaskState.from_dict(saved['task_state'])
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    assert len(chat.requests) == 3
    assert 'independent node Assessment' in chat.requests[-1][0][0].content
    assert len(engine.history) == 1


def test_relation_sidecar_is_durable_and_noop_still_needs_graph_assessment(tmp_path):
    chat = NodeChat(followup=True)
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    assert len(engine.relation_followups) == 1
    saved = WorkspaceStore(engine.workspace_path).load('node')
    assert saved['relation_followups'] == engine.relation_followups
    with pytest.raises(IntegrationIncomplete, match='unresolved'):
        engine._publish_checks()
    drain_relation_followups(engine, 'node')
    assert not engine.relation_followups
    assert len(chat.requests) == 4, 'Empty local draft submission and independent assessment must both execute'
    assert engine.history[-1]['phase'] == 'reconcile'
    assert engine.history[-1]['assessment'] == 'graph_assess'


def test_committed_followup_is_not_replayed_after_queue_removal_interruption(tmp_path):
    chat = NodeChat(followup=True)
    engine, task = fixture(tmp_path, chat)
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    queue = deepcopy(engine.relation_followups)
    drain_relation_followups(engine, 'node')
    requests = len(chat.requests)
    engine.relation_followups = queue
    drain_relation_followups(engine, 'node')
    assert len(chat.requests) == requests and not engine.relation_followups


def test_followup_cannot_be_rebound_to_a_different_draft(tmp_path):
    engine, _ = fixture(tmp_path, NodeChat())
    from cortex.capabilities.graph_merge.integration.task_state import build_contract_versions
    task = TaskState.capture(engine.current, task_id='t', phase='consolidate', versions=build_contract_versions(engine.ctx.schema))
    task = task.with_draft({'explanation': 'current'})
    invalid = replace(task, coupled_relation={'draft_id': 'different-draft'})
    with pytest.raises(ValueError, match='different node draft'):
        TaskState.from_dict(invalid.to_dict())


def force_relation_handoff(monkeypatch):
    from cortex.capabilities.graph_merge.integration.node_relations import NodeRelationCoordinationRequired

    def handoff(base, integrated, *, node_aliases, edge_aliases, owner_ids):
        raise NodeRelationCoordinationRequired(integrated, owner_ids=owner_ids,
            diagnostics=[{'code': 'explicit_test_relation_outcome', 'owner_id': 'A'}],
            node_aliases=node_aliases, edge_aliases=edge_aliases)
    monkeypatch.setattr('cortex.capabilities.graph_merge.integration.node_relations.sync_node_relations', handoff)


class InterruptedGraphChat(NodeChat):
    def __init__(self):
        super().__init__()
        self.interrupt_graph = True

    def invoke(self, messages, config=None):
        local = 'node integration Writer' in messages[0].content or 'independent node Assessment' in messages[0].content
        names = {tool['function']['name'] for tool in self.tools}
        if not local and 'edit_draft' not in names and 'submit_patch' not in names and self.interrupt_graph:
            self.interrupt_graph = False
            self.requests.append((deepcopy(messages), deepcopy(self.tools)))
            raise RuntimeError('Network lost after relation Patch was checkpointed')
        return super().invoke(messages, config=config)


def test_runner_resumes_coupled_patch_before_any_node_writer_replay(tmp_path, monkeypatch):
    force_relation_handoff(monkeypatch)
    chat = InterruptedGraphChat()
    engine, task = fixture(tmp_path, chat)
    with pytest.raises(RuntimeError, match='Network lost'):
        engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    saved = WorkspaceStore(engine.workspace_path).load('node')
    pending = TaskState.from_dict(saved['task_state'])
    assert pending.stage == 'validating' and pending.last_valid_candidate is None
    assert pending.coupled_relation['patch'] and pending.coupled_relation['stage'] == 'assessing'
    engine.task_state = pending
    engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    writers = [request for request, _ in chat.requests if 'node integration Writer' in request[0].content]
    assert len(writers) == 1
    assert len(engine.history) == 1
    assert sum('independent node Assessment' in request[0].content for request, _ in chat.requests) == 1


def test_coupled_checkpoint_corruption_stops_without_node_writer_retry(tmp_path, monkeypatch):
    force_relation_handoff(monkeypatch)
    chat = InterruptedGraphChat()
    engine, task = fixture(tmp_path, chat)
    with pytest.raises(RuntimeError):
        engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    saved = WorkspaceStore(engine.workspace_path).load('node')
    saved['task_state']['coupled_relation']['intermediate_hash'] = 'wrong'
    engine.task_state = TaskState.from_dict(saved['task_state'])
    before_calls = len(chat.requests)
    with pytest.raises(IntegrationIncomplete, match='relation coordination'):
        engine._run_task(task, 'node', aliases={'A': 'A', 'B': 'A'})
    assert len(chat.requests) == before_calls
    assert engine.task_state.coupled_relation['controller_error']


def test_mandatory_noop_assessment_receives_current_owner_and_relations(tmp_path):
    from cortex.capabilities.graph_merge.integration.relation_context import relation_context
    engine, _ = fixture(tmp_path, NodeChat())
    task = LocalTask('obligation', 'graph_assess', {'A'}, feedback=[{'kind': 'node_relation_followup'}])
    packet = relation_context(engine.ctx, engine.current, task, before=engine.current, assessment=True)
    assert 'A' in {node['id'] for node in packet['nodes']}
    assert 'e-A' in {edge['id'] for edge in packet['edges']}


def test_nested_real_controller_feedback_repairs_same_writer_without_node_replay(tmp_path,monkeypatch):
    force_relation_handoff(monkeypatch)
    class Feedback(NodeChat):
        reported=False
        def invoke(self,messages,config=None):
            local='node integration Writer' in messages[0].content or 'independent node Assessment' in messages[0].content
            if not local:
                names={tool['function']['name'] for tool in self.tools}
                rt=self.engine.ctx.reconciliation_runtime
                if 'edit_draft' not in names and not self.reported:
                    self.reported=True;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                    return AIMessage(content=json.dumps({'explanation':'The retained relation description needs a precise business role.',
                        'feedback':[{'problem':'The explanation is too vague.','required_outcome':'Clarify the existing representation.',
                            'refs':[{'kind':'edge','id':'e-A','view':'candidate','field':'properties.description'}]}]}))
                if 'edit_draft' in names and rt.feedback.get('semantic_issues') and not rt.draft.changes:
                    self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                    return AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'clarify','args':{'put_changes':[{
                        'change_id':'description','kind':'edge_update','edge_id':'e-A',
                        'set':{'properties':{'description':'The accepted object is represented by the original a field.'}},
                        'reason':'Clarify the retained original representation without changing its scope.'}]}}])
            return super().invoke(messages,config)
    chat=Feedback();engine,task=fixture(tmp_path,chat)
    engine._run_task(task,'nested-feedback',aliases={'A':'A','B':'A'})
    assert len(engine.history)==1
    assert sum('node integration Writer' in request[0].content for request,_ in chat.requests)==1
    assert next(e for e in engine.current.graph.edges if e.id=='e-A').properties['description'].startswith('The accepted object')


@pytest.mark.parametrize('window',['action_saved','compiled'])
def test_nested_complete_edit_intent_resumes_without_any_writer_replay(tmp_path,monkeypatch,window):
    force_relation_handoff(monkeypatch)
    chat=NodeChat();engine,task=fixture(tmp_path,chat)
    original=engine._persist;stopped=False
    def persist(run_id):
        nonlocal stopped
        original(run_id)
        coupled=engine.task_state.coupled_relation or {}
        if (not stopped and coupled.get('complete_draft_action') and
                (window=='action_saved' or coupled.get('stage')=='compiled_unsubmitted')):
            stopped=True;raise RuntimeError('Nested complete edit saved')
    engine._persist=persist
    with pytest.raises(RuntimeError,match='Nested complete edit saved'):
        engine._run_task(task,'node',aliases={'A':'A','B':'A'})
    saved=WorkspaceStore(engine.workspace_path).load('node')
    assert saved['task_state']['coupled_relation']['complete_draft_action']
    engine.task_state=TaskState.from_dict(saved['task_state'])
    engine._run_task(task,'node',aliases={'A':'A','B':'A'})
    assert sum('node integration Writer' in messages[0].content for messages,_ in chat.requests)==1
    assert sum(any(t['function']['name']=='edit_draft' for t in tools) for _,tools in chat.requests)==1
    assert len(engine.history)==1
