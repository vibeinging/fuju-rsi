"""fuju-tune 适配器：被测对象 = tau2-bench 上的 pi 栈 Agent。

runner 把候选提示词注入 PiAgent（覆盖默认策略渲染），用官方 tau2 harness
真实重跑该任务；evaluator 使用官方 reward（>=1.0 记 1 分）。
本模块在 tau2 venv 下运行（需要 tau2 与 yitrace 两个包）。

环境变量：
- PI_TAU2_ENDPOINT   桥服务地址（默认 http://127.0.0.1:7891）
- PI_TAU2_AGENT_MODEL 被测 Agent 模型（默认 mock-agent）
- PI_TAU2_USER_LLM   用户模拟器 litellm 串（默认 openai/mock-user）
- PI_TAU2_CASEBOOK   冻结 Benchmark 目录（默认 <integration>/benchmarks/mock-v1）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION_ROOT = HERE.parent
if str(INTEGRATION_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT.parent))

from fuju_rsi.benchmark import load_bundle  # noqa: E402
from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction  # noqa: E402

sys.path.insert(0, str(HERE))
from pi_agent import default_prompt  # noqa: E402
from run_task import configure_mock_llm, run_one_task  # noqa: E402
from tau2.domains.mock.environment import get_environment as mock_env  # noqa: E402
from tau2.runner import get_tasks  # noqa: E402

ENDPOINT = os.environ.get("PI_TAU2_ENDPOINT", "http://127.0.0.1:7891")
AGENT_MODEL = os.environ.get("PI_TAU2_AGENT_MODEL", "mock-agent")
USER_LLM = os.environ.get("PI_TAU2_USER_LLM", "openai/mock-user")
TASK_TIMEOUT = float(os.environ.get("PI_TAU2_TASK_TIMEOUT", "600"))
CASEBOOK = Path(os.environ.get("PI_TAU2_CASEBOOK", INTEGRATION_ROOT / "benchmarks" / "mock-v1"))

_TASK_CACHE: dict = {}


def _task(task_id: str, domain: str):
    key = (domain, task_id)
    if key not in _TASK_CACHE:
        found = get_tasks(domain, task_ids=[task_id])
        if not found:
            raise ValueError(f"tau2 task not found: {domain}/{task_id}")
        _TASK_CACHE[key] = found[0]
    return _TASK_CACHE[key]


def _baseline_prompt() -> str:
    environment = mock_env()
    return default_prompt(environment.get_policy())


def runner(prompt: str, example_input) -> Prediction:
    """真实重跑：候选提示词 → pi 桥 Agent → 官方 orchestrator → 官方 reward。"""
    configure_mock_llm(ENDPOINT, USER_LLM)
    if not isinstance(example_input, dict) or "task_id" not in example_input:
        raise ValueError("example.input 必须包含 task_id/domain（来自 tau2 casebook）")
    task = _task(example_input["task_id"], example_input.get("domain", "mock"))
    row = run_one_task(
        task,
        domain=example_input.get("domain", "mock"),
        endpoint=ENDPOINT,
        agent_model=AGENT_MODEL,
        user_llm=USER_LLM,
        prompt_override=prompt,
        max_steps=200,
        max_errors=10,
        seed=42,
        task_timeout=TASK_TIMEOUT,
    )
    return Prediction(
        output={"reward": row["reward"], "termination": row["termination"], "task_id": row["task_id"]},
        tokens=None,
        cost_usd=None,
    )


def evaluator(expected, prediction) -> Evaluation:
    """官方 reward 即判分：>=1.0 通过，0 分如实保留。"""
    reward = float(prediction.output.get("reward", 0.0)) if isinstance(prediction.output, dict) else 0.0
    passed = reward >= 1.0
    return Evaluation(score=1.0 if passed else 0.0, reason=f"tau2 reward={reward}")


def proposer(prompt: str, feedback, iteration: int) -> str:
    """默认原样返回；真实候选经 run_experiment --candidate-file 注入并替换本回调。"""
    return prompt


def build_agent() -> AgentSpec:
    _manifest, rows = load_bundle(CASEBOOK)
    examples = [
        Example(
            id=row["id"],
            input=row["input"],
            expected=row["expected"],
            split=row["split"],
            group_id=row.get("group_id"),
            source_id=row.get("source_id"),
            exposure=row.get("exposure", "development"),
            critical=bool(row.get("critical", False)),
        )
        for row in rows
    ]
    return AgentSpec(
        id="pi-agent-tau2-mock",
        name="pi-agent × tau2-bench mock",
        baseline_prompt=_baseline_prompt(),
        examples=examples,
        runner=runner,
        proposer=proposer,
        evaluator=evaluator,
        description=(
            "被测 Agent = tau2 mock 域上的 pi 栈桥 Agent（mock-agent 确定性模型）。"
            "runner 经官方 tau2 orchestrator 真实重跑，evaluator 使用官方 reward。"
        ),
        kind="custom",
    )
