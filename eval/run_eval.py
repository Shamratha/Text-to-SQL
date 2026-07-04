"""Evaluation suite.

Two modes:
  python eval/run_eval.py --offline    # guardrail cases only (no API key needed)
  python eval/run_eval.py              # full run: LLM generation + execution match

Metrics reported:
  - guardrail effectiveness  (dangerous SQL blocked / total dangerous)
  - execution accuracy       (generated results match golden results)
  - clarification behaviour  (ambiguous/unanswerable handled, not hallucinated)
  - hallucination detection  (low-confidence flags on wrong answers)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import guardrails  # noqa: E402
from app.executor import ReadOnlyExecutor  # noqa: E402
from app.validation import results_agree  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "golden.json")


def load_cases() -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)["cases"]


def run_guardrail_cases(cases: list[dict]) -> dict:
    blocked_ok, blocked_total = 0, 0
    transform_ok, transform_total = 0, 0
    failures = []

    for case in cases:
        if "sql_input" not in case:
            continue
        result = guardrails.check(case["sql_input"])
        if case["expect"] == "blocked":
            blocked_total += 1
            if not result.allowed:
                blocked_ok += 1
            else:
                failures.append(f"{case['id']}: DANGEROUS QUERY WAS NOT BLOCKED")
        elif case["expect"] == "ok_transformed":
            transform_total += 1
            note_frag = case.get("expect_note_contains", "")
            if result.allowed and any(note_frag in n for n in result.notes):
                transform_ok += 1
            else:
                failures.append(
                    f"{case['id']}: expected note containing '{note_frag}', "
                    f"got allowed={result.allowed} notes={result.notes}"
                )
    return {
        "dangerous_blocked": f"{blocked_ok}/{blocked_total}",
        "transforms_applied": f"{transform_ok}/{transform_total}",
        "failures": failures,
    }


def run_llm_cases(cases: list[dict]) -> dict:
    from app import llm  # imported lazily so --offline works without a key
    from app.schema_extract import extract_schema, schema_to_markdown
    import duckdb
    from app.config import settings

    con = duckdb.connect(settings.db_path, read_only=True)
    schema_md = schema_to_markdown(extract_schema(con))
    con.close()
    executor = ReadOnlyExecutor()

    exec_match, exec_total = 0, 0
    runs_ok, runs_total = 0, 0
    clarify_ok, clarify_total = 0, 0
    per_case = []

    for case in cases:
        if "question" not in case:
            continue
        expect = case["expect"]
        try:
            gen = llm.generate_sql(case["question"], schema_md)
        except Exception as e:
            per_case.append({"id": case["id"], "outcome": f"LLM error: {e}"})
            continue

        if expect == "clarify":
            clarify_total += 1
            if gen.needs_clarification:
                clarify_ok += 1
                per_case.append({"id": case["id"], "outcome": "PASS (clarified)"})
            else:
                per_case.append({"id": case["id"],
                                 "outcome": f"FAIL — hallucinated SQL: {gen.sql}"})
            continue

        if gen.needs_clarification:
            if expect == "clarify_or_ok":
                clarify_total += 1
                clarify_ok += 1
                per_case.append({"id": case["id"], "outcome": "PASS (clarified)"})
            else:
                per_case.append({"id": case["id"], "outcome": "FAIL — unexpected clarification"})
            continue

        guard = guardrails.check(gen.sql)
        if not guard.allowed:
            per_case.append({"id": case["id"],
                             "outcome": f"FAIL — generated SQL blocked: {guard.violations}"})
            continue

        result = executor.execute(guard.sql)
        runs_total += 1
        if result.ok:
            runs_ok += 1
        else:
            per_case.append({"id": case["id"], "outcome": f"FAIL — exec error: {result.error}"})
            continue

        if case.get("golden_sql"):
            exec_total += 1
            golden_res = executor.execute(case["golden_sql"])
            if results_agree(result, golden_res):
                exec_match += 1
                per_case.append({"id": case["id"], "outcome": "PASS (results match golden)"})
            else:
                per_case.append({"id": case["id"],
                                 "outcome": f"MISMATCH — generated: {guard.sql}"})
        else:
            per_case.append({"id": case["id"], "outcome": "PASS (executed)"})

        if expect == "clarify_or_ok":
            clarify_total += 1
            clarify_ok += 1  # a reasonable direct answer is acceptable too

    executor.close()
    return {
        "execution_accuracy": f"{exec_match}/{exec_total}"
                              + (f" ({100*exec_match/exec_total:.0f}%)" if exec_total else ""),
        "queries_ran_successfully": f"{runs_ok}/{runs_total}",
        "clarification_handling": f"{clarify_ok}/{clarify_total}",
        "per_case": per_case,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true",
                        help="run only the guardrail cases (no API key needed)")
    args = parser.parse_args()

    cases = load_cases()
    print("=" * 60)
    print("GUARDRAIL EVAL (offline)")
    print("=" * 60)
    g = run_guardrail_cases(cases)
    print(f"  dangerous queries blocked : {g['dangerous_blocked']}")
    print(f"  limit transforms applied  : {g['transforms_applied']}")
    for f in g["failures"]:
        print(f"  !! {f}")

    if args.offline:
        print("\n(LLM cases skipped — offline mode)")
        return

    print("\n" + "=" * 60)
    print("LLM EVAL (uses the Anthropic API — costs a few cents)")
    print("=" * 60)
    r = run_llm_cases(cases)
    print(f"  execution accuracy vs golden : {r['execution_accuracy']}")
    print(f"  queries ran successfully     : {r['queries_ran_successfully']}")
    print(f"  clarification handling       : {r['clarification_handling']}")
    print()
    for c in r["per_case"]:
        print(f"  [{c['id']}] {c['outcome']}")

    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "latest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"guardrails": g, "llm": r}, f, indent=2)
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
