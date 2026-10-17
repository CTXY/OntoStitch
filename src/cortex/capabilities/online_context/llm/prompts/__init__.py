from functools import lru_cache
from pathlib import Path

_PROMPT_DIR = Path(__file__).parent
_PROMPT_FILES = {
    "select_semantic_assets": "select_semantic_assets.md",
    "select_schema_grounding": "select_schema_grounding.md",
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
    try:
        filename = _PROMPT_FILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown Online Context prompt: {name}") from exc
    prompt = (_PROMPT_DIR / filename).read_text(encoding="utf-8").strip()
    if name in _SLOT_CONTRACT_PROMPTS:
        shared = (
            (_PROMPT_DIR / "shared_slot_candidate_contract.md").read_text(encoding="utf-8").strip()
        )
        return f"{shared}\n\n{prompt}"
    return prompt


__all__ = ["load_prompt"]
