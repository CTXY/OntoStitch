"""Exact batch-local group handles for the description-only relation model.

Long graph identifiers remain controller-owned. Completed old receipts are
reused; an unfinished old request is archived before changing its presentation.
The adapter has its own source/manifest binding, independent of node policy.
"""
from copy import deepcopy
from hashlib import sha256
from importlib.resources import files
from pathlib import Path

from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def adapter_hash():
    return sha256(Path(__file__).read_bytes()).hexdigest()


def make_adapter(native):
    def invoke(agent, context, packet, *, checkpoint=None, save=None):
        from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults
        original_hash = canonical_hash({key: value for key, value in packet.items() if key != 'input_hash'})
        if packet.get('input_hash') != original_hash:
            raise ValueError('Original relation packet hash differs from its actual content')
        groups = packet['groups']
        full_ids = [group['group_id'] for group in groups]
        if len(full_ids) != len(set(full_ids)):
            raise ValueError('Duplicate original relation group IDs')
        # A fully validated receipt is already completed work, even if a crash
        # occurred before its descriptions entered the outer result map.
        if checkpoint and checkpoint.get('result') is not None and not checkpoint.get('local_id_protocol'):
            return native(agent, context, packet, checkpoint=checkpoint, save=save)
        id_map = {f'G{index}': ident for index, ident in enumerate(full_ids, 1)}
        displayed = deepcopy(packet)
        for index, group in enumerate(displayed['groups'], 1):
            group['group_id'] = f'G{index}'
        displayed.pop('input_hash')
        displayed['input_hash'] = canonical_hash(displayed)
        binding = {'version': 1, 'adapter_hash': adapter_hash(),
                   'original_input_hash': packet['input_hash'],
                   'alias_input_hash': displayed['input_hash'], 'id_map': id_map}
        cp, legacy = None, None
        if checkpoint and checkpoint.get('local_id_protocol'):
            if checkpoint['local_id_protocol'] != binding:
                raise ValueError('Relation local-ID protocol or immutable manifest changed')
            cp = deepcopy(checkpoint)
            legacy = cp.get('legacy_request_checkpoint')
        elif checkpoint:
            # Check the prior binding before adopting its read facts. Never
            # accept or edit its rejected descriptions or reset accepted work.
            system = files('cortex.capabilities.graph_merge.llm.prompts').joinpath('relation_merger/writer.en.md').read_text()
            expected = canonical_hash({'system': system, 'packet': packet,
                                       'schema': RelationResults.model_json_schema()})
            if checkpoint.get('binding') != expected:
                raise ValueError('Legacy relation checkpoint does not match its original batch')
            legacy = deepcopy(checkpoint)
            new_binding = canonical_hash({'system': system, 'packet': displayed,
                                          'schema': RelationResults.model_json_schema()})
            cp = {'binding': new_binding, 'requests': 0,
                  'evidence': deepcopy(checkpoint.get('evidence', {})),
                  'feedback': {'status': 'local_ids_supplied',
                    'instruction': 'Use only the displayed G1, G2, ... identifiers; copy each exactly once.'}}

        def persist(record):
            record = deepcopy(record)
            record['local_id_protocol'] = deepcopy(binding)
            if legacy is not None:
                record['legacy_request_checkpoint'] = deepcopy(legacy)
            if save:
                save(record)

        result = native(agent, context, displayed, checkpoint=cp, save=persist)
        result = result if isinstance(result, RelationResults) else RelationResults.model_validate(result)
        ids = [row.group_id for row in result.results]
        if len(ids) != len(set(ids)) or set(ids) != set(id_map):
            raise ValueError('Local relation results must cover each displayed ID exactly once')
        return RelationResults.model_validate({'results': [
            {'group_id': id_map[row.group_id], 'description': row.description}
            for row in result.results]})
    return invoke


def install():
    """Opt in for one worker process; do not hot-reload another running worker."""
    from cortex.capabilities.graph_merge.llm import relation_merger
    if not getattr(relation_merger.invoke_relation_merger, 'uses_local_ids', False):
        wrapped = make_adapter(relation_merger.invoke_relation_merger)
        wrapped.uses_local_ids = True
        relation_merger.invoke_relation_merger = wrapped
