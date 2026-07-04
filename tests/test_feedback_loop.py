"""Demonstrates the feedback flywheel end-to-end (point: the loop is tested,
not just asserted in prose).

👍 correct  -> add_fewshot() appends to fewshot.json -> _load_fewshots() serves it
              back into the next prompt (unless eval mode pins seed examples).
👎 incorrect (via the API layer) -> recorded as an eval candidate.
"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture()
def llm_module(tmp_path, monkeypatch):
    """Fresh llm module with the few-shot store redirected to a temp file."""
    monkeypatch.delenv("TEXT2SQL_EVAL_MODE", raising=False)
    import app.llm as llm
    importlib.reload(llm)
    monkeypatch.setattr(llm, "_FEWSHOT_PATH", str(tmp_path / "fewshot.json"))
    return llm


def test_thumbs_up_becomes_fewshot(llm_module):
    llm = llm_module
    q = "How many customers are from Australia?"
    sql = "SELECT COUNT(*) FROM customers WHERE country = 'Australia'"

    # Not present before feedback
    assert all(ex["question"] != q for ex in llm._load_fewshots())

    # 👍 records it
    llm.add_fewshot(q, sql)

    # It is now served back into future prompts, and is embedded in the system prompt
    loaded = llm._load_fewshots()
    assert any(ex["question"] == q and ex["sql"] == sql for ex in loaded)
    assert q in llm._system_prompt("SCHEMA HERE")


def test_learned_fewshots_persist_and_cap(llm_module):
    llm = llm_module
    for i in range(60):
        llm.add_fewshot(f"question {i}", f"SELECT {i}")
    with open(llm._FEWSHOT_PATH, encoding="utf-8") as f:
        stored = json.load(f)
    assert len(stored) == 50           # capped at newest 50 on disk
    assert stored[-1]["question"] == "question 59"


def test_eval_mode_pins_seed_examples(llm_module, monkeypatch):
    """In eval mode the prompt ignores learned examples so runs are reproducible."""
    llm = llm_module
    llm.add_fewshot("a learned question", "SELECT 1")
    assert any(ex["question"] == "a learned question" for ex in llm._load_fewshots())

    monkeypatch.setenv("TEXT2SQL_EVAL_MODE", "1")
    pinned = llm._load_fewshots()
    assert all(ex["question"] != "a learned question" for ex in pinned)
    assert pinned == llm._SEED_FEWSHOTS
