"""Explicit presentation modes preserve exact graph identity without global mutation."""
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace
import json

import pytest
from langchain_core.messages import AIMessage

from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults, invoke_relation_merger
from cortex.capabilities.graph_merge.llm.relation_merger_ids import make_adapter
from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def packet():
    result = {'groups': [{'group_id': 'relation-group:' + letter * 64, 'member_edge_ids': ['E' + letter]}
                         for letter in ('a', 'b')],
              'nodes': [{'id': 'N', 'content': {'description': 'Business statement, not an ID alias.'}}],
              'relations': [{'id': 'Ea', 'properties': {'description': 'Original relation.', 'alternative_ids': ['A']}}],
              'contracts': []}
    result['input_hash'] = canonical_hash(result)
    return result


class Chat:
    calls = 0
    seen = None
    def bind_tools(self, *args, **kwargs):
        return self
    def invoke(self, messages, config=None):
        self.calls += 1
        self.seen = json.loads(messages[1].content)
        return AIMessage(content='', tool_calls=[{'id': 'result', 'name': 'submit_relations',
            'args': {'results': [{'group_id': row['group_id'], 'description': 'Preserved description ' + row['group_id']}
                                 for row in self.seen['groups']]}}])


def test_aliases_only_group_ids_and_resumes_saved_result_without_model_call():
    original = packet(); before = deepcopy(original); chat = Chat()
    agent = SimpleNamespace(llm=chat, max_calls=3, request_max_characters=210000)
    invoke = make_adapter(invoke_relation_merger); saved = []
    result = invoke(agent, SimpleNamespace(evidence=()), original, save=saved.append)
    assert [row.group_id for row in result.results] == [g['group_id'] for g in original['groups']]
    assert [g['group_id'] for g in chat.seen['groups']] == ['G1', 'G2']
    assert chat.seen['nodes'] == before['nodes'] and chat.seen['relations'] == before['relations']
    assert original == before
    assert saved[-1]['local_id_protocol']['original_input_hash'] == original['input_hash']
    assert saved[-1]['local_id_protocol']['alias_input_hash'] == chat.seen['input_hash']
    resumed = invoke(agent, SimpleNamespace(evidence=()), original, checkpoint=saved[-1])
    assert resumed == result and chat.calls == 1
    # Without its controller adapter, an alias checkpoint is not a native one.
    with pytest.raises(ValueError, match='binding differs'):
        invoke_relation_merger(agent, SimpleNamespace(evidence=()), original, checkpoint=saved[-1])


def native_checkpoint(packet, *, result=False):
    system = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text()
    cp = {'binding': canonical_hash({'system': system, 'packet': packet, 'schema': RelationResults.model_json_schema()}),
          'requests': 16, 'evidence': {}, 'feedback': {'error': 'Repeated long identifier typo'}}
    if result:
        cp['result'] = {'results': [{'group_id': group['group_id'], 'description': 'Already accepted model output.'}
                                  for group in packet['groups']]}
    return cp


@pytest.mark.parametrize('completed', [False, True])
def test_native_checkpoint_cannot_be_reinterpreted_as_local_ids(completed):
    original = packet(); checkpoint = native_checkpoint(original, result=completed)
    before = deepcopy(checkpoint)
    chat = Chat(); agent = SimpleNamespace(llm=chat, max_calls=3, request_max_characters=210000)
    with pytest.raises(ValueError, match='protocol or immutable manifest changed'):
        make_adapter(invoke_relation_merger)(agent, SimpleNamespace(evidence=()), original,
                                             checkpoint=checkpoint)
    assert checkpoint == before and chat.calls == 0


def test_manifest_changes_and_forged_original_hash_rejected_before_call():
    original = packet(); chat = Chat(); agent = SimpleNamespace(llm=chat, max_calls=3, request_max_characters=210000)
    invoke = make_adapter(invoke_relation_merger); saved = []
    invoke(agent, SimpleNamespace(evidence=()), original, save=saved.append)
    changed = deepcopy(original); changed['groups'].reverse()
    changed['input_hash'] = canonical_hash({k: v for k, v in changed.items() if k != 'input_hash'})
    with pytest.raises(ValueError, match='manifest changed'):
        invoke(agent, SimpleNamespace(evidence=()), changed, checkpoint=saved[-1])
    changed = deepcopy(original); changed['relations'][0]['properties']['description'] = 'Changed without rebinding'
    with pytest.raises(ValueError, match='actual content'):
        invoke(agent, SimpleNamespace(evidence=()), changed)
    assert chat.calls == 1


def test_no_fuzzy_correction_of_unknown_local_result_id():
    def wrong(*args, **kwargs):
        return {'results': [{'group_id': 'G9', 'description': 'Do not assign this to another group.'}]}
    with pytest.raises(ValueError, match='cover each displayed ID'):
        make_adapter(wrong)(None, None, packet())


def test_service_modes_are_explicit_isolated_and_preserve_sources_on_resume():
    from test_relation_merger import fixture
    from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
    from cortex.capabilities.graph_merge.llm import relation_merger as native_module
    from cortex.capabilities.graph_merge.reconciliation.relation_merger import consolidate_relations

    graph, context = fixture()
    original = graph.model_dump(mode='json')
    native_function = native_module.invoke_relation_merger
    local_chat, native_chat = Chat(), Chat()
    local_agent = IncrementalIntegrationAgent(local_chat, relation_id_mode='local')
    native_agent = IncrementalIntegrationAgent(native_chat)
    assert native_agent.relation_id_mode == 'native'
    saved = []
    local = consolidate_relations(local_agent, context, graph, save=saved.append)
    assert native_module.invoke_relation_merger is native_function
    assert [group['group_id'] for group in local_chat.seen['groups']] == ['G1']
    # The output and persisted service manifest always retain the original IDs.
    assert all(key.startswith('relation-group:') for key in local.checkpoint['results'])
    assert next(iter(local.checkpoint['batches'].values()))['local_id_protocol']['id_map']['G1'].startswith('relation-group:')
    assert {source.source_id for edge in local.state.graph.edges for source in edge.sources} == {'first', 'second'}
    resumed = consolidate_relations(local_agent, context, graph, checkpoint=saved[-1])
    assert resumed.state == local.state and local_chat.calls == 1
    native = consolidate_relations(native_agent, context, graph)
    assert all(group['group_id'].startswith('relation-group:') for group in native_chat.seen['groups'])
    assert native_module.invoke_relation_merger is native_function
    assert not any('local_id_protocol' in cp for cp in native.checkpoint['batches'].values())
    assert {source.source_id for edge in native.state.graph.edges for source in edge.sources} == {'first', 'second'}
    assert graph.model_dump(mode='json') == original
    with pytest.raises(ValueError, match='policy differs'):
        consolidate_relations(native_agent, context, graph, checkpoint=local.checkpoint)
    with pytest.raises(ValueError, match='policy differs'):
        consolidate_relations(local_agent, context, graph, checkpoint=native.checkpoint)
    assert local_chat.calls == native_chat.calls == 1


def test_agent_rejects_unknown_relation_presentation_mode_before_model_call():
    from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent
    with pytest.raises(ValueError, match='relation_id_mode must be native or local'):
        IncrementalIntegrationAgent(Chat(), relation_id_mode='guess')


def test_interrupted_local_request_resumes_bound_aliases_with_a_fresh_agent():
    from cortex.capabilities.graph_merge.llm.agent import IncrementalIntegrationAgent

    class Interrupted(Chat):
        def invoke(self, messages, config=None):
            self.calls += 1
            raise RuntimeError('Stopped after the request checkpoint was saved')

    original = packet()
    saved = []
    first = IncrementalIntegrationAgent(Interrupted(), relation_id_mode='local')
    invoke = make_adapter(invoke_relation_merger)
    with pytest.raises(RuntimeError, match='request checkpoint was saved'):
        invoke(first, SimpleNamespace(evidence=()), original, save=saved.append)
    interrupted = deepcopy(saved[-1])
    assert interrupted['requests'] == 1 and 'result' not in interrupted
    resumed_chat = Chat()
    resumed = IncrementalIntegrationAgent(resumed_chat, relation_id_mode='local')
    result = invoke(resumed, SimpleNamespace(evidence=()), original,
                    checkpoint=interrupted, save=saved.append)
    assert saved[-1]['requests'] == 2 and resumed_chat.calls == 1
    assert [row.group_id for row in result.results] == [group['group_id'] for group in original['groups']]
    assert [group['group_id'] for group in resumed_chat.seen['groups']] == ['G1', 'G2']
    assert interrupted['requests'] == 1 and 'result' not in interrupted
