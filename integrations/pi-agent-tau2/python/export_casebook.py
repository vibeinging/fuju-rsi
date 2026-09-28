"""把 tau2-bench 官方任务导出为 yitrace casebook（v2 开发包草案）。

每个任务一个 case、一个独立 group 和一个唯一 source ref：
- group = task_id（同源变体不跨 split 的规则天然满足，任务粒度即来源粒度）
- source.ref = tau2-bench@<sha>:<domain>/<task_id>（绑定基准版本）
- expected/evidence 取自官方 evaluation_criteria（不猜测答案）
- split 由排序轮转分配（train/validation），确定性可复现

运行环境：tau2 venv Python（读任务）+ 仓库内 yitrace SDK 源码（校验）。
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

from tau2.runner import get_tasks  # noqa: E402
from fuju_rsi.benchmark import inspect_book, write_new  # noqa: E402

DEFAULT_SHA = "2174a603f6d014ef94473ffa95957f6ce27100db"


def task_category(task) -> str:
    # telecom 任务 ID 形如 [mobile_data_issue]scenario[PERSONA:None]，前缀即类别
    prefix = __import__("re").match(r"\[([a-z_]+)\]", task.id)
    if prefix:
        return prefix.group(1)
    name = task.id
    for marker in ("create", "update", "impossible"):
        if marker in name:
            return marker
    return "other"


def build_case(task, domain: str, sha: str) -> dict:
    criteria = task.evaluation_criteria
    actions = [action.model_dump() for action in (criteria.actions or [])]
    return {
        "id": task.id,
        "input": {
            "domain": domain,
            "task_id": task.id,
            "ticket": task.ticket,
        },
        "expected": {
            "reward": 1.0,
            "actions": actions,
        },
        "split": None,  # 由调用方分配
        "group": task.id,
        "tags": [domain, task_category(task)],
        "source": {"kind": "benchmark", "ref": f"tau2-bench@{sha}:{domain}/{task.id}"},
        "expectedStatus": "verified",
        "evidence": (
            f"tau2-bench 官方任务 {domain}/{task.id} 的 evaluation_criteria"
            f"（actions={len(actions)}，pinned commit {sha[:12]}）；"
            "评分 = 官方 run_simulation(EvaluationType.ALL) 的 reward >= 1.0"
        ),
    }


def build_book(
    domain: str,
    sha: str,
    *,
    tau2_split: str | None = None,
    role: str = "development",
    split_override: dict | None = None,
) -> dict:
    tasks = sorted(get_tasks(domain, task_split_name=tau2_split), key=lambda t: t.id)
    cases = [build_case(task, domain, sha) for task in tasks]
    if role == "holdout":
        # 独立验收包：tau2 官方 test split，yitrace 侧全部标 test split
        for case in cases:
            case["split"] = "test"
    else:
        # 按类别分层轮转分配 train/validation：确定、可复现，且两侧都能看到
        # 同类任务。--split-override 允许作者显式调整个别归属（公开、实验前冻结）。
        by_category: dict[str, list[dict]] = {}
        for case in cases:
            by_category.setdefault(case["tags"][1], []).append(case)
        for members in by_category.values():
            for index, case in enumerate(members):
                case["split"] = "train" if index % 2 == 0 else "validation"
        for task_id, split in (split_override or {}).items():
            for case in cases:
                if case["id"] == task_id:
                    case["split"] = split
    return {
        "schemaVersion": 2,
        "id": f"tau2-{domain}" + (f"-{tau2_split}" if tau2_split else ""),
        "name": f"tau2-bench {domain} tasks" + (f" ({tau2_split})" if tau2_split else ""),
        "role": role,
        "environment": {
            "benchmark": "tau2-bench",
            "commit": sha,
            "domain": domain,
            "harness": "tau2 run_simulation(EvaluationType.ALL)",
            "agent": "pi-agent-core/pi-ai bridge (mock-agent)",
        },
        "scoring": "score=1.0 当且仅当 tau2 官方 reward >= 1.0；否则 0.0",
        "cases": cases,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="mock")
    parser.add_argument("--sha", default=DEFAULT_SHA)
    parser.add_argument("--tau2-split", default=None, help="tau2 官方划分（telecom: train/test/base/small）")
    parser.add_argument("--role", default="development", choices=["development", "holdout"])
    parser.add_argument("--split-override", default=None, help='JSON 字符串，如 \'{"create_task_1_with_env_assertions": "validation"}\'')
    parser.add_argument("--output", required=True, help="casebook JSON 输出路径（不允许覆盖已有文件）")
    args = parser.parse_args(argv)

    override = json.loads(args.split_override) if args.split_override else None
    book = build_book(args.domain, args.sha, tau2_split=args.tau2_split, role=args.role, split_override=override)
    summary = inspect_book(book)
    write_new(Path(args.output), book)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
