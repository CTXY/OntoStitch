"""Small description-only model protocol, deliberately without Assessment."""
from copy import deepcopy
from importlib.resources import files
import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cortex.capabilities.graph_merge.evidence import EvidencePresentation
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.llm.budget import check_request
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery


class RelationDescription(BaseModel):
    model_config = ConfigDict(extra='forbid')
    group_id: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator('group_id', 'description')
    @classmethod
    def nonempty(cls, value):
        if not value.strip(): raise ValueError('Nonempty text required')
        return value.strip()


class RelationResults(BaseModel):
    model_config = ConfigDict(extra='forbid')
    results: list[RelationDescription] = Field(min_length=1, max_length=10)


class ReadEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ids: list[str] = Field(min_length=1)


def invoke_relation_merger(agent, context, packet, *, checkpoint=None, save=None):
    system = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text()
    key = canonical_hash({'system': system, 'packet': packet,
        'schema': RelationResults.model_json_schema()})
    cp = deepcopy(checkpoint) if checkpoint else {'binding': key, 'requests': 0, 'evidence': {}, 'feedback': {}}
    if cp.get('binding') != key:
        raise ValueError('Relation merger model recovery binding differs')
    if cp.get('result') is not None:
        result = RelationResults.model_validate(cp['result'])
        ids = [row.group_id for row in result.results]
        if len(ids) != len(set(ids)) or set(ids) != {row['group_id'] for row in packet['groups']}:
            raise ValueError('Saved relation result does not match its exact group manifest')
        return result
    evidence = EvidencePresentation(context)
    allowed = {ident for row in [*packet['nodes'], *packet['relations']]
               for ident in row.get('evidence_ids', [])}
    for node in packet['nodes']:
        allowed.update(ident for alt in node.get('alternatives', []) for ident in alt['evidence_ids'])
    sink = getattr(agent, 'trace_sink', None)
    if hasattr(sink, 'path'):
        from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
        sink = FileSystemGraphMergeTraceSink(sink.path.with_name('model_calls.jsonl'))
    trace = EpisodeTrace(sink, 'relation_merger', budget=getattr(agent, 'call_budget', None))
    tools = [StructuredTool.from_function(lambda **kwargs: kwargs, name='read_evidence',
        description='Read original evidence by displayed authorized IDs.', args_schema=ReadEvidence),
        StructuredTool.from_function(lambda **kwargs: kwargs, name='submit_relations',
        description='Return one description for every supplied group, with no graph or controller fields.', args_schema=RelationResults)]
    def persist():
        if save: save(deepcopy(cp))
    max_calls = getattr(agent, 'max_calls', None) or 8
    while cp['requests'] < max_calls:
        messages = [SystemMessage(system), HumanMessage(json.dumps(packet, ensure_ascii=False, sort_keys=True)),
            HumanMessage('read_evidence (data):\n' + json.dumps(cp['evidence'], ensure_ascii=False, sort_keys=True)),
            HumanMessage('feedback:\n' + json.dumps(cp['feedback'], ensure_ascii=False, sort_keys=True))]
        check_request(messages, tools, getattr(agent, 'request_max_characters', 210000),
            on_measure=lambda row: trace.record('request_budget', **row),
            message_sections=('immutable_relation_batch', 'read_evidence', 'feedback'))
        trace.record('request_contract', tool_names=[tool.name for tool in tools], message_count=4,
            input_hash=packet['input_hash'])
        cp['requests'] += 1
        persist()
        response = invoke_with_recovery(agent, tools, messages,
            config={'callbacks': [trace]}, tool_choice='required')
        calls = list(getattr(response, 'tool_calls', ()) or ())
        try:
            if getattr(response, 'invalid_tool_calls', ()):
                raise ValueError('Malformed tool call: use the advertised schema')
            if len(calls) == 1 and calls[0]['name'] == 'submit_relations':
                result = RelationResults.model_validate(calls[0]['args'])
                ids = [row.group_id for row in result.results]
                if len(ids) != len(set(ids)) or set(ids) != {row['group_id'] for row in packet['groups']}:
                    raise ValueError('Each supplied group must occur exactly once, with no extra groups')
                cp['result'] = result.model_dump(mode='json')
                persist()
                trace.record('tool_result', name='submit_relations', arguments=calls[0]['args'], result={'status': 'validated_no_assessment'})
                return result
            if not calls or any(call['name'] != 'read_evidence' for call in calls):
                raise ValueError('Use only a read_evidence batch or one exclusive submit_relations call')
            requested = set()
            for call in calls:
                parsed = ReadEvidence.model_validate(call['args'])
                if set(parsed.ids) - allowed:
                    raise ValueError('Evidence IDs are outside this relation batch')
                requested.update(parsed.ids)
            fetched = evidence.read(sorted(requested), refresh=True)
            if fetched['missing_ids']:
                raise ValueError('Requested evidence is unavailable: ' + str(fetched['missing_ids']))
            for item in fetched['items']:
                for ident in item.get('evidence_ids', [item['id']]): cp['evidence'][ident] = item['content']
            cp['feedback'] = {'status': 'evidence_supplied'}
            trace.record('tool_result', name='read_evidence', arguments={'ids': sorted(requested)},
                result={'status': 'supplied', 'ids': sorted(requested)})
        except (ValueError, TypeError, KeyError) as error:
            cp['feedback'] = {'status': 'rejected_unapplied', 'error': str(error)}
            trace.record('tool_result', name=calls[0]['name'] if calls else 'output', result=cp['feedback'])
        persist()
    raise RuntimeError('Relation merger local model request limit reached; checkpoint retained')
