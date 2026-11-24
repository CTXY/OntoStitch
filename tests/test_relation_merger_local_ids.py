"""Exact presentation aliases preserve native receipts and checkpoint bindings."""
from copy import deepcopy
from importlib.resources import files
from types import SimpleNamespace
import json

import pytest
from langchain_core.messages import AIMessage

from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults, invoke_relation_merger
from cortex.capabilities.graph_merge.llm.relation_merger_ids import make_adapter
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash


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


def legacy(packet, *, result=False):
    system = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text()
    cp = {'binding': canonical_hash({'system': system, 'packet': packet, 'schema': RelationResults.model_json_schema()}),
          'requests': 16, 'evidence': {}, 'feedback': {'error': 'Repeated long identifier typo'}}
    if result:
        cp['result'] = {'results': [{'group_id': group['group_id'], 'description': 'Already accepted model output.'}
                                  for group in packet['groups']]}
    return cp


def test_failed_long_id_request_is_archived_but_completed_legacy_result_is_reused():
    original = packet(); old = legacy(original); before = deepcopy(old)
    chat = Chat(); agent = SimpleNamespace(llm=chat, max_calls=3, request_max_characters=210000)
    invoke = make_adapter(invoke_relation_merger); saved = []
    invoke(agent, SimpleNamespace(evidence=()), original, checkpoint=old, save=saved.append)
    assert old == before and saved[-1]['legacy_request_checkpoint'] == before
    assert saved[-1]['requests'] == 1 and saved[-1]['legacy_request_checkpoint']['requests'] == 16
    count = chat.calls
    result = invoke(agent, SimpleNamespace(evidence=()), original, checkpoint=legacy(original, result=True))
    assert chat.calls == count and result.results[0].description == 'Already accepted model output.'


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


def test_install_is_idempotent(monkeypatch):
    from cortex.capabilities.graph_merge.llm import relation_merger
    from cortex.capabilities.graph_merge.llm.relation_merger_ids import install
    monkeypatch.setattr(relation_merger, 'invoke_relation_merger', lambda *a, **kw: None)
    install()
    first = relation_merger.invoke_relation_merger
    install()
    assert relation_merger.invoke_relation_merger is first
