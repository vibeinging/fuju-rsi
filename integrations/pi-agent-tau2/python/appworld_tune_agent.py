"""fuju-tune 适配器：被测对象 = AppWorld 上的 FullCode Agent（GLM）。

runner 以 spawn 子进程方式真实运行 AppWorld 沙箱并取官方 state 单元测试
判分：AppWorld 会冻结进程内全局时间（freezegun）并起 anyio 后台线程，
与 SDK 实验工作线程同进程冲突；spawn 子进程保证每次重跑环境干净。
任务 ID 经白名单校验；evaluator 使用 world.evaluate().success。
本模块在 appworld venv 下运行。

环境变量：
- ZHIPU_API_KEY / ZHIPU_BASE_URL / ZHIPU_MODEL   模型端点（同 appworld_agent）
- APPWORLD_CASEBOOK  冻结 Benchmark 目录（默认 <integration>/benchmarks/appworld-dev-v1）
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import queue
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INTEGRATION_ROOT = HERE.parent
if str(INTEGRATION_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(INTEGRATION_ROOT.parent))

from fuju_rsi.benchmark import load_bundle  # noqa: E402
from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction  # noqa: E402

sys.path.insert(0, str(HERE))
from appworld_agent import SYSTEM_TEMPLATE, resolve_endpoint  # noqa: E402

ENDPOINT = resolve_endpoint(os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4"))
MODEL = os.environ.get("ZHIPU_MODEL", "glm-5.3-flash")
CASEBOOK = Path(os.environ.get("APPWORLD_CASEBOOK", INTEGRATION_ROOT / "benchmarks" / "appworld-dev-v1"))

# AppWorld 官方任务 ID 形如 82e2fac_1；白名单校验防 casebook 篡改
_TASK_ID_PATTERN = re.compile(r"^[0-9a-f]{6,8}_[0-9]+$")
_ISOLATION_TIMEOUT_SECONDS = 1200


def _isolated_child(task_id: str, system_override: str | None, model: str, endpoint: str,
                    api_key: str, result_queue) -> None:  # pragma: no cover - 子进程入口
    import traceback

    try:
        from appworld_agent import run_task

        row = run_task(task_id, model=model, endpoint=endpoint, api_key=api_key,
                       max_interactions=25, system_override=system_override)
        result_queue.put({"ok": row})
    except BaseException:
        result_queue.put({"error": traceback.format_exc()[-500:]})


def runner(prompt: str, example_input) -> Prediction:
    """spawn 子进程真实重跑：候选系统提示词 → AppWorld 沙箱 → 官方单元测试判分。"""
    if not isinstance(example_input, dict) or example_input.get("benchmark") != "appworld":
        raise ValueError("example.input 必须来自 AppWorld casebook")
    task_id = str(example_input["task_id"])
    if not _TASK_ID_PATTERN.match(task_id):
        raise ValueError(f"非法 AppWorld 任务 ID: {task_id!r}")
    api_key = os.environ.get("ZHIPU_API_KEY")
    if not api_key:
        raise RuntimeError("缺少 ZHIPU_API_KEY")
    started = time.time()
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    process = context.Process(
        target=_isolated_child,
        args=(task_id, prompt, MODEL, ENDPOINT, api_key, result_queue),
        daemon=True,
    )
    process.start()
    process.join(_ISOLATION_TIMEOUT_SECONDS)
    try:
        message = result_queue.get_nowait()
    except queue.Empty:
        process.terminate()
        process.join(10)
        raise RuntimeError(f"AppWorld 子进程超时（{_ISOLATION_TIMEOUT_SECONDS}s）")
    if process.is_alive():
        process.terminate()
        process.join(10)
    if "error" in message:
        raise RuntimeError("AppWorld 子进程失败: " + str(message["error"]))
    row = message["ok"]
    return Prediction(
        output={
            "passed": row["passed"],
            "num_passes": row["num_passes"],
            "num_tests": row["num_tests"],
            "seconds": round(time.time() - started, 1),
        },
        tokens=None,
        cost_usd=None,
    )


def evaluator(expected, prediction) -> Evaluation:
    passed = bool(prediction.output.get("passed")) if isinstance(prediction.output, dict) else False
    return Evaluation(score=1.0 if passed else 0.0, reason=f"appworld success={passed}")


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
        id="appworld-glm-fullcode",
        name="AppWorld FullCode Agent (GLM)",
        baseline_prompt=SYSTEM_TEMPLATE.rstrip(),
        examples=examples,
        runner=runner,
        proposer=proposer,
        evaluator=evaluator,
        description=(
            "被测 Agent = AppWorld dev 集上的 FullCode Agent（GLM coding 端点）。"
            "runner 经 spawn 子进程真实运行 AppWorld 沙箱，evaluator 使用官方 state 单元测试。"
        ),
        kind="custom",
    )
