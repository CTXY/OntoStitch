"""Read explicitly configured API credentials without exposing them in logs."""
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import SecretStr


@dataclass(frozen=True)
class KeySlot:
    index: int
    api_key: SecretStr = field(repr=False)


def read_key_slots(path: str | Path) -> tuple[KeySlot, ...]:
    values = [line.strip() for line in Path(path).read_text().splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    if not values or any(len(v.split()) != 1 for v in values):
        raise ValueError("Key file requires one API key per non-comment line")
    if len(set(values)) != len(values):
        raise ValueError("Key file contains duplicate slots")
    return tuple(KeySlot(i, SecretStr(v)) for i, v in enumerate(values))
