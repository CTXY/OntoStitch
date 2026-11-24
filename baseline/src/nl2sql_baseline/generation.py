from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_openai import ChatOpenAI

from nl2sql_baseline.models import RetrievedExample


class SqlGenerator:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None,
        temperature: float,
        timeout: float,
        llm: Any | None = None,
    ) -> None:
        self._llm = llm or ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url,
            temperature=temperature,
            timeout=timeout,
            max_retries=2,
        )

    def generate(
        self,
        *,
        question: str,
        evidence: str,
        schema_ddl: str,
        examples: list[RetrievedExample],
        reference_time: str,
        dialect: str,
        downstream_guidance: str = "",
        reference_memory: str = "",
        previous_sql: str | None = None,
        execution_error: str | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        messages = build_messages(
            question=question,
            evidence=evidence,
            schema_ddl=schema_ddl,
            examples=examples,
            reference_time=reference_time,
            dialect=dialect,
            downstream_guidance=downstream_guidance,
            reference_memory=reference_memory,
            previous_sql=previous_sql,
            execution_error=execution_error,
        )
        response = self._llm.invoke(messages)
        return _content(response), messages

    def review(
        self, *, question: str, evidence: str, schema_ddl: str,
        draft_sql: str, resolved_guidance: list[dict], dialect: str,
    ) -> tuple[str, list[dict[str, str]]]:
        """Uniform request-only review; no Gold or evaluation-result argument."""
        prompt = (Path(__file__).parent / "prompts/sql_review.md").read_text()
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps({
                "dialect": dialect, "database_schema": schema_ddl,
                "question": question, "evidence": evidence,
                "resolved_guidance": resolved_guidance, "draft_sql": draft_sql,
            }, ensure_ascii=False)},
        ]
        response = self._llm.invoke(messages)
        return _content(response), messages


def build_messages(
    *,
    question: str,
    evidence: str = "",
    schema_ddl: str,
    examples: list[RetrievedExample],
    reference_time: str,
    dialect: str,
    downstream_guidance: str = "",
    reference_memory: str = "",
    previous_sql: str | None = None,
    execution_error: str | None = None,
) -> list[dict[str, str]]:
    system = (
        "You are an expert data analyst. Generate one executable read-only SQL query for the "
        "current task. Use only tables and columns declared in database_schema. Similar examples "
        "are references, not answers: adapt their dates, dimensions, filters, grouping, ranking, "
        "and output columns to the current task and never copy query-specific conditions that the "
        "current task does not request. Return exactly the output fields requested by the question "
        "and its explicit evidence or resolved user choices, in the requested order. Expressions "
        "used only for filtering, grouping, or ranking do not automatically belong in SELECT; "
        "do not add explanatory names, identifiers, or aggregate values merely to make a result "
        "more informative. A business object's alternative bindings are representations, not an "
        "instruction to output every bound field. When downstream_guidance contains resolved user guidance, "
        "treat every user-confirmed should_be as a mandatory SQL constraint and every must_not_be "
        "as prohibited, but implement those constraints in the requested dialect instead of "
        "mechanically copying the guidance text. Never let ontology assets, query examples, "
        "initial SQL, or your own preference override resolved user guidance. Before returning, "
        "verify that the SQL obeys every resolved user constraint, uses only valid functions and "
        "syntax for the requested dialect, and has every referenced table.column in FROM/JOIN "
        "scope. Retrieved reference memory is only optional background: use it when it helps "
        "interpret the question, but never let it override database_schema, foreign keys, "
        "available columns, the user's question, or task evidence. Return SQL only, without "
        "Markdown fences or explanation. The response must start with SELECT or WITH "
        "and contain exactly one complete SQL query."
        " Preserve every explicit task-evidence formula, including its operands, numerator, "
        "denominator, sign, scale, population, date bounds and requested MIN/MAX direction. "
        "A resolved choice changes only its stated decision: confirming how to count does not "
        "change a supplied ratio formula; confirming an age expression does not choose MIN or MAX. "
        "Do not replace explicit evidence with a conventional interpretation of the question. "
        "Preserve supplied categorical literal spelling and case; do not substitute a nearby "
        "memory's value or physical binding merely because it describes a related concept."
    )
    rendered_examples = "\n\n".join(
        f"## Example {item.rank}\nQuestion: {item.record.question}"
        + (f"\nEvidence: {item.record.evidence}" if item.record.evidence else "")
        + f"\nSQL:\n{item.record.sql}"
        for item in examples
    )
    examples_section = (
        f"# similar_question_sql_examples\n{rendered_examples}\n\n" if rendered_examples else ""
    )
    evidence_section = f"# evidence\n{evidence}\n\n" if evidence else ""
    guidance_rules = (
        "The resolved user guidance below is binding. Implement every user-confirmed `should_be` "
        "as a mandatory constraint, do not implement any `must_not_be`, and ignore any conflicting "
        "supplementary ontology asset or query example. Do not copy guidance text as a complete "
        "query. Translate each guidance expression into legal SQL for the `# dialect` above. If "
        "the dialect is sqlite, do not use non-SQLite functions such as DIVIDE(); use arithmetic "
        "operators and CAST(... AS REAL) or multiplication by 1.0 when floating-point division is "
        "needed. If you alias a table, every reference to that table's columns must use the alias; "
        "do not mix `FROM table AS t` with `table.column`. Every table.column mentioned by the "
        "guidance or by your SQL must be present in the query's FROM/JOIN scope. Check every "
        "resolved choice before emitting SQL."
    )
    reference_rules = (
        "The retrieved memory below is optional context, not a binding instruction. Use it only as "
        "a hint for interpreting metric names, dimensions, filters, and aggregation semantics. Do "
        "not copy unsupported constraints from it. Do not transform join keys, add filters, change "
        "output columns, or use columns/tables unless they are present in database_schema and "
        "required by the current task. If this memory conflicts with database_schema, foreign keys, "
        "task evidence, or the question, ignore the memory."
    )
    # Both arms and all cases share these instructions and the schema prefix.
    # Query-specific memory/examples must not interrupt that reusable prefix.
    system += (
        "\n\nWhen downstream_guidance is provided:\n" + guidance_rules
        + "\n\nWhen retrieved_reference_memory is provided:\n" + reference_rules
    )
    guidance_section = f"# downstream_guidance\n{downstream_guidance}\n\n" if downstream_guidance else ""
    reference_section = f"# retrieved_reference_memory\n{reference_memory}\n\n" if reference_memory else ""
    user = (
        f"# dialect\n{dialect}\n\n"
        f"# database_schema\n{schema_ddl}\n\n"
        f"# current_time\n{reference_time}\n\n"
        f"{examples_section}"
        f"{guidance_section}"
        f"{reference_section}"
        f"{evidence_section}"
        f"# task\n{question}"
    )
    messages = [{"role": "system", "content": system}, {"role": "human", "content": user}]
    if execution_error is not None:
        messages.extend([
            {"role": "assistant", "content": previous_sql or ""},
            {"role": "human", "content": (
                "The previous SQL could not be executed. Fix the reported error while "
                "preserving the original task, evidence, and resolved guidance. "
                "Return only one complete SQL query starting with SELECT or WITH; "
                "do not include explanation or Markdown.\n\n"
                f"Execution error:\n{execution_error}"
            )},
        ])
    return messages


def _content(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(
            item if isinstance(item, str) else str(item.get("text", "")) for item in content
        )
    text = content if isinstance(content, str) else str(content)
    # Normalize one exact presentation envelope, never extract SQL from prose or
    # rewrite the query. The database's single-read-only-statement guard still
    # validates the entire body. Raw provider responses remain in call/wire logs.
    fenced = re.fullmatch(r"\s*```(?:sql)?[ \t]*\r?\n([\s\S]*?)\r?\n```\s*", text, re.IGNORECASE)
    if fenced and '```' not in fenced.group(1):
        return fenced.group(1)
    return text
