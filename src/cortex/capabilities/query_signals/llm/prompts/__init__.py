from hashlib import sha256
from importlib.resources import files


def load_prompt():
    return (
        files(__package__)
        .joinpath("initial_sql.md")
        .read_text(encoding="utf-8")
        .strip()
    )


def prompt_version():
    return sha256(load_prompt().encode()).hexdigest()
