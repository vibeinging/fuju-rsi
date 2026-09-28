# 接入契约

factory 在测试工程工作目录可导入，例如 `tune_agent.py` 的 `build_agent`。它配置 runner、evaluator 与开发数据，不能在导入时执行有费用的实验。业务程序不 import 适配器；yiTrace 作为独立测试依赖。Node/TypeScript 业务可以由 Python 测试适配器调用，其生产进程无需安装 Python 或 yiTrace。

## Python 适配器

下面是需要绑定项目真实入口的骨架。示例约定业务入口为 `app.py`，替换为项目已有函数；不要用直接返回预期答案的假 runner 代替执行。`reset_test_state` 属于测试设施，可以由适配器创建新进程、数据库或会话实现，不要求产品增加公开接口。

```python
from pathlib import Path
from fuju_rsi import AgentSpec, Evaluation, Example, Prediction
from fuju_rsi.benchmark import load_bundle
from app import run_agent, reset_test_state

ROOT = Path(__file__).resolve().parent


def runner(prompt, input_value):
    output = run_agent(input_value, system_prompt=prompt)
    return Prediction(output=output)


def evaluate(expected, prediction):
    return Evaluation(float(prediction.output == expected), "与固定业务答案比对")


def reset():
    reset_test_state()


def file_candidates_only(prompt, training_feedback, trial_index):
    raise RuntimeError("Use the skill helper with --candidate-file")


def build_agent():
    # role 是用途声明：开发入口拿不到验收包，验收包默认拒绝读取。
    manifest, cases = load_bundle(ROOT / "benchmarks/development-v1", role="development")
    return AgentSpec(
        id="my-agent", name="我的 Agent",
        baseline_prompt=(ROOT / "system-prompt.txt").read_text(encoding="utf-8"),
        examples=[Example(**item) for item in cases],
        runner=runner, evaluator=evaluate, proposer=file_candidates_only,
    )
```

runner 和 evaluator 用于搜索及验收，并在固定候选时登记源文件。`system_prompt` 必须真正贯穿最终调用，不能被应用默认值覆盖。reset 在验收每次原版或候选调用前执行，避免上一条请求的会话或数据库状态影响结果。只有每次调用都天然创建独立状态时，reset 才可明确使用空操作。

精确相等评分只适合答案能确定比较的业务。问数比较真实结果，不比较 SQL 字符串；开放回答按固定业务规则评分，模型裁判需要可信样本校准。业务失败正常返回 Prediction 后判分，网络、认证和超时失败抛异常。`tokens`、`cost_usd`、`trace_id` 可选，未知时不填写，不编造 0。

## 数据与快照

v2 开发样本包含 `id/input/expected/split/group_id/source_id/exposure/critical`。`split` 为 train 或 validation，两类均非空；`exposure` 为 development。相关来源和输入不能跨集合。独立 test 只交给 `VerificationSpec`，且必须有真实来源、分组及 unseen 标记，详见 [业务评测](business-evals.md) 和 [独立验收](verification.md)。

旧四字段 Example 和 v1 Benchmark 仍能执行开发搜索，旧 test 不运行，也不会自动成为未见题。v1 helper 会补充已记录的分组信息，但不伪造未知来源。需要固定候选时，应先迁移到 v2 开发包，再用一致的 factory 重新搜索；不能在搜索后悄悄更改 Example 元数据。

固定候选会记录明确声明的代码和评分文件摘要。运行或评分函数定义文件必须在 `--source-file` 中；其他影响结果的依赖也应显式列出。工具不自动找出全部 import、外部模型版本、数据快照或环境变化，environment 是声明，需由测试设施实际保持一致。

## 命令或 HTTP 入口

优先适配业务已有协议，不要求线上增加调优接口。命令方式在独立业务进程运行，使用 `subprocess.run(..., timeout=..., capture_output=True, check=True)`，将输出解析成 Prediction，不把未经处理的子进程日志透传给提案者。异步项目由同步 runner 按项目事件循环规则调用真实 async 入口。

HTTP 适配只把本次提示词与输入送到用户指定的测试服务。响应业务结果映射到 Prediction；错误状态抛异常。使用已配置的认证方式和明确超时，不输出密钥。接口不接受候选提示词时，可以在测试副本中通过原有配置启动候选实例；完全不可配置的黑盒只能先评测。接通 trace ingest 不等于接通真实重跑。

helper 重定向 Python stdout/stderr，不是任意子进程或 native 输出的隔离器。业务 runner 和 evaluator 各自负责安全的日志与超时。

## 版本生效与可移除性

默认交付普通配置或代码差异，生产程序不读取实验目录。验证时使用原有启动方式，并停用测试依赖和服务。移除工具后，改进与原有回退仍应正常工作。

工作区 `active-prompts.json` 只作为可选的实验基线记录；`read_active_prompt` 适用于显式选择的测试集成，不作为默认生产配置源。产品当前基线来自它自己的配置。如果旧 active 指针与产品不符，使用新实验工作区，但同一项目继续复用原有验收 registry，不能清空保留题使用历史。

搜索工作区不允许 helper 与服务同时持锁。固定文件模式 factory 没有自动提案能力，查看已有实验不受影响，新搜索继续使用 helper。skill 可整体复制，不承诺所有宿主已完成安装与兼容性验收。
