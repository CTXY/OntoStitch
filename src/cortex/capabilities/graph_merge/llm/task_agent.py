"""Shared task tools and structured outputs for the serial merge roles."""
import json
from importlib.resources import files
from itertools import count

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from cortex.capabilities.graph_merge.integration.compiler import identity_map
from cortex.capabilities.graph_merge.integration.edit_feedback import validation_feedback
from cortex.capabilities.graph_merge.integration.models import IdentityDecision
from cortex.capabilities.graph_merge.integration.patch import MergePatch, compile_patch
from cortex.capabilities.graph_merge.integration.proposal_scope import scope_error
from cortex.capabilities.graph_merge.integration.review import Review, validate_review
from cortex.capabilities.graph_merge.integration.tools import build_tools
from cortex.capabilities.graph_merge.llm.input_budget import check_request
from cortex.capabilities.graph_merge.llm.packets import action_message
from cortex.capabilities.graph_merge.llm.task_tools import build_read_tools
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery


def failure_feedback(error):
    result = {'status': 'rejected_unapplied', 'error': validation_feedback(error), **scope_error(error)}
    for attribute in ('before', 'after', 'missing_sources', 'counterexamples'):
        if hasattr(error, attribute):
            result[attribute] = [v.model_dump(mode='json') if hasattr(v, 'model_dump') else v
                                 for v in getattr(error, attribute)]
    return result


class IncrementalIntegrationAgent:
    contract_version = 'merge-task-v2'

    def __init__(self, llm, *, max_calls=12, trace_sink=None, request_max_characters=210000):
        if request_max_characters < 1:
            raise ValueError('request_max_characters must be positive')
        self.llm, self.max_calls, self.trace_sink = llm, max_calls, trace_sink
        self.request_max_characters = request_max_characters
        self.call_budget = None

    def invoke(self, phase, context, payload):
        identity = phase == 'identity'
        writer = phase in {'consolidate', 'reconcile'}
        output = IdentityDecision if identity else MergePatch if writer else Review
        root = files('cortex.capabilities.graph_merge.llm.prompts')
        name = 'integration_identity.md' if identity else f'incremental_{phase}.md'
        if phase in {'reconcile', 'graph_assess'}:
            name = 'incremental_relation_write.md' if writer else 'incremental_relation_assess.md'
        system = root.joinpath(name).read_text()
        if not identity:
            shared = 'incremental_content_shared.md' if phase in {'consolidate', 'node_assess'} else 'incremental_relation_shared.md'
            system = root.joinpath(shared).read_text() + '\n\n' + system
        system += '\n\n' + root.joinpath('incremental_protocol.md').read_text()
        messages = [SystemMessage(system), HumanMessage(json.dumps(payload, ensure_ascii=False, separators=(',', ':')))]
        before = getattr(context, 'task_before', None)
        if identity:
            raw = {t.name: t for t in build_tools(context)}
            lookup = {name: raw[name] for name in ('inspect_graph', 'read_evidence', 'read_guidance')}
            context.read_ids.update(n['id'] for n in payload.get('nodes', []) if n['id'] in context.nodes)
        else:
            lookup = build_read_tools(context, payload, before=before,
                draft_provider=lambda: getattr(context, 'active_draft', None))
        completed = None

        def validate(value):
            nonlocal completed
            if identity:
                identity_map(context, value)
            elif writer:
                context.active_draft = value.model_dump(mode='json', exclude_unset=True)
                submit = getattr(context, 'compile_task_patch', None)
                if submit:
                    compiled = submit(value)
                else:
                    batch = context.active_batch
                    compiled = compile_patch(context, before or batch.base, value,
                        targets=batch.targets, payload=payload)
                context.last_compiled_patch = compiled
            else:
                value = validate_review(value, context, before=before)
            completed = value
            return {'status': 'validated', 'committed': False}

        @tool(args_schema=MergePatch)
        def submit_patch(**arguments):
            """Submit the complete local draft relative to the fixed before graph.

            Replaces the previous draft. Omitted operations are withdrawn; omitted
            object fields keep their before value. A valid candidate ends this
            Writer and awaits independent assessment, without committing a graph.
            """
            return validate(MergePatch.model_validate(arguments))

        final_tool = submit_patch if writer else output
        result_name = 'submit_patch' if writer else output.__name__
        call_sink = self.trace_sink
        if hasattr(self.trace_sink, 'path'):
            from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
            call_sink = FileSystemGraphMergeTraceSink(self.trace_sink.path.with_name('model_calls.jsonl'))
        trace = EpisodeTrace(call_sink, phase, budget=self.call_budget)
        fallback = False
        fallback_announced = False
        try:
            for step in (count() if self.max_calls is None else range(self.max_calls)):
                final = self.max_calls is not None and step == self.max_calls - 1
                available = list(lookup.values()) if fallback else [*lookup.values(), final_tool]
                if final:
                    available = [] if fallback else [final_tool]
                check_request(messages, available, max_characters=self.request_max_characters)
                response = invoke_with_recovery(self, available, messages,
                    config={'callbacks': [trace]}, tool_choice='auto' if fallback else 'required')
                messages.append(action_message(response))
                if response.invalid_tool_calls:
                    for call in response.invalid_tool_calls:
                        messages.append(ToolMessage('Malformed tool JSON; no proposal was applied.', tool_call_id=call['id']))
                    fallback = True
                calls = response.tool_calls
                if not calls and not response.invalid_tool_calls:
                    try:
                        if writer:
                            raw = json.loads(response.content)
                            context.active_draft = raw
                            if getattr(context, 'record_task_draft', None):
                                context.record_task_draft(raw)
                        validate(output.model_validate_json(response.content))
                        return completed
                    except (ValueError, TypeError, KeyError) as error:
                        feedback = failure_feedback(error)
                        if writer and getattr(context, 'record_task_error', None):
                            context.record_task_error(error)
                        messages.append(HumanMessage(json.dumps(feedback, ensure_ascii=False, default=str)))
                        if isinstance(error, (ValidationError, json.JSONDecodeError)):
                            fallback = True
                for call in calls:
                    try:
                        if call['name'] == result_name:
                            if response.invalid_tool_calls or len(calls) != 1:
                                raise ValueError('Read first, then submit one result alone')
                            if writer:
                                context.active_draft = call['args']
                                if getattr(context, 'record_task_draft', None):
                                    context.record_task_draft(call['args'])
                            result = validate(output.model_validate(call['args']))
                        elif call['name'] in lookup and not final:
                            result = lookup[call['name']].invoke(call['args'])
                            if isinstance(result, dict) and result.get('view') == 'candidate':
                                ids = {r['id'] for key in ('items', 'endpoint_nodes') for r in result.get(key, []) if 'id' in r}
                                context.task_visible_ids = getattr(context, 'task_visible_ids', set()) | ids
                        else:
                            raise ValueError('Unknown or unavailable tool')
                    except (ValueError, TypeError, KeyError) as error:
                        result = failure_feedback(error)
                        if writer and getattr(context, 'record_task_error', None):
                            context.record_task_error(error)
                        packet = result.get('required_context') or {}
                        context.task_visible_ids = getattr(context, 'task_visible_ids', set()) | {
                            r['id'] for key in ('nodes', 'edges') for r in packet.get(key, [])}
                        if call['name'] == result_name and isinstance(error, ValidationError):
                            fallback = True
                    trace.record('tool_result', name=call['name'], arguments=call['args'], result=result)
                    if self.trace_sink:
                        self.trace_sink.record({'event_type': 'tool_result', 'component': f'integration_{phase}',
                                                'name': call['name'], 'arguments': call['args'], 'result': result})
                    messages.append(ToolMessage(json.dumps(result, ensure_ascii=False, default=str), tool_call_id=call['id']))
                if completed is not None:
                    return completed
                if fallback and not fallback_announced:
                    messages.append(HumanMessage('Return the final result as plain JSON matching this schema: '
                                                 + json.dumps(output.model_json_schema(), ensure_ascii=False)))
                    fallback_announced = True
            raise RuntimeError(f'{phase} exhausted the configured local call budget; graph unchanged')
        finally:
            if self.trace_sink:
                self.trace_sink.record({'event_type': 'agent_episode', 'component': f'integration_{phase}',
                    'task_id': payload.get('task_id'), 'input_payload': payload, 'calls': trace.calls,
                    'parsed_response': completed.model_dump(mode='json') if completed else None})
