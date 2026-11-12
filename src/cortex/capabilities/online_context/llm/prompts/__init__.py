from functools import lru_cache
from hashlib import sha256
from importlib.resources import files

_PROMPT_DIR = files(__package__)
_PROMPT_FILES = {
    "intra_node_conflicts": "intra_node_conflicts.md",
    "initial_sql_memory_conflicts": "initial_sql_memory_conflicts.md",
    "cross_node_conflicts": "cross_node_conflicts.md",
    "audit_slot_relevance": "audit_slot_relevance.md",
    "guidance": "guidance.md",
    "simulated_user": "simulated_user.md",
    "simulated_user_review": "simulated_user_review.md",
}

_SLOT_CONTRACT_PROMPTS = {
    "intra_node_conflicts",
    "initial_sql_memory_conflicts",
    "cross_node_conflicts",
}


@lru_cache
def load_prompt(name: str) -> str:
    if name in {"select_semantic_assets", "select_schema_grounding"}:
        from cortex.capabilities.retrieve_db.llm.prompts import (
            load_prompt as retrieval_prompt,
        )

        return retrieval_prompt(name)
    try:
        filename = _PROMPT_FILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Online Context prompt: {name}") from exc
    prompt = _PROMPT_DIR.joinpath(filename).read_text(encoding="utf-8").strip()
    if name in _SLOT_CONTRACT_PROMPTS:
        shared = (
            _PROMPT_DIR.joinpath("shared_slot_candidate_contract.md")
            .read_text(encoding="utf-8")
            .strip()
        )
        return f"{shared}\n\n{prompt}"
    return prompt


def prompt_fingerprint(name: str) -> str:
    return sha256(load_prompt(name).encode()).hexdigest()


def prompt_version() -> str:
    roles = sorted(
        name
        for name in _PROMPT_FILES
        if not name.startswith("simulated_user") and not name.startswith("select_")
    )
    return sha256(
        "".join(prompt_fingerprint(name) for name in roles).encode()
    ).hexdigest()


__all__ = ["load_prompt", "prompt_fingerprint", "prompt_version"]
