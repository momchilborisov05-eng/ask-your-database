"""
Turns a question in plain language into a SQL query, runs it safely and explains the result.

1. Claude gets the database schema (cached with prompt caching) and the question, and returns
   a SQLite query, a short explanation and a chart suggestion, using Structured Outputs.
2. The query runs through safe_sql (read-only, SELECT only, row and time limits).
3. If SQLite reports an error, the error goes back to Claude, which fixes the query (max 2 retries).
4. Claude writes a one- or two-sentence answer from the first rows of the result.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from safe_sql import QueryResult, UnsafeQuery, describe_schema, run_query

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
MAX_FIXES = 2
ROWS_FOR_ANSWER = 30

SQL_SCHEMA = {
    "type": "object",
    "properties": {
        "can_answer": {"type": "boolean"},
        "sql": {"type": "string"},
        "explanation": {"type": "string"},
        "chart": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["none", "bar", "line", "pie"]},
                "x": {"type": "string"},
                "y": {"type": "string"},
            },
            "required": ["type", "x", "y"],
            "additionalProperties": False,
        },
    },
    "required": ["can_answer", "sql", "explanation", "chart"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


class AskError(RuntimeError):
    """A problem to show the user, such as a question the data can't answer."""


@dataclass
class Answer:
    question: str
    sql: str
    explanation: str
    answer: str
    chart: dict
    columns: list[str]
    rows: list[list]
    truncated: bool
    attempts: int
    elapsed_ms: int
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class DatabaseAssistant:
    def __init__(self, client, db_path: Path):
        self.client = client
        self.db_path = Path(db_path)
        self.schema = describe_schema(self.db_path)
        self.system = [{
            "type": "text",
            "text": (
                "You are a senior data analyst. You write SQLite queries that answer business questions "
                "about the database below.\n\n"
                "Rules:\n"
                "- Write exactly one SQLite SELECT query (CTEs with WITH are fine). Never modify data.\n"
                "- Use only the tables and columns in the schema. Use the listed values exactly.\n"
                "- Revenue means SUM(order_items.quantity * order_items.unit_price_eur) for orders whose "
                "status is not 'cancelled' or 'returned', unless the question says otherwise.\n"
                "- Dates are ISO text: use strftime() and date() for grouping and filtering.\n"
                "- Give columns clear aliases, round money to 2 decimals, sort results meaningfully, and add "
                "LIMIT 50 unless the question needs every row.\n"
                "- For top-N questions, also return the rows tied with the last place (e.g. use RANK() and "
                "filter on the rank) so ties aren't cut off at random.\n"
                "- If the question can't be answered from this data, set can_answer to false and explain why "
                "in explanation; leave sql empty.\n"
                "- explanation: one or two plain-language sentences describing what the query does.\n"
                "- chart: suggest 'line' for values over time, 'bar' for comparing categories, 'pie' for shares "
                "of a whole (max 8 slices), otherwise 'none'. x and y must be column aliases from your query.\n\n"
                f"Database schema:\n\n{self.schema}"
            ),
            "cache_control": {"type": "ephemeral"},  # the schema is the same for every question
        }]
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

    def _ask(self, messages: list, schema: dict, max_tokens: int = 4000) -> dict:
        extra = {}
        if MODEL in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=self.system,
            messages=messages,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
        u = response.usage
        cached = u.cache_read_input_tokens or 0
        self.usage["input_tokens"] += u.input_tokens + cached + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.usage["cached_tokens"] += cached
        self.usage["output_tokens"] += u.output_tokens
        if response.stop_reason == "refusal":
            raise AskError("The AI declined to answer this question.")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text), response.content

    def ask(self, question: str) -> Answer:
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}
        messages = [{
            "role": "user",
            "content": f"Today is {date.today().isoformat()}.\n\nQuestion: {question}",
        }]

        for attempt in range(1, MAX_FIXES + 2):
            plan, content = self._ask(messages, SQL_SCHEMA)
            if not plan["can_answer"] or not plan["sql"].strip():
                raise AskError(plan["explanation"] or "This question can't be answered from the data.")
            try:
                result: QueryResult = run_query(self.db_path, plan["sql"])
                break
            except UnsafeQuery as e:
                if attempt > MAX_FIXES:
                    raise AskError(f"Could not build a working query: {e}") from e
                # Give the error back to the model so it can correct its own query.
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"That query failed with this error:\n{e}\n\nFix the query."},
                ]

        answer = self._summarize(question, plan["sql"], result)
        chart = plan["chart"]
        if chart["type"] != "none" and not {chart["x"], chart["y"]} <= set(result.columns):
            chart = {"type": "none", "x": "", "y": ""}  # the suggested columns aren't in the result
        return Answer(
            question=question, sql=plan["sql"], explanation=plan["explanation"], answer=answer,
            chart=chart, columns=result.columns, rows=result.rows, truncated=result.truncated,
            attempts=attempt, elapsed_ms=result.elapsed_ms, usage=dict(self.usage),
        )

    def _summarize(self, question: str, sql: str, result: QueryResult) -> str:
        if not result.rows:
            return "The query returned no rows."
        preview = [dict(zip(result.columns, row)) for row in result.rows[:ROWS_FOR_ANSWER]]
        data, _ = self._ask(
            [{"role": "user", "content": (
                f"Question: {question}\n\nSQL used:\n{sql}\n\n"
                f"First {len(preview)} of {len(result.rows)}{'+' if result.truncated else ''} result rows:\n"
                f"{json.dumps(preview, default=str)}\n\n"
                "Answer the question in one or two sentences, using the numbers from the result. "
                "If several rows are tied at the cut-off of a top list, say so. Don't describe the SQL."
            )}],
            ANSWER_SCHEMA,
            max_tokens=1000,
        )
        return data["answer"]
