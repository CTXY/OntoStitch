from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cortex.capabilities.sql_training.llm.parsing import run_llm_extraction


class _JoinColumnPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_column: str
    to_column: str


class _GoldJoinProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_table: str
    to_table: str
    join_type: Literal["INNER", "LEFT", "RIGHT", "FULL"]
    column_pairs: list[_JoinColumnPair] = Field(min_length=1)
    sql_construct_type: str | None = None
    evidence_fragment: str | None = None


class _GoldJoinOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    joins: list[_GoldJoinProposal] = Field(default_factory=list)


def extract_gold_join_proposals(
    llm: Any,
    record: dict[str, Any],
    prompt: str,
    *,
    trace_capture: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return run_llm_extraction(
        llm=llm,
        prompt=prompt,
        output_model=_GoldJoinOutput,
        human_payload={
            "sql": str(record["sql"]),
            "dialect": str(record.get("database_dialect") or ""),
            "table_columns": record["table_columns"],
        },
        map_result=lambda result: [
            join.model_dump() for join in _GoldJoinOutput.model_validate(result).joins
        ],
        trace_capture=trace_capture,
    )
