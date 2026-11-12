"""BIRD loader implementation detail: batch conversion of its two text fields."""

import json

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import Field

from .agent import load_prompt
from .loading import LoaderResult
from .management import MetadataChange
from .models import Column, Model
from .run import AgentExecutionError, AgentResult


class BirdColumnText(Model):
    object_id: str
    source_ref: str
    content: dict[str, str]


class NormalizedColumn(Model):
    object_id: str = Field(description="Copy the supplied target address exactly.")
    description: str | None = Field(
        description="Faithful extraction of the original column explanation. Preserve terse labels verbatim or use null; never expand them from names or common sense."
    )
    value_description: str | None = Field(
        description="Extract only explicitly stated value meanings, codes and conditions; preserve literal values. No inferred interpretations."
    )
    unresolved: str | None = Field(
        default=None,
        description="If the source cannot be interpreted safely, explain why. This record will remain unchanged.",
    )


class BirdDescriptionInterpreter:
    """Normalize bounded batches using only file text and current column metadata."""

    def __init__(self, model):
        self.model = model

    def convert(self, snapshot, records, *, options):
        result = LoaderResult()
        objects = snapshot.objects()
        grouped = {}
        for record in records:
            obj = objects.get(record.object_id)
            if not isinstance(obj, Column):
                raise TypeError(
                    f"File record does not target a column: {record.object_id}"
                )
            item = grouped.setdefault(
                record.object_id,
                {
                    "object_id": record.object_id,
                    "name": obj.name,
                    "current": obj.model_dump(
                        include={"description", "value_description"}
                    ),
                    "file_records": [],
                },
            )
            item["file_records"].append(
                {"source_ref": record.source_ref, **record.content}
            )

        def issue(message):
            result.status = "incomplete"
            result.issues.append("BIRD interpretation: " + message)

        batches, batch = [], []
        for item in grouped.values():
            if len(json.dumps(item, ensure_ascii=False)) > 40000:
                issue(
                    f"{item['object_id']}: source text exceeds batch limit; existing metadata unchanged"
                )
                continue
            if batch and (
                len(batch) >= 20
                or len(json.dumps(batch + [item], ensure_ascii=False)) > 40000
            ):
                batches.append(batch)
                batch = []
            batch.append(item)
        if batch:
            batches.append(batch)
        if not batches:
            return result

        @tool
        def submit_file_metadata(entries: list[NormalizedColumn]) -> dict:
            """Return exactly one normalized entry for every supplied column; no other targets."""
            return {"entries": [entry.model_dump() for entry in entries]}

        try:
            bound = self.model.bind_tools([submit_file_metadata])
            calls = 0
            for batch in batches:
                targets = {item["object_id"]: item for item in batch}
                error = None
                for attempt in range(2):
                    if calls >= options.max_agent_calls:
                        issue(
                            "Model-call budget exhausted; remaining existing metadata unchanged"
                        )
                        return result
                    messages = [
                        SystemMessage(content=load_prompt("bird_import")),
                        HumanMessage(
                            content=json.dumps({"columns": batch}, ensure_ascii=False)
                        ),
                    ]
                    if error:
                        messages.append(
                            HumanMessage(
                                content=f"Correct the previous batch validation failure: {error}"
                            )
                        )
                    calls += 1
                    response = bound.invoke(messages)
                    result.trace.append(
                        {
                            "targets": list(targets),
                            "model_response": response.model_dump(mode="json"),
                        }
                    )
                    try:
                        if (
                            response.invalid_tool_calls
                            or response.response_metadata.get("finish_reason")
                            in {"length", "content_filter", "error"}
                        ):
                            raise ValueError("Malformed or incomplete model response")
                        if (
                            len(response.tool_calls) != 1
                            or response.tool_calls[0]["name"] != "submit_file_metadata"
                        ):
                            raise ValueError(
                                "Return one submit_file_metadata tool call"
                            )
                        output = submit_file_metadata.invoke(
                            response.tool_calls[0]["args"]
                        )
                        entries = [
                            NormalizedColumn.model_validate(entry)
                            for entry in output["entries"]
                        ]
                        ids = [entry.object_id for entry in entries]
                        if len(ids) != len(set(ids)) or set(ids) != set(targets):
                            raise ValueError(
                                "Output must cover each input target exactly once, with no additional targets"
                            )
                        changes = []
                        unresolved = []
                        for entry in entries:
                            original = targets[entry.object_id]
                            source_texts = {
                                text.strip()
                                for record in original["file_records"]
                                for key, text in record.items()
                                if key in {"column_description", "value_description"}
                                and text.strip()
                            }
                            label_only = source_texts and all(
                                text.casefold() == original["name"].strip().casefold()
                                for text in source_texts
                            )
                            if label_only and any(
                                value is not None and value not in source_texts
                                for value in (
                                    entry.description,
                                    entry.value_description,
                                )
                            ):
                                raise ValueError(
                                    f"SOURCE_LABEL_EXPANDED: {entry.object_id}; retain the original label or null"
                                )
                            if entry.unresolved:
                                unresolved.append(
                                    f"{entry.object_id}: {entry.unresolved}; existing metadata unchanged"
                                )
                                continue
                            for field in (
                                "description",
                                "value_description",
                            ):
                                value = getattr(entry, field)
                                if value != original["current"][field]:
                                    changes.append(
                                        MetadataChange(
                                            object_id=entry.object_id,
                                            field=field,
                                            value=value,
                                            expected_value=original["current"][field],
                                            source_refs=list(
                                                dict.fromkeys(
                                                    r["source_ref"]
                                                    for r in original["file_records"]
                                                )
                                            ),
                                        )
                                    )
                        if changes:
                            result.batches.append(changes)
                        for message in unresolved:
                            issue(message)
                        result.trace.append(
                            {
                                "targets": list(targets),
                                "changed": [c.model_dump() for c in changes],
                                "unresolved": unresolved,
                            }
                        )
                        break
                    except (ValueError, TypeError, KeyError) as exc:
                        error = str(exc)
                        result.trace.append(
                            {"targets": list(targets), "validation_error": error}
                        )
                        if attempt == 1:
                            issue(
                                f"{list(targets)}: {error}; existing metadata unchanged"
                            )
            return result
        except Exception as exc:
            raise AgentExecutionError(
                AgentResult(
                    snapshot, status="failed", trace=result.trace, error=str(exc)
                )
            ) from exc
