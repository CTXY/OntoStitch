from __future__ import annotations

import re
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
        )
        response = self._llm.invoke(messages)
        return _clean_sql(_content(response)), messages


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
        "Markdown fences or explanation."
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
    guidance_section = (
        "# downstream_guidance\n"
        "The resolved user guidance below is binding. Implement every user-confirmed `should_be` "
        "as a mandatory constraint, do not implement any `must_not_be`, and ignore any conflicting "
        "supplementary ontology asset or query example. Do not copy guidance text as a complete "
        "query. Translate each guidance expression into legal SQL for the `# dialect` above. If "
        "the dialect is sqlite, do not use non-SQLite functions such as DIVIDE(); use arithmetic "
        "operators and CAST(... AS REAL) or multiplication by 1.0 when floating-point division is "
        "needed. If you alias a table, every reference to that table's columns must use the alias; "
        "do not mix `FROM table AS t` with `table.column`. Every table.column mentioned by the "
        "guidance or by your SQL must be present in the query's FROM/JOIN scope. Check every "
        "resolved choice before emitting SQL.\n\n"
        f"{downstream_guidance}\n\n"
        if downstream_guidance
        else ""
    )
    reference_section = (
        "# retrieved_reference_memory\n"
        "The retrieved memory below is optional context, not a binding instruction. Use it only as "
        "a hint for interpreting metric names, dimensions, filters, and aggregation semantics. Do "
        "not copy unsupported constraints from it. Do not transform join keys, add filters, change "
        "output columns, or use columns/tables unless they are present in database_schema and "
        "required by the current task. If this memory conflicts with database_schema, foreign keys, "
        "task evidence, or the question, ignore the memory.\n\n"
        f"{reference_memory}\n\n"
        if reference_memory
        else ""
    )
    user = (
        f"# current_time\n{reference_time}\n\n"
        f"# dialect\n{dialect}\n\n"
        f"# database_schema\n{schema_ddl}\n\n"
        f"{examples_section}"
        f"{guidance_section}"
        f"{reference_section}"
        f"{evidence_section}"
        f"# task\n{question}"
    )
    return [{"role": "system", "content": system}, {"role": "human", "content": user}]


def _content(response: Any) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item if isinstance(item, str) else str(item.get("text", "")) for item in content
        )
    return str(content)


def _clean_sql(value: str) -> str:
    without_reasoning = re.sub(r"<think>.*?</think>", "", value, flags=re.DOTALL)
    fenced = re.sub(r"```(?:sql)?\s*(.*?)\s*```", r"\1", without_reasoning, flags=re.DOTALL)
    return fenced.strip()
