"""Audit bounded probes from captured requests and immutable graph snapshots."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import defaultdict


def read(path):
    return json.loads(path.read_text())


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    root=parser.parse_args().root
    checks=[];contexts=[]
    for result_path in sorted(root.glob('live*/*/result.json')):
        folder=result_path.parent;result=read(result_path)
        trace=folder/'model_calls.jsonl'
        events=[json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
        wires=[read(p)['body'] for p in sorted((folder/'wire').glob('*request.json'))]
        starts=[e for e in events if e.get('event_type')=='model_start']
        episodes=defaultdict(list)
        for event in starts: episodes[event['episode_id']].append(event['messages'][:2])
        repeated=[]
        for wire in wires:
            messages=wire['messages']
            assert [m['role'] for m in messages]==['system','user','user']
            schema=json.dumps(wire.get('tools',[]))
            assert all(name not in schema for name in ('submit_draft','AlternativeUpdate','alternative_update','Review'))
            pattern=r'### 节点 `([^`]+)`[^\n]*\n\n```json\n(.*?)\n```'
            fixed={i:json.loads(body) for i,body in re.findall(pattern,messages[1]['content'],re.S)}
            for ident,body in re.findall(pattern,messages[2]['content'],re.S):
                if ident in fixed and fixed[ident]==json.loads(body): repeated.append(ident)
        transitions=[]
        for i,event in enumerate(events):
            if (event.get('event_type')=='tool_result' and event.get('name')=='edit_draft'
                    and event.get('result',{}).get('draft_saved') and not event['result'].get('errors')):
                nxt=next((e for e in events[i+1:] if e.get('event_type')=='model_start'),None)
                transitions.append(nxt['component'] if nxt else 'controlled_checkpoint_stop')
                assert nxt is None or nxt['component']=='integration_graph_assess'
        contexts.append({'run':folder.parent.name,'case':folder.name,'status':result['status'],
            'actual_api_requests':len(wires),'traced_model_requests':len(starts),'controlled_harness_actions':int((folder/'seed_submission.json').exists()),
            'each_request_roles':['system','user','user'],
            'fixed_prefix_unchanged_within_each_episode':all(all(row==rows[0] for row in rows) for rows in episodes.values()),
            'unchanged_node_bodies_repeated':repeated,
            'successful_edit_next_request':transitions,
            'removed_tools_and_operation_absent':True,
            'maximum_request_characters':max((len(json.dumps(w,ensure_ascii=False)) for w in wires),default=0),
            'compile_errors':[e['result'] for e in events if e.get('event_type')=='tool_result' and e.get('name')=='edit_draft' and e.get('result',{}).get('errors')],
            'assessment_results':[e.get('result') for e in events if e.get('event_type')=='assessment_result']})
        assert contexts[-1]['fixed_prefix_unchanged_within_each_episode'] and not repeated
        if result['status']!='passed': continue
        from cortex.schema import GraphState,load_root_schema
        from cortex.application.ontology_graph import GraphManager
        before=read(folder/'before.json');after=read(folder/'after.json')
        GraphManager(load_root_schema()).validate_state(GraphState.model_validate(after))
        old_nodes={n['id']:n for n in before['graph']['nodes']};new_nodes={n['id']:n for n in after['graph']['nodes']}
        old_edges={e['id']:e for e in before['graph']['edges']};new_edges={e['id']:e for e in after['graph']['edges']}
        check={'run':folder.parent.name,'case':folder.name,'strict_graph_validation':True,
            'independent_assessment':result['independent_assessment'],
            'before_nodes':len(old_nodes),'after_nodes':len(new_nodes),'before_edges':len(old_edges),'after_edges':len(new_edges),
            'all_before_nodes_unchanged':all(new_nodes.get(i)==n for i,n in old_nodes.items()),
            'source_files_unchanged':result['source_files_unchanged']}
        assert check['all_before_nodes_unchanged'] and check['source_files_unchanged']
        if folder.name=='metric_retirement':
            old_id='case204::edge.carcinogenic_count_depends_on_carcinogenic_molecule'
            successor='case266::edge.carcinogenic_count_depends'
            normalized=lambda rows:{json.dumps(row,sort_keys=True) for row in rows}
            union=normalized(old_edges[old_id]['sources'])|normalized(old_edges[successor]['sources'])
            check.update(removed_edge=old_id,successor_edge=successor,successor_sources=new_edges[successor]['sources'],
                exact_source_union=old_id not in new_edges and normalized(new_edges[successor]['sources'])==union)
            assert check['exact_source_union']
        if folder.name=='value_concepts':
            check.update(all_before_edges_unchanged=all(new_edges.get(i)==e for i,e in old_edges.items()),
                created_nodes=[n for i,n in new_nodes.items() if i not in old_nodes],
                created_edges=[e for i,e in new_edges.items() if i not in old_edges])
            assert check['all_before_edges_unchanged'] and len(check['created_nodes'])==1
        if folder.name=='preserve_conflict':
            check['whole_graph_exactly_unchanged']=before==after
            check['conflict_owners']=[i for i,n in old_nodes.items() if n.get('alternatives')]
            check['scoped_edges']=[e for e in old_edges.values() if e.get('properties',{}).get('alternative_ids')]
            assert check['whole_graph_exactly_unchanged'] and check['scoped_edges']
        if result.get('resume_from'):
            source=Path(result['resume_from']['case_directory'])
            saved=read(source/'task_state.json')
            candidate=GraphState.model_validate(saved['last_valid_candidate'])
            from cortex.capabilities.graph_merge.runtime.state import canonical_hash
            check.update(resumed_without_writer=all(e.get('component')!='integration_reconcile' for e in starts),
                candidate_hash_matches_checkpoint=canonical_hash(candidate)==canonical_hash(GraphState.model_validate(after)),
                saved_draft=result['resume_from']['draft'],source_checkpoint_sha256=digest(source/'workspace.sqlite'))
            assert check['resumed_without_writer'] and check['candidate_hash_matches_checkpoint']
        checks.append(check)
    write(root/'context_audit.json',contexts);write(root/'post_run_checks.json',checks)
    prior=read(root/'prior_records_manifest.json');changed=[p for p,h in prior.items() if not Path(p).is_file() or digest(Path(p))!=h]
    policy=read(root/'policy_before_final_live.json');policy_changed=[p for p,h in policy.items() if digest(Path(p))!=h]
    backup=read(root/'before_manifest.json');backup_changed=[p for p,h in backup.items() if digest(Path(p))!=h]
    suite=ET.parse(root/'pytest.xml').getroot();suites=list(suite.iter('testsuite'))
    validation={'targeted_tests_passed':sum(int(s.get('tests',0))-int(s.get('failures',0))-int(s.get('errors',0))-int(s.get('skipped',0)) for s in suites),
        'targeted_tests_failed':sum(int(s.get('failures',0))+int(s.get('errors',0)) for s in suites),
        'full_repository_suite_run':False,'prior_record_hashes_checked':len(prior),'prior_records_unchanged':not changed,
        'modified_prior_records':changed,'before_snapshots_checked':len(backup),'before_snapshots_unchanged':not backup_changed,
        'runtime_code_and_prompt_policy_unchanged_during_real_probe':not policy_changed,
        'policy_changes':policy_changed,'background_runtime_switched':False,
        'passed_real_scenarios':[r['run']+'/'+r['case'] for r in checks],
        'exploratory_legacy_tests':'Two older submit_patch-based recovery tests fail before model invocation because they provide no explicit reconciliation Signal; current canonical recovery tests cover the new contract. Full legacy suite was not migrated.'}
    assert not changed and not backup_changed and not policy_changed and not validation['targeted_tests_failed']
    write(root/'validation.json',validation)
    print(json.dumps({'passed_tests':validation['targeted_tests_passed'],'real_scenarios':len(checks),'prior_records_unchanged':not changed},ensure_ascii=False))


if __name__=='__main__': main()
