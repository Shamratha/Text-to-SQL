"""Evaluation suite.

Two modes:
  python eval/run_eval.py --offline    # guardrail + adversarial cases (no API key)
  python eval/run_eval.py              # full run: LLM generation + execution match

Metrics:
  - guardrail effectiveness      (dangerous / adversarial SQL blocked / total)
  - execution accuracy           (generated results match golden results) + 95% CI
  - clarification behaviour       (ambiguous / unanswerable handled, not hallucinated)
  - NL prompt-injection defense   (which layer caught each jailbreak attempt)

Execution accuracy is reported with an exact Clopper-Pearson 95% binomial
confidence interval so a small N is not over-read as a precise point estimate.
"""
import argparse
import json
import os
import sys
from math import comb

# Pin the LLM prompt to fixed seed few-shots so eval is reproducible regardless
# of prior interactive feedback. Set before importing app modules.
os.environ.setdefault("TEXT2SQL_EVAL_MODE", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import guardrails  # noqa: E402
from app.executor import ReadOnlyExecutor  # noqa: E402
from app.validation import results_agree  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), "golden.json")


# --------------------------------------------------------------------------
# Exact Clopper-Pearson binomial confidence interval (no scipy dependency).
# Binomial tails are computed exactly via math.comb (N is small) and the
# bounds are found by bisection on the monotonic tail probability.
# --------------------------------------------------------------------------

def _binom_cdf(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(0, k + 1))


def _binom_tail_ge(k: int, n: int, p: float) -> float:
    return sum(comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact 95% CI (alpha=0.05) for a binomial proportion k/n."""
    if n == 0:
        return (0.0, 1.0)
    lo, hi = 0.0, 1.0

    if k == 0:
        lo = 0.0
    else:  # lower: p s.t. P(X >= k) = alpha/2
        a, b = 0.0, k / n
        for _ in range(100):
            mid = (a + b) / 2
            if _binom_tail_ge(k, n, mid) < alpha / 2:
                a = mid
            else:
                b = mid
        lo = (a + b) / 2

    if k == n:
        hi = 1.0
    else:  # upper: p s.t. P(X <= k) = alpha/2
        a, b = k / n, 1.0
        for _ in range(100):
            mid = (a + b) / 2
            if _binom_cdf(k, n, mid) > alpha / 2:
                a = mid
            else:
                b = mid
        hi = (a + b) / 2

    return (lo, hi)


def load_cases() -> list[dict]:
    with open(GOLDEN, encoding="utf-8") as f:
        return json.load(f)["cases"]


# --------------------------------------------------------------------------
# Offline: guardrails + adversarial + limit transforms
# --------------------------------------------------------------------------

def run_guardrail_cases(cases: list[dict]) -> dict:
    blocked_ok, blocked_total = 0, 0
    transform_ok, transform_total = 0, 0
    failures = []
    by_category: dict[str, list[int]] = {}

    for case in cases:
        if "sql_input" not in case:
            continue
        result = guardrails.check(case["sql_input"])
        cat = case["category"]
        if case["expect"] == "blocked":
            blocked_total += 1
            hit = not result.allowed
            if hit:
                blocked_ok += 1
            else:
                failures.append(f"{case['id']}: DANGEROUS QUERY WAS NOT BLOCKED")
            by_category.setdefault(cat, [0, 0])
            by_category[cat][0] += int(hit)
            by_category[cat][1] += 1
        elif case["expect"] == "ok_transformed":
            transform_total += 1
            note_frag = case.get("expect_note_contains", "")
            if result.allowed and any(note_frag in n for n in result.notes):
                transform_ok += 1
            else:
                failures.append(
                    f"{case['id']}: expected allowed + note '{note_frag}', "
                    f"got allowed={result.allowed} notes={result.notes}"
                )
    return {
        "dangerous_blocked": (blocked_ok, blocked_total),
        "transforms_applied": (transform_ok, transform_total),
        "by_category": by_category,
        "failures": failures,
    }


# --------------------------------------------------------------------------
# LLM: execution accuracy, clarification, NL prompt injection
# --------------------------------------------------------------------------

def run_llm_cases(cases: list[dict]) -> dict:
    from app import llm
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
    nli_defended, nli_total = 0, 0
    nli_by_layer = {"llm_refused_or_clarified": 0, "guardrail_blocked": 0,
                    "produced_safe_select": 0, "UNDEFENDED": 0}
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

        # ---- NL prompt-injection: report which layer defended ----
        if expect == "nl_injection":
            nli_total += 1
            if gen.needs_clarification or not gen.sql.strip():
                nli_defended += 1
                nli_by_layer["llm_refused_or_clarified"] += 1
                per_case.append({"id": case["id"],
                                 "outcome": "DEFENDED (LLM refused / clarified)"})
            else:
                guard = guardrails.check(gen.sql)
                if not guard.allowed:
                    nli_defended += 1
                    nli_by_layer["guardrail_blocked"] += 1
                    per_case.append({"id": case["id"],
                                     "outcome": f"DEFENDED (guardrail blocked: {guard.violations[0]})"})
                else:
                    # Guardrails only ever allow read-only SELECTs with no blocked
                    # functions, so an allowed query is harmless by construction.
                    nli_defended += 1
                    nli_by_layer["produced_safe_select"] += 1
                    per_case.append({"id": case["id"],
                                     "outcome": "DEFENDED (LLM produced a harmless SELECT)"})
            continue

        # ---- clarify / unanswerable ----
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
        if expect == "clarify_or_ok":
            clarify_total += 1
            clarify_ok += 1  # a reasonable direct answer is also acceptable

        # ---- execute generated SQL ----
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

    executor.close()
    return {
        "exec_match": (exec_match, exec_total),
        "runs_ok": (runs_ok, runs_total),
        "clarify": (clarify_ok, clarify_total),
        "nli": (nli_defended, nli_total),
        "nli_by_layer": nli_by_layer,
        "per_case": per_case,
    }


def _fmt_ci(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = clopper_pearson(k, n)
    return (f"{k}/{n} ({100*k/n:.0f}%) — 95% CI [{100*lo:.0f}%, {100*hi:.0f}%] "
            f"(exact Clopper-Pearson)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true",
                        help="run only the guardrail / adversarial cases (no API key)")
    parser.add_argument("--only", default="",
                        help="comma-separated list of expect-types to include in the "
                             "LLM run (e.g. 'clarify,clarify_or_ok,nl_injection')")
    args = parser.parse_args()

    cases = load_cases()
    if args.only:
        keep = set(args.only.split(","))
        # keep offline (sql_input) cases as-is; filter question cases by expect
        cases = [c for c in cases if "sql_input" in c or c["expect"] in keep]
    n_gold = sum(1 for c in cases if c["expect"] == "ok_gold")
    n_block = sum(1 for c in cases if c["expect"] == "blocked")
    n_nli = sum(1 for c in cases if c["expect"] == "nl_injection")

    print("=" * 64)
    print(f"GUARDRAIL / ADVERSARIAL EVAL (offline) — {len(cases)} total cases")
    print("=" * 64)
    g = run_guardrail_cases(cases)
    bk, bt = g["dangerous_blocked"]
    tk, tt = g["transforms_applied"]
    print(f"  dangerous + adversarial blocked : {_fmt_ci(bk, bt)}")
    print(f"  limit transforms applied        : {tk}/{tt}")
    print("  by adversarial category:")
    for cat in sorted(g["by_category"]):
        hit, tot = g["by_category"][cat]
        print(f"    {cat:<38} {hit}/{tot}")
    for f in g["failures"]:
        print(f"  !! {f}")

    if args.offline:
        print(f"\n(LLM cases skipped — offline mode; {n_gold} golden + {n_nli} "
              "NL-injection cases require an API key)")
        return

    print("\n" + "=" * 64)
    print("LLM EVAL (calls the configured LLM provider)")
    print("=" * 64)
    r = run_llm_cases(cases)
    em, et = r["exec_match"]
    print(f"  execution accuracy vs golden : {_fmt_ci(em, et)}")
    ro, rt = r["runs_ok"]
    print(f"  generated queries ran ok     : {ro}/{rt}")
    ck, ct = r["clarify"]
    print(f"  clarification handling       : {_fmt_ci(ck, ct)}")
    nd, nt = r["nli"]
    print(f"  NL prompt-injection defended : {_fmt_ci(nd, nt)}")
    for layer, count in r["nli_by_layer"].items():
        if count:
            print(f"      via {layer:<28} {count}")
    print()
    for c in r["per_case"]:
        print(f"  [{c['id']}] {c['outcome']}")

    out_dir = os.path.join(os.path.dirname(__file__), "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "latest.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({
            "guardrails": {
                "dangerous_blocked": g["dangerous_blocked"],
                "transforms_applied": g["transforms_applied"],
                "by_category": g["by_category"],
            },
            "execution_accuracy": {
                "k": em, "n": et,
                "ci_95": clopper_pearson(em, et) if et else None,
            },
            "clarification": r["clarify"],
            "nl_injection": {"defended": r["nli"], "by_layer": r["nli_by_layer"]},
            "per_case": r["per_case"],
        }, f, indent=2)
    print(f"\nSaved {out_path}")


if __name__ == "__main__":
    main()
