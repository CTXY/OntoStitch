"""Optional tool-using agent; tools choose context and produce provisional semantics."""

import json
import sqlite3
from importlib.resources import files as package_files
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from .management import (
    WRITABLE,
    MetadataChange,
    RelationProposal,
    add_relation,
    apply_changes,
)
from .models import BuildOptions, MetadataSnapshot, SourceRecord
from .run import AgentExecutionError, AgentResult
from .views import MetadataReader, ObjectKind


def load_prompt(name="enrich") -> str:
    return (
        package_files("cortex.capabilities.metadata")
        .joinpath(f"prompts/{name}.md")
        .read_text(encoding="utf-8")
    )


def initial_context(snapshot, options):
    reader = MetadataReader(snapshot)
    columns = [c for t in snapshot.tables for c in t.columns]
    overview = {
        "database": snapshot.database.name,
        "dialect": snapshot.database.dialect,
        "tables": len(snapshot.tables),
        "columns": len(columns),
        "tables_with_profiles": sum(t.profile is not None for t in snapshot.tables),
        "columns_with_profiles": sum(c.profile is not None for c in columns),
        "semantic_properties": sum(
            getattr(obj, field) is not None
            for obj in snapshot.objects().values()
            for field in WRITABLE.get(type(obj), ())
        ),
        "initial_files": sum(s.kind == "initial_file" for s in snapshot.sources),
        "issues": snapshot.issues,
        "table_directory": reader.list(kind="table").model_dump(mode="json"),
        "model_call_budget": options.max_agent_calls,
        "tool_call_budget": options.max_tool_calls,
    }
    return (
        "# Current metadata and available inputs\n\n"
        + json.dumps(overview, ensure_ascii=False, indent=2)
        + "\n\n# Additional context\n\n"
        + (options.context or "No additional domain context supplied.")
        + "\n\n# Requested work\n\nInvestigate missing or ambiguous explanations and update the metadata using the supplied tools."
    )


class MetadataAgent:
    def __init__(self, model, *, task="enrich"):
        if task not in {"enrich", "files"}:
            raise ValueError("Unknown metadata Agent task")
        self.task = task
        self.model = model

    def run(
        self,
        snapshot: MetadataSnapshot,
        source,
        *,
        options: BuildOptions,
    ) -> AgentResult:
        working = snapshot.model_copy(deep=True)
        result = AgentResult(working)
        inspected = set()
        required_columns = {
            key for key in working.objects() if key.startswith("column.")
        }

        def record_coverage():
            unread = sorted(required_columns - inspected)
            result.trace.append(
                {
                    "coverage": {
                        "inspected_columns": sorted(required_columns & inspected),
                        "unread_columns": unread,
                    }
                }
            )
            return unread

        def incomplete(message):
            working.issues.append(message)
            result.snapshot, result.status = working, "incomplete"
            record_coverage()
            return result

        reader = MetadataReader(working)

        @tool
        def list_metadata(
            kind: ObjectKind = "table",
            table_id: str | None = None,
            offset: int = 0,
            limit: int = 20,
        ) -> dict:
            """Browse a typed object directory. Use kind=column and table_id to browse one table's columns; follow next_offset."""
            return reader.list(
                kind=kind, table_id=table_id, offset=offset, limit=limit
            ).model_dump(mode="json")

        @tool
        def inspect_metadata(object_id: str, offset: int = 0, limit: int = 20) -> dict:
            """Read current metadata for one exact object ID. Tables include a paginated column directory; columns include their full metadata. Pagination applies only to table columns."""
            data = reader.inspect(object_id, offset=offset, limit=limit).model_dump(
                mode="json"
            )
            inspected.add(object_id)
            return data

        @tool
        def inspect_metadata_batch(
            object_ids: list[str], include_profiles: bool = False
        ) -> dict:
            """Read 1–20 objects together, omitting profiles by default. Table entries are paginated column directories, not full column metadata."""
            data = reader.inspect_many(object_ids, include_profiles=include_profiles)
            inspected.update(object_ids)
            return data

        @tool
        def metadata_schema(
            kind: Literal["database", "table", "column", "relation", "changes"],
        ) -> dict:
            """Read one type's Pydantic JSON Schema, field meanings and writable properties. Use changes for typed write requests."""
            return reader.schema(kind)

        @tool
        def list_sources(offset: int = 0, limit: int = 20) -> dict:
            """List source references for original files, schema and completed probes without expanding their contents."""
            return reader.list_sources(offset=offset, limit=limit).model_dump(
                mode="json"
            )

        @tool
        def read_source(source_ref: str, offset: int = 0, limit: int = 12000) -> dict:
            """Read a bounded page of one stored source, including files or previous probe results. No arbitrary file access."""
            return reader.read_source(
                source_ref, offset=offset, limit=limit
            ).model_dump(mode="json")

        @tool
        def inspect_database(sql: str) -> dict:
            """Run a read-only SELECT probe with timeout and 100-row output limit; return a source reference."""
            nonlocal reader
            result = source.query(sql)
            source_ref = f"probe:{len(working.sources)}"
            working.sources.append(
                SourceRecord(
                    id=source_ref, kind="database_probe", locator=sql, content=result
                )
            )
            reader = MetadataReader(working)
            return {"source_ref": source_ref, **result}

        @tool
        def propose_metadata(changes: list[MetadataChange]) -> dict:
            """Apply an atomic provisional semantic edit batch after schema/reference checks. DDL/profiles cannot be edited."""
            nonlocal working, reader
            working = apply_changes(working, changes)
            reader = MetadataReader(working)
            return {
                "changed": [
                    {"object_id": c.object_id, "field": c.field} for c in changes
                ],
            }

        @tool
        def propose_relation(proposal: RelationProposal) -> dict:
            """Add a provisional data join, functional dependency or existing-field derivation with source references."""
            nonlocal working, reader
            working = add_relation(working, proposal)
            reader = MetadataReader(working)
            return {
                "relation": working.relations[-1].model_dump(mode="json"),
            }

        tools = [
            list_metadata,
            inspect_metadata,
            inspect_metadata_batch,
            metadata_schema,
            list_sources,
            read_source,
            inspect_database,
            propose_metadata,
            propose_relation,
        ]
        by_name = {t.name: t for t in tools}
        try:
            bound = self.model.bind_tools(tools)
        except Exception as exc:
            result.status, result.error = "failed", str(exc)
            raise AgentExecutionError(result) from exc
        messages = [
            SystemMessage(content=load_prompt(self.task)),
            HumanMessage(
                content=initial_context(working, options)
                + (
                    "\n\nThis run interprets the supplied initial files. Inspect their structure yourself; focus on file-supported metadata."
                    if self.task == "files"
                    else ""
                )
            ),
        ]
        tool_count = 0
        for _ in range(options.max_agent_calls):
            try:
                response = bound.invoke(messages)
            except Exception as exc:
                result.snapshot, result.status, result.error = (
                    working,
                    "failed",
                    str(exc),
                )
                raise AgentExecutionError(result) from exc
            messages.append(response)
            result.trace.append({"model_response": response.model_dump(mode="json")})
            if response.invalid_tool_calls:
                return incomplete(
                    "Agent returned invalid tool calls; semantic coverage is incomplete."
                )
            if response.response_metadata.get("finish_reason") in {
                "length",
                "content_filter",
                "error",
            }:
                return incomplete(
                    "Agent response ended abnormally; semantic coverage is incomplete."
                )
            if not response.tool_calls:
                if self.task == "enrich" and required_columns - inspected:
                    messages.append(
                        HumanMessage(
                            content=(
                                "Coverage check: you have not inspected these columns yet. Use inspect_metadata_batch "
                                "in groups of up to 20 before finishing; retain supported descriptions and leave unknown "
                                "meanings null. Remaining columns: "
                                + json.dumps(sorted(required_columns - inspected))
                            )
                        )
                    )
                    continue
                result.snapshot = working
                record_coverage()
                return result
            for call in response.tool_calls:
                tool_count += 1
                if tool_count > options.max_tool_calls:
                    return incomplete(
                        "Agent tool budget exhausted; semantic coverage is incomplete."
                    )
                try:
                    output = by_name[call["name"]].invoke(call["args"])
                except (ValueError, KeyError, TypeError) as exc:
                    output = {"error": str(exc)}
                except sqlite3.DatabaseError as exc:
                    # Query/runtime failures remain visible and never become fabricated facts.
                    output = {"error": f"{type(exc).__name__}: {exc}"}
                except Exception as exc:
                    result.snapshot, result.status, result.error = (
                        working,
                        "failed",
                        str(exc),
                    )
                    result.trace.append(
                        {"tool": call["name"], "args": call["args"], "error": str(exc)}
                    )
                    raise AgentExecutionError(result) from exc
                result.trace.append(
                    {"tool": call["name"], "args": call["args"], "result": output}
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(output, ensure_ascii=False, default=str),
                        tool_call_id=call["id"],
                    )
                )
        return incomplete(
            "Agent model-call budget exhausted; semantic coverage is incomplete."
        )
