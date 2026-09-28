"""用官方 tau2-bench harness 运行任务并把 reward 写成 JSON。

运行环境：.vendor/tau2-bench/.venv 里的 Python（需要 tau2 依赖）。
被测 Agent 走 PiAgent（pi-ai 桥）；用户模拟器与判分器保持 tau2 官方
litellm 路径——mock 模式下 OPENAI_BASE_URL 指向桥服务的 /v1 端点。

用法示例：
    .venv/bin/python python/run_task.py --domain mock \
        --task-ids create_task_1 --prompt-file candidates/exact.txt --output out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pi_agent import PiAgent, load_prompt_override  # noqa: E402

from tau2.evaluator.evaluator import EvaluationType  # noqa: E402
from tau2.orchestrator.orchestrator import Orchestrator  # noqa: E402
from tau2.runner import build_environment, build_user, get_tasks, run_simulation  # noqa: E402


def configure_mock_llm(endpoint: str, user_llm: str) -> None:
    """用户模拟器走 litellm；mock 模型时把 OpenAI 兼容入口指到桥服务。"""
    if "/mock-" in user_llm:
        os.environ.setdefault("OPENAI_API_KEY", "mock")
        os.environ["OPENAI_BASE_URL"] = endpoint.rstrip("/") + "/v1"


def run_one_task(
    task,
    *,
    domain: str,
    endpoint: str,
    agent_model: str,
    user_llm: str,
    prompt_file: str | None = None,
    prompt_override: str | None = None,
    max_steps: int,
    max_errors: int,
    seed: int | None,
    task_timeout: float = 300.0,
) -> dict:
    environment = build_environment(domain)
    policy = environment.get_policy()
    if prompt_override is None:
        prompt_override = load_prompt_override(prompt_file)
    agent = PiAgent(
        environment.get_tools(),
        policy,
        endpoint=endpoint,
        model=agent_model,
        prompt_override=prompt_override,
    )
    user = build_user("user_simulator", environment, task, llm=user_llm)
    orchestrator = Orchestrator(
        domain=domain,
        agent=agent,
        user=user,
        environment=environment,
        task=task,
        max_steps=max_steps,
        max_errors=max_errors,
        seed=seed,
        timeout=task_timeout,
    )
    started = time.time()
    result = run_simulation(orchestrator, evaluation_type=EvaluationType.ALL)
    reward_info = result.reward_info
    termination = getattr(result, "termination_reason", None)
    return {
        "task_id": task.id,
        "reward": float(reward_info.reward) if reward_info is not None else 0.0,
        "termination": str(termination) if termination is not None else None,
        "steps": orchestrator.step_count,
        "toolErrors": orchestrator.num_errors,
        "messages": len(result.messages),
        "seconds": round(time.time() - started, 3),
        "promptFile": prompt_file,
    }


def _payload(args, rows):
    return {
        "domain": args.domain,
        "agentModel": args.agent_model,
        "userLlm": args.user_llm,
        "promptFile": args.prompt_file,
        "tasks": rows,
        "summary": {
            "count": len(rows),
            "rewardSum": sum(row["reward"] for row in rows),
            "passCount": sum(1 for row in rows if row["reward"] >= 1.0),
        },
    }


def _write_output(path, args, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(_payload(args, rows), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default="mock")
    parser.add_argument("--split", default=None, help="tau2 官方任务集划分（如 telecom 的 train/test/base/small）")
    parser.add_argument("--task-ids", default="", help="逗号分隔；为空时跑该域全部任务")
    parser.add_argument("--endpoint", default=os.environ.get("PI_TAU2_ENDPOINT", "http://127.0.0.1:7891"))
    parser.add_argument("--agent-model", default=os.environ.get("PI_TAU2_AGENT_MODEL", "mock-agent"))
    parser.add_argument("--user-llm", default=os.environ.get("PI_TAU2_USER_LLM", "openai/mock-user"))
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--max-errors", type=int, default=10)
    parser.add_argument("--task-timeout", type=float, default=300.0, help="单任务墙钟上限秒数；超时记 0 分并终止")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", default=None, help="结果 JSON 路径；每题完成即增量写入")
    args = parser.parse_args(argv)

    configure_mock_llm(args.endpoint, args.user_llm)
    task_ids = [item.strip() for item in args.task_ids.split(",") if item.strip()]
    tasks = get_tasks(args.domain, task_ids=task_ids or None, task_split_name=args.split)

    # 断点续跑：已有输出里的任务跳过（模型调用昂贵，崩溃后不该从头再来）
    rows = []
    done_ids = set()
    if args.output and Path(args.output).exists():
        try:
            previous = json.loads(Path(args.output).read_text(encoding="utf-8"))
            rows = list(previous.get("tasks", []))
            done_ids = {row["task_id"] for row in rows}
        except (json.JSONDecodeError, OSError):
            rows, done_ids = [], set()

    for task in tasks:
        if task.id in done_ids:
            continue
        try:
            row = run_one_task(
                task,
                domain=args.domain,
                endpoint=args.endpoint,
                agent_model=args.agent_model,
                user_llm=args.user_llm,
                prompt_file=args.prompt_file,
                max_steps=args.max_steps,
                max_errors=args.max_errors,
                seed=args.seed,
                task_timeout=args.task_timeout,
            )
        except Exception as exc:  # 单题异常不终止整轮：记 0 分与错误，继续
            row = {
                "task_id": task.id,
                "reward": 0.0,
                "termination": "RUN_ERROR",
                "steps": 0,
                "toolErrors": 0,
                "messages": 0,
                "seconds": 0.0,
                "promptFile": args.prompt_file,
                "error": f"{type(exc).__name__}: {exc}"[:300],
            }
        rows.append(row)
        if args.output:  # 每题完成即写盘
            _write_output(args.output, args, rows)

    if not args.output:
        print(json.dumps(_payload(args, rows), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
