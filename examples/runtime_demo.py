"""Recur Runtime 无模型协议演示，候选由规则产生，不证明 L2 策略自主或业务提升。

将本文件与 runtime_product.py 复制到同一个测试项目。运行此文件准备开发 Benchmark。
"""
from pathlib import Path

from fuju_rsi import AgentSpec, Evaluation, Example, Prediction
from fuju_rsi.benchmark import freeze_book
from runtime_product import calculate


def run(prompt, value):
    return Prediction(calculate(prompt, value["values"]))


def evaluate(expected, prediction):
    return Evaluation(float(expected == prediction.output), "Compare complete fixture sum")


def propose(prompt, feedback, index):
    return "sum_all" if any(item["score"] < 1 for item in feedback) else prompt


def build_agent():
    return AgentSpec(id="runtime-sum", name="Runtime protocol fixture", baseline_prompt="preview",
                     examples=[Example("train", {"values": [2, 3]}, 5, "train"),
                               Example("validation", {"values": [11, 13]}, 24, "validation")],
                     runner=run, evaluator=evaluate, proposer=propose)


def prepare(output="development-v1"):
    book = {"schemaVersion": 2, "role": "development", "id": "runtime-sum", "name": "Synthetic runtime protocol",
            "scoring": "Sum all numbers; fixture arithmetic, not model evaluation",
            "environment": {"model": "none", "fixture": "runtime-sum-v1"}, "cases": []}
    for example in build_agent().examples:
        book["cases"].append({"id": example.id, "input": example.input, "expected": example.expected,
                              "split": example.split, "group": example.id, "tags": ["arithmetic"],
                              "source": {"kind": "synthetic", "ref": "fixture-" + example.id},
                              "expectedStatus": "verified", "evidence": "Complete sum from fixture values"})
    return freeze_book(book, output=output, version="v1", scorer_file=Path(__file__).name)


if __name__ == "__main__":
    prepare()
