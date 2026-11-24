"""One-time, explicit preservation of legacy group prose in node definitions."""
from copy import deepcopy
from cortex.schema import GraphState


def migrate_relation_notes(state):
    """Accept archived raw JSON without relaxing the current write contract."""
    raw=deepcopy(state.model_dump(mode='json') if hasattr(state,'model_dump') else state)
    records=[]
    def migrate(body,node_id,alternative_id=None):
        properties=body['properties']
        for group in properties.pop('relation_metadata',[]):
            text=group['description'].strip();current=properties.get('definition','')
            if text not in current:properties['definition']=(current.rstrip()+'\n\n'+text).strip()
            records.append({'node_id':node_id,'alternative_id':alternative_id,'legacy_group_id':group['id'],
                'preserved_text':text,'destination':'properties.definition','original_edge_ids':group['edges']})
        return body
    nodes=[]
    for node in raw['graph']['nodes']:
        if not node.get('alternatives'):
            nodes.append(migrate(node,node['id']));continue
        if 'relation_metadata' not in node['properties'] and not any(
                'properties.relation_metadata' in alt['overrides'] for alt in node['alternatives']):
            nodes.append(node);continue
        versions=[]
        for alt in node['alternatives']:
            body={key:deepcopy(node.get(key)) for key in ('name','description','aliases','tags','properties')}
            for path,value in alt['overrides'].items():
                if path.startswith('properties.'):body['properties'][path[len('properties.'):]]=deepcopy(value)
                else:body[path]=deepcopy(value)
            versions.append((alt['alternative_id'],migrate(body,node['id'],alt['alternative_id']),alt['sources']))
        clean=deepcopy(node)
        clean['properties'].pop('relation_metadata',None)
        clean['properties'].pop('definition',None)
        # Preserve every branch ID and scope even when the only old varying
        # field was the group structure. Definitions are complete per branch;
        # no conflict is compacted or resolved during migration.
        for alt,(_,body,_) in zip(clean['alternatives'],versions):
            alt['overrides'].pop('properties.relation_metadata',None)
            alt['overrides']['properties.definition']=body['properties'].get('definition','')
        nodes.append(clean)
    raw['graph']['nodes']=nodes
    return GraphState.model_validate(raw),records
