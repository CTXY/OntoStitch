"""Import completed predictions and uniformly adopt a reviewed guided rerun.

Original records and checkpoints are read-only. All rerun results are adopted,
including failures. Exact SQL fence normalization is replayed for every arm.
"""
from pathlib import Path
import sys
REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO/'src'), str(REPO/'scripts'), str(REPO/'baseline/src')]

import argparse
from datetime import datetime
from hashlib import sha256
import json
import shutil
from types import SimpleNamespace
import yaml
from dotenv import load_dotenv
from cortex.config import load_config
from cortex.infrastructure.llm.key_slots import read_key_slots
from nl2sql_baseline.database import Database
from nl2sql_baseline.evaluation import evaluate_results
from nl2sql_baseline.generation import _content
from run_merged_graph_ea import ARMS, prediction_finished, pending_arms, summarize, paired_statistics
from run_graph_guided_test import read, write


def digest(path): return sha256(path.read_bytes()).hexdigest()


def prepare(parent, rerun, root):
    assert read(parent/'status.json')['status'] == 'paused_by_user'
    assert read(rerun/'status.json')['status'] == 'complete'
    old_protocol = read(parent/'protocol.json'); replay = read(rerun/'protocol.json')
    assert replay['source_run'] == str(parent) and replay['graph_sha256'] == old_protocol['graph_sha256']
    assert not replay['additional_definition_filter']
    cases = [json.loads(line) for line in (parent/'test_cases.jsonl').read_text().splitlines()]
    assert len(cases) == len({str(c['source_id']) for c in cases}) == 81
    selected = set(replay['selected_case_ids'])
    expected = {c['source_id'] for c in cases if (parent/'cases'/c['source_id']/'guided_prediction.json').exists()
        and read(parent/'cases'/c['source_id']/'guided_prediction.json')['evaluation']['exec'] != 1}
    assert selected == expected and len(selected) == 7
    root.mkdir(parents=True, exist_ok=False)
    watched = []
    for source in (parent, rerun):
        watched += [source/'protocol.json', source/'merged_state.json', source/'test_cases.jsonl']
        watched += list(source.glob('cases/*/*_prediction.json')) + list(source.glob('cases/*/online.json'))
        watched += list(source.glob('cases/*/online.sqlite')) + list(source.glob('cases/*/retrieval/checkpoints/*.sqlite'))
    original_hashes = {str(p): digest(p) for p in watched}
    write(root/'source_preservation.json', {'before': original_hashes})
    for name in ('merged_state.json', 'test_cases.jsonl'):
        shutil.copy2(parent/name, root/name)
    db_root = Path(old_protocol['database_root'])
    database = Database('sqlite:///'+str(db_root/'thrombosis_prediction/thrombosis_prediction.sqlite'), query_timeout=30)
    imports = {}; formatting = []; counts = {arm:0 for arm in ARMS}
    for case in cases:
        sid = case['source_id']; folder = root/'cases'/sid
        records = {}
        for arm in ARMS:
            source_run = rerun if arm == 'guided' and sid in selected else parent
            path = source_run/'cases'/sid/f'{arm}_prediction.json'
            if not path.exists(): continue
            pred = read(path)
            if not prediction_finished(pred): continue
            folder.mkdir(parents=True, exist_ok=True)
            local = folder/path.name
            raw_sql = pred.get('predicted_sql', '')
            normalized = _content(SimpleNamespace(content=raw_sql))
            provenance = {'kind':'guided_rerun' if source_run == rerun else 'preserved_original',
                'path':str(path), 'sha256':digest(path), 'runtime':str(source_run/'runtime')}
            if normalized != raw_sql:
                updated = dict(pred)
                try:
                    evaluation = evaluate_results(database.execute_read_only(normalized), database.execute_read_only(case['sql']))
                    error = stage = None
                except Exception as exc:
                    error, stage = str(exc), 'sql_execution'
                    evaluation = {'exec':0,'reason':error}
                updated.update(predicted_sql=normalized, evaluation=evaluation, error=error, error_stage=stage,
                    original_predicted_sql=raw_sql, original_evaluation=pred.get('evaluation'),
                    original_sql_attempts=pred.get('sql_attempts',[]),
                    sql_attempts=[{'sql':normalized,'error':error}],
                    continuation_normalization='one exact SQL fence; same parser for all arms; zero new model calls')
                write(local, updated)
                provenance['kind'] = 'uniform_format_replay'
                provenance['source_kind'] = 'guided_rerun' if source_run == rerun else 'preserved_original'
                formatting.append({'case_id':sid,'arm':arm,'original_evaluation':pred['evaluation'],
                    'evaluation':evaluation,'new_model_calls':0})
            else:
                shutil.copy2(path, local)
                assert digest(local) == provenance['sha256']
            provenance['imported_sha256'] = digest(local)
            records[arm] = provenance; counts[arm] += 1
        if records:
            if all(arm in records for arm in ARMS):
                source = rerun if sid in selected else parent
                path = source/'cases'/sid/'online.json'
                if path.exists(): records['online_summary'] = {'status':read(path)['status'],'path':str(path)}
            imports[sid] = records
    write(root/'imported_results.json', imports)
    work = {case['source_id']: list(pending_arms(root/'cases'/case['source_id'])) for case in cases}
    work = {sid:arms for sid,arms in work.items() if arms}
    rows, totals = summarize(cases, root)
    assert counts == {'plain':28,'retrieved_memory':24,'guided':23}
    assert totals['guided']['correct'] == 19
    assert sum('guided' in arms for arms in work.values()) == 58
    settings = yaml.safe_load((parent/'config.yaml').read_text())
    settings['llm']['base_url'] = load_config(REPO/'configs/cortex.yaml').llm.base_url
    settings['services']['retrieve_db'].update(semantic_node_limit=30,physical_node_limit=50,workspace=str(root/'unused_retrieval'))
    settings['services']['online_context']['checkpoint_path'] = str(root/'unused_online.sqlite')
    settings['services']['graph_merge']['checkpoint_url'] = 'sqlite:///'+str(root/'unused_merge.sqlite')
    (root/'config.yaml').write_text(yaml.safe_dump(settings,sort_keys=False))
    keys = read_key_slots(REPO/'configs/sql_training.keys'); slots = [0,2,4,6]
    toxic = read(REPO/'output/toxicology_remaining_merge_pro_multi_results_20261122_225749/status.json')
    assert not {keys[i].api_key.get_secret_value() for i in slots} & {keys[i].api_key.get_secret_value() for i in toxic['credential_pool']}
    runtime = root/'runtime'; ignore=shutil.ignore_patterns('__pycache__','*.pyc')
    shutil.copytree(REPO/'src/cortex', runtime/'src/cortex', ignore=ignore)
    shutil.copytree(REPO/'baseline/src/nl2sql_baseline', runtime/'baseline/src/nl2sql_baseline', ignore=ignore)
    for name in ('run_merged_graph_ea.py','run_bird_dev_online_guided_baseline.py','run_graph_guided_test.py'):
        dest=runtime/'scripts'/name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(REPO/'scripts'/name,dest)
    manifest = {str(p.relative_to(runtime)):digest(p) for p in runtime.rglob('*') if p.is_file()}
    continuation = {'source_run':str(parent),'guided_rerun':str(rerun),'imported_predictions':counts,
        'guided_rerun_results_adopted':sorted(selected),'guided_newly_passing':['1155','1156','1160'],
        'uniform_format_replay':formatting,'completed_sql_not_resampled':True,
        'historical_runtime_provenance_preserved':True,'not_a_fresh_uniform_runtime_blind_evaluation':True,
        'incomplete_legacy_online_strategy':'Rebuild under the changed 30/50 context policy; old checkpoints remain untouched. Completed SQL results are reused.',
        'proxy_environment_disabled':True}
    protocol = {**old_protocol,'created_at':datetime.now().astimezone().isoformat(),'mode':'continued_paired_evaluation',
        'source_run':str(parent),'graph_sha256':digest(root/'merged_state.json'),'cases_sha256':digest(root/'test_cases.jsonl'),
        'runtime_manifest':manifest,'arms':list(ARMS),'key_slots':slots,'keys_file':str(REPO/'configs/sql_training.keys'),
        'semantic_node_limit':30,'physical_node_limit':50,'additional_definition_filter':False,
        'continuation':continuation,'resume_authorized_by_user':True,
        'retrieved_memory':'Pre-interaction retrieved graph in the existing compact analysis presentation; complete original graph retained.'}
    protocol.pop('launch_recovery',None)
    write(root/'protocol.json',protocol)
    plan={'created_at':protocol['created_at'],'imported_counts':counts,'initial_totals':totals,
        'initial_paired_comparisons':paired_statistics(rows),'remaining_cases':len(work),
        'remaining_arms':{arm:sum(arm in arms for arms in work.values()) for arm in ARMS},
        'work':work,'guided_rerun_all_seven_adopted':True,'format_normalization':formatting}
    write(root/'continuation_plan.json',plan)
    assert all(digest(Path(p)) == value for p,value in original_hashes.items())
    write(root/'source_preservation.json',{'before':original_hashes,'all_original_files_unchanged':True})
    write(root/'status.json',{'status':'prepared','total':81,'arms':totals,'continuation':continuation})
    print(json.dumps({'output':str(root),'initial_totals':totals,'remaining_arms':plan['remaining_arms'],
        'remaining_cases':len(work),'format_normalization':formatting},ensure_ascii=False),flush=True)
    return root


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--parent',type=Path,required=True);parser.add_argument('--rerun',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();load_dotenv(REPO/'.env',override=False)
    prepare(args.parent.resolve(),args.rerun.resolve(),args.output.resolve())
