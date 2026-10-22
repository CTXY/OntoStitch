from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from cortex.capabilities.sql_training.llm.generation_rule_taxonomy import (
    render_generation_rule_taxonomy,
)
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
    "metric": "metric.md",
    "semantic_mapping": "semantic_mapping.md",
    "generation_rule": "generation_rule.md",
    "error_analysis_minimal_repair_action": "error_analysis_minimal_repair_action.md",
    "error_analysis_minimal_repair_ablation": "error_analysis_minimal_repair_ablation.md",
    "error_analysis_minimal_repair_joint_ablation": (
        "error_analysis_minimal_repair_joint_ablation.md"
    ),
    "error_analysis_minimal_repair_review": "error_analysis_minimal_repair_review.md",
    "error_analysis_execution_failure": "error_analysis_execution_failure.md",
    "train_initial_candidate_assessment": (
        "train_initial_candidate_assessment.md"
    ),
    "train_residual_concept_optimizer": "train_residual_concept_optimizer.md",
    "train_residual_generation_rule_optimizer": (
        "train_residual_generation_rule_optimizer.md"
    ),
    "train_residual_joint_assessment": "train_residual_joint_assessment.md",
    "train_residual_metric_optimizer": "train_residual_metric_optimizer.md",
}

_PROMPT_CONTRACT_SECTIONS: dict[str, tuple[AssetContractSection, ...]] = {
    "metric": ("common", "metric"),
    "train_initial_candidate_assessment": ("common", "owners"),
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
    "train_residual_concept_optimizer": (
        "common",
        "owners",
        "concept",
        "training_construction",
    ),
    "train_residual_metric_optimizer": (
        "common",
        "owners",
        "metric",
        "training_construction",
    ),
    "train_residual_generation_rule_optimizer": (
        "common",
        "owners",
        "generation_rule",
        "training_construction",
    ),
    "train_residual_joint_assessment": ("common", "owners", "generation_rule"),
}

_PROMPT_SHARED_RULES = {
    "semantic_mapping": ("shared_initial_extraction_rules.md",),
    "generation_rule": ("shared_initial_extraction_rules.md",),
    "error_analysis_execution_failure": ("shared_repair_group_rules.md",),
    "train_initial_candidate_assessment": (
        "shared_assessment_rules.md",
    ),
    "train_residual_joint_assessment": ("shared_assessment_rules.md",),
    "train_residual_concept_optimizer": ("shared_optimizer_rules.md",),
    "train_residual_metric_optimizer": ("shared_optimizer_rules.md",),
    "train_residual_generation_rule_optimizer": ("shared_optimizer_rules.md",),
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
    if name == "generation_rule":
        prompt = prompt.replace(
            "{generation_rule_taxonomy}", render_generation_rule_taxonomy()
        )
    if name == "learning_system":
        rules = (_PROMPT_DIR / "shared_initial_extraction_rules.md").read_text(encoding="utf-8")
        # Reuse domain rules, excluding the legacy one-shot response envelope.
        prompt += "\n\n" + rules.split("## Output", 1)[0].strip()
    return prompt


def prompt_fingerprint(name: str) -> str:
    """Content identity covers composed shared rules, not only the named file."""
    import hashlib
    return hashlib.sha256(load_prompt(name).encode()).hexdigest()
