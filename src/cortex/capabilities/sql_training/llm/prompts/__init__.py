from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from cortex.schema.ontology_asset_definitions import (
    AssetContractSection,
    render_ontology_asset_contract,
)

_PROMPT_DIR = Path(__file__).resolve().parent
_PROMPT_FILES = {
    "learning_system": "learning/system.md",
    "learning_direct": "learning/direct.md",
    "learning_repair": "learning/repair.md",
    "learning_assessment": "learning/assessment.md",
    "gold_join_edges": "gold_join_edges.md",
    "error_analysis_minimal_repair_action": "error_analysis_minimal_repair_action.md",
    "error_analysis_minimal_repair_ablation": "error_analysis_minimal_repair_ablation.md",
    "error_analysis_minimal_repair_joint_ablation": (
        "error_analysis_minimal_repair_joint_ablation.md"
    ),
    "error_analysis_minimal_repair_review": "error_analysis_minimal_repair_review.md",
    "error_analysis_execution_failure": "error_analysis_execution_failure.md",
}

_PROMPT_CONTRACT_SECTIONS: dict[str, tuple[AssetContractSection, ...]] = {
    "error_analysis_minimal_repair_review": (
        "common",
        "owners",
        "training_construction",
    ),
    "error_analysis_execution_failure": (
        "common",
        "owners",
        "training_construction",
    ),
}

_PROMPT_SHARED_RULES = {
    "error_analysis_execution_failure": ("shared_repair_group_rules.md",),
}


@lru_cache
def load_prompt(name: str) -> str:
    try:
        filename = _PROMPT_FILES[name]
    except KeyError as exc:
        raise ValueError(f"Unknown prompt: {name}") from exc
    prompt = (_PROMPT_DIR / filename).read_text(encoding="utf-8").strip()
    prefixes: list[str] = []
    if sections := _PROMPT_CONTRACT_SECTIONS.get(name):
        contract = render_ontology_asset_contract(sections).strip()
        if "{asset_contract}" in prompt:
            prompt = prompt.replace("{asset_contract}", contract)
        else:
            prefixes.append(contract)
    shared_rules = "\n\n".join(
        (_PROMPT_DIR / shared_filename).read_text(encoding="utf-8").strip()
        for shared_filename in _PROMPT_SHARED_RULES.get(name, ())
    )
    if "{shared_stage_rules}" in prompt:
        prompt = prompt.replace("{shared_stage_rules}", shared_rules)
    elif shared_rules:
        prefixes.append(shared_rules)
    if prefixes:
        prefix = "\n\n".join(prefixes)
        prompt = f"{prefix}\n\n{prompt}"
    if name == "learning_system":
        rules = (_PROMPT_DIR / "shared_learning_rules.md").read_text(encoding="utf-8")
        prompt += "\n\n" + rules.strip()
    return prompt


def prompt_fingerprint(name: str) -> str:
    """Content identity covers composed shared rules, not only the named file."""
    import hashlib
    return hashlib.sha256(load_prompt(name).encode()).hexdigest()
