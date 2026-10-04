"""读取普通词典和规则的 SQLite 演示；合成案例不构成真实产品收益。

普通业务函数只依赖标准库。开发用 RSI 导入留在适配器函数里，移除工具后
仍能直接调用 run_business 或用 --config-root/--request-json 启动此脚本。
候选只修正三个金额别名，保留计数和金额指标的 SQL、单位与含义。
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3


PROJECT = Path(__file__).resolve().parent / "file_config_project"
PRIVATE_INPUT = "PRIVATE_INPUT_FILE_CONFIG_7f9641"
PRIVATE_EXPECTED = "PRIVATE_EXPECTED_FILE_CONFIG_42a1dc"


def _load_files(root):
    """先读真实文件再记录摘要，不把预期摘要当成已加载回执。"""
    values, loaded = {}, {}
    for name in ("dictionary.json", "rules.json"):
        raw = (Path(root) / name).read_bytes()
        loaded[name] = hashlib.sha256(raw).hexdigest()
        values[name] = json.loads(raw.decode("utf-8"))
    return values, loaded


def run_business(root, request, *, with_loaded_hashes=False):
    """原有业务入口：读取配置，在新建内存库执行参数化 SQL。"""
    values, loaded = _load_files(root)
    question = request if isinstance(request, str) else request["question"]
    region = "A" if "A" in question else "B"
    aliases = values["dictionary.json"]["aliases"]
    metric = next((value for alias, value in aliases.items() if alias in question), None)
    if metric is None:
        raise ValueError("Question has no configured metric alias")
    rule = values["rules.json"]["metrics"][metric]
    with closing(sqlite3.connect(":memory:")) as database:
        database.execute("CREATE TABLE orders (region TEXT, amount INTEGER)")
        database.executemany("INSERT INTO orders VALUES (?, ?)",
                             [("A", 40), ("A", 60), ("B", 30), ("B", 50)])
        amount = database.execute(rule["sql"], (region,)).fetchone()[0]
    output = {"status": "succeeded", "behavior": "answer",
              "result": {"columns": ["amount"], "rows": [[amount]],
                         "complete": True, "unit": rule["unit"]},
              "answer": "金额为 %s %s" % (amount, rule["unit"])}
    return (output, loaded) if with_loaded_hashes else output


def run(context, request):
    from fuju_rsi import Prediction
    from fuju_rsi.file_runs import FileRunResult

    if os.environ.get("FUJU_FILE_FIXTURE_NOISY") == "1":
        print("private business debug:", request)
    output, loaded = run_business(context.root, request, with_loaded_hashes=True)
    audit = os.environ.get("FUJU_FILE_FIXTURE_RUN_LOG")
    if audit:
        # 合成夹具测试只记录临时目录和真实读取摘要，用于核对清理；不记录案例。
        with Path(audit).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"root": str(context.root), "loadedHashes": loaded}) + "\n")
    return FileRunResult(Prediction(output), loaded_hashes=loaded)


def score(expected, prediction):
    """答案是手工相加的独立值，评分不借用候选词典或候选 SQL。"""
    from fuju_rsi import Evaluation

    if os.environ.get("FUJU_FILE_FIXTURE_NOISY") == "1":
        print("private evaluation debug:", expected)
    result = prediction.output["result"]
    reference = expected["result"]
    correct = (prediction.output.get("status") == "succeeded"
               and result.get("complete") is True
               and result["columns"] == reference["columns"]
               and result["rows"] == reference["rows"]
               and result["unit"] == reference["unit"]
               and prediction.output.get("answer") ==
               "金额为 %s %s" % (reference["rows"][0][0], reference["unit"]))
    return Evaluation(float(correct), "Checked fixed SQLite amount, unit and final answer")


def _baseline():
    from fuju_rsi.file_candidates import FileBaseline, FileTarget

    return FileBaseline.capture(PROJECT, [
        FileTarget("dictionary.json", "dictionary", json_pointers=("/aliases",)),
        FileTarget("rules.json", "project-rules", json_pointers=("/displayHints",)),
    ])


def build_candidate(output=None):
    """生成普通 JSON 候选包，不修改原目录；业务公式和单位保持不变。"""
    from fuju_rsi.file_candidates import FileCandidate

    baseline = _baseline()
    dictionary = json.loads(baseline.files["dictionary.json"])
    dictionary["aliases"] = {alias: "revenue" for alias in dictionary["aliases"]}
    candidate = FileCandidate.propose(
        baseline, {"dictionary.json": json.dumps(dictionary, ensure_ascii=False, indent=2) + "\n"},
        rationale="金额别名应指向既有 revenue 指标；不修改指标公式、单位或评分答案。",
    )
    if output is not None:
        destination = Path(output)
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(candidate.to_json())
    return candidate


def build_agent():
    from fuju_rsi import Example
    from fuju_rsi.file_runs import FileExperimentSpec

    benchmark = os.environ.get("FUJU_RSI_BENCHMARK")
    if benchmark:
        from fuju_rsi.benchmark import load_bundle
        _manifest, rows = load_bundle(benchmark)
        examples = [Example(**row) for row in rows]
        kind = "ask-data"
    else:
        examples = [
            Example("store-a", {"question": "门店 A 的销售额是多少？", "privateNote": PRIVATE_INPUT},
                    {"result": {"columns": ["amount"], "rows": [[100]], "unit": "元"},
                     "privateReference": PRIVATE_EXPECTED}, "train",
                    group_id="fixture-store-a", source_id="synthetic-store-a", exposure="development"),
            Example("store-b", {"question": "门店 B 的营业额是多少？", "privateNote": PRIVATE_INPUT},
                    {"result": {"columns": ["amount"], "rows": [[80]], "unit": "元"},
                     "privateReference": PRIVATE_EXPECTED}, "validation",
                    group_id="fixture-store-b", source_id="synthetic-store-b", exposure="development"),
        ]
        kind = "custom"
    return FileExperimentSpec("file-config-fixture", "配置别名修正协议演示", _baseline(),
                              examples, run, score, kind=kind)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-output")
    parser.add_argument("--config-root", default=str(PROJECT))
    parser.add_argument("--request-json")
    args = parser.parse_args()
    if args.candidate_output:
        candidate = build_candidate(args.candidate_output)
        print(json.dumps({"candidateDigest": candidate.digest, "output": str(args.candidate_output)}))
    else:
        if args.request_json is None:
            parser.error("--request-json is required for the business entry")
        print(json.dumps(run_business(args.config_root, json.loads(args.request_json)), ensure_ascii=False))


if __name__ == "__main__":
    main()
