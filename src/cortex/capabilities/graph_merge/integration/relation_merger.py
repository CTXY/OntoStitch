"""Description-only relation consolidation, with durable model/application stages."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.integration.context import PHYSICAL, remap_node
from cortex.capabilities.graph_merge.integration.evidence_refs import EvidencePresentation
from cortex.capabilities.graph_merge.integration.task_state import canonical_hash
from cortex.schema import GraphState
from cortex.schema.relation_contracts import effective_schema
from cortex.schema.validation import validate_graph_state
from cortex.capabilities.graph_merge.llm.prompt_language import english_contract


@dataclass
class RelationMergeResult:
    state: GraphState
    edge_aliases: dict
    checkpoint: dict


def policy_fingerprint():
    from cortex.capabilities.graph_merge.llm import relation_merger as llm_module
    prompt = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text()
    return canonical_hash({'version': 1, 'prompt': prompt,
        'integration': sha256(Path(__file__).read_bytes()).hexdigest(),
        'llm': sha256(Path(llm_module.__file__).read_bytes()).hexdigest()})


def _fixed(properties):
    result = deepcopy(properties)
    result.pop('description', None)
    if 'alternative_ids' in result:
        result['alternative_ids'] = sorted(set(result['alternative_ids']))
    return result


def relation_groups(state, focus_ids=None):
    """Only equal structural assertions may share a generated description."""
    nodes = {node.id: node for node in state.graph.nodes}
    buckets = {}
    for edge in state.graph.edges:
        if nodes[edge.from_node_id].node_type in PHYSICAL:
            continue
        key = canonical_hash([edge.from_node_id, edge.to_node_id, edge.edge_type, _fixed(edge.properties)])
        buckets.setdefault(key, []).append(edge)
    focus = None if focus_ids is None else set(focus_ids)
    return [{'group_id': 'relation-group:' + key, 'from_node_id': group[0].from_node_id,
        'to_node_id': group[0].to_node_id, 'edge_type': group[0].edge_type,
        'fixed_properties': _fixed(group[0].properties),
        'member_edge_ids': sorted(edge.id for edge in group)}
        for key, group in sorted(buckets.items()) if len(group) > 1
        and (focus is None or focus & {group[0].from_node_id, group[0].to_node_id, *(edge.id for edge in group)})]


def _public_node(node, evidence):
    record = {'id': node.id, 'node_type': node.node_type,
        'evidence_ids': evidence.ids_for((*node.sources, *node.common_sources))}
    if node.node_type in PHYSICAL:
        record['content'] = {'name': node.name, 'description': node.description,
            'properties': {key: deepcopy(value) for key, value in node.properties.items()
                if key in {'table_name', 'column_name', 'table_ref', 'column_ref', 'value', 'data_type'}}}
    elif node.alternatives:
        record['alternatives'] = [{'alternative_id': alt.alternative_id,
            'content': GraphManager.resolve_alternative(node, alt).model_dump(mode='json'),
            'evidence_ids': evidence.ids_for(alt.sources)} for alt in node.alternatives]
        record['conflict_points'] = [point.model_dump(mode='json') for point in node.conflict_points]
    else:
        record['content'] = node.model_dump(mode='json', include={'name', 'description', 'aliases', 'tags', 'properties'})
    return record


def build_relation_packet(context, state, groups):
    evidence = EvidencePresentation(context)
    nodes = {node.id: node for node in state.graph.nodes}
    edges = {edge.id: edge for edge in state.graph.edges}
    edge_ids = {ident for group in groups for ident in group['member_edge_ids']}
    selected = {ident for group in groups for ident in (group['from_node_id'], group['to_node_id'])}
    contracts = effective_schema(context.schema, state.graph).edge_type_index()
    packet = {'groups': deepcopy(groups),
        'nodes': [_public_node(nodes[ident], evidence) for ident in sorted(selected)],
        'relations': [{**edge.model_dump(mode='json', exclude={'sources', 'reference_origin'}),
                      'evidence_ids': evidence.ids_for(edge.sources)} for ident, edge in sorted(edges.items()) if ident in edge_ids],
        'contracts': english_contract([contracts[key].model_dump(mode='json') for key in sorted({edges[k].edge_type for k in edge_ids if k in edges})])}
    packet['input_hash'] = canonical_hash(packet)
    return packet


def consolidate_relations(agent, context, state, checkpoint=None, save=None, batch_size=10, *, validate=True, focus_ids=None):
    """Return an atomically applied result; model outputs survive apply failures.

    `save` receives a JSON-compatible complete checkpoint. Reuse a checkpoint
    with the same source state, or with its exact already-applied output state.
    No caller state is mutated, and no Assessment is invoked.
    """
    from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults, invoke_relation_merger
    if not 1 <= batch_size <= 10:
        raise ValueError('Relation merger batches contain 1–10 complete groups')
    state = GraphState.model_validate(state)
    source_hash, policy = canonical_hash(state), policy_fingerprint()
    focus = None if focus_ids is None else sorted(set(focus_ids))
    scope_hash = canonical_hash(focus)
    schema_hash = canonical_hash(context.schema)
    evidence_hash = canonical_hash(sorted(({'id': row.id, 'source': row.source.model_dump(mode='json'),
        'content': row.content} for row in context.evidence), key=lambda row: (row['id'], canonical_hash(row))))
    cp = deepcopy(checkpoint) if checkpoint else {'version': 1, 'source_hash': source_hash,
        'schema_hash': schema_hash, 'evidence_hash': evidence_hash, 'scope_hash': scope_hash,
        'policy': policy, 'stage': 'collecting', 'results': {}, 'batches': {}, 'edge_aliases': {}}
    if checkpoint and cp.get('integrity_hash') != canonical_hash({key: value for key, value in cp.items() if key != 'integrity_hash'}):
        raise ValueError('Relation merger checkpoint integrity differs')
    if cp.get('version') != 1 or cp.get('policy') != policy:
        raise ValueError('Relation merger checkpoint policy differs; retain it and explicitly start a new task')
    if cp.get('schema_hash') != schema_hash or cp.get('evidence_hash') != evidence_hash:
        raise ValueError('Relation merger checkpoint schema/evidence binding differs')
    if cp.get('scope_hash') != scope_hash:
        raise ValueError('Relation merger checkpoint focus scope differs')
    if cp.get('stage') == 'completed' and cp.get('state_hash') == source_hash:
        return RelationMergeResult(state, deepcopy(cp['edge_aliases']), cp)
    if cp.get('source_hash') != source_hash:
        raise ValueError('Relation merger checkpoint source differs from its immutable input')
    groups = relation_groups(state, focus_ids=focus)
    group_hash = canonical_hash(groups)
    if cp.get('groups_hash', group_hash) != group_hash:
        raise ValueError('Relation merger group manifest differs')
    cp['groups_hash'] = group_hash

    def persist():
        cp['integrity_hash'] = canonical_hash({key: value for key, value in cp.items() if key != 'integrity_hash'})
        if save: save(deepcopy(cp))

    persist()
    if not groups:
        cp.update(stage='completed' if validate else 'previewed', state_hash=source_hash, edge_aliases={})
        persist()
        return RelationMergeResult(state, {}, cp)
    # Collect all batches before touching a graph. This also keeps metadata
    # components atomic when they span more than ten model groups.
    for offset in range(0, len(groups), batch_size):
        batch = groups[offset:offset + batch_size]
        if all(group['group_id'] in cp['results'] for group in batch):
            continue
        packet = build_relation_packet(context, state, batch)
        batch_key = packet['input_hash']
        saved = cp['batches'].get(batch_key)
        def save_batch(record):
            cp['batches'][batch_key] = deepcopy(record)
            persist()
        hook = getattr(agent, 'merge_relations', None)
        raw = hook(context, packet) if hook else invoke_relation_merger(agent, context, packet, checkpoint=saved, save=save_batch)
        parsed = raw if isinstance(raw, RelationResults) else RelationResults.model_validate(raw)
        expected = {group['group_id'] for group in batch}
        actual = [row.group_id for row in parsed.results]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError('Relation merger output must cover each and only batch group exactly once')
        for row in parsed.results:
            cp['results'][row.group_id] = row.description
        persist()
    cp['stage'] = 'pending_application'
    persist()
    edge_index = {edge.id: edge for edge in state.graph.edges}
    aliases = {}
    for group in groups:
        members = [edge_index[key] for key in group['member_edge_ids']]
        survivor = members[0]  # member IDs were sorted, independent of input order.
        merged = survivor.model_copy(update={'properties': {**survivor.properties,
            'description': cp['results'][group['group_id']]},
            'sources': tuple(dict.fromkeys(source for edge in members for source in edge.sources))})
        for edge in members:
            aliases[edge.id] = survivor.id
            edge_index.pop(edge.id)
        edge_index[survivor.id] = merged
    candidate = state.model_copy(update={'graph': state.graph.model_copy(update={
        'nodes': tuple(remap_node(node, {}, aliases) for node in state.graph.nodes),
        'edges': tuple(edge_index[key] for key in sorted(edge_index))})})
    try:
        if validate and groups:
            validate_graph_state(candidate, effective_schema(context.schema, candidate.graph))
            from cortex.capabilities.graph_merge.integration.local_validation import validate_local_transition
            validate_local_transition(context.schema,state,candidate)
    except (ValueError, TypeError, KeyError) as error:
        cp['application_error'] = {'code': 'RELATION_MERGER_COUPLED_APPLICATION', 'problem': str(error)}
        persist()
        raise
    cp.update(stage='completed' if validate else 'previewed', state_hash=canonical_hash(candidate), edge_aliases=aliases)
    cp.pop('application_error', None)
    persist()
    return RelationMergeResult(candidate, aliases, cp)
