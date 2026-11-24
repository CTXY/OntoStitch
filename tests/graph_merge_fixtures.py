"""Portable graph factories and deterministic current-protocol test agents."""
from cortex.application.ontology_graph import GraphManager
from cortex.capabilities.graph_merge.inputs import IntegrationContext
from cortex.schema import GraphEdge, GraphNode, GraphSpace, GraphState, SourceReference, load_root_schema

def concept(key, target, *, name=None):
    return GraphNode(
        id=key,
        node_type="concept",
        name=name or key,
        properties={"targets": [[target]]},
        sources=(SourceReference(source_id=key),),
    )


def edge(key, source, target, kind="grounded_on"):
    return GraphEdge(
        id=key,
        edge_type=kind,
        from_node_id=source,
        to_node_id=target,
        properties={"description": "The evidenced business relation."},
    )


def business_contract():
    return load_root_schema().edge_type_index()["has_attribute"].model_copy(update={
        "id": "has_feature", "name": "has feature", "description": "The object has the specified business feature.",
    })


def state(nodes=(), edges=()):
    return GraphManager(load_root_schema()).prepare_state(
        GraphState(graph=GraphSpace(nodes=tuple(nodes), edges=tuple(edges), relation_contracts=(business_contract(),) if any(e.edge_type == "has_feature" for e in edges) else ()))
    )[0]


def catalog():
    return [
        GraphNode(
            id="table",
            node_type="table",
            name="objects",
            properties={"table_name": "objects"},
        ),
        *[
            GraphNode(
                id=k,
                node_type="column",
                name=k,
                properties={"table_ref": "table", "column_name": k},
            )
            for k in ("a", "b", "c")
        ],
    ]


def contexts(old, new):
    context = IntegrationContext(old, new, load_root_schema())
    context.payload("identity")
    return context


def rule(key, action):
    return GraphNode(
        id=key,
        node_type="generation_rule",
        name=key,
        properties={
            "triggers": {"question": {"phrases": ["test behavior"]}},
            "condition": "When the evidenced operation is requested",
            "action": action,
            "reason": "Supported by fixture source",
        },
        sources=(SourceReference(source_id="case"),),
    )


def aligned_preview(context):
    """Use the current engine's physical alignment and exact assertion aliases."""
    from cortex.capabilities.graph_merge.graph.batch import alias_view
    from cortex.capabilities.graph_merge.graph.publication import Preview
    current, edges = alias_view(context.union_state(), {})
    context.candidate = current
    return Preview(current, dict(context.physical_map), edges, [])


def compile_graph_edits(context, edits=(), changes=(), relation_contracts=()):
    """Exercise the current internal batch compiler, without an old engine."""
    from cortex.capabilities.graph_merge.graph.batch import ActiveBatch, BatchResult
    base = aligned_preview(context).state
    context.task_visible_ids = {node.id for node in base.graph.nodes} | {edge.id for edge in base.graph.edges}
    batch = ActiveBatch(context, base, targets=set(context.task_visible_ids))
    batch.submit_atomic(BatchResult(edits=[edit.model_dump(mode='json', exclude_none=True) if hasattr(edit, 'model_dump') else edit for edit in edits],
                                   changes=changes, relation_contracts=relation_contracts))
    from cortex.capabilities.graph_merge.graph.publication import Preview
    surviving = {node.id for node in batch.preview.graph.nodes}
    node_map = {key: key for key in context.raw_nodes if key in surviving}
    node_map.update(context.physical_map)
    return Preview(batch.preview, node_map, dict(batch.edge_aliases), [])


class CurrentNoopAgent:
    """Deterministic no-op decisions through the supported transaction protocol."""
    node_local_contract = True
    reconciliation_contract = True
    relation_merger_contract = True
    relation_merger_batch_size = 10

    def invoke(self, phase, context, payload):
        from cortex.capabilities.graph_merge.identity.contract import IdentityDecision
        from cortex.capabilities.graph_merge.reconciliation.contract import AssessmentRecord, DraftDocument
        from cortex.capabilities.graph_merge.runtime.state import canonical_hash
        if phase == 'identity':
            return IdentityDecision(distinctions=[{'members': list(pair), 'reason': 'Keep separately evidenced meanings.'}
                                                  for pair in context.mandatory])
        if phase in {'consolidate', 'node_assess'}:
            from cortex.capabilities.graph_merge.consolidation.contract import NodeWriterResult, NodeAssessmentResult, normalize_node_result
            from cortex.capabilities.graph_merge.consolidation.presentation import build_node_packet
            packet = build_node_packet(context, {**payload, 'phase': phase}, before=context.task_before,
                                       candidate=getattr(context, 'candidate', None))
            raw = (NodeWriterResult(keep_separate_ids=tuple(packet['group']['member_ids']),
                                    explanation='Preserve the separately evidenced input meanings.')
                   if phase == 'consolidate' else NodeAssessmentResult(verdict='accept'))
            if phase == 'consolidate':
                context.node_local_raw = raw.model_dump(mode='json', exclude_unset=True)
            result, _ = normalize_node_result(phase, raw, payload=packet, base=context.task_before)
            return result
        if phase == 'reconcile':
            return DraftDocument(task_id=payload['task_id'], before_id=canonical_hash(context.task_before), changes=())
        if phase == 'graph_assess':
            return AssessmentRecord(expected_version=context.reconciliation_runtime.draft.version,
                                    explanation='The accepted graph and sources remain unchanged.', issues=())
        raise AssertionError('Unexpected phase in no-op fixture: ' + phase)

    def merge_relations(self, context, packet):
        relations = {row['id']: row for row in packet['relations']}
        results = []
        for group in packet['groups']:
            descriptions = {relations[ident]['properties']['description'] for ident in group['member_edge_ids']}
            if len(descriptions) != 1:
                raise AssertionError('CurrentNoopAgent cannot judge different relation descriptions; supply an explicit merger agent')
            results.append({'group_id': group['group_id'], 'description': descriptions.pop()})
        return {'results': results}
