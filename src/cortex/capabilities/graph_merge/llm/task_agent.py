"""Shared task tools and structured outputs for the serial merge roles."""
import json
from importlib.resources import files
from itertools import count

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
from cortex.capabilities.graph_merge.llm.packets import TaskDialogue, compact_node_packet
from cortex.capabilities.graph_merge.llm.task_tools import build_read_tools
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery


def failure_feedback(error):
    result = {'status': 'rejected_unapplied', 'error': validation_feedback(error), **scope_error(error)}
    for attribute in ('before', 'after', 'missing_sources', 'counterexamples'):
        if hasattr(error, attribute):
            result[attribute] = [v.model_dump(mode='json') if hasattr(v, 'model_dump') else v
                                 for v in getattr(error, attribute)]
    if getattr(error, 'details', None) is not None:
        result['details'] = error.details
    return result


class TaskNoProgress(RuntimeError):
    retryable = False


class IncrementalIntegrationAgent:
    contract_version = 'merge-task-v3'

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
            system += '\n\n' + root.joinpath('conflict_contract.md').read_text()
        system += '\n\n' + root.joinpath('incremental_protocol.md').read_text()
        payload = compact_node_packet(payload)
        dialogue = TaskDialogue(system, payload)
        before = getattr(context, 'task_before', None)
        if identity:
            raw = {t.name: t for t in build_tools(context)}
            lookup = {name: raw[name] for name in ('inspect_graph', 'read_evidence', 'read_guidance')}
            context.read_ids.update(n['id'] for n in payload.get('nodes', []) if n['id'] in context.nodes)
        else:
            lookup = build_read_tools(context, payload, before=before,
                draft_provider=lambda: getattr(context, 'active_draft', None))
        dialogue.read_names = set(lookup)
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
        rejected_outputs = {}
        try:
            for step in (count() if self.max_calls is None else range(self.max_calls)):
                final = self.max_calls is not None and step == self.max_calls - 1
                available = list(lookup.values()) if fallback else [*lookup.values(), final_tool]
                if final:
                    available = [] if fallback else [final_tool]
                messages = dialogue.messages()
                check_request(messages, available, max_characters=self.request_max_characters)
                response = invoke_with_recovery(self, available, messages,
                    config={'callbacks': [trace]}, tool_choice='auto' if fallback else 'required')
                results = []
                if response.invalid_tool_calls:
                    for call in response.invalid_tool_calls:
                        results.append((call['id'], 'Malformed tool JSON; no proposal was applied.'))
                    fallback = True
                calls = response.tool_calls
                if not calls and not response.invalid_tool_calls:
                    raw = None
                    try:
                        raw = json.loads(response.content)
                        if writer:
                            context.active_draft = raw
                            if getattr(context, 'record_task_draft', None):
                                context.record_task_draft(raw)
                        validate(output.model_validate(raw))
                        return completed
                    except (ValueError, TypeError, KeyError) as error:
                        feedback = failure_feedback(error)
                        if writer and isinstance(raw, dict) and getattr(context, 'record_task_error', None):
                            context.record_task_error(error)
                        dialogue.plain_failure(response, feedback, draft=raw if isinstance(raw, dict) else None)
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
                        if writer and call['name'] == result_name and getattr(context, 'record_task_error', None):
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
                    results.append((call['id'], result))
                dialogue.exchange(response, results, result_name=result_name)
                if completed is not None:
                    return completed
                # Exact no-progress is distinct from format repair: repeated
                # unchanged rejected output cannot consume an unlimited loop.
                rejected = [(call['args'], result) for call in calls if call['name'] == result_name
                            for call_id, result in results if call_id == call['id']
                            and isinstance(result, dict) and result.get('status') == 'rejected_unapplied']
                if not calls and not response.invalid_tool_calls:
                    rejected.append((raw if raw is not None else response.content, feedback))
                rejected.extend(((call['name'], call.get('args')), 'Malformed tool JSON; no proposal was applied.')
                                for call in response.invalid_tool_calls)
                for draft, error in rejected:
                    signature = json.dumps([draft, error], sort_keys=True, ensure_ascii=False, default=str)
                    rejected_outputs[signature] = rejected_outputs.get(signature, 0) + 1
                    if rejected_outputs[signature] >= 2:
                        raise TaskNoProgress('The identical draft and validation error repeated; checkpoint retained')
                if fallback:
                    dialogue.use_fallback(output.model_json_schema())
            raise RuntimeError(f'{phase} exhausted the configured local call budget; graph unchanged')
        finally:
            if self.trace_sink:
                self.trace_sink.record({'event_type': 'agent_episode', 'component': f'integration_{phase}',
                    'task_id': payload.get('task_id'), 'input_payload': payload, 'calls': trace.calls,
                    'parsed_response': completed.model_dump(mode='json') if completed else None})
