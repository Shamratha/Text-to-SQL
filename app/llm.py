"""Claude integration: SQL generation, back-translation, alignment judging,
and alternative-approach generation — all with schema-validated structured output.
"""
import json
import os

import anthropic
from pydantic import BaseModel

from .config import settings

client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env / .env

_FEWSHOT_PATH = os.path.join(settings.log_dir, "fewshot.json")

# Seed few-shot examples specific to this schema (the feedback loop appends more)
_SEED_FEWSHOTS = [
    {
        "question": "How many customers signed up in 2025?",
        "sql": "SELECT COUNT(*) AS customers FROM customers "
               "WHERE signup_date BETWEEN '2025-01-01' AND '2025-12-31'",
    },
    {
        "question": "Top 5 products by total revenue",
        "sql": "SELECT p.name, SUM(oi.quantity * oi.unit_price * (1 - oi.discount)) AS revenue "
               "FROM order_items oi JOIN products p ON p.product_id = oi.product_id "
               "GROUP BY p.name ORDER BY revenue DESC LIMIT 5",
    },
    {
        "question": "What share of orders were cancelled, by channel?",
        "sql": "SELECT channel, COUNT(*) FILTER (WHERE status = 'cancelled') * 1.0 / COUNT(*) "
               "AS cancel_rate FROM orders GROUP BY channel ORDER BY cancel_rate DESC",
    },
    {
        "question": "Average payment amount for captured credit card payments",
        "sql": "SELECT AVG(amount) AS avg_amount FROM payments "
               "WHERE method = 'credit_card' AND status = 'captured'",
    },
]


class Interpretation(BaseModel):
    description: str
    example_sql: str


class GeneratedSQL(BaseModel):
    needs_clarification: bool
    # Populated when needs_clarification is true — one entry per plausible reading
    interpretations: list[Interpretation]
    sql: str                    # empty string when clarification is needed
    explanation: str
    confidence: float           # model's own estimate, 0.0 - 1.0
    tables_used: list[str]
    columns_used: list[str]


class BackTranslation(BaseModel):
    question: str               # the question this SQL answers, in plain English


class AlignmentJudgment(BaseModel):
    score: float                # 0.0 (different question) - 1.0 (same question)
    reasoning: str


def _load_fewshots() -> list[dict]:
    examples = list(_SEED_FEWSHOTS)
    if os.path.exists(_FEWSHOT_PATH):
        try:
            with open(_FEWSHOT_PATH, encoding="utf-8") as f:
                examples.extend(json.load(f)[-3:])  # newest 3 learned examples
        except (json.JSONDecodeError, OSError):
            pass
    return examples


def add_fewshot(question: str, sql: str) -> None:
    """Feedback loop: results the user marked correct become future examples."""
    existing = []
    if os.path.exists(_FEWSHOT_PATH):
        try:
            with open(_FEWSHOT_PATH, encoding="utf-8") as f:
                existing = json.load(f)
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append({"question": question, "sql": sql})
    with open(_FEWSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(existing[-50:], f, indent=2)


def _system_prompt(schema_md: str) -> str:
    shots = "\n\n".join(
        f"Q: {ex['question']}\nSQL: {ex['sql']}" for ex in _load_fewshots()
    )
    return f"""You are an expert DuckDB SQL analyst. Translate the user's natural
language question into a single read-only DuckDB SELECT query against this schema:

{schema_md}

Rules:
- SELECT queries only. Never write INSERT/UPDATE/DELETE/DDL.
- Use only tables and columns that exist in the schema above.
- Revenue for an order item = quantity * unit_price * (1 - discount).
- Prefer explicit JOINs following the foreign keys shown.
- Include a sensible LIMIT for queries that could return many rows.
- If the question is genuinely ambiguous (a term maps to multiple plausible
  interpretations, e.g. "revenue" could be gross vs net of refunds), set
  needs_clarification=true, leave sql empty, and list each interpretation with
  an example query. Only do this for real ambiguity — not for minor wording.
- If the question cannot be answered from this schema at all, set
  needs_clarification=true and explain why in the interpretations.
- Report an honest confidence (0-1) that your SQL answers the question.

Examples:
{shots}"""


def generate_sql(question: str, schema_md: str) -> GeneratedSQL:
    response = client.messages.parse(
        model=settings.model,
        max_tokens=2000,
        system=_system_prompt(schema_md),
        messages=[{"role": "user", "content": question}],
        output_format=GeneratedSQL,
    )
    out = response.parsed_output
    out.confidence = min(1.0, max(0.0, out.confidence))
    return out


def generate_alternative_sql(question: str, schema_md: str, first_sql: str) -> GeneratedSQL:
    """Independent second approach for multi-query validation."""
    response = client.messages.parse(
        model=settings.model,
        max_tokens=2000,
        system=_system_prompt(schema_md),
        messages=[{
            "role": "user",
            "content": (
                f"{question}\n\n"
                "Write a DIFFERENT query for this question than the one below — use an "
                "alternative strategy (different join order, a subquery instead of a join, "
                "FILTER instead of CASE, etc.) while producing the same logical answer with "
                "the same output columns in the same order.\n\n"
                f"Query to differ from:\n{first_sql}"
            ),
        }],
        output_format=GeneratedSQL,
    )
    out = response.parsed_output
    out.confidence = min(1.0, max(0.0, out.confidence))
    return out


def back_translate(sql: str, schema_md: str) -> str:
    """Hallucination check step 1: what question does this SQL actually answer?"""
    response = client.messages.parse(
        model=settings.model,
        max_tokens=500,
        output_config={"effort": "low"},
        system=("You translate SQL queries back into the plain-English question they "
                "answer. Be precise about filters, groupings and metrics. Schema for "
                f"context:\n\n{schema_md}"),
        messages=[{"role": "user", "content": f"What question does this SQL query answer?\n\n{sql}"}],
        output_format=BackTranslation,
    )
    return response.parsed_output.question


def judge_alignment(original_question: str, back_translated: str) -> AlignmentJudgment:
    """Hallucination check step 2: does the back-translated question match the original?"""
    response = client.messages.parse(
        model=settings.model,
        max_tokens=500,
        output_config={"effort": "low"},
        system=("You judge whether two questions ask for the same thing. Score 1.0 when "
                "they would be answered by identical data, ~0.5 when related but with "
                "different filters/metrics/groupings, 0.0 when unrelated. Wording "
                "differences don't matter; semantic differences do."),
        messages=[{
            "role": "user",
            "content": (f"Question A (what the user asked):\n{original_question}\n\n"
                        f"Question B (what the generated SQL actually answers):\n{back_translated}"),
        }],
        output_format=AlignmentJudgment,
    )
    out = response.parsed_output
    out.score = min(1.0, max(0.0, out.score))
    return out
