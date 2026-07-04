# Text2SQL — Natural Language SQL Interface with Guardrails & Hallucination Detection

Ask questions in plain English against a real analytical database. The system
generates SQL with Claude, refuses to run anything destructive, verifies the
query actually answers the question asked, and attaches an evidence-based
confidence score to every result.

**Measured eval results** (golden dataset, Groq `llama-3.3-70b-versatile`):

| Metric | Result |
|---|---|
| Dangerous queries blocked | **8/8 (100%)** — zero unsafe queries executed |
| Execution accuracy vs golden SQL | **12/14 (86%)** |
| Generated queries that ran successfully | 16/16 |
| Ambiguous / unanswerable questions correctly flagged | 4/4 |
| Row-limit transforms applied | 2/2 |

Reproduce with `python eval/run_eval.py` (full report in `eval/results/latest.json`).

## Why this is hard (and interesting)

Text-to-SQL demos are easy; *production* text-to-SQL is not. Two failure modes
kill it in the real world:

1. **Destructive or runaway queries** — an LLM asked "clean up the test data"
   will happily write `DELETE`. This project has two independent defense
   layers: a SQL-AST guardrail middleware (sqlglot) *and* a read-only database
   connection, so even a guardrail bypass cannot write.
2. **Confidently wrong answers** — the SQL parses, runs, returns numbers…
   that answer a different question. This project detects that with
   back-translation ("what question does this SQL answer?" → semantic
   comparison with the original), result sanity checks, schema-coverage
   verification, and multi-query cross-validation (two independent SQL
   strategies must agree).

## Architecture

```
question ──► schema-aware prompt (auto-introspected: tables, types, FKs, sample values)
         ──► LLM (Groq Llama 3.3 70B or Claude — schema-validated structured output)
         ──► ambiguity? ──► structured clarification request (multiple interpretations)
         ──► GUARDRAILS  block DDL/DML/file-reads/multi-statement/deep nesting,
         │               inject/clamp LIMIT, log every block
         ──► SANDBOX     read-only DuckDB connection + wall-clock timeout + EXPLAIN
         ──► VALIDATION  back-translation alignment · result sanity · schema coverage
         │               · multi-query agreement (independent 2nd strategy)
         ──► confidence score (weighted blend, full breakdown shown in UI)
```

| Component | Choice |
|---|---|
| LLM | Provider-abstracted: **Groq** `llama-3.3-70b-versatile` (free tier; JSON mode + Pydantic validation with repair retry) or **Claude** `claude-sonnet-5` (native structured outputs via `messages.parse`) — auto-detected from which key is in `.env` |
| Database | DuckDB (real SQL engine w/ EXPLAIN; zero-setup, deploys anywhere) |
| SQL analysis | sqlglot AST (statement whitelist, depth checks, LIMIT rewriting) |
| API | FastAPI |
| Frontend | Self-contained HTML/JS dashboard served by FastAPI |

## Quickstart

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on mac/linux)
pip install -r requirements.txt

python data/seed.py               # build the sample e-commerce warehouse (3k orders)
copy .env.example .env            # then add a key: GROQ_API_KEY (free, console.groq.com)
                                  # or ANTHROPIC_API_KEY — provider is auto-detected

uvicorn app.main:app --port 8000
# open http://localhost:8000
```

Try: *"Top 5 products by revenue in 2025"* — then try *"DROP TABLE customers"*
to watch the guardrails catch it.

## The safety layer

Every query — LLM-generated or user-edited — passes through both layers:

1. **Guardrail middleware** (`app/guardrails.py`, all rules configurable)
   - single-statement only; SELECT/CTE/UNION whitelist (all DDL & DML blocked)
   - file/environment table functions blocked (`read_csv`, `read_parquet`, …)
   - subquery nesting depth ≤ 3
   - `LIMIT 1000` injected when missing, clamped when larger
   - every blocked query logged with the failing rule (`logs/blocked.jsonl`)
2. **Sandboxed execution** (`app/executor.py`)
   - connection opened `read_only=True` — writes are impossible at the engine level
   - wall-clock timeout via `interrupt()` (default 10 s)
   - `EXPLAIN` captured for every query for auditability

## Hallucination detection & confidence

| Signal | Weight | How |
|---|---|---|
| Back-translation alignment | 0.35 | SQL → "what question does this answer?" → LLM judge scores semantic match with the original |
| Multi-query agreement | 0.20 | independently generated second strategy; result sets compared (order-insensitive, float-tolerant) |
| Model self-confidence | 0.15 | structured output field |
| Result sanity | 0.15 | NULL-heavy columns (bad JOIN signal), empty results, dates outside the data's timespan |
| Schema coverage | 0.15 | do the tables/columns the model claims to use actually exist? |

Signals that can't run (e.g. deep validation off) redistribute their weight.
Scores below 0.6 are flagged **low confidence — verify before trusting**.

## API

| Endpoint | Purpose |
|---|---|
| `POST /v1/query` | `{question, deep_validate}` → SQL, results, confidence + breakdown, warnings |
| `POST /v1/execute` | raw/edited SQL through guardrails + sandbox (no LLM) |
| `GET /v1/schema` | introspected schema |
| `GET /v1/history` | recent queries |
| `POST /v1/feedback` | 👍 becomes a few-shot example; 👎 becomes an eval candidate (the flywheel) |

## Evaluation

```bash
python eval/run_eval.py --offline   # guardrail suite, no API key needed
python eval/run_eval.py             # + LLM: execution accuracy vs golden SQL,
                                    #   clarification handling, per-case report
python -m pytest tests -q           # unit tests (guardrails + sandbox)
```

The golden dataset (`eval/golden.json`) covers simple lookups, multi-table
JOINs, aggregations, date ranges, ambiguous phrasing, unanswerable questions,
and a battery of dangerous SQL that must be blocked.

## Project layout

```
app/
  main.py            FastAPI service + pipeline orchestration
  llm.py             Claude calls (generation, back-translation, judging)
  guardrails.py      SQL-AST safety middleware
  executor.py        read-only sandboxed execution
  validation.py      sanity checks, agreement, confidence scoring
  schema_extract.py  automatic schema introspection + relevance filter
  static/index.html  dashboard
data/seed.py         deterministic sample warehouse (5 tables)
eval/                golden dataset + eval runner
tests/               offline unit tests
```
