"""LLM integration: SQL generation, back-translation, alignment judging, and
alternative-approach generation — all with schema-validated structured output.

Two providers, selected automatically from the environment (see config.py):
  - groq      : free tier, OpenAI-compatible API (JSON mode + Pydantic validation)
  - anthropic : Claude structured outputs via messages.parse
"""
import json
import os
import re

from pydantic import BaseModel, ValidationError

from .config import settings

_SECRET_RE = re.compile(r"(gsk_|sk-)[A-Za-z0-9\-_]{10,}")


def _redact(text: str) -> str:
    """Scrub API keys / bearer tokens from any string before it's surfaced or logged."""
    scrubbed = _SECRET_RE.sub("***REDACTED***", text)
    if settings.llm_api_key:
        scrubbed = scrubbed.replace(settings.llm_api_key, "***REDACTED***")
    return scrubbed


class LLMError(Exception):
    """Normalized provider error so the API layer stays provider-agnostic."""


class LLMAuthError(LLMError):
    """Missing/invalid credentials or no credits."""


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


# --------------------------------------------------------------------------
# Provider backends
# --------------------------------------------------------------------------

_clients: dict = {}


def _anthropic_structured(system: str, user: str, model_cls, max_tokens: int,
                          low_effort: bool = False):
    import anthropic

    if "anthropic" not in _clients:
        _clients["anthropic"] = anthropic.Anthropic()
    kwargs = {}
    if low_effort:
        kwargs["output_config"] = {"effort": "low"}
    try:
        response = _clients["anthropic"].messages.parse(
            model=settings.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=model_cls,
            **kwargs,
        )
        return response.parsed_output
    except anthropic.AuthenticationError as e:
        raise LLMAuthError(f"Anthropic auth failed: {e}") from e
    except anthropic.APIStatusError as e:
        if "credit balance" in str(e).lower():
            raise LLMAuthError(f"Anthropic account has no credits: {e.message}") from e
        raise LLMError(f"Anthropic error ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Could not reach the Anthropic API — check your network") from e
    except TypeError as e:  # SDK raises bare TypeError when no credentials resolve
        if "authentication" in str(e).lower():
            raise LLMAuthError("No Anthropic API key configured") from e
        raise


def _openai_structured(system: str, user: str, model_cls, max_tokens: int):
    """OpenAI-compatible providers (Groq, Gemini-compat, OpenRouter, Ollama):
    JSON mode + schema-in-prompt, validated with Pydantic, one repair retry."""
    import openai

    if "openai" not in _clients:
        _clients["openai"] = openai.OpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            max_retries=5,   # free tiers rate-limit aggressively; SDK backs off on 429
        )
    schema = json.dumps(model_cls.model_json_schema())
    messages = [
        {"role": "system", "content":
            f"{system}\n\nRespond ONLY with a single JSON object that validates "
            f"against this JSON schema — no markdown fences, no prose:\n{schema}"},
        {"role": "user", "content": user},
    ]

    last_err = None
    for _ in range(2):  # one repair retry on validation failure
        try:
            resp = _clients["openai"].chat.completions.create(
                model=settings.model,
                max_tokens=max_tokens,
                temperature=0,
                response_format={"type": "json_object"},
                messages=messages,
            )
        except openai.AuthenticationError as e:
            raise LLMAuthError(f"{settings.llm_provider} auth failed — check the API key: {e}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"{settings.llm_provider} error ({e.status_code}): {e}") from e
        except openai.APIConnectionError as e:
            # Surface the underlying cause (DNS / SSL / timeout / blocked egress)
            # so a deploy-environment network failure is diagnosable — but redact
            # any credential from the message first (the cause can echo the header).
            cause = e.__cause__ or e
            raise LLMError(
                f"Could not reach {settings.llm_provider} at {settings.llm_base_url} "
                f"({type(cause).__name__}: {_redact(str(cause))})"
            ) from e

        text = resp.choices[0].message.content or ""
        try:
            return model_cls.model_validate_json(text)
        except ValidationError as e:
            last_err = e
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content":
                f"That JSON failed schema validation:\n{e}\nReturn a corrected JSON object only."})
    raise LLMError(f"Model returned invalid JSON twice: {last_err}")


def _parse_structured(system: str, user: str, model_cls, max_tokens: int,
                      low_effort: bool = False):
    if settings.llm_provider == "anthropic":
        return _anthropic_structured(system, user, model_cls, max_tokens, low_effort)
    return _openai_structured(system, user, model_cls, max_tokens)


# --------------------------------------------------------------------------
# Few-shot examples (seed + feedback-loop learned)
# --------------------------------------------------------------------------

_FEWSHOT_PATH = os.path.join(settings.log_dir, "fewshot.json")

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


def _load_fewshots() -> list[dict]:
    examples = list(_SEED_FEWSHOTS)
    # TEXT2SQL_EVAL_MODE pins the prompt to the fixed seed examples so eval runs
    # are reproducible regardless of prior interactive 👍 feedback.
    if os.getenv("TEXT2SQL_EVAL_MODE"):
        return examples
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


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def generate_sql(question: str, schema_md: str) -> GeneratedSQL:
    out = _parse_structured(_system_prompt(schema_md), question, GeneratedSQL, 2000)
    out.confidence = min(1.0, max(0.0, out.confidence))
    return out


def generate_alternative_sql(question: str, schema_md: str, first_sql: str) -> GeneratedSQL:
    """Independent second approach for multi-query validation."""
    user = (
        f"{question}\n\n"
        "Write a DIFFERENT query for this question than the one below — use an "
        "alternative strategy (different join order, a subquery instead of a join, "
        "FILTER instead of CASE, etc.) while producing the same logical answer with "
        "the same output columns in the same order.\n\n"
        f"Query to differ from:\n{first_sql}"
    )
    out = _parse_structured(_system_prompt(schema_md), user, GeneratedSQL, 2000)
    out.confidence = min(1.0, max(0.0, out.confidence))
    return out


def back_translate(sql: str, schema_md: str) -> str:
    """Hallucination check step 1: what question does this SQL actually answer?"""
    system = ("You translate SQL queries back into the plain-English question they "
              "answer. Be precise about filters, groupings and metrics. Schema for "
              f"context:\n\n{schema_md}")
    out = _parse_structured(system, f"What question does this SQL query answer?\n\n{sql}",
                            BackTranslation, 500, low_effort=True)
    return out.question


def judge_alignment(original_question: str, back_translated: str) -> AlignmentJudgment:
    """Hallucination check step 2: does the back-translated question match the original?"""
    system = ("You judge whether two questions ask for the same thing. Score 1.0 when "
              "they would be answered by identical data, ~0.5 when related but with "
              "different filters/metrics/groupings, 0.0 when unrelated. Wording "
              "differences don't matter; semantic differences do.")
    user = (f"Question A (what the user asked):\n{original_question}\n\n"
            f"Question B (what the generated SQL actually answers):\n{back_translated}")
    out = _parse_structured(system, user, AlignmentJudgment, 500, low_effort=True)
    out.score = min(1.0, max(0.0, out.score))
    return out
