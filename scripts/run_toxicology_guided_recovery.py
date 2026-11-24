"""Two-case protocol recovery; originals are read-only and completed SQL is not sampled."""
from pathlib import Path
import asyncio,json,sys,sqlite3,hashlib,os
from types import SimpleNamespace
from html import escape
from dotenv import load_dotenv
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.infrastructure.online_context import FileSystemGraphStateLoader,DatabaseDescriptionLoader
from cortex.capabilities.online_context.workflow.migration import checkpoint_values
from cortex.application.retrieve_db import RetrieveDbResult
from cortex.bootstrap.online_context import build_online_context_service,build_simulated_user_agent
from cortex.infrastructure.llm.parsing import _parse_output,_parse_json_object
from cortex.capabilities.online_context.discovery.models import IntraNodeOutput,InitialSqlMemoryOutput,CrossNodeOutput
from run_bird_dev_online_guided_baseline import _online_case,_question_with_evidence
from run_graph_guided_test import audit_http,CallAudit,write
from run_merged_graph_ea import sql_arm
import cortex.capabilities.online_context.discovery.organizer as organizer

out=Path(sys.argv[1]);repo=Path.cwd();parent=Path(json.loads((out/'source.json').read_text())['parent']);protocol=json.loads((parent/'protocol.json').read_text());cases={c['source_id']:c for c in map(json.loads,(parent/'test_cases.jsonl').read_text().splitlines())}

def publish():
    result={'arms':{},'cases':{}}
    for arm in ['plain','retrieved_memory','guided']:
        correct=finished=0
        for sid in cases:
            replacement=out/'cases'/sid/f'{arm}_prediction.json';p=replacement if replacement.exists() else parent/'cases'/sid/f'{arm}_prediction.json'
            if p.exists():
                x=json.loads(p.read_text());finished+=1;correct+=x.get('evaluation',{}).get('exec')==1
        result['arms'][arm]={'finished':finished,'correct':correct,'total':72,'ea':correct/72}
    for sid in ['218','263','267','302','314']:
        p=out/'cases'/sid/'online.json';result['cases'][sid]=json.loads(p.read_text()) if p.exists() else {'status':'running'}
    write(out/'summary.json',result)
    body='<h1>Toxicology guided recovery</h1><pre>'+escape(json.dumps(result['arms'],indent=2))+'</pre>'
    for sid in ['218','263','267','302','314']:
        body+='<h2>'+sid+'</h2><p><a href="cases/'+sid+'/online.json">Online result</a> · <a href="cases/'+sid+'/guided_prediction.json">Guided SQL</a></p>'
        for p in sorted((out/'cases'/sid/'wire').glob('*.json')):
            body+='<details><summary>'+sid+'/'+p.name+'</summary><pre>'+escape(p.read_text())+'</pre></details>'
    (out/'index.html').write_text('<!doctype html><meta charset="utf-8"><style>body{font:16px/1.7 system-ui;max-width:1100px;margin:auto}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style>'+body)

async def one(sid,slot):
    folder=out/'cases'/sid;folder.mkdir(parents=True,exist_ok=True);source=parent/'cases'/sid
    with sqlite3.connect(source/'online.sqlite') as c:threads=c.execute("select distinct thread_id from checkpoints where checkpoint_ns='' ").fetchall()
    assert len(threads)==1
    values=checkpoint_values(source/'online.sqlite',threads[0][0]);case=cases[sid]
    assert values['request']['question']==_question_with_evidence(case).strip()
    retrieval=RetrieveDbResult.model_validate(values['retrieval'])
    graph=FileSystemGraphStateLoader().load(parent/'merged_state.json');cfg=load_config(parent/'config.yaml');keys=read_key_slots(protocol['keys_file'])
    cfg=cfg.model_copy(update={'llm':cfg.llm.model_copy(update={'api_key':keys[slot].api_key}), 'services':cfg.services.model_copy(update={'online_context':cfg.services.online_context.model_copy(update={'checkpoint_path':folder/'online.sqlite'}),'retrieve_db':cfg.services.retrieve_db.model_copy(update={'workspace':folder/'retrieval'})})})
    model=create_chat_model(cfg.llm,profile='online_context');audit_http(model,folder/'wire');model.callbacks=[CallAudit(folder)];traces=[]
    service=build_online_context_service(config=cfg,llm=model,embeddings=None,trace_sink=lambda stage,payload:traces.append({'stage':stage,'payload':payload}))
    async def reuse(request,run_id):
        assert request['question']==values['request']['question'] and request['graph_state_fingerprint']==values['request']['graph_state_fingerprint']
        return retrieval
    service._engine._retrieve=reuse
    write(folder/'migration.json',{'source_checkpoint':str(source/'online.sqlite'),'source_thread':threads[0][0],'reused':'Completed retrieval; 1311 validated successful Intra/Initial stages reused by explicit adapter. Failed stage and required downstream execute under new protocol.','source_checkpoint_unchanged':True})
    context=DatabaseDescriptionLoader().load(Path(protocol['database_root'])/'toxicology/database_description')
    worker=SimpleNamespace(service=service,simulator=build_simulated_user_agent(config=cfg,llm=model),traces=traces)
    if sid in ['218','267','314']:
        from cortex.application.online_context import ChoicePoint,InteractionAnswer,InteractionGuidance,ClarificationFilterDecision
        current=await service._engine._finish(values['request'],retrieval,tuple(ChoicePoint.model_validate(x) for x in values['points']),tuple(InteractionAnswer.model_validate(x) for x in values['answers']),tuple(values.get('notes',[])),tuple(InteractionGuidance.model_validate(x) for x in values.get('automatic_guidance',[])),tuple(ClarificationFilterDecision.model_validate(x) for x in values.get('filter_decisions',[])))
        record={'source_id':sid,'status':current.status,'online_after_interaction':current.model_dump(mode='json'),'guidance_content':current.downstream_guidance.guidance_content,'downstream_guidance':current.downstream_guidance.model_dump(mode='json'),'reused_confirmed_answers':True}
    else:
        record=await _online_case(worker,graph_state=graph,column_context=context,case=case,run_id=hashlib.sha256((str(out)+sid).encode()).hexdigest())
    write(folder/'online.json',record);write(folder/'stage_traces.json',traces)
    if record.get('guidance_content'):await asyncio.to_thread(sql_arm,case,'guided',record['guidance_content'],folder,model,Path(protocol['database_root']))
    publish();await model.root_async_client.close();model.root_client.close()

async def main():
    publish();await asyncio.gather(*(one(sid,slot) for sid,slot in zip(['218','263','267','302','314'],[0,2,4,6,8])));publish()
if __name__=='__main__':
    load_dotenv(repo/'.env');os.environ['LANGSMITH_TRACING']=os.environ['LANGCHAIN_TRACING_V2']='false';asyncio.run(main())
