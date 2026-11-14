"""Apply one identical request-only SQL review to every guided/control draft.

All cases are reviewed regardless of earlier correctness. Gold is used only by
execution comparison after review. No per-case best-of selection is permitted.
"""
import argparse,asyncio,json,pathlib,os,time
from dotenv import load_dotenv
from cortex.config import load_config
from cortex.infrastructure.llm import create_chat_model
from cortex.infrastructure.llm.key_slots import read_key_slots
from run_graph_guided_test import write,read,CallAudit,audit_http
from nl2sql_baseline.database import Database
from nl2sql_baseline.evaluation import evaluate_results
from nl2sql_baseline.generation import SqlGenerator


async def main(args):
    load_dotenv('.env',override=False);os.environ['LANGSMITH_TRACING']='false';os.environ['LANGCHAIN_TRACING_V2']='false'
    args.output=args.output.absolute();args.output.mkdir(parents=True,exist_ok=True)
    cases=[json.loads(l) for l in args.cases.read_text().splitlines() if l.strip()];slots=read_key_slots(args.keys);cfg=load_config(args.config);sem=asyncio.Semaphore(min(args.workers,len(slots)));results={}
    async def one(index,case):
        async with sem:
            original=args.run/'cases'/case['source_id']
            while not all((original/f'{mode}_prediction.json').exists() for mode in ['guided','plain']):await asyncio.sleep(5)
            folder=args.output/'cases'/case['source_id'];folder.mkdir(parents=True,exist_ok=True)
            config=cfg.llm.model_copy(update={'model':args.model,'api_key':slots[index%len(slots)].api_key,'timeout':3600,'max_retries':8,'enable_thinking':False})
            model=create_chat_model(config,profile='online_context');model.callbacks=[CallAudit(folder)];audit_http(model,folder/'wire')
            db=Database(f"sqlite:///{args.database_root/case['database_id']/(case['database_id']+'.sqlite')}",query_timeout=30)
            schema=await asyncio.to_thread(db.introspect_schema,description_dir=args.database_root/case['database_id']/'database_description')
            generator=SqlGenerator(model=args.model,api_key='injected',base_url=None,temperature=0,timeout=3600,llm=model)
            online=read(original/'online.json');h=online['downstream_guidance']
            guidance=[{k:item.get(k) for k in ['decision_question','should_be','must_not_be','source']} for item in h.get('interaction_guidance',[])]
            pair={}
            for mode in ['guided','plain']:
                saved=folder/f'{mode}_prediction.json'
                if saved.exists():pair[mode]=read(saved);continue
                draft=read(original/f'{mode}_prediction.json')['predicted_sql']
                sql='';error=None;messages=[]
                try:
                    sql,messages=await asyncio.to_thread(generator.review,question=case['question'],evidence=case.get('evidence',''),schema_ddl=schema.ddl,draft_sql=draft,resolved_guidance=guidance if mode=='guided' else [],dialect='sqlite')
                    gold=await asyncio.to_thread(db.execute_read_only,case['sql']);pred=await asyncio.to_thread(db.execute_read_only,sql);evaluation=evaluate_results(pred,gold)
                except Exception as exc:error=type(exc).__name__+': '+str(exc);evaluation={'exec':0,'reason':error}
                write(folder/f'{mode}_review_input.json',{'messages':messages,'gold_supplied':False})
                pair[mode]={'source_id':case['source_id'],'database_id':case['database_id'],'question':case['question'],'gold_sql':case['sql'],'draft_sql':draft,'predicted_sql':sql,'evaluation':evaluation,'error':error,'guidance_available':mode=='guided','review_applied_to_every_case':True}
                write(saved,pair[mode])
            await model.root_async_client.close();model.root_client.close();results[case['source_id']]=pair
            write(args.output/'progress.json',{'total':len(cases),'completed':len(results),'correct':sum(x['guided']['evaluation']['exec'] for x in results.values()),'control_correct':sum(x['plain']['evaluation']['exec'] for x in results.values()),'updated':time.time()})
            print(json.dumps({'case':case['source_id'],'completed':len(results),'guided':pair['guided']['evaluation']['exec'],'plain':pair['plain']['evaluation']['exec']}),flush=True)
    await asyncio.gather(*(one(i,c) for i,c in enumerate(cases)))
    for mode in ['guided','plain']:
        rows=[results[c['source_id']][mode] for c in cases];(args.output/('predictions.jsonl' if mode=='guided' else 'plain_predictions.jsonl')).write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in rows))
    summary={'total':len(cases),'guided_correct':sum(x['guided']['evaluation']['exec'] for x in results.values()),'plain_correct':sum(x['plain']['evaluation']['exec'] for x in results.values()),'protocol':'Every draft reviewed once, no Gold or correctness feedback to reviewer, no selecting best result; full denominator.'};summary.update(guided_ea=summary['guided_correct']/len(cases),plain_ea=summary['plain_correct']/len(cases));write(args.output/'summary.json',summary);print(json.dumps(summary),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ['run','cases','config','database-root','keys','output']:p.add_argument('--'+key,type=pathlib.Path,required=True)
    p.add_argument('--model',default='deepseek-v4-pro');p.add_argument('--workers',type=int,default=8)
    asyncio.run(main(p.parse_args()))
