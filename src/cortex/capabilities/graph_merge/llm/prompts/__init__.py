"""Versioned Graph Merge Agent prompts."""

"""Central prompt registry. Files are versioned with code; hashes identify rendered prompts."""

from hashlib import sha256
from importlib.resources import files

PROMPTS = {
    "identity": "identity.md",
    "cross_type_identity": "cross_type_identity.md",
    "consolidate": "consolidate.md",
    "assess": "consolidation_check.md",
}
SEMANTIC_TYPES = frozenset({"concept", "metric", "generation_rule"})


def load_prompt(role: str, node_type: str | None = None) -> str:
    root = files(__package__)
    sections = [root.joinpath(PROMPTS[role]).read_text(encoding="utf-8").strip()]
    if role in {"consolidate", "assess"}:
        sections.append(root.joinpath("shared.md").read_text(encoding="utf-8").strip())
        if node_type in SEMANTIC_TYPES:
            sections.append(
                root.joinpath("merge_types", f"{node_type}.md")
                .read_text(encoding="utf-8")
                .strip()
            )
    return "\n\n".join(sections)


def prompt_fingerprint(role: str, node_type: str | None = None) -> str:
    return sha256(load_prompt(role, node_type).encode()).hexdigest()


def prompt_version() -> str:
    return sha256(
        "".join(
            prompt_fingerprint(role, kind)
            for role in PROMPTS
            for kind in sorted(SEMANTIC_TYPES)
        ).encode()
    ).hexdigest()
