"""Local retrieval prompt registry; content is versioned with code."""

from hashlib import sha256
from importlib.resources import files

PROMPTS = {
    name: name + ".md" for name in ("select_semantic_assets", "select_schema_grounding")
}


def load_prompt(name: str) -> str:
    if name not in PROMPTS:
        raise ValueError(f"Unknown retrieval prompt: {name}")
    return (
        files(__package__).joinpath(PROMPTS[name]).read_text(encoding="utf-8").strip()
    )


def prompt_version() -> str:
    return sha256(
        "\n".join(name + load_prompt(name) for name in sorted(PROMPTS)).encode()
    ).hexdigest()
