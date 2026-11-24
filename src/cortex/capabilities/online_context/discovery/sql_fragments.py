"""Exact SQL evidence handles; no rewriting or semantic equivalence guesses."""
from copy import deepcopy


def sql_fragments(sql):
    if not sql: return {}
    # Whole SQL is always a valid fallback. Smaller exact spans avoid quote copying.
    import re
    spans=[sql]
    for pattern in [r'COUNT\([^()]*\)', r'SELECT\s+(?:DISTINCT\s+)?[^;]*?(?=\s+FROM\b)']:
        spans.extend(m.group(0) for m in re.finditer(pattern,sql,re.I))
    return {f'S{i}':text for i,text in enumerate(dict.fromkeys(spans))}


def citation_schema(model):
    schema=deepcopy(model.model_json_schema())
    def visit(value):
        if isinstance(value,dict):
            props=value.get('properties',{})
            if 'initial_sql_evidence' in props:
                props.pop('initial_sql_evidence')
                props['initial_sql_refs']={'type':'array','items':{'type':'string'}}
                if value.get('required')==['initial_sql_evidence']:
                    props['initial_sql_refs']['minItems']=1
            if 'required' in value:
                value['required']=['initial_sql_refs' if k=='initial_sql_evidence' else k for k in value['required']]
            for child in value.values():visit(child)
        elif isinstance(value,list):
            for child in value:visit(child)
    visit(schema)
    return schema
