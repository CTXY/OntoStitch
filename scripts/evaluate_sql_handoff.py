"""Compare a focused SQL handoff with the existing full-guidance draft, one call each."""
import argparse,asyncio,json,pathlib,os
from dotenv import load_dotenv
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from cortex.capabilities.online_context.output.sql_handoff import render_sql_handoff
from run_graph_guided_test import write,read,CallAudit,audit_http,baseline


async def main(args):
    load_dotenv('.env',override=False);os.environ['LANGSMITH_TRACING']='false';os.environ['LANGCHAIN_TRACING_V2']='false'
    args.output=args.output.absolute();args.output.mkdir(parents=True,exist_ok=True)
    cases=[json.loads(x) for x in args.cases.read_text().splitlines() if x.strip()];cfg=load_config(args.config);slots=read_key_slots(args.keys);sem=asyncio.Semaphore(min(args.workers,len(slots)));results={};guidances=[]
    async def one(index,case):
        async with sem:
            original=args.run/'cases'/case['source_id'];online=read(original/'online.json');folder=args.output/'cases'/case['source_id'];folder.mkdir(parents=True,exist_ok=True)
            content=render_sql_handoff(online['downstream_guidance'])
            item={'source_id':case['source_id'],'question':case['question'],'database_id':case['database_id'],'guidance_content':content,'downstream_guidance':{'agent_handoff':{'guidance_content':content}}};write(folder/'guidance'/f"{case['source_id']}.json",item)
            config=cfg.llm.model_copy(update={'model':args.model,'api_key':slots[index%len(slots)].api_key,'timeout':3600,'max_retries':8,'enable_thinking':False})
            model=create_chat_model(config,profile='online_context');model.callbacks=[CallAudit(folder)];audit_http(model,folder/'wire')
            prediction=await asyncio.to_thread(baseline,case,item,'guided',folder,model,args);results[case['source_id']]=prediction;guidances.append(item)
            await model.root_async_client.close();model.root_client.close()
            write(args.output/'progress.json',{'completed':len(results),'total':len(cases),'correct':sum(x['evaluation']['exec'] for x in results.values())});print(json.dumps({'case':case['source_id'],'completed':len(results),'exec':prediction['evaluation']['exec']}),flush=True)
    await asyncio.gather(*(one(i,c) for i,c in enumerate(cases)))
    rows=[results[c['source_id']] for c in cases];(args.output/'predictions.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows));lookup={x['source_id']:x for x in guidances};write(args.output/'guidance_cases.json',{'cases':[lookup[c['source_id']] for c in cases]})
    summary={'total':len(rows),'correct':sum(x['evaluation']['exec'] for x in rows),'protocol':'Every case, single generation call, same baseline generator as full handoff; only guidance serialization changes. No correctness retries or best-of selection.'};summary['ea']=summary['correct']/len(rows);write(args.output/'summary.json',summary);print(json.dumps(summary),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ['run','cases','config','database-root','keys','output']:p.add_argument('--'+key,type=pathlib.Path,required=True)
    p.add_argument('--workers',type=int,default=8);p.add_argument('--model',default='deepseek-v4-pro');asyncio.run(main(p.parse_args()))
