"""Bound intra-node work per call without dropping retrieved graph context."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cortex.capabilities.online_context.discovery.models import IntraNodeOutput
from cortex.infrastructure.llm import run_llm_extraction
from .sql_fragments import citation_schema


async def extract_intra_batches(*, llm, prompt, human_payload, batch_size,
                                output_validator, checkpoint_root=None, trace_sink=None):
    if isinstance(batch_size,bool) or not isinstance(batch_size,int) or batch_size<1:
        raise ValueError("intra batch size must be a positive integer")
    signals=tuple(human_payload['signal_node_ids'])
    if len(set(signals))!=len(signals):raise ValueError('signal IDs must be unique')
    prompt += ("\n\n# Batched execution\nThis call handles only the supplied signal_node_ids, "
               "which are one batch of the complete signal list. Other signal nodes in the "
               "full graph are context, not additional assessment targets. Return exactly "
               "one assessment for each supplied signal and none for other signals. Keep "
               "each assessment reason to one concise sentence. Do not discard supporting context.")
    batches=[signals[i:i+batch_size] for i in range(0,len(signals),batch_size)] or [()]
    assessments=[];slots=[]
    for index,selected in enumerate(batches,1):
        # All graph nodes and JOIN candidates remain available in every batch.
        # Put the only varying field last to keep the repeated prefix cacheable.
        payload={k:human_payload[k] for k in ('retrieved_graph_state','join_candidates','question','initial_sql')}
        payload['signal_node_ids']=selected
        signature={'prompt':prompt,'schema':IntraNodeOutput.model_json_schema(),'payload':payload,
                   'model':getattr(llm,'model_name',None),'temperature':getattr(llm,'temperature',None),
                   'base_url':getattr(llm,'openai_api_base',None),
                   'reasoning_effort':getattr(llm,'reasoning_effort',None)}
        signature['wire_native_thinking_only']=getattr(llm,'wire_native_thinking_only',False)
        digest=hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        path=Path(checkpoint_root)/f'{digest}.json' if checkpoint_root else None

        def validate(result):
            observed=[a.signal_node_id for a in result.assessments]
            if len(observed)!=len(selected) or set(observed)!=set(selected):
                raise ValueError('intra batch must assess exactly the requested signal IDs once')
            output_validator(result)

        trace={'batch':index,'total_batches':len(batches),'signal_node_ids':selected,'fingerprint':digest}
        if trace_sink:trace_sink('intra_batch',trace)
        if path and path.exists():
            result=IntraNodeOutput.model_validate(json.loads(path.read_text())['output'])
            validate(result);trace['checkpoint_reused']=True
        else:
            result=await run_llm_extraction(llm=llm,prompt=prompt,output_model=IntraNodeOutput,
                json_mode=True,output_schema=citation_schema(IntraNodeOutput),validation_context={"sql_fragments":{}},repair_with_context=True,output_validator=validate,trace_capture=trace,human_payload=payload)
            validate(result)
            if path:
                path.parent.mkdir(parents=True,exist_ok=True)
                tmp=path.with_suffix('.tmp')
                tmp.write_text(json.dumps({'fingerprint':digest,'output':result.model_dump(mode='json')},ensure_ascii=False))
                tmp.replace(path)
            trace['checkpoint_reused']=False
        # Different calls may independently choose the same local slot ID.
        names={slot.slot_id:f'intra_batch_{index}_{slot.slot_id}' for slot in result.slots}
        slots.extend(slot.model_copy(update={'slot_id':names[slot.slot_id]}) for slot in result.slots)
        assessments.extend(a.model_copy(update={'slot_ids':tuple(names[s] for s in a.slot_ids)}) for a in result.assessments)
    combined=IntraNodeOutput(assessments=tuple(assessments),slots=tuple(slots))
    observed=[a.signal_node_id for a in combined.assessments]
    if len(observed)!=len(signals) or set(observed)!=set(signals):
        raise ValueError('combined intra batches must cover every signal exactly once')
    output_validator(combined)
    return combined
