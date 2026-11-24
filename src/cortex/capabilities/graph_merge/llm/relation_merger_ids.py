"""Exact batch-local group handles for the description-only relation model.

Long graph identifiers remain controller-owned. Each invocation binds its exact
original and displayed manifests. The adapter is local to one service call; it
never replaces a process-wide model function or interprets a native checkpoint.
"""

from copy import deepcopy
from hashlib import sha256
from pathlib import Path

from cortex.capabilities.graph_merge.runtime.state import canonical_hash


def adapter_hash():
    return sha256(Path(__file__).read_bytes()).hexdigest()


def make_adapter(native):
    def invoke(agent, context, packet, *, checkpoint=None, save=None):
        from cortex.capabilities.graph_merge.llm.relation_merger import RelationResults

        original_hash = canonical_hash(
            {key: value for key, value in packet.items() if key != "input_hash"}
        )
        if packet.get("input_hash") != original_hash:
            raise ValueError(
                "Original relation packet hash differs from its actual content"
            )
        groups = packet["groups"]
        full_ids = [group["group_id"] for group in groups]
        if len(full_ids) != len(set(full_ids)):
            raise ValueError("Duplicate original relation group IDs")
        id_map = {f"G{index}": ident for index, ident in enumerate(full_ids, 1)}
        displayed = deepcopy(packet)
        for index, group in enumerate(displayed["groups"], 1):
            group["group_id"] = f"G{index}"
        displayed.pop("input_hash")
        displayed["input_hash"] = canonical_hash(displayed)
        binding = {
            "version": 1,
            "adapter_hash": adapter_hash(),
            "original_input_hash": packet["input_hash"],
            "alias_input_hash": displayed["input_hash"],
            "id_map": id_map,
        }
        cp = None
        if checkpoint is not None:
            if checkpoint.get("local_id_protocol") != binding:
                raise ValueError(
                    "Relation local-ID protocol or immutable manifest changed"
                )
            cp = deepcopy(checkpoint)

        def persist(record):
            record = deepcopy(record)
            record["local_id_protocol"] = deepcopy(binding)
            if save:
                save(record)

        result = native(agent, context, displayed, checkpoint=cp, save=persist)
        result = (
            result
            if isinstance(result, RelationResults)
            else RelationResults.model_validate(result)
        )
        ids = [row.group_id for row in result.results]
        if len(ids) != len(set(ids)) or set(ids) != set(id_map):
            raise ValueError(
                "Local relation results must cover each displayed ID exactly once"
            )
        return RelationResults.model_validate(
            {
                "results": [
                    {"group_id": id_map[row.group_id], "description": row.description}
                    for row in result.results
                ]
            }
        )

    return invoke
