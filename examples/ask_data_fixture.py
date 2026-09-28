"""离线问数接入演示：固定 SQLite 数据和规则候选，不调用模型。"""
from __future__ import annotations

import os
import sqlite3

from fuju_rsi import AgentSpec, Example, Prediction
from fuju_rsi.ask_data import evaluate
from fuju_rsi.benchmark import load_bundle


def run(prompt, question):
    region = "A" if "A" in question else "B"
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE orders (region TEXT, amount INTEGER)")
        db.executemany("INSERT INTO orders VALUES (?, ?)",
                       [("A", 40), ("A", 60), ("B", 30), ("B", 50)])
        if prompt == "sum-orders":
            value = db.execute("SELECT SUM(amount) FROM orders WHERE region = ?",
                               (region,)).fetchone()[0]
        else:
            value = db.execute("SELECT COUNT(*) FROM orders WHERE region = ?",
                               (region,)).fetchone()[0]
    return Prediction({"status": "succeeded", "behavior": "answer",
                       "result": {"columns": ["amount"], "rows": [[value]],
                                  "complete": True, "unit": "元"},
                       "answer": "金额为 %s 元" % value})


def propose(_prompt, _training_feedback, _index):
    return "sum-orders"


def score(expected, prediction):
    def check_delivery(reference, observed):
        value = reference["result"]["rows"][0][0]
        return observed.get("answer") == "金额为 %s 元" % value

    return evaluate(expected, prediction, delivery_judge=check_delivery)


def build_agent():
    bundle = os.environ["FUJU_RSI_BENCHMARK"]
    _manifest, rows = load_bundle(bundle)
    return AgentSpec(id="ask-data-fixture", name="离线问数演示",
                     baseline_prompt="count-orders",
                     examples=[Example(**row) for row in rows],
                     runner=run, proposer=propose, evaluator=score,
                     kind="ask-data")
