"""Simplified local drafts exercise the real controller, Assessment and storage."""
from copy import deepcopy
import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from test_graph_integration import catalog, concept, edge, state
from test_merge_task_review_boundary import engine_for

from cortex.capabilities.graph_merge.integration.task_context import LocalTask
from cortex.capabilities.graph_merge.llm.task_agent import IncrementalIntegrationAgent


def call(name, args):
    return AIMessage(content='', tool_calls=[{'name': name, 'id': name, 'args': args}])


def edit(*, target=None, description='A precise existing feature relationship.'):
    fields = {'properties': {'description': description}}
    if target is not None: fields['to_node_id'] = target
    return {'put_changes': [{'change_id': 'clarify', 'kind': 'edge_update', 'edge_id': 'e',
        'set': fields, 'reason': 'Clarify the existing business relationship without losing knowledge.'}]}


class CanonicalChat:
    def __init__(self, *, edit_edge=False, interrupt=False, **kwargs):
        self.requests, self.edit_edge, self.interrupt = [], edit_edge, interrupt

    def bind(self, **kwargs):
        self.output_options=kwargs
        return self

    def bind_tools(self, tools, **kwargs):
        self.tools = [convert_to_openai_tool(item) for item in tools]
        return self

    @property
    def writer(self):
        return any(row['function']['name']=='edit_draft' for row in self.tools)

    def invoke(self, messages, config=None):
        self.requests.append((deepcopy(messages), deepcopy(self.tools)))
        assert len(messages)==3
        rt=self.engine.ctx.reconciliation_runtime
        if self.writer:
            if self.edit_edge and not rt.draft.changes: return call('edit_draft', edit())
            return call('edit_draft', {})
        if self.interrupt:
            self.interrupt=False
            raise RuntimeError('Interrupt independent assessment')
        return AIMessage(content=json.dumps({'explanation':'The local relation is valid and original knowledge and sources are preserved.', 'feedback':[]}))


def fixture(tmp_path, chat):
    base=state([*catalog(),concept('A','a'),concept('B','b')],[edge('e','A','B','has_feature')])
    agent=IncrementalIntegrationAgent(chat,max_calls=8)
    engine,_=engine_for(base,agent,tmp_path/'canonical.sqlite')
    chat.engine=engine
    task=LocalTask('canonical-local','reconcile',{'A','B','e'},checks=[{
        'id':'S-parallel','kind':'parallel_assertions','node_ids':['A','B'],'edge_ids':['e'],
        'observed':'Earlier duplicate already removed; verify the surviving relation.'}])
    return engine,task


def test_empty_draft_needs_no_keep_decision_but_independent_assessment(tmp_path):
    chat=CanonicalChat();engine,task=fixture(tmp_path,chat);before=engine.current
    engine._run_task(task,'noop')
    assert engine.current==before and len(chat.requests)==2
    assert engine.history[-1]['assessment']=='graph_assess'
    names=[{tool['function']['name'] for tool in tools} for _,tools in chat.requests]
    assert names==[{'read_evidence','read_predicates','edit_draft'},{'read_evidence'}]
    assert '# Assessment task for this round' in chat.requests[-1][0][1].content


def test_assessment_feedback_returns_to_same_writer_and_is_rechecked(tmp_path):
    class FeedbackChat(CanonicalChat):
        reviews=0
        def invoke(self,messages,config=None):
            rt=self.engine.ctx.reconciliation_runtime
            if not self.writer and self.reviews==0:
                self.reviews+=1; self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return AIMessage(content=json.dumps({'explanation':'The preserved relationship needs a clearer definition.',
                    'feedback':[{'problem':'Description is too vague.','required_outcome':'Explain the existing feature relation precisely.',
                    'refs':[{'kind':'edge','view':'candidate','id':'e','field':'properties.description'}]}]}))
            if self.writer and rt.feedback.get('semantic_issues') and not rt.draft.changes:
                assert 'Issues raised by the last independent assessment' in rt.messages()[-1].content
                self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return call('edit_draft',edit())
            return super().invoke(messages,config)
    chat=FeedbackChat();engine,task=fixture(tmp_path,chat)
    engine._run_task(task,'feedback-loop')
    assert len(engine.history)==1 and len(chat.requests)==4
    assert next(e for e in engine.current.graph.edges if e.id=='e').properties['description'].startswith('A precise')


def test_malformed_assessment_is_repaired_by_assessment_only(tmp_path):
    class Bad(CanonicalChat):
        sent=False
        def invoke(self,messages,config=None):
            if not self.writer and not self.sent:
                self.sent=True;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return AIMessage(content='{"explanation":"ok","feedback":[],"verdict":"accept"}')
            if not self.writer: assert self.engine.ctx.reconciliation_runtime.feedback['assessment_output_error']
            return super().invoke(messages,config)
    chat=Bad();engine,task=fixture(tmp_path,chat);engine._run_task(task,'format')
    assert len(chat.requests)==3 and len(engine.history)==1


def test_mixed_actions_do_not_apply_any_edit(tmp_path):
    class Mixed(CanonicalChat):
        sent=False
        def invoke(self,messages,config=None):
            if self.writer and not self.sent:
                self.sent=True;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return AIMessage(content='',tool_calls=[{'name':'edit_draft','id':'a','args':edit()},
                    {'name':'edit_draft','id':'b','args':{}}])
            if self.writer: assert self.engine.ctx.reconciliation_runtime.draft.version==0
            return super().invoke(messages,config)
    chat=Mixed();engine,task=fixture(tmp_path,chat);engine._run_task(task,'mixed')
    assert len(chat.requests)==3


def test_repeated_unchanged_edit_reuses_compilation(tmp_path,monkeypatch):
    from cortex.capabilities.graph_merge.integration import reconciliation_compile
    original=reconciliation_compile.compile_draft;versions=[]
    def counted(*args,**kw): versions.append(args[2].version);return original(*args,**kw)
    monkeypatch.setattr(reconciliation_compile,'compile_draft',counted)
    class Duplicate(CanonicalChat):
        edits=0
        def invoke(self,messages,config=None):
            if self.writer and self.edits<2:
                self.edits+=1;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return call('edit_draft',edit())
            return super().invoke(messages,config)
    chat=Duplicate();engine,task=fixture(tmp_path,chat);engine._run_task(task,'duplicate')
    assert versions==[0,1] and len(chat.requests)==2


def test_compiler_failure_saved_then_fixed_and_error_cleared(tmp_path):
    class Repair(CanonicalChat):
        edits=0
        def invoke(self,messages,config=None):
            rt=self.engine.ctx.reconciliation_runtime
            if self.writer and self.edits<2:
                self.edits+=1;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                if self.edits==2:
                    assert rt.draft.version==1 and rt.feedback['latest_receipt']['errors']
                    assert 'missing-endpoint' in json.dumps(rt.feedback['latest_receipt'])
                    assert rt._candidate is None
                replacement=edit(target='missing-endpoint' if self.edits==1 else 'B')
                if self.edits==2:
                    replacement['put_changes'][0]['change_id']='corrected'
                    replacement['remove_changes']=['clarify']
                return call('edit_draft',replacement)
            if self.writer: assert rt.feedback['mechanical'] is None and not rt.feedback['latest_receipt']['errors']
            return super().invoke(messages,config)
    chat=Repair();engine,task=fixture(tmp_path,chat);engine._run_task(task,'repair')
    assert len(engine.history)==1 and len(chat.requests)==3


@pytest.mark.parametrize('bad',[{'put_changes':[{'kind':'alternative_update','change_id':'bad','reason':'No branch writes',
    'node_id':'A','alternative_id':'one','set':{'name':'changed'}}]}, {'unexpected':True}])
def test_schema_error_does_not_save_draft_and_returns_complete_feedback(tmp_path,bad):
    class Invalid(CanonicalChat):
        sent=False
        def invoke(self,messages,config=None):
            if self.writer and not self.sent:
                self.sent=True;self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                return call('edit_draft',bad)
            if self.writer:
                rt=self.engine.ctx.reconciliation_runtime
                assert rt.draft.version==0 and not rt.draft.changes
                assert rt.feedback['latest_receipt']['draft_saved'] is False
                assert rt.feedback['latest_receipt']['errors']
            return super().invoke(messages,config)
    chat=Invalid();engine,task=fixture(tmp_path,chat);engine._run_task(task,'schema-atomic')
    assert len(chat.requests)==3 and len(engine.history)==1


def test_assessment_third_protocol_attempt_is_result_only_with_previous_output(tmp_path, unbound_chat_tools):
    class Repair(CanonicalChat):
        assessments=0
        def invoke(self,messages,config=None):
            if not self.writer:
                self.assessments+=1
                if self.assessments<=2:
                    self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                    return AIMessage(content='Analysis before JSON: {"explanation":"Preserved","feedback":[]}')
                assert self.tools==[]
                assert 'Analysis before JSON' in messages[-1].content
                assert 'Required JSON schema' in messages[-1].content
            return super().invoke(messages,config)
    chat=Repair();engine,task=fixture(tmp_path,chat);engine._run_task(task,'bounded-result-repair')
    assert chat.assessments==3 and len(engine.history)==1


def test_assessment_after_redundant_read_gets_result_only_not_another_read(tmp_path, unbound_chat_tools):
    class ReadOnce(CanonicalChat):
        assessments=0
        def invoke(self,messages,config=None):
            if not self.writer:
                self.assessments+=1
                if self.assessments==1:
                    self.requests.append((deepcopy(messages),deepcopy(self.tools)))
                    return call('read_evidence',{'ids':['not-an-authorized-id']})
                assert self.tools==[]
                assert 'Do not repeat completed reads' in messages[-1].content
            return super().invoke(messages,config)
    chat=ReadOnce();engine,task=fixture(tmp_path,chat);engine._run_task(task,'bounded-read-repair')
    assert chat.assessments==2 and len(engine.history)==1


@pytest.fixture
def unbound_chat_tools(monkeypatch):
    # Real bind_tools returns a separate runnable; this mutable test chat needs
    # its old tool list cleared when transport deliberately uses the base LLM.
    from cortex.capabilities.graph_merge.llm import reconciliation_agent
    original=reconciliation_agent.invoke_with_recovery
    def invoke(agent,tools,messages,**kwargs):
        if not tools: agent.llm.tools=[]
        return original(agent,tools,messages,**kwargs)
    monkeypatch.setattr(reconciliation_agent,'invoke_with_recovery',invoke)
