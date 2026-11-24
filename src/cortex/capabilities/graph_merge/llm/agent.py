"""Shared task tools and structured outputs for the serial merge roles."""
from importlib.resources import files
from itertools import count

from langchain_core.tools import tool

from cortex.capabilities.graph_merge.identity.contract import identity_map
from cortex.capabilities.graph_merge.runtime.errors import failure_feedback
from cortex.capabilities.graph_merge.identity.contract import IdentityDecision
from cortex.capabilities.graph_merge.consolidation.compiler import MergePatch, compile_patch
from cortex.capabilities.graph_merge.consolidation.review import Review, validate_review
from cortex.capabilities.graph_merge.identity.tools import build_tools
from cortex.capabilities.graph_merge.llm.budget import check_request
from cortex.capabilities.graph_merge.llm.packets import (
    TaskDialogue,
    compact_node_packet,
    output_protocol_feedback,
)
from cortex.capabilities.graph_merge.llm.recovery import (
    OutputProtocolRecoveryExhausted,
    RecoveryTracker,
    TaskNoProgress,
)
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery

__all__ = ['IncrementalIntegrationAgent', 'OutputProtocolRecoveryExhausted', 'TaskNoProgress']




class IncrementalIntegrationAgent:
    contract_version = 'merge-task-v6-local-relations'
    node_local_contract = True
    reconciliation_contract = True
    relation_merger_contract = True
    relation_merger_batch_size = 10

    def __init__(self, llm, *, max_calls=12, trace_sink=None, request_max_characters=210000):
        if request_max_characters < 1:
            raise ValueError('request_max_characters must be positive')
        self.llm, self.max_calls, self.trace_sink = llm, max_calls, trace_sink
        self.request_max_characters = request_max_characters
        self.call_budget = None

    def invoke(self, phase, context, payload):
        if phase in {'reconcile', 'graph_assess'}:
            from cortex.capabilities.graph_merge.reconciliation.agent import invoke_reconciliation
            return invoke_reconciliation(self, phase, context, payload)
        identity = phase == 'identity'
        writer = phase in {'consolidate', 'reconcile'}
        local_node = phase in {'consolidate', 'node_assess'}
        output = IdentityDecision if identity else MergePatch if writer else Review
        compiler_payload = payload
        before = getattr(context, 'task_before', None)
        if local_node:
            from cortex.capabilities.graph_merge.consolidation.contract import node_output_model, node_system
            from cortex.capabilities.graph_merge.consolidation.presentation import build_node_packet, NodeTaskDialogue
            payload = build_node_packet(context, {**payload, 'phase': phase}, before=before, candidate=getattr(context, 'candidate', None))
            output = node_output_model(phase)
            system = node_system(phase, payload['group']['node_type'])
            dialogue = NodeTaskDialogue(system, payload, context=context)
        else:
            root = files('cortex.capabilities.graph_merge.llm.prompts')
            system = root.joinpath('integration_identity.md').read_text()
            system += '\n\n' + root.joinpath('incremental_protocol.en.md').read_text()
            from cortex.capabilities.graph_merge.llm.prompt_language import english_contract
            payload = compact_node_packet(payload)
            if 'local_contracts' in payload:
                payload['local_contracts'] = english_contract(payload['local_contracts'])
            dialogue = TaskDialogue(system, payload)
        if identity:
            raw = {t.name: t for t in build_tools(context)}
            lookup = {name: raw[name] for name in ('inspect_graph', 'read_evidence', 'read_guidance')}
            from cortex.capabilities.graph_merge.identity.guidance import build_identity_guidance
            lookup['read_guidance'] = build_identity_guidance(context)
            context.read_ids.update(n['id'] for n in payload.get('nodes', []) if n['id'] in context.nodes)
        elif local_node:
            from cortex.capabilities.graph_merge.consolidation.contract import build_node_read_tools
            lookup = build_node_read_tools(context, payload)
        dialogue.read_names = set(lookup)
        completed = None

        def validate(value):
            nonlocal completed
            if local_node:
                from cortex.capabilities.graph_merge.consolidation.contract import normalize_node_result
                value, followups = normalize_node_result(phase, value, payload=payload, base=before)
                if writer:
                    context.node_writer_followups = followups
                else:
                    context.node_assessment_followups = followups
            if identity:
                identity_map(context, value)
            elif writer:
                if not local_node:
                    context.active_draft = value.model_dump(mode='json', exclude_unset=True)
                submit = getattr(context, 'compile_task_patch', None)
                if submit:
                    compiled = submit(value)
                else:
                    batch = context.active_batch
                    compiled = compile_patch(context, before or batch.base, value,
                        targets=batch.targets, payload=compiler_payload)
                context.last_compiled_patch = compiled
            else:
                value = validate_review(value, context, before=before)
            completed = value
            return {'status': 'validated', 'committed': False}

        @tool(args_schema=output)
        def submit_patch(**arguments):
            """Submit the complete local draft relative to the fixed before graph.

            Replaces the previous draft. Omitted operations are withdrawn.
            NodeContent is complete replacement as defined by the role contract. A valid candidate ends this
            Writer and awaits independent assessment, without committing a graph.
            """
            return validate(output.model_validate(arguments))

        @tool('Review', args_schema=output)
        def local_review(**arguments):
            """Assess the actual local node candidate; keep relation followups separate from node issues."""
            return validate(output.model_validate(arguments))

        final_tool = submit_patch if writer else local_review if local_node else output
        result_name = 'submit_patch' if writer else 'Review' if local_node else output.__name__
        call_sink = self.trace_sink
        if hasattr(self.trace_sink, 'path'):
            from cortex.infrastructure.artifacts.graph_merge_trace import (
                FileSystemGraphMergeTraceSink,
            )
            call_sink = FileSystemGraphMergeTraceSink(self.trace_sink.path.with_name('model_calls.jsonl'))
        trace = EpisodeTrace(call_sink, phase, budget=self.call_budget)
        from cortex.capabilities.graph_merge.runtime.state import (
            canonical_hash,
        )
        policy = canonical_hash({'system': system, 'result': output.model_json_schema(),
            'reads': {key: {'description': value.description, 'schema': value.args_schema.model_json_schema()}
                      for key, value in lookup.items()}})
        scope = {'task': payload.get('task_id', getattr(getattr(context, 'active_task', None), 'id', None)),
                 'phase': phase, 'before': canonical_hash(before) if before is not None else None,
                 'candidate': canonical_hash(context.candidate) if getattr(context, 'candidate', None) is not None else None,
                 'policy': policy}
        recovery_key = 'agent:' + canonical_hash(scope)
        load = getattr(context, 'load_task_recovery', None)
        saved = load(recovery_key) if load else None
        if saved is not None:
            required = {'version', 'failure_rounds', 'protocol_failures', 'protocol_error',
                        'fallback', 'failures', 'last_failure', 'blocked'}
            if (not isinstance(saved, dict) or not isinstance(saved.get('tracker'), dict)
                    or not required <= saved['tracker'].keys() or not isinstance(saved.get('dialogue'), dict)):
                raise ValueError('Incomplete saved Agent recovery envelope')
            if (saved.get('scope') != scope
                    or saved.get('dialogue_hash') != canonical_hash(saved['dialogue'])
                    or saved.get('tracker_hash') != canonical_hash(saved['tracker'])):
                raise ValueError('Saved Agent recovery does not match the task/policy or its dialogue')
        tracker = RecoveryTracker(saved['tracker'] if saved is not None else None)
        if saved is not None:
            dialogue.restore(saved['dialogue'], draft_authoritative=writer)
            visibility = saved['dialogue'].get('candidate_visibility')
            if visibility is not None:
                candidate = getattr(context, 'candidate', None)
                objects = {item.id for group in (candidate.graph.nodes, candidate.graph.edges) for item in group} if candidate is not None else set()
                if (not isinstance(visibility, dict) or visibility.get('candidate_hash') != scope['candidate']
                        or not isinstance(visibility.get('ids'), list)
                        or any(not isinstance(ident, str) or ident not in objects for ident in visibility['ids'])):
                    raise ValueError('Saved compiler visibility does not match the candidate snapshot')
                context.task_visible_ids = set(getattr(context, 'task_visible_ids', ())) | set(visibility['ids'])
        if writer and isinstance(payload.get('draft'), dict):
            context.active_draft = payload['draft']

        def persist_recovery():
            store = getattr(context, 'record_task_recovery', None)
            if store:
                snapshot = dialogue.snapshot()
                candidate = getattr(context, 'candidate', None)
                if candidate is not None and canonical_hash(candidate) == scope['candidate']:
                    objects = {item.id for group in (candidate.graph.nodes, candidate.graph.edges) for item in group}
                    snapshot['candidate_visibility'] = {'candidate_hash': scope['candidate'],
                        'ids': sorted(set(getattr(context, 'task_visible_ids', ())) & objects)}
                store(recovery_key, {'scope': scope, 'tracker': tracker.data,
                    'tracker_hash': canonical_hash(tracker.data),
                    'dialogue': snapshot, 'dialogue_hash': canonical_hash(snapshot)})

        def sync_draft_identity():
            if writer and dialogue.latest_draft == getattr(context, 'active_draft', None):
                ident = getattr(context, 'task_draft_id', None)
                if ident:
                    dialogue.draft_id = ident
                    dialogue.issues_draft_id = getattr(context, 'task_draft_errors_draft_id', ident)

        def record_tool(call, result):
            trace.record('tool_result', name=call.get('name'), arguments=call.get('args'), result=result)
            if self.trace_sink:
                self.trace_sink.record({'event_type': 'tool_result', 'component': f'integration_{phase}',
                    'name': call.get('name'), 'arguments': call.get('args'), 'result': result})

        def restore_visible_facts():
            # Read visibility must match restored model premises. It does not
            # grant unrelated writes; ProposalScope still checks coupling.
            visible = set(getattr(context, 'task_visible_ids', ()))
            def collect(packet, view=None):
                if not isinstance(packet, dict):
                    return
                view = packet.get('view', view)
                if view == 'candidate':
                    for key in ('items', 'endpoint_nodes', 'nodes', 'edges'):
                        visible.update(row['id'] for row in packet.get(key, []) if isinstance(row, dict) and 'id' in row)
                if isinstance(packet.get('facts'), dict):
                    collect(packet['facts'], view)
                for entry in packet.get('required_context', []):
                    collect(entry.get('result'), entry.get('request', {}).get('view'))
            for fact in dialogue.premises():
                collect(fact.get('result'), fact.get('args', {}).get('view'))
            context.task_visible_ids = visible

        try:
            tracker.ensure_active(facts_hash=dialogue.premise_hash(policy=policy))
            for step in (count() if self.max_calls is None else range(self.max_calls)):
                final = self.max_calls is not None and step == self.max_calls - 1
                if tracker.fallback:
                    dialogue.use_fallback(output.model_json_schema())
                available = ([] if local_node else list(lookup.values())) if tracker.fallback else [*lookup.values(), final_tool]
                if final or tracker.submit_only:
                    available = [] if tracker.fallback else [final_tool]
                messages = dialogue.messages()
                restore_visible_facts()
                from langchain_core.utils.function_calling import convert_to_openai_tool
                advertised = [convert_to_openai_tool(item) for item in available]
                trace.record('request_contract', tool_names=[item['function']['name'] for item in advertised],
                    tools_hash=canonical_hash(advertised), message_count=len(messages),
                    facts_hash=dialogue.premise_hash(policy=policy))
                check_request(messages, available, max_characters=self.request_max_characters,
                    on_measure=lambda details: trace.record('request_budget', **details))
                # Capture these premises before invoking the model. The new
                # error's counterexamples belong only to the following round.
                input_facts = dialogue.premise_hash(policy=policy)
                context.task_relevant_facts = dialogue.premises()
                response = invoke_with_recovery(self, available, messages,
                    config={'callbacks': [trace]}, tool_choice='auto' if tracker.fallback else 'required')
                calls = response.tool_calls
                invalid = response.invalid_tool_calls
                protocol_error = output_protocol_feedback(response, result_name=result_name,
                    allowed_names={getattr(t, 'name', getattr(t, '__name__', None)) for t in available}
                    | ({result_name} if tracker.fallback else set()))
                if protocol_error:
                    tracker.fallback = tracker.fallback or bool(invalid)
                    for call in [*calls, *invalid]:
                        record_tool(call, protocol_error)
                    dialogue.plain_failure(response, protocol_error)
                    try:
                        tracker.observe('output_protocol', [c.get('args') for c in [*calls, *invalid]],
                                        protocol_error, input_facts)
                    finally:
                        persist_recovery()
                    continue

                if calls and all(c['name'] in lookup for c in calls):
                    results = []
                    for call in calls:
                        try:
                            result = lookup[call['name']].invoke(call['args'])
                            if isinstance(result, dict) and result.get('view') == 'candidate':
                                ids = {r['id'] for key in ('items', 'endpoint_nodes') for r in result.get(key, []) if 'id' in r}
                                context.task_visible_ids = getattr(context, 'task_visible_ids', set()) | ids
                        except (ValueError, TypeError, KeyError) as error:
                            result = failure_feedback(error)
                        record_tool(call, result)
                        results.append((call['id'], result))
                    dialogue.exchange(response, results, result_name=result_name)
                    sync_draft_identity()
                    persist_recovery()
                    continue

                raw = None
                call = calls[0] if calls else None
                try:
                    if call:
                        raw = call['args']
                    else:
                        # Share the exact-envelope parser with reconciliation:
                        # one JSON fence is harmless; surrounding prose is not.
                        from cortex.capabilities.graph_merge.reconciliation.agent import _json_object
                        raw = _json_object(response.content)
                    if not isinstance(raw, dict):
                        raise TypeError('A complete result must be a JSON object; the previous draft is unchanged')
                    canonical_hash(raw)  # Reject non-finite/non-JSON values before changing any draft.
                except (ValueError, TypeError) as error:
                    protocol_error = {'status': 'rejected_unapplied', 'code': 'output_protocol', 'error': str(error)}
                    dialogue.plain_failure(response, protocol_error)
                    tracker.fallback = True
                    try:
                        tracker.observe('output_protocol', [raw if raw is not None else response.content],
                                        protocol_error, input_facts)
                    finally:
                        persist_recovery()
                    continue

                stage = 'output_schema'
                try:
                    if writer:
                        if local_node:
                            context.node_local_raw = raw
                        context.active_draft = raw
                        if getattr(context, 'record_task_draft', None):
                            context.record_task_draft(raw)
                    value = output.model_validate(raw)
                    stage = 'patch_compile' if writer else 'identity_validation' if identity else 'assessment_validation'
                    context.task_failure_stage = stage
                    validate(value)
                except (ValueError, TypeError, KeyError) as error:
                    feedback = {**failure_feedback(error), 'stage': stage}
                    if writer and getattr(context, 'record_task_error', None):
                        context.task_failure_stage = stage
                        context.record_task_error(error)
                    packet = feedback.get('required_context') or {}
                    context.task_visible_ids = getattr(context, 'task_visible_ids', set()) | {
                        r['id'] for key in ('nodes', 'edges') for r in packet.get(key, [])}
                    # Valid native tool JSON with missing/wrong fields is a
                    # schema repair, not a broken serialization protocol.
                    tracker.fallback = tracker.fallback or (stage == 'output_schema' and not local_node)
                    dialogue.set_draft(raw, feedback)
                    sync_draft_identity()
                    if call:
                        record_tool(call, feedback)
                    try:
                        tracker.observe(stage, [raw], feedback, input_facts)
                    finally:
                        persist_recovery()
                    continue
                if call:
                    record_tool(call, {'status': 'validated', 'committed': False})
                dialogue.set_draft(raw)
                sync_draft_identity()
                tracker.success()
                persist_recovery()
                context.task_relevant_facts = dialogue.premises()
                return completed
            raise RuntimeError(f'{phase} exhausted the configured local call budget; graph unchanged')
        finally:
            if self.trace_sink:
                self.trace_sink.record({'event_type': 'agent_episode', 'component': f'integration_{phase}',
                    'task_id': payload.get('task_id'), 'input_payload': payload, 'calls': trace.calls,
                    'parsed_response': completed.model_dump(mode='json') if completed else None})
