"""AppWorld dev 任务 → yitrace casebook（v2 开发包）。

license 注意：AppWorld 任务数据加密分发、不得明文再分发——casebook 只存
task_id 与数据集引用，不复制 instruction / 测试内容（yitrace 的 source
引用模式天然兼容）。

运行环境：appworld venv（读任务列表）+ 仓库内 yitrace SDK 源码（校验）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

INTEGRATION_ROOT = Path(__file__).resolve().parent.parent
SDK_SRC = INTEGRATION_ROOT.parent.parent / "src"
for path in (str(INTEGRATION_ROOT.parent), str(SDK_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

from appworld.task import Task  # noqa: E402
from fuju_rsi.benchmark import inspect_book, write_new  # noqa: E402


def load_ids(dataset: str) -> list[str]:
    ids_file = INTEGRATION_ROOT / "data" / "datasets" / f"{dataset}.txt"
    return [line.strip() for line in ids_file.read_text().splitlines() if line.strip()]


def build_case(task_id: str, dataset: str) -> dict:
    return {
        "id": task_id,
        "input": {"benchmark": "appworld", "dataset": dataset, "task_id": task_id},
        "expected": {"passed": True},
        "split": None,
        "group": task_id,
        "tags": ["appworld", dataset],
        "source": {"kind": "benchmark", "ref": f"appworld@0.1.3:{dataset}/{task_id}"},
        "expectedStatus": "verified",
        "evidence": (
            f"AppWorld 官方任务 {dataset}/{task_id} 的加密 ground-truth 单元测试"
            "（state-based tests，程序化判分）；评分 = world.evaluate().success"
        ),
    }


def build_book(dataset: str, *, sample_every: int = 1, role: str = "development",
               split_override: dict | None = None) -> dict:
    ids = sorted(load_ids(dataset))
    if sample_every > 1:
        ids = ids[::sample_every]
    cases = [build_case(task_id, dataset) for task_id in ids]
    if role == "holdout":
        for case in cases:
            case["split"] = "test"
    else:
        for index, case in enumerate(cases):
            case["split"] = "train" if index % 2 == 0 else "validation"
        for task_id, split in (split_override or {}).items():
            for case in cases:
                if case["id"] == task_id:
                    case["split"] = split
    return {
        "schemaVersion": 2,
        "id": f"appworld-{dataset}" + (f"-s{sample_every}" if sample_every > 1 else ""),
        "name": f"AppWorld {dataset} tasks" + (f" (1/{sample_every} 抽样)" if sample_every > 1 else ""),
        "role": role,
        "environment": {
            "benchmark": "appworld",
            "version": "0.1.3.post1",
            "dataset": dataset,
            "harness": "AppWorld sandbox + state-based unit tests",
            "agent": "fullcode agent via GLM coding endpoint",
            "max_interactions": 25,
        },
        "scoring": "score=1.0 当且仅当 AppWorld world.evaluate().success 为真；否则 0.0",
        "cases": cases,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="dev")
    parser.add_argument("--sample-every", type=int, default=1, help="等距抽样间隔（如 3 = 取 1/3）")
    parser.add_argument("--role", default="development", choices=["development", "holdout"])
    parser.add_argument("--split-override", default=None)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    override = json.loads(args.split_override) if args.split_override else None
    book = build_book(args.dataset, sample_every=args.sample_every, role=args.role, split_override=override)
    summary = inspect_book(book)
    write_new(Path(args.output), book)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
