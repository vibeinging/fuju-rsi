# Fuju RSI

**让编码 Agent 帮你改进已有的问数 Agent，并给出能复核的证据。** 在项目中唤起 Fuju RSI Skill，它会围绕真实问题完成：找出失败 → 提出限定范围的改动 → 重跑原版和候选 → 检查退步与证据 → 交付普通提示词或代码改动。

Fuju RSI 面向开发和测试阶段。业务程序继续按原来的方式运行，不需要在生产环境安装 RSI。当前自动比较的候选是**完整提示词**；配置和代码改动可以由编码 Agent 提出，但仍需走业务项目自己的测试与交付流程。

> **当前状态：** 可复制的 Skill、问数案例工具、离线重跑和独立验收协议已实现；SQLite 合成示例和测试已通过。尚未在真实问数产品中完成端到端效果验收，因此不要把示例分数当作业务收益。本仓库尚未发布到 PyPI。

## 从哪里开始

| 想做的事 | 入口 |
| --- | --- |
| 让编码 Agent 检查并改进现有问数程序 | [`skills/fuju-tune/SKILL.md`](skills/fuju-tune/SKILL.md) |
| 准备有来源、可复核的问数案例 | [问数工作流](skills/fuju-tune/references/ask-data.md) |
| 接上真实 Python、命令或 HTTP 程序 | [接入契约](skills/fuju-tune/references/integration.md) |
| 了解独立验收和交付要求 | [验收流程](skills/fuju-tune/references/verification.md) |
| 查看实现与尚未完成的验证 | [实施记录](docs/reports/2026-09-28_ask-data-rsi-implementation.md) |

### 安装开发工具和 Skill

从本仓库根目录执行（Python 3.8+）：

```bash
python -m pip install .
mkdir -p ~/.codex/skills
cp -R skills/fuju-tune ~/.codex/skills/
```

上面是 Codex 的本地 Skill 目录示例；其他编码 Agent 将 `skills/fuju-tune/` 复制到其 Skill 目录。Skill 与 `fuju_rsi` Python 包分别安装。进入你的业务项目后，向编码 Agent 调用 `fuju-tune`，说明问数入口、已知失败和允许改动的范围。它会先寻找实际运行路径和已有证据；无需先安装另一份 `ask-data-benchmark` Skill。

Python 包只在开发、测试或 CI 环境执行固定规则。被测程序可以是 Python、Node、Go 或其他语言；测试适配器通过现有函数、命令或测试接口调用它，产品运行时无需 Python RSI 依赖。

## 问数改进如何工作

1. **准备案例。** 先登记原题来源与语义家族，再分配训练、验证和独立验收用途。同源改写不能跨用途；改变时间、维度、条件或公式时重新核对标准答案。答案需绑定数据快照、业务口径和独立计算依据。
2. **跑原版。** 调用产品真实问数入口，读取每轮已保存的**完整结果**和最终答复。任务完成、SQL 可执行或页面预览都不等于答对。
3. **比较候选。** 编码 Agent 查看训练失败并提出有限候选；工具重跑原版和候选，只给提案者验证汇总，并在共用账本中记录验证题使用次数。
4. **独立验收。** 固定一个候选、评分规则和运行条件，再由独立环境用未见来源比较。开发集分数提高时仍是 `adoptable=false`，不能直接宣称可采用。
5. **交付改动。** 输出报告和普通提示词或代码差异，说明改善、退步、异常、已知费用与未知项，并按业务项目原有方式验证生效和恢复。

Fuju Trace 是可选的诊断插件；没有安装或写入失败时，实验观测退回本地 JSONL。Trace 帮助定位失败阶段，不能代替结果判分或独立验收。

## 本地试跑：不调用模型

下面的 SQLite 示例只有两个**已知合成来源**，用于检查协议链路。每次使用新的临时目录，不会覆盖之前的实验；命令在仓库根目录运行。

```bash
trial_dir="$(mktemp -d)"
fuju-rsi ask-data prepare --catalog examples/ask_data_catalog.json \
  --role development --output "$trial_dir/casebook.json"
fuju-rsi ask-data freeze --casebook "$trial_dir/casebook.json" \
  --role development --output "$trial_dir/development-v1" --version v1 \
  --scorer-file examples/ask_data_fixture.py
fuju-rsi ask-data init-validation --directory "$trial_dir/validation-history" \
  --max-exposures 2
PYTHONPATH=examples fuju-rsi optimize \
  --agent ask_data_fixture:build_agent \
  --workspace "$trial_dir/workspace" \
  --ask-data-bundle "$trial_dir/development-v1" \
  --validation-ledger "$trial_dir/validation-history" \
  --max-trials 1 --max-calls 5 --telemetry log
```

`--max-exposures 2` 只够这次原版和一个候选各看一次验证题。真实项目应在首次使用验证题前确定上限，长期复用同一个账本。`--max-calls` 只统计 runner/proposer 回调，不是模型费用上限。这个示例不会授予采用资格。

接真实问数系统时，让测试工程提供返回 `AgentSpec(kind="ask-data")` 的 `module:factory`。Skill 的 `run_experiment.py` 接受 `--benchmark`、`--validation-ledger` 和可选的 `--candidate-file`；核心 `fuju-rsi optimize/report` 使用 `--ask-data-bundle`。两种入口都会把开发题集路径交给 factory。完整步骤见[问数工作流](skills/fuju-tune/references/ask-data.md)。如果候选需要改共享业务配置，先建立隔离测试项目；不要让并发会话读到实验提示词。

## 结果与证据

每次有实验记录的 Skill 搜索会导出 `report.md`、结构化 `result.json`、原版提示词；选出候选时另有候选文本和差异。训练明细用于找错，验证汇总用于有限次选方案。只有可信独立验收满足采用条件时才会产生已验证提示词。报告要区分“真实运行完成”“开发集改善”“独立验收通过”和“已在业务产品生效”。

问数评分检查完整列、行、重复行、排序、数值容差、单位和多轮结果。澄清行为与最终答复需要业务侧的独立判分；标准答案不能来自被测 Agent 自评。验收材料应由单独账号或 CI 环境持有，本机目录和文件摘要不是权限隔离。默认验收门槛为 30 个独立来源组；合成小样本只能证明协议或已知回归。

## 可选能力与仓库结构

- `skills/fuju-tune/`：可复制的编码 Agent Skill；安装 Python 包后使用。
- `src/fuju_rsi/`：案例冻结、实验、评分、账本、独立验收、报告和 CLI；只依赖 Python 标准库。
- `examples/`：离线问数与其他接入示例；`integrations/`：现有业务及公开基准的适配材料。
- `console/`：可选的本地实验查看器，不是使用 Skill 的前提。
- `docs/`：产品设计、实施记录和研究材料。

需要把实验状态写入 Fuju Trace 时，可安装 `python -m pip install '.[trace]'`；默认 `--telemetry auto` 会使用可用的 SQLite Trace 插件，失败时退回 `telemetry/events.jsonl`。显式传 `--telemetry log` 只写 JSONL。观测事件只含状态和计数，不含题目、答案、提示词或私有验收数据。已有 VexDB 等 Trace 存储可由调用方注入 [`FujuTracePlugin`](src/fuju_rsi/plugins/fuju_trace.py)；数据库连接由调用方管理。

`fuju-rsi optimize --serve` 可以打开本地查看器；普通 Skill 流程不需要启动服务。`fuju-rsi verify`、`report` 和 `runtime` 的参数与边界见 Skill 的[验收](skills/fuju-tune/references/verification.md)和[可选后台流程](skills/fuju-tune/references/runtime.md)。

## 开发与迁移

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
cd console && npm ci && npm run build && npm test
```

构建前端后回到仓库根目录运行 `python scripts/sync_console.py`，再执行 `python -m build`。`scripts/verify_python_consumer.py` 可在干净环境检查 wheel 的 CLI、报告和静态资源。私有题集、密钥、实验工作区和验收日志不要提交到仓库。

本仓库从原 `yitrace` 优化模块拆出：旧导入 `yitrace.optimization` 改为 `fuju_rsi`，旧命令改为 `fuju-rsi` 的同名子命令。`fuju-trace` 是独立项目，只负责记录与检索执行过程。迁移旧工作区时先复制目录，在副本上验证读取、验收和恢复；切换生产调用方需要另行回归。

许可证：MIT。
