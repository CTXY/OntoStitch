"""English presentation of known schema metadata, never of graph/evidence facts.

The lookup changes prose only. Schema keys, IDs, types and validation rules remain
identical. Unknown authored text is preserved rather than guessed or translated
at request time. Changes to this asset are part of checkpoint policy binding.
"""

import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def _catalog():
    return json.loads(
        files("cortex.capabilities.graph_merge.llm.prompts")
        .joinpath("schema-contracts.en.json")
        .read_text()
    )


def english_contract(value):
    if isinstance(value, str):
        return _catalog().get(value, value)
    if isinstance(value, dict):
        return {key: english_contract(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [english_contract(item) for item in value]
    return value
