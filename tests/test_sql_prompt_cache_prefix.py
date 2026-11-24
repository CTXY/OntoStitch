import json

from pydantic import BaseModel

from nl2sql_baseline.generation import build_messages
from cortex.infrastructure.llm.parsing import build_prompt_messages


def sql_messages(**changes):
    return build_messages(**{
        "question":"First question", "evidence":"First evidence",
        "schema_ddl":"CREATE TABLE numbers(value INTEGER);", "examples":[],
        "reference_time":"2026-01-01", "dialect":"sqlite", **changes,
    })


def test_plain_guided_and_different_times_share_the_entire_schema_prefix():
    first=sql_messages()
    second=sql_messages(question="Other question", evidence="Other evidence",
                        reference_time="2027-12-01", downstream_guidance="Use numbers.value")
    assert first[0]==second[0]
    prefix=lambda messages: messages[1]['content'].split('# current_time')[0]
    assert prefix(first)==prefix(second)
    assert 'CREATE TABLE numbers' in prefix(first)
    assert second[1]['content'].endswith('# task\nOther question')
    assert 'Use numbers.value' not in second[0]['content']


def test_execution_repair_appends_to_the_unchanged_initial_messages():
    first=sql_messages(downstream_guidance='Use numbers.value')
    repair=sql_messages(downstream_guidance='Use numbers.value', previous_sql='SELECT bad', execution_error='no such column')
    assert repair[:2]==first and repair[-2]['content']=='SELECT bad'
    assert 'no such column' in repair[-1]['content']


def test_retrieval_sql_signal_puts_static_schema_before_question():
    class Output(BaseModel):
        sql: str
    def make(question):
        return build_prompt_messages('Generate SQL', Output, {
            'question':question,'physical_schema':'TABLE numbers(value INTEGER)','dialect':'sqlite',
        })
    first,second=make('First'),make('Second')
    assert first[0]==second[0]
    assert first[1]['content'].split('"question"')[0]==second[1]['content'].split('"question"')[0]
    assert list(json.loads(first[1]['content']))==['dialect','physical_schema','question']
    explicit=build_prompt_messages('Generate SQL',Output,{'physical_schema':'ignored'},human_content='exact body')
    assert explicit[1]['content']=='exact body'
