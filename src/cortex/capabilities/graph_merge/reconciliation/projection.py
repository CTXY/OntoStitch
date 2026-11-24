"""One public object projection for display, citations and removal evidence."""
from copy import deepcopy

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.inputs import PHYSICAL
from cortex.capabilities.graph_merge.evidence import EvidencePresentation
from cortex.schema.relation_contracts import effective_schema

CATALOG_FIELDS = {'table_name','column_name','table_ref','column_ref','value',
                  'data_type','nullable','semantic_role','key_role','unit'}
PRIVATE_FIELDS = {'sources','common_sources','reference_origin','overrides','source_units'}


def node_record(context, node):
    evidence = EvidencePresentation(context)
    row = {'id':node.id,'node_type':node.node_type,'content_complete':True,
        'evidence_ids':evidence.ids_for((*node.sources,*node.common_sources,
            *(source for alt in node.alternatives for source in alt.sources)))}
    if node.node_type in PHYSICAL:
        row.update(name=node.name,description=node.description,
            properties={key:deepcopy(value) for key,value in node.properties.items() if key in CATALOG_FIELDS},readonly_catalog=True)
    elif node.alternatives:
        row.update(name=node.name,alternatives=[{'alternative_id':alt.alternative_id,
            'content':GraphManager.resolve_alternative(node,alt).model_dump(mode='json'),
            'evidence_ids':evidence.ids_for(alt.sources)} for alt in node.alternatives],
            conflict_points=[point.model_dump(mode='json') for point in node.conflict_points])
        if node.common_sources: row['common_evidence_ids']=evidence.ids_for(node.common_sources)
    else:
        row.update({key:deepcopy(getattr(node,key)) for key in ('name','description','aliases','tags','properties')})
    return row


def edge_record(context, edge):
    return {**edge.model_dump(mode='json',exclude={'sources','reference_origin'}),
        'evidence_ids':EvidencePresentation(context).ids_for(edge.sources)}


def field_value(record, path):
    if path is None: return record
    parts=path.split('.')
    if not path.strip() or '[' in path or any(part in PRIVATE_FIELDS or part.isdigit() or part.startswith('_') for part in parts):
        raise ValueError('NONPUBLIC_REFERENCE_FIELD: '+path)
    current=record
    for part in parts:
        if not isinstance(current,dict) or part not in current:
            raise ValueError('UNKNOWN_PUBLIC_FIELD: '+path)
        current=current[part]
    return current


def validate_reference(context, state, reference):
    if reference.view != 'before': raise ValueError('Removal evidence must cite the fixed before graph')
    if reference.kind=='node':
        objects={row.id:row for row in state.graph.nodes}
        record=node_record(context,objects[reference.id]) if reference.id in objects else None
    elif reference.kind=='edge':
        objects={row.id:row for row in state.graph.edges}
        record=edge_record(context,objects[reference.id]) if reference.id in objects else None
    else:
        objects=effective_schema(context.schema,state.graph).edge_type_index()
        record=objects[reference.id].model_dump(mode='json') if reference.id in objects else None
    if record is None: raise ValueError('UNKNOWN_REFERENCE_OBJECT: '+reference.id)
    return field_value(record,reference.field)
