"""Archive cleanup must prove response equality and exclude active data."""
import json
from pathlib import Path
import pytest
import cleanup_merge_transport_duplicates as cleanup
from cortex.infrastructure.llm.stream_audit import completion_from_sse


def setup(tmp_path,monkeypatch):
    monkeypatch.setattr(cleanup,'REPO',tmp_path)
    output=tmp_path/'output';archive=output/'thrombosis_test_archive';active=output/'toxicology_running'
    archive.mkdir(parents=True);active.mkdir()
    (output/'active_merge_runs.json').write_text(json.dumps({'databases':{'toxicology':{'root':str(active)}}}))
    return archive,active,tmp_path/'report'


def stream(folder,ident='000001',*,complete=True):
    folder.mkdir(parents=True,exist_ok=True)
    chunks=[{'id':'chat-test','model':'test','choices':[{'index':0,'delta':{'content':'{"ok":true}'},'finish_reason':None}]},
            {'id':'chat-test','model':'test','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]}]
    raw=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
    sse=folder/(ident+'_stream.sse');sse.write_text(raw)
    response=folder/(ident+'_response.json');response.write_text(json.dumps({'status':200,'body':completion_from_sse(raw)}))
    (folder/(ident+'_progress.json')).write_text(json.dumps({'completed':complete,'model_complete':complete}))
    return sse,response


def test_only_exact_success_copy_deleted_and_sources_and_active_run_retained(tmp_path,monkeypatch):
    archive,active,report=setup(tmp_path,monkeypatch);sse,response=stream(archive/'wire');live,_=stream(active/'wire')
    cp=archive/'workspace.sqlite';cp.write_bytes(b'original checkpoint')
    graph=archive/'state.json';graph.write_text('{"accepted":true}')
    original=response.read_bytes();cleanup.plan(report);cleanup.apply(report)
    assert not sse.exists() and response.read_bytes()==original
    assert live.exists() and cp.read_bytes()==b'original checkpoint' and graph.exists()


@pytest.mark.parametrize('reason',['incomplete','different','no_response','bad_tool_json'])
def test_incomplete_failed_or_nonidentical_stream_kept(tmp_path,monkeypatch,reason):
    archive,active,report=setup(tmp_path,monkeypatch);sse,response=stream(archive/'wire',complete=reason!='incomplete')
    if reason=='different':
        data=json.loads(response.read_text());data['body']['choices'][0]['message']['content']='different';response.write_text(json.dumps(data))
    if reason=='no_response':response.unlink()
    if reason=='bad_tool_json':
        raw='data: '+json.dumps({'id':'chat-test','model':'test','choices':[{'index':0,'delta':{'tool_calls':[{'index':0,'id':'call','function':{'name':'submit_patch','arguments':'{'}}]},'finish_reason':'tool_calls'}]})+'\n\ndata: [DONE]\n\n'
        sse.write_text(raw);response.write_text(json.dumps({'status':200,'body':completion_from_sse(raw)}))
    cleanup.plan(report);cleanup.apply(report);assert sse.exists()


def test_changed_retained_response_prevents_deletion(tmp_path,monkeypatch):
    archive,active,report=setup(tmp_path,monkeypatch);sse,response=stream(archive/'wire');cleanup.plan(report)
    response.write_text('{"changed":true}')
    with pytest.raises(ValueError,match='Retained response changed'):cleanup.apply(report)
    assert sse.exists()
