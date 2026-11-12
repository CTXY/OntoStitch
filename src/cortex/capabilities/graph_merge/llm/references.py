from __future__ import annotations

import hashlib
import json
from typing import Any

_REF_ORIGINS = frozenset({"existing_node", "incoming_node", "existing_alternative"})


class _RefCodec:
    def __init__(self, mapping: dict[tuple[str, str], str]):
        self.forward = mapping
        self.reverse = {
            (origin, short): {"origin": origin, "ref_id": ref_id}
            for (origin, ref_id), short in mapping.items()
        }

    @classmethod
    def from_payload(cls, payload: Any) -> _RefCodec:
        refs: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()

        def visit(value: Any) -> None:
            if _is_ref_payload(value):
                key = (str(value["origin"]), str(value["ref_id"]))
                if key not in seen:
                    seen.add(key)
                    refs.append(key)
                return
            if isinstance(value, dict):
                for child in value.values():
                    visit(child)
            elif isinstance(value, list | tuple):
                for child in value:
                    visit(child)

        visit(payload)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        prefix = "gm_ref"
        if any(
            f"{prefix}_{index:03d}" in serialized for index in range(1, len(refs) + 1)
        ):
            digest = hashlib.sha256(serialized.encode()).hexdigest()[:6]
            prefix = f"gm_ref_{digest}"
        return cls(
            {key: f"{prefix}_{index:03d}" for index, key in enumerate(refs, start=1)}
        )

    def encode(self, value: Any) -> Any:
        if _is_ref_payload(value):
            key = (str(value["origin"]), str(value["ref_id"]))
            short = self.forward.get(key)
            if short is None:
                return value
            return {**value, "ref_id": short}
        if isinstance(value, dict):
            return {key: self.encode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.encode(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.encode(child) for child in value)
        return value

    def decode(self, value: Any) -> Any:
        if _is_ref_payload(value):
            original = self.reverse.get((str(value["origin"]), str(value["ref_id"])))
            if original is None:
                return value
            return {**value, **original}
        if isinstance(value, dict):
            return {key: self.decode(child) for key, child in value.items()}
        if isinstance(value, list):
            return [self.decode(child) for child in value]
        if isinstance(value, tuple):
            return tuple(self.decode(child) for child in value)
        return value

    def decode_model(self, result, output_model):
        if not self.forward:
            return result
        decoded = self.decode(result.model_dump(mode="json"))
        return output_model.model_validate(decoded)


def _is_ref_payload(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("origin") in _REF_ORIGINS
        and isinstance(value.get("ref_id"), str)
    )
