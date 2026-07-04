"""FastAPI service: natural language -> guarded SQL -> validated results."""
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import guardrails, llm, validation
from .config import settings
from .executor import ReadOnlyExecutor
from .schema_extract import (extract_schema, filter_relevant_tables,
                             schema_to_dict, schema_to_markdown)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("text2sql")

_QUERY_LOG = os.path.join(settings.log_dir, "queries.jsonl")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Self-heal: seed the warehouse if it's missing (e.g. a fresh serverless /
    # container instance where the build-time seed didn't persist). Deterministic,
    # so every instance gets the identical dataset.
    if not os.path.exists(settings.db_path):
        logger.info("Warehouse not found at %s — seeding now", settings.db_path)
        from data.seed import main as seed_main
        seed_main(settings.db_path)

    intro_con = duckdb.connect(settings.db_path, read_only=True)
    state["schema"] = extract_schema(intro_con)
    lo, hi = intro_con.execute("SELECT MIN(order_date), MAX(order_date) FROM orders").fetchone()
    state["date_range"] = (str(lo), str(hi))
    intro_con.close()

    state["executor"] = ReadOnlyExecutor()
    state["history"] = _load_history()
    logger.info("Schema loaded: %d tables, data range %s", len(state["schema"]), state["date_range"])
    yield
    state["executor"].close()


app = FastAPI(title="Text2SQL", version="1.0", lifespan=lifespan)


class QueryRequest(BaseModel):
    question: str
    deep_validate: bool = True   # run the multi-query agreement check (extra LLM call)


class ExecuteRequest(BaseModel):
    sql: str                     # power-user path: raw SQL through guardrails only


class FeedbackRequest(BaseModel):
    query_id: str
    correct: bool
    note: str = ""


def _load_history() -> list[dict]:
    if not os.path.exists(_QUERY_LOG):
        return []
    records = []
    with open(_QUERY_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-200:]


def _log_query(record: dict) -> None:
    state["history"].append(record)
    with open(_QUERY_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


@app.post("/v1/query")
def query(req: QueryRequest):
    if not req.question.strip():
        raise HTTPException(400, "Question is empty")

    query_id = uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    schema = state["schema"]
    relevant = filter_relevant_tables(schema, req.question)
    schema_md = schema_to_markdown(relevant)

    # ---- 1. Generate SQL (structured output) --------------------------------
    try:
        gen = llm.generate_sql(req.question, schema_md)
    except llm.LLMAuthError as e:
        raise HTTPException(502, f"{e} — set the API key in .env and restart")
    except llm.LLMError as e:
        raise HTTPException(502, str(e))

    if gen.needs_clarification:
        record = {
            "query_id": query_id, "ts": time.time(), "question": req.question,
            "status": "clarification",
            "interpretations": [i.model_dump() for i in gen.interpretations],
            "explanation": gen.explanation,
        }
        _log_query(record)
        return record

    # ---- 2. Guardrails -------------------------------------------------------
    guard = guardrails.check(gen.sql)
    if not guard.allowed:
        record = {
            "query_id": query_id, "ts": time.time(), "question": req.question,
            "status": "blocked", "sql": gen.sql, "violations": guard.violations,
        }
        _log_query(record)
        return record

    # ---- 3. Execute (read-only, timeout) ------------------------------------
    result = state["executor"].execute(guard.sql)
    if not result.ok:
        record = {
            "query_id": query_id, "ts": time.time(), "question": req.question,
            "status": "error", "sql": guard.sql, "error": result.error,
        }
        _log_query(record)
        return record

    # ---- 4. Hallucination detection ------------------------------------------
    alignment_score = None
    back_translated = None
    alignment_reasoning = None
    try:
        back_translated = llm.back_translate(guard.sql, schema_md)
        judgment = llm.judge_alignment(req.question, back_translated)
        alignment_score = judgment.score
        alignment_reasoning = judgment.reasoning
    except llm.LLMError as e:
        logger.warning("Back-translation skipped: %s", e)

    sanity = validation.sanity_check(result, state["date_range"])
    coverage, coverage_problems = validation.schema_coverage(
        gen.tables_used, gen.columns_used, schema_to_dict(schema)
    )

    # ---- 5. Multi-query agreement (optional) ----------------------------------
    agreement = None
    alt_sql = None
    if req.deep_validate:
        try:
            alt = llm.generate_alternative_sql(req.question, schema_md, guard.sql)
            if not alt.needs_clarification and alt.sql.strip():
                alt_guard = guardrails.check(alt.sql)
                if alt_guard.allowed:
                    alt_sql = alt_guard.sql
                    alt_result = state["executor"].execute(alt_guard.sql)
                    agreement = validation.results_agree(result, alt_result)
        except llm.LLMError as e:
            logger.warning("Multi-query validation skipped: %s", e)

    # ---- 6. Confidence --------------------------------------------------------
    confidence, breakdown = validation.combine_confidence(
        gen.confidence, alignment_score, sanity.score, agreement, coverage,
    )

    record = {
        "query_id": query_id,
        "ts": time.time(),
        "question": req.question,
        "status": "ok",
        "sql": guard.sql,
        "explanation": gen.explanation,
        "guardrail_notes": guard.notes,
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "elapsed_ms": result.elapsed_ms,
        "explain_plan": result.explain_plan,
        "confidence": confidence,
        "confidence_breakdown": breakdown,
        "low_confidence": confidence < settings.low_confidence_threshold,
        "back_translated_question": back_translated,
        "alignment_reasoning": alignment_reasoning,
        "sanity_warnings": sanity.warnings,
        "coverage_problems": coverage_problems,
        "alternative_sql": alt_sql,
        "agreement": agreement,
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
    _log_query(record)
    return record


@app.post("/v1/execute")
def execute_raw(req: ExecuteRequest):
    """Power-user path: run (edited) SQL through guardrails + sandbox, no LLM."""
    guard = guardrails.check(req.sql)
    if not guard.allowed:
        return {"status": "blocked", "sql": req.sql, "violations": guard.violations}
    result = state["executor"].execute(guard.sql)
    if not result.ok:
        return {"status": "error", "sql": guard.sql, "error": result.error}
    return {
        "status": "ok", "sql": guard.sql, "guardrail_notes": guard.notes,
        "columns": result.columns, "rows": result.rows,
        "row_count": result.row_count, "elapsed_ms": result.elapsed_ms,
        "explain_plan": result.explain_plan,
    }


@app.get("/v1/schema")
def get_schema():
    return {"date_range": state["date_range"], "tables": schema_to_dict(state["schema"])}


@app.get("/v1/history")
def get_history():
    return {"queries": state["history"][-50:][::-1]}


@app.post("/v1/feedback")
def feedback(req: FeedbackRequest):
    match = next((r for r in state["history"] if r.get("query_id") == req.query_id), None)
    if match is None:
        raise HTTPException(404, "query_id not found in history")

    record = {"ts": time.time(), "query_id": req.query_id, "correct": req.correct,
              "note": req.note, "question": match.get("question"), "sql": match.get("sql")}
    with open(os.path.join(settings.log_dir, "feedback.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    # The flywheel: correct answers become few-shot examples; incorrect ones
    # are saved as candidate eval cases.
    if req.correct and match.get("sql"):
        llm.add_fewshot(match["question"], match["sql"])
    elif not req.correct:
        with open(os.path.join(settings.log_dir, "eval_candidates.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"question": match.get("question"),
                                "bad_sql": match.get("sql"), "note": req.note}) + "\n")
    return {"ok": True}


_STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))
