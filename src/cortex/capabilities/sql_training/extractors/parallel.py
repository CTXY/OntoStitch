from __future__ import annotations

import time
from typing import Any

from cortex.capabilities.sql_training.llm.prompts import load_prompt
from cortex.capabilities.sql_training.llm.provider import LLMProvider

_EXTRACTION_MAX_ATTEMPTS = 2
_EXTRACTION_RETRY_DELAY_SECONDS = 2.0


def run_parallel_extractions(
    *,
    provider: LLMProvider,
    record: dict[str, Any],
    context: dict[str, Any],
) -> list[dict]:
    # Case-level concurrency is owned by TrainPipeline. Keep prompt extraction
    # sequential so configured case workers remain the true global LLM concurrency.
    metric_assets = _extract_prompt_with_retry(
        provider=provider,
        record=record,
        context=context,
        prompt_name="metric",
    )
    metric_additions = _extract_prompt_with_retry(
        provider=provider,
        record={
            **record,
            "prior_case_metric_candidates": metric_assets,
            "extraction_self_check": (
                "Re-run this same Metric extraction contract to find only Metric terms omitted "
                "from the first pass. Return no already proposed Metric ID. Return an empty "
                "candidate_sets list when nothing is missing."
            ),
        },
        context=context,
        prompt_name="metric",
    )
    metric_assets = _append_missing_assets(metric_assets, metric_additions, owner="Metric")
    concept_assets = _extract_prompt_with_retry(
        provider=provider,
        record=record,
        context=context,
        prompt_name="semantic_mapping",
    )
    concept_additions = _extract_prompt_with_retry(
        provider=provider,
        record={
            **record,
            "prior_case_concept_candidates": concept_assets,
            "extraction_self_check": (
                "Re-run this same Concept extraction contract to find only Concept terms omitted "
                "from the first pass. Return no already proposed Concept ID. Return an empty "
                "assets list when nothing is missing."
            ),
        },
        context=context,
        prompt_name="semantic_mapping",
    )
    concept_assets = _append_missing_assets(concept_assets, concept_additions, owner="Concept")
    rule_record = {
        **record,
        "current_case_metric_candidates": metric_assets,
        "current_case_sm_candidates": concept_assets,
    }
    rule_assets = _extract_prompt_with_retry(
        provider=provider,
        record=rule_record,
        context=context,
        prompt_name="generation_rule",
    )
    return [*metric_assets, *concept_assets, *rule_assets]


def _append_missing_assets(base: list[dict], additions: list[dict], *, owner: str) -> list[dict]:
    """Append only newly discovered same-owner assets from the second extraction pass."""

    known_ids = {str(asset.get("id") or "") for asset in base}
    result = list(base)
    for asset in additions:
        asset_id = str(asset.get("id") or "")
        if not asset_id:
            raise ValueError(f"Second-pass {owner} extraction returned a candidate without an ID")
        if asset_id in known_ids:
            raise ValueError(
                f"Second-pass {owner} extraction repeated first-pass candidate ID: {asset_id}"
            )
        known_ids.add(asset_id)
        result.append(asset)
    return result


def _extract_prompt_with_retry(
    *,
    provider: LLMProvider,
    record: dict[str, Any],
    context: dict[str, Any],
    prompt_name: str,
) -> list[dict]:
    for attempt in range(1, _EXTRACTION_MAX_ATTEMPTS + 1):
        try:
            return provider.extract_assets(
                record,
                context={
                    **context,
                    "prompt_name": prompt_name,
                    "prompt": load_prompt(prompt_name),
                },
            )
        except Exception as exc:
            if attempt == _EXTRACTION_MAX_ATTEMPTS or not is_retryable_llm_error(exc):
                raise
            time.sleep(_EXTRACTION_RETRY_DELAY_SECONDS)

    raise AssertionError("unreachable")


def is_retryable_llm_error(exc: BaseException) -> bool:
    """Return whether an LLM failure is likely transient and safe to retry."""

    retryable_names = {
        "APIConnectionError",
        "APITimeoutError",
        "ConnectError",
        "ConnectTimeout",
        "InternalServerError",
        "RateLimitError",
        "ReadTimeout",
        "TimeoutError",
    }
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if any(cls.__name__ in retryable_names for cls in type(current).__mro__):
            return True
        status_code = getattr(current, "status_code", None)
        if status_code == 429 or isinstance(status_code, int) and status_code >= 500:
            return True
        current = current.__cause__ or current.__context__
    return False
