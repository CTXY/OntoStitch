"""Validated, evidence-bearing semantic edits. No physical/DDL/profile writes."""

import hashlib
import json
from typing import Literal

from pydantic import Field

from .models import (
    Column,
    Database,
    MetadataSnapshot,
    Model,
    Relation,
    Table,
)


class MetadataChange(Model):
    object_id: str = Field(
        description="Derived object address from list_metadata; not a stored table/column property."
    )
    field: Literal[
        "description", "grain", "scope", "representation", "value_description"
    ] = Field(
        description="One plain-text semantic property: description, grain, scope, representation, or value_description. No nested objects or arbitrary paths."
    )
    value: str | None
    source_refs: list[str] = Field(
        default_factory=list,
        description="Existing source IDs to add to the object-level source_refs. References are deduplicated and accumulated; they are not verification labels.",
    )
    reason: str | None = Field(
        default=None,
        description="Optional explanation for the operation trace; not stored in the metadata property.",
    )
    expected_value: str | None = Field(
        default=None,
        description="Current property value; stale or conflicting updates are rejected.",
    )


WRITABLE = {
    Database: {"description"},
    Table: {"description", "grain", "scope"},
    Column: {"description", "representation", "value_description"},
    Relation: {"description"},
}


class RelationProposal(Model):
    type: Literal["join", "dependency", "derivation"]
    source_columns: list[str] = Field(
        min_length=1,
        description="Use table.column names, e.g. satscores.cds; never tool addresses such as column.satscores.cds.",
    )
    target_columns: list[str] = Field(
        min_length=1,
        description="Use table.column names, e.g. schools.CDSCode; quote components containing dots or double quotes.",
    )
    expression: str | None = Field(
        default=None,
        description="For joins use s/t as source/target aliases and preserve ALL conditions. Dependency means source determines target, not business hierarchy.",
    )
    description: str = Field(min_length=1)
    source_refs: list[str] = Field(
        default_factory=list,
    )
    reason: str | None = None


def add_relation(
    snapshot: MetadataSnapshot, proposal: RelationProposal
) -> MetadataSnapshot:
    candidate = snapshot.model_copy(deep=True)
    payload = proposal.model_dump(exclude={"source_refs", "reason"})
    if proposal.type == "join" and not proposal.expression:
        raise ValueError("join proposal requires a complete expression")
    identity = hashlib.sha256(
        json.dumps(
            {k: v for k, v in payload.items() if k != "description"},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()[:24]
    relation = Relation(
        id=f"relation.{identity}",
        **payload,
        source_refs=list(dict.fromkeys(proposal.source_refs)),
    )
    if any(
        r.model_dump(include={"type", "source_columns", "target_columns", "expression"})
        == relation.model_dump(
            include={"type", "source_columns", "target_columns", "expression"}
        )
        for r in candidate.relations
    ):
        raise ValueError(
            "relation already exists; use its ID to update the description"
        )
    candidate.relations.append(relation)
    return MetadataSnapshot.model_validate(candidate.model_dump(mode="json"))


def apply_changes(
    snapshot: MetadataSnapshot,
    changes: list[MetadataChange],
) -> MetadataSnapshot:
    """Atomic batch on a copy; callers publish only the returned validated state."""
    candidate = snapshot.model_copy(deep=True)
    objects = candidate.objects()
    for index, change in enumerate(changes):
        try:
            obj = objects.get(change.object_id)
            if obj is None:
                raise ValueError(f"UNKNOWN_OBJECT: {change.object_id}")
            if change.field not in WRITABLE.get(type(obj), set()):
                raise ValueError(f"READ_ONLY_PROPERTY: {change.field}")
            current = obj.model_dump(mode="json", include={change.field})[change.field]
            if current != change.model_dump(mode="json")["expected_value"]:
                raise ValueError(f"STALE_VALUE: {change.object_id}.{change.field}")
            if not set(change.source_refs) <= {e.id for e in candidate.sources}:
                raise ValueError(
                    f"UNKNOWN_SOURCE: {sorted(set(change.source_refs) - {e.id for e in candidate.sources})}"
                )
            setattr(obj, change.field, change.value)
            obj.source_refs = list(
                dict.fromkeys([*obj.source_refs, *change.source_refs])
            )
        except ValueError as exc:
            raise ValueError(
                f"change[{index}] {change.object_id}.{change.field}: {exc}"
            ) from exc
    return MetadataSnapshot.model_validate(candidate.model_dump(mode="json"))
