"""离线 SQL 示例：实际执行 SQLite，候选由明确规则生成，不调用云模型。"""
from __future__ import annotations

import sqlite3
from contextlib import closing

from .core import AgentSpec, Evaluation, Example, Prediction


def build_demo_agent() -> AgentSpec:
    def runner(prompt, item):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE orders (buyer_id TEXT, month TEXT)")
            db.executemany("INSERT INTO orders VALUES (?, ?)", [
                ("a", "2026-01"), ("a", "2026-01"), ("b", "2026-01"),
                ("a", "2026-02"), ("b", "2026-02"), ("b", "2026-02"), ("c", "2026-02"),
                ("a", "2026-03"), ("a", "2026-03"), ("b", "2026-03"), ("b", "2026-03"), ("c", "2026-03"),
            ])
            if item["metric"] == "orders":
                sql = "SELECT COUNT(*) FROM orders WHERE month = ?"
            else:
                column = "buyer_id" if "buyer_id" in prompt else "customer_id"
                sql = "SELECT COUNT(*) FROM (SELECT " + column + " FROM orders WHERE month = ? GROUP BY " + column + " HAVING COUNT(*) >= 2)"
            try:
                value = db.execute(sql, (item["month"],)).fetchone()[0]
                output = {"value": value, "sql": sql, "parameters": [item["month"]]}
            except sqlite3.Error as err:
                output = {"error": str(err), "sql": sql, "parameters": [item["month"]]}
        return Prediction(output=output, tokens=0, cost_usd=0.0)

    def evaluator(expected, prediction):
        actual = prediction.output
        if actual.get("value") == expected and "error" not in actual:
            return Evaluation(1.0, "查询结果与固定数据的业务答案一致")
        return Evaluation(0.0, actual.get("error", "查询结果与预期不一致"))

    def proposer(prompt, feedback, trial_index):
        # 示例故意只根据训练反馈修正列名；没有读取验证或验收题答案。
        if any("customer_id" in str(case.get("reason", "")) or "customer_id" in str(case.get("output", "")) for case in feedback):
            return prompt + "\n使用 orders 表真实存在的 buyer_id 作为客户标识；统计当月订单不少于两次的客户数量。"
        return prompt

    examples = []
    for split, month, orders, repeat in [
        ("train", "2026-01", 3, 1), ("validation", "2026-02", 4, 1), ("test", "2026-03", 5, 2),
    ]:
        for metric, expected, label in [("orders", orders, "订单数量"), ("repeat_buyers", repeat, "购买至少两次的客户数量")]:
            examples.append(Example(
                id=split + "-" + metric,
                input={"question": month + " 的" + label + "是多少？", "month": month, "metric": metric},
                expected=expected, split=split,
            ))
    return AgentSpec(
        id="sql-demo", name="问数助手 · 离线示例", kind="demo",
        description="实际执行 SQLite 并核对答案；规则生成提示词候选，不调用云模型。",
        baseline_prompt="根据问题生成 SQL。统计指定月份的订单数或购买至少两次的客户数。",
        examples=examples, runner=runner, proposer=proposer, evaluator=evaluator,
    )
