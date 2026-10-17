from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cortex.schema import GraphSpace


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    question: str
    dialect: str
    graph_space: GraphSpace
    join_paths: tuple[dict[str, Any], ...] = ()
    enable_query_execution: bool = True
    max_retry_count: int = Field(default=2, ge=0)

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class GenerationResult(ResponseModel):
    datasource_id: str | None = None
    table_id: list[str] = Field(default_factory=list)
    query: str = ""
    exec_query: str | None = None
    result_json: Any = None
    result_markdown: str | None = None
    retry_count: int | None = None
    error_message: str | None = None
    execution_time: dict[str, Any] = Field(default_factory=dict)


class GenerationResponse(ResponseModel):
    code: int
    data: list[GenerationResult] = Field(default_factory=list)
    message: str | None = ""
