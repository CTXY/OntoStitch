"""Edit/compile/assess, then independent feedback; no decision or routing protocol."""
from copy import deepcopy
import json
import sys

from langchain_core.tools import StructuredTool

from cortex.capabilities.graph_merge.reconciliation.contract import (
    TOOLS, AssessmentResult, apply_edit_shape,
)
from cortex.capabilities.graph_merge.runtime.state import canonical_hash
from cortex.capabilities.graph_merge.llm.budget import check_request
from cortex.capabilities.graph_merge.reconciliation.permissions import ReconciliationRuntime
from cortex.capabilities.graph_merge.reconciliation.policy import reconciliation_system
from cortex.capabilities.graph_merge.llm.recovery import RecoveryTracker, TaskNoProgress
from cortex.capabilities.graph_merge.llm.trace import EpisodeTrace
from cortex.capabilities.graph_merge.llm.transport import invoke_with_recovery

SECTIONS = ('fixed_task_and_before_graph','current_draft_candidate_and_feedback')
DESCRIPTIONS = {
    'edit_draft': 'Incrementally edit the saved draft: withdraw old change_ids with remove_changes and add replacements with new change_ids in put_changes in the SAME call. Omitted entries remain. Conflicting edits are rejected before saving. Then compile and automatically request independent Assessment on success. Empty arguments explicitly request assessment of the current draft. Same content preserves its version.',
}


def _scope(agent, phase, context, runtime):
    from cortex.capabilities.graph_merge.reconciliation.presentation import presentation_policy
    return {'protocol':'readable-local-reconciliation-v2','phase':phase,'task_id':runtime.task_id,
        'before_id':runtime.before_id,
        'candidate':canonical_hash(context.candidate) if phase=='graph_assess' else None,
        'checks':canonical_hash([runtime.packet['signals'],runtime.packet['obligations']]),
        'policy':canonical_hash({'system':runtime.system,'reads':'complete-authorized-local-v1',
            'tools':{name:model.model_json_schema() for name,model in TOOLS.items()},
            'user_presentation':presentation_policy(),'assessment_output':AssessmentResult.model_json_schema(),'assessment_response_format':'json_object'})}


def prepare_runtime(agent, phase, context, payload, *, draft=None, fresh=False):
    current=getattr(context,'reconciliation_runtime',None)
    if not fresh and current is not None and current.phase==phase:
        if current.before_id==canonical_hash(getattr(context,'task_before',None)) and current.task_id==payload.get('task_id'):
            if draft is not None: current.set_draft(draft)
            return current
    runtime=ReconciliationRuntime(context,payload,phase)
    runtime.system=reconciliation_system('writer' if phase=='reconcile' else 'assessment',
        [row['kind'] for row in runtime.packet['signals']])
    scope=_scope(agent,phase,context,runtime)
    key='simple:'+canonical_hash(scope)
    loader=getattr(context,'load_task_recovery',None)
    saved=loader(key) if loader else None
    if saved is not None:
        required={'scope','runtime','runtime_hash','tracker','tracker_hash','requests'}
        tracker_fields={'version','failure_rounds','protocol_failures','protocol_error','fallback','failures','last_failure','blocked'}
        if (not isinstance(saved,dict) or not required<=saved.keys() or saved['scope']!=scope
                or saved['runtime_hash']!=canonical_hash(saved['runtime'])
                or not isinstance(saved['tracker'],dict) or not tracker_fields<=saved['tracker'].keys()
                or saved['tracker_hash']!=canonical_hash(saved['tracker'])
                or type(saved['requests']) is not int or saved['requests']<0):
            raise ValueError('Incomplete or mismatched local reconciliation recovery record')
        runtime.restore(saved['runtime'])
    if draft is not None: runtime.set_draft(draft)
    runtime.recovery_key,runtime.recovery_scope,runtime.recovery_record=key,scope,saved
    context.reconciliation_runtime=runtime
    return runtime


def _json_object(content):
    # Some compatible endpoints ignore JSON mode and wrap a valid result in a
    # single code fence. Normalize only that exact envelope; never extract JSON
    # from prose or repair the business result. All keys/schema/refs stay strict.
    import re
    if isinstance(content,str):
        fenced=re.fullmatch(r'\s*```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```\s*',content)
        if fenced: content=fenced.group(1)
    def pairs(rows):
        result={}
        for key,value in rows:
            if key in result: raise ValueError('Duplicate result JSON key: '+key)
            result[key]=value
        return result
    try:
        value=json.loads(content,object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        raise ValueError("The result must be a JSON object without explanation before or after. Please rewrite according to the current role's public fields; put analysis into structured explanation fields. Parse error:"+str(error)) from error
    if not isinstance(value,dict): raise ValueError('Return one JSON object without wrapper or Markdown')
    return value


def invoke_reconciliation(agent, phase, context, payload):
    runtime=prepare_runtime(agent,phase,context,payload,fresh=True)
    writer=phase=='reconcile'
    reads=runtime.build_read_tools()
    results={name:StructuredTool.from_function(lambda **kwargs:kwargs,name=name,
        description=DESCRIPTIONS[name],args_schema=TOOLS[name]) for name in ('edit_draft',)} if writer else {}
    saved=runtime.recovery_record
    tracker=RecoveryTracker(saved['tracker'] if saved else None)
    requested=saved['requests'] if saved else 0
    if getattr(runtime,'recovery_head_advanced',False):
        tracker.data.update(protocol_failures=0,protocol_error=None,fallback=False)
    sink=agent.trace_sink
    if hasattr(sink,'path'):
        from cortex.infrastructure.artifacts.graph_merge_trace import FileSystemGraphMergeTraceSink
        sink=FileSystemGraphMergeTraceSink(sink.path.with_name('model_calls.jsonl'))
    trace=EpisodeTrace(sink,phase,budget=agent.call_budget)
    max_calls=agent.max_calls if agent.max_calls is not None else 12

    def facts(): return runtime.premise_hash(policy=runtime.recovery_scope['policy'])

    def persist():
        snapshot=runtime.snapshot()
        record={'scope':runtime.recovery_scope,'runtime':snapshot,'runtime_hash':canonical_hash(snapshot),
            'tracker':deepcopy(tracker.data),'tracker_hash':canonical_hash(tracker.data),'requests':requested}
        store=getattr(context,'record_task_recovery',None)
        if store: store(runtime.recovery_key,record)

    def error_body(error):
        from cortex.capabilities.graph_merge.llm.agent import failure_feedback
        return failure_feedback(error)

    def observe(stage, raw, body, premises):
        try:
            tracker.observe(stage,raw,body,premises)
        except TaskNoProgress:
            if stage=='read_stall':
                message='Repeated reads added no new facts; use the complete local input. Checkpoint retained.'
                tracker.data['blocked']['message']=message
                raise TaskNoProgress(message) from None
            raise
        finally: persist()

    def preview():
        callback=getattr(context,'preview_task_draft',None)
        if callback is not None: compiled=callback(runtime.draft)
        else:
            from cortex.capabilities.graph_merge.reconciliation.compiler import compile_draft
            compiled=compile_draft(context,context.task_before,runtime.draft,
                targets=context.active_task.targets,payload=payload)
            context.last_compiled_patch,context.candidate=compiled,compiled.state
        runtime.set_compiled_candidate(compiled.state,runtime.draft.version)
        old=runtime.feedback.get('mechanical')
        if old: runtime.set_feedback({'historical_compile_feedback':{'mechanical':deepcopy(old)}})
        runtime.set_feedback({'mechanical':None,'latest_receipt':{'draft_saved':True,'errors':[]}})
        return compiled

    def handoff():
        callback=getattr(context,'accept_compiled_draft',None)
        compiled=callback(runtime.draft) if callback else context.last_compiled_patch
        if runtime.candidate_version!=runtime.draft.version or canonical_hash(compiled.state)!=canonical_hash(runtime._candidate):
            raise ValueError('STALE_CANDIDATE: exact compiled current draft required')
        tracker.success()
        context.task_relevant_facts=[{'canonical_runtime':runtime.snapshot()}]
        persist()
        return runtime.draft

    try:
        tracker.ensure_active(facts_hash=facts())
        if writer:
            # Baseline preview is not a Writer judgment. Only a durable explicit
            # edit action (including empty/identical) may resume directly to Assessment.
            pending=getattr(context,'has_pending_draft_action',lambda value:False)(runtime.draft)
            try:
                preview()
                if pending: return handoff()
            except (ValueError,TypeError,KeyError) as error:
                body=error_body(error)
                runtime.set_feedback({'mechanical':body,'latest_receipt':{'draft_saved':True,'errors':[body]}})
            persist()
        while requested<max_calls:
            offered=runtime.available_read_tools(reads)
            result_only = not writer and (tracker.submit_only or bool(runtime.feedback.get('read_progress')))
            if result_only:
                offered = {}
            available=[*offered.values(),*results.values()]
            messages=runtime.messages()
            if result_only:
                messages[-1] = messages[-1].model_copy(update={'content': messages[-1].content +
                    '\n\nReturn only the assessment JSON object now, without prose or Markdown. '
                    'Use the supplied facts and the previous rejected output as data; correct its format and references. '
                    'Do not repeat completed reads. If a decisive fact is genuinely unavailable, report that specific gap in feedback. '
                    'Required JSON schema:\n' + json.dumps(AssessmentResult.model_json_schema(), ensure_ascii=False)})
            premises=facts()
            request_version=runtime.draft.version
            request_candidate=canonical_hash(context.candidate)
            trace.record('request_contract',tool_names=[tool.name for tool in available],message_count=len(messages),
                facts_hash=premises,draft_version=request_version)
            check_request(messages,available,max_characters=agent.request_max_characters,message_sections=SECTIONS,
                on_measure=lambda row:trace.record('request_budget',**row))
            requested+=1
            persist()
            response=invoke_with_recovery(agent,available,messages,config={'callbacks':[trace]},tool_choice='auto',
                response_format={'type':'json_object'} if not writer else None)
            if runtime.draft.version!=request_version or canonical_hash(context.candidate)!=request_candidate or facts()!=premises:
                raise ValueError('STALE_REQUEST: task facts or draft changed during the model request; response not applied')
            calls=list(response.tool_calls or ())
            invalid=list(response.invalid_tool_calls or ())
            if not writer and not calls and not invalid:
                stage='output_protocol'
                try:
                    raw=_json_object(response.content)
                    value=AssessmentResult.model_validate(raw)
                    stage='assessment_validation'
                    record=runtime.validate_review(value)
                    tracker.success()
                    runtime.set_feedback({'assessment_output_error':None,'protocol_error':None})
                    trace.record('assessment_result',arguments=raw,result={'validated':True,'feedback_count':len(record.issues)})
                    persist()
                    context.task_relevant_facts=[{'canonical_runtime':runtime.snapshot()}]
                    return record
                except (ValueError,TypeError,KeyError) as error:
                    body=error_body(error)
                    runtime.set_feedback({'assessment_output_error':{**body, 'previous_output': response.content}})
                    trace.record('assessment_result',result=body)
                    observe(stage,response.content,body,premises)
                    continue
            if writer and not calls and not invalid:
                try:
                    raw=_json_object(response.content)
                    if set(raw)!={'problem','refs'} or not isinstance(raw['problem'],str) or not raw['problem'].strip() or not raw['refs']:
                        raise ValueError('Use edit_draft; only an inability-to-continue report may return problem and refs JSON')
                    for ref in raw['refs']: runtime.resolve_reference(ref)
                except (ValueError,TypeError,KeyError) as error:
                    body=error_body(error)
                    runtime.set_feedback({'protocol_error':body})
                    observe('output_protocol',response.content,body,premises)
                    continue
                runtime.set_feedback({'writer_problem':raw})
                persist()
                from cortex.capabilities.graph_merge.integration.engine import IntegrationIncomplete
                raise IntegrationIncomplete('Writer cannot safely continue: '+raw['problem'])
            read_batch=bool(calls) and all(call.get('name') in offered for call in calls)
            result_call=len(calls)==1 and calls[0].get('name') in results
            mixed_result=bool(calls) and isinstance(response.content,str) and response.content.lstrip().startswith(('{','['))
            if invalid or mixed_result or not (read_batch or result_call):
                body={'error':'Use one authorized read batch, one draft action, or the specified final JSON; do not mix results and tools.',
                    'available_tools':[tool.name for tool in available]}
                runtime.set_feedback({'protocol_error':body})
                trace.record('protocol_rejected',calls=calls,invalid_tool_calls=invalid,feedback=body)
                observe('output_protocol',[call.get('args') for call in calls+invalid],body,premises)
                continue
            if read_batch:
                for call in calls:
                    try:
                        result=offered[call['name']].invoke(call['args'])
                        runtime.set_feedback({'read_error':None,'read_progress':None})
                    except (ValueError,TypeError,KeyError) as error:
                        result=error_body(error)
                        runtime.set_feedback({'read_error':result})
                    trace.record('tool_result',name=call['name'],arguments=call['args'],result=result)
                if facts()==premises:
                    body={'error':'No new facts: requested material is complete or the read failed.'}
                    runtime.set_feedback({'read_progress':body})
                    observe('read_stall',{'no_new_facts':True},body,premises)
                persist()
                continue
            call=calls[0]
            name,raw=call['name'],call['args']
            stage='output_schema'
            try:
                value=TOOLS[name].model_validate(raw)
                if name=='edit_draft':
                    previous=runtime.draft
                    last=runtime.feedback.get('last_draft_edit') or {}
                    request=value.model_dump(mode='json')
                    if last.get('request')==request and last.get('draft_hash')==canonical_hash(previous):
                        updated=previous
                    else:
                        updated=apply_edit_shape(previous,value,request_version=request_version)
                    runtime.set_draft(updated)
                    runtime.set_feedback({'last_draft_edit':{'request':request,'draft_hash':canonical_hash(updated)}})
                    store=getattr(context,'record_task_draft',None)
                    complete=getattr(context,'record_complete_draft_action',None)
                    if complete: complete(updated.model_dump(mode='json',exclude_unset=True))
                    elif store: store(updated.model_dump(mode='json',exclude_unset=True))
                    context.active_draft=updated.model_dump(mode='json',exclude_unset=True)
                    stage='draft_compile'
                    try:
                        preview()
                        receipt={'draft_saved':True,'errors':[]}
                    except (ValueError,TypeError,KeyError) as error:
                        body=error_body(error)
                        receipt={'draft_saved':True,'errors':[body]}
                        runtime.set_feedback({'mechanical':body,'latest_receipt':receipt})
                        trace.record('tool_result',name=name,arguments=raw,result=receipt,changed=updated!=previous,draft_version=updated.version)
                        observe(stage,updated.model_dump(mode='json',exclude={'version'}),body,facts())
                        continue
                    runtime.set_feedback({'protocol_error':None,'latest_receipt':receipt})
                    tracker.data.update(protocol_failures=0,protocol_error=None,fallback=False)
                    trace.record('tool_result',name=name,arguments=raw,result=receipt,changed=updated!=previous,draft_version=updated.version)
                    stage='assessment_handoff'
                    return handoff()
            except (ValueError,TypeError,KeyError) as error:
                body=error_body(error)
                if stage=='assessment_handoff': runtime.clear_compiled_candidate()
                receipt={'draft_saved':stage!='output_schema','errors':[body]}
                runtime.set_feedback({'protocol_error':body,'latest_receipt':receipt})
                trace.record('tool_result',name=name,arguments=raw,result=receipt)
                observe(stage,{'tool':name,'arguments':raw},body,premises)
        raise RuntimeError(f'{phase} exhausted {max_calls} local model requests; draft and checkpoint retained')
    finally:
        active_error=sys.exception()
        try: persist()
        except Exception as error:
            if active_error is None: raise
            active_error.add_note(f'Checkpoint persistence also failed: {type(error).__name__}: {error}')
        trace.record('agent_episode',task_id=runtime.task_id,calls=trace.calls,requests=requested,
            draft_version=runtime.draft.version,message_count=3)
