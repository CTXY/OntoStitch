"""Readable role-specific user prompts; authoritative state stays in the runtime."""
import json
import re
from copy import deepcopy
from importlib.resources import files
from string import Template

LAYOUT_VERSION = 'signal-selected-local-users-v4-english'
USER_FILES = tuple(f'user/{role}-{part}.en.md' for role in ('writer', 'assessment') for part in ('task', 'state'))
TYPE_NAMES = {'concept':'Concept', 'metric':'Metric', 'event':'Event', 'generation_rule':'GenerationRule',
              'table':"Table (read-only catalog)", 'column':"Column (read-only catalog)", 'dimension_value':"DimensionValue (read-only catalog)"}
SIGNAL_NAMES = {'abstract_predicate':"Abstract predicate", 'binding_contest':"Direct physical representation competition", 'parallel_assertions':"Parallel relationship assertions",
    'triangle':"Triangular relationship", 'same_role_neighbors':"Same-role neighbors", 'related_decisions':"Related decisions",
    'source_selection_variants':"Source selection differences", 'value_concepts':"Value and dimension concepts",
    'metric_direct_inputs':"Metric direct inputs", 
    'target_alternative_scope':"Target version and relationship scope", 'node_relation_followup':"Relationship checks handed over by node consolidation"}


def user_assets():
    root=files('cortex.capabilities.graph_merge.llm.prompts').joinpath('reconciliation')
    return {name:root.joinpath(name).read_text() for name in USER_FILES}


def presentation_policy():
    from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
    return canonical_hash({'version':LAYOUT_VERSION,'templates':user_assets(),
        'renderer':files('cortex.capabilities.graph_merge.llm').joinpath('reconciliation_presentation.py').read_text()})


def block(value):
    # JSON encoding keeps source strings as data; do not synthesize instructions.
    return '```json\n'+json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n```'


def section(title, body):
    return '## '+title+'\n\n'+body if body else ''


def public_node(row):
    return {key:deepcopy(item) for key,item in row.items()
            if key not in {'content_complete','source_snapshot_id','readonly_history'}}


def render_nodes(rows):
    parts=[]
    for ident,row in sorted(rows.items()):
        value=public_node(row)
        label=TYPE_NAMES.get(row.get('node_type'),row.get('node_type',"Node"))
        parts.append("### Node `"+ident+'` · '+label+'\n\n'+block(value))
    return '\n\n'.join(parts)


def render_edges(rows):
    parts=[]
    for ident,row in sorted(rows.items()):
        parts.append("### Relationship `"+ident+"`\n\nConnections (`from_node_id → edge_type → to_node_id`):\n"+
            block([row['from_node_id'],row['edge_type'],row['to_node_id']])+ "\n\nRelationship properties (`properties`):\n"+
            block(row.get('properties',{}))+"\n\nSource evidence (`evidence_ids`):\n"+block(row.get('evidence_ids',[])))
    return '\n\n'.join(parts)


def render_contracts(rows):
    return '\n\n'.join("### Predicate `"+ident+'`\n\n'+block(row) for ident,row in sorted(rows.items()))


def check_data(value,runtime):
    return deepcopy(value)


def render_checks(runtime):
    parts=["The following are the actual input records to be checked in this round, not generic examples or guidance. The node and edge IDs here are only for locating; complete content is expanded in the later \"Local graph before modification\". General judgment principles and examples are in the corresponding Signal section in system."]
    edges={edge.id:edge for edge in runtime.before.graph.edges}
    from cortex.schema.relation_vocabulary import ABSTRACT_RELATIONS
    for index,row in enumerate([*runtime.packet['signals'],*runtime.packet['obligations']],1):
        parts.append("## Items to check this round "+str(index)+': '+SIGNAL_NAMES[row['kind']]+' (`'+row['id']+'`)' )
        if row.get('check_question'): parts.append("Specific task for this round (original text):"+str(row['check_question']))
        if row.get('required_outcome'): parts.append("Required outcome:"+str(row['required_outcome']))
        if row.get('mode')=='required': parts.append("This is an explicit handling obligation that must be completed and checked.")
        for key,label in (('node_ids',"Matched node IDs"),('edge_ids',"Matched relationship IDs")):
            if row.get(key): parts.append(label+"(complete content see the local graph below):"+ ', '.join('`'+ident+'`' for ident in row[key]))
        if row.get('column_id'): parts.append("Involved physical column IDs: `"+row['column_id']+'`.')
        facts={key:deepcopy(value) for key,value in row.items()
               if key not in {'id','signal_id','kind','mode','check_question','required_outcome','node_ids','edge_ids','column_id'}}
        if row['kind']=='abstract_predicate':
            hit=[edges[ident] for ident in row.get('edge_ids',()) if ident in edges and edges[ident].edge_type in ABSTRACT_RELATIONS]
            for edge in hit:
                parts.append("Why detected this time: relationship `"+edge.id+"` before modification actually used the abstract base predicate `"+edge.edge_type+"`. Therefore you need to check the specific business role; this trigger condition has not determined a replacement predicate, and does not prove that the endpoints should be changed.")
            observed=facts.get('observed')
            if isinstance(observed,dict):
                if observed.get('edge_id') in {edge.id for edge in hit}: observed.pop('edge_id')
                if observed.get('abstract_predicate') in {edge.edge_type for edge in hit}: observed.pop('abstract_predicate')
                hint=observed.pop('available_original_predicate',None)
                if hint: parts.append("Additional candidate hint for this round: `"+hint+"`. This is a hint given by the current input and still needs to be checked against endpoint meaning and contract; it is not a conclusion from a generic example.")
                if not observed: facts.pop('observed')
        if facts:
            parts.append("Specific records provided by this round's detection/task (data to be checked, not examples, and not generic processing rules):\n"+block(check_data(facts,runtime)))
    return '\n\n'.join(parts)


def fixed_prompt(runtime, before):
    role='writer' if runtime.phase=='reconcile' else 'assessment'
    business={ident for ident,row in before['nodes'].items() if row['node_type'] not in {'table','column','dimension_value'}}
    metadata_only=set(runtime.packet.get('write_scope', {}).get('metadata_only_node_ids', ())) & business
    writable_nodes=(business & runtime.write_node_ids) - metadata_only if role=='writer' else set()
    writable_edges={ident for ident in runtime.write_edge_ids if ident in before['edges']
                    and before['edges'][ident]['from_node_id'] in business} if role=='writer' else set()
    scope=("The local graph below is selected for the listed Signals and obligations. It includes the necessary node content and relationship sets, without a generic one-hop expansion. "
           "Completeness applies only to the declared relationship selectors; unlisted graph relationships are not proven absent. Readable boundary nodes are context, not permission to expand their neighborhood or modify them.")
    relation_sets=runtime.packet.get('relation_sets', ())
    if relation_sets:
        descriptions=[]
        for row in relation_sets:
            selector={key:deepcopy(value) for key,value in row.items()
                      if key not in {'kind','node_ids','edge_ids','contract_ids','description'}}
            description=str(row.get('description', row.get('kind', 'Selected relationship set')))
            # State the selector, not a second list of its matching graph records.
            if selector:
                description+=' Selector: `'+json.dumps(selector, ensure_ascii=False, sort_keys=True, separators=(',', ':'))+'`.'
            descriptions.append('- '+description)
        scope+="\n\nComplete relationship sets for this check:\n"+'\n'.join(descriptions)
    if role=='writer':
        scope+="\n\nExisting business nodes allowed to be modified:\n"+block(sorted(writable_nodes))
        if metadata_only:
            scope+="\n\nExisting Concepts writable only at `properties.relation_metadata` (retain unrelated groups unchanged):\n"+block(sorted(metadata_only))
        scope+="\n\nExisting business relationships allowed to be modified:\n"+block(sorted(writable_edges))
        scope+="\n\nAll other existing objects are read-only. Evidence or predicate reads do not expand this write scope. Substantiated business objects may be created only for this round's issue, using authorized endpoints; the physical catalog is read-only."
    nodes=render_nodes(before['nodes']);edges=render_edges(before['edges'])
    graph="## Node content\n\n"+nodes+"\n\n## Relationships\n\n"+(edges or "There are no relationships in this scope.")
    types='\n\n'.join('## '+TYPE_NAMES[row['node_type']]+" properties\n\n"+block(row['properties'])
        for row in runtime.packet.get('node_contracts',()))
    text=Template(user_assets()['user/'+role+'-task.en.md']).substitute(checks=render_checks(runtime),scope=scope,
        before_graph=graph,contracts=render_contracts(before.get('contracts',{})) or "There are no additional predicate contracts in this round.",
        node_contracts=types,history="Read original Question, Gold SQL and evidence by the displayed evidence IDs only.")
    return re.sub(r'\n{3,}', '\n\n', text).rstrip()


def candidate_prompt(runtime):
    if runtime._candidate is None or runtime.candidate_version!=runtime.draft.version:
        return ''
    old,new=runtime.working['before'],runtime.working['candidate']
    parts=[]
    for kind,label,render in (('nodes',"Node",render_nodes),('edges',"Relationship",render_edges),('contracts',"Predicate contract",render_contracts)):
        left,right=old.get(kind,{}),new.get(kind,{})
        # Candidate completeness is guaranteed by the controller. Public body
        # equality avoids claiming that a bookkeeping-only change is semantic.
        if kind=='contracts':
            from cortex.schema.relation_contracts import effective_schema
            original=set(effective_schema(runtime.context.schema,runtime.before.graph).edge_type_index())
            current=set(effective_schema(runtime.context.schema,runtime._candidate.graph).edge_type_index())
        else:
            original={obj.id for obj in getattr(runtime.before.graph,kind)}
            current={obj.id for obj in getattr(runtime._candidate.graph,kind)}
        created={k:v for k,v in right.items() if k not in original}
        updated={k:v for k,v in right.items() if k in original and k in left and
                 (public_node(v)!=public_node(left[k]) if kind=='nodes' else v!=left[k])}
        supplemented={k:v for k,v in right.items() if k in original and k not in left}
        deleted=[k for k in left if k not in current]
        if created: parts.append(section("New "+label+"(actual candidate content)",render(created)))
        if supplemented: parts.append(section("Existing additionally used by candidate "+label+"(not expanded before, not newly created or rewritten)",render(supplemented)))
        if updated: parts.append(section("Modified "+label+"(actual candidate content)",render(updated)))
        if deleted: parts.append(section("Deleted "+label+"ID (original content see local graph before modification)",block(sorted(deleted))))
    return '\n\n'.join(parts)


def read_prompt(runtime):
    initial=runtime._initial_before
    before=runtime.working['before']
    parts=[]
    for kind,label,render in (('nodes',"Supplementary pre-modification nodes",render_nodes),('edges',"Supplementary pre-modification relationships",render_edges),('contracts',"Supplementary predicate contracts",render_contracts)):
        extra={k:v for k,v in before.get(kind,{}).items() if
               (public_node(initial.get(kind,{}).get(k,{}))!=public_node(v) if kind=='nodes' else initial.get(kind,{}).get(k)!=v)}
        if extra: parts.append(section(label,render(extra)))
    for digest,body in sorted(runtime.evidence_view['bodies'].items()):
        ids=sorted(ident for ident,target in runtime.evidence_view['id_to_body'].items() if target==digest)
        parts.append(section("Read original evidence:"+', '.join(ids),block(body)))
    empty_queries=[row for row in runtime.working.get('predicate_queries',{}).values() if not row['predicate_ids']]
    if empty_queries:
        parts.append(section("Read predicate catalog query results", '\n'.join(
            "- Queried `"+row['from_node_type']+' → '+row['to_node_type']+"`: no matching predicate in the installed catalog of the fixed original graph."
            for row in empty_queries)))
    return '\n\n'.join(parts)


def feedback_prompt(runtime):
    from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
    feedback=runtime._public_feedback()
    parts=[];seen=set()
    for key,title in (('mechanical',"Compilation issues in the current draft"),('protocol_error',"Format or call issues in the last reply"),
                      ('assessment_output_error',"Format or reference issues in the last assessment output"),('read_error',"Read failure"),
                      ('read_progress',"Read hint"),('writer_problem',"Specific blockers still pending")):
        value=feedback.get(key)
        if value:
            digest=canonical_hash(value)
            if digest not in seen: parts.append(section(title,block(value)));seen.add(digest)
    for error in feedback.get('latest_receipt',{}).get('errors',[]):
        digest=canonical_hash(error)
        if digest not in seen: parts.append(section("Issues with the last edit request",block(error)));seen.add(digest)
    previous=feedback.get('previous_feedback')
    if previous:
        parts.append(section("Issues raised by the last independent assessment",
            "The following descriptions come from the last assessed candidate and need to be rechecked item by item against the current actual result; the candidate references therein point to the candidate at that time and do not mean the issue still exists currently.\n\n"+block(previous)))
    return '\n\n'.join(parts)


def state_prompt(runtime):
    role='writer' if runtime.phase=='reconcile' else 'assessment'
    candidate=candidate_prompt(runtime)
    compiled=runtime._candidate is not None and runtime.candidate_version==runtime.draft.version
    if role=='assessment':
        summary=("The following are actual candidate changes after code compilation. Objects not listed as changed are exactly the same as before modification; current object references use `view=\"candidate\"`."
            if candidate else "The candidate graph is exactly the same as before modification. Please check item by item whether the specific task and required outcome have been satisfied; retaining original knowledge does not mean the task is complete.")
    elif not compiled:
        summary=("The current draft did not compile successfully; please revise according to the specific errors below; original input and saved draft are still retained."
            if runtime.feedback.get('mechanical') else "There is currently no compiled candidate; original input, current draft, and obtained materials are still retained.")
    elif runtime.draft.changes:
        summary="The current draft has been compiled; below is the actual result. Please revise incorporating the feedback; after the complete revision for this round compiles successfully, it automatically enters independent assessment."
    else:
        summary="No modification has been proposed yet; current local graph is the same as before modification. When modification is unnecessary, call edit_draft({}) to explicitly request independent assessment."
    if runtime.feedback.get('latest_receipt',{}).get('draft_saved') is False:
        summary+=" The last request was not saved; what is shown below is still the current valid draft and candidate."
    draft=section("Current saved draft: omitted entries remain; withdraw obsolete change_ids before adding replacements",block([
        row.model_dump(mode='json',exclude_unset=True) for row in runtime.draft.changes])) if role=='writer' and runtime.draft.changes else ''
    text=Template(user_assets()['user/'+role+'-state.en.md']).substitute(summary=summary,draft=draft,
        candidate=candidate,reads=read_prompt(runtime),feedback=feedback_prompt(runtime))
    # Remove empty optional sections, without touching strings inside data blocks.
    return re.sub(r'\n{3,}', '\n\n', text).rstrip()
