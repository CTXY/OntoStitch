from __future__ import annotations

import hashlib
import json

from cortex.config.models import CortexConfig


def redacted_config(config: CortexConfig) -> dict:
    payload = config.model_dump(mode="json")
    if payload["llm"].get("api_key") is not None:
        payload["llm"]["api_key"] = "***"
    if payload["database"].get("url") is not None:
        payload["database"]["url"] = "***"
    payload["services"]["graph_merge"]["checkpoint_url"] = "***"
    validation = payload["services"]["sql_training"]["validation"]
    if validation.get("token") is not None:
        validation["token"] = "***"
    return payload


def config_fingerprint(config: CortexConfig) -> str:
    canonical = json.dumps(
        redacted_config(config),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
