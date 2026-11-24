import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("online_result_recovery", Path(__file__).parents[1]/"scripts/online_result_recovery.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def data(tmp_path):
    row = {"database_id":"db", "source_id":"1", "question":"List a", "evidence":"", "sql":"SELECT a",
           "plain":{"evaluation":{"exec":1}}, "guided":None, "case_error":"checkpoint incompatible",
           "completed":True, "status":"completed_with_online_error"}
    guide = {"database_id":"db", "source_id":"1", "question":"List a", "gold_sql":"SELECT a",
             "predicted_sql":"SELECT a", "error":None, "evaluation":{"exec":1}}
    path=tmp_path/'guided.json';path.write_text(json.dumps(guide))
    entry={"input_fingerprint":module.input_fingerprint(row), "baseline_fingerprint":module.fingerprint(row['plain']),
           "guided_path":str(path), "guided_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    return row, guide, {"accepted_at":"now", "cases":{"db/1":entry}}


def test_backfill_preserves_baseline_and_old_row_and_is_idempotent(tmp_path):
    row,guide,manifest=data(tmp_path)
    untouched={"database_id":"other", "source_id":"2"}
    result=module.overlay_recoveries([row,untouched],manifest)
    assert row['guided'] is None and row['case_error']
    assert result[0]['guided']==guide and result[0]['plain']==row['plain']
    assert result[0]['case_error'] is None and result[1] is untouched
    assert module.overlay_recoveries(result,manifest)==result


@pytest.mark.parametrize('field,value', [('plain',{'evaluation':{'exec':0}}),('sql','SELECT different')])
def test_backfill_rejects_changed_inputs_or_baseline(tmp_path,field,value):
    row,_,manifest=data(tmp_path);row[field]=value
    with pytest.raises(ValueError):module.overlay_recoveries([row],manifest)


def test_backfill_cannot_replace_previous_success_or_accept_an_unknown_case(tmp_path):
    row,_,manifest=data(tmp_path);row['guided']={'evaluation':{'exec':1},'predicted_sql':'SELECT old'}
    with pytest.raises(ValueError,match='previously passing'):module.overlay_recoveries([row],manifest)
    with pytest.raises(ValueError,match='outside'):module.overlay_recoveries([],manifest)


def test_explicit_failed_evaluation_remains_a_mismatch_instead_of_online_failure(tmp_path):
    row,guide,manifest=data(tmp_path);guide['evaluation']['exec']=0
    path=Path(manifest['cases']['db/1']['guided_path']);path.write_text(json.dumps(guide))
    manifest['cases']['db/1']['guided_sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):module.overlay_recoveries([row],manifest)
    manifest['include_evaluated_mismatches']=True
    updated=module.overlay_recoveries([row],manifest)[0]
    assert updated['outcome']=='ea_mismatch' and updated['guided']['evaluation']['exec']==0
    assert updated['case_error'] is None and updated['plain']==row['plain']
