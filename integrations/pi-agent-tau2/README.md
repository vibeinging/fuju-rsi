# pi-agent × τ²-bench × fuju-tune 调优底座

本仓库只包含适配器代码、公开的 mock 开发包和示例候选。原工作树中的 telecom/AppWorld 数据包、holdout、逐次 evidence 与运行结果没有迁入。下面涉及这些路径的历史结果是旧记录；在本仓库内复核时要重新取得有权限的数据并重跑，不能把旧统计当作当前验收结论。

用 [pi 栈](https://github.com/earendil-works/pi)（`pi-ai` 多模型层）作为被测 Agent 的底座，
在 [tau2-bench](https://github.com/sierra-research/tau2-bench) 官方 harness 上运行真实任务，
用 **fuju-tune skill** 对 Agent 的系统提示词做真实调优。判分全程使用 tau2 官方
reward（数据库状态断言 + 动作匹配），不需要 LLM 评分。

对应调研与设计：`docs/research/2026-09-15_pi-agent-tau2bench-tuning-scenario.md`。

## 架构

```
fuju-tune skill（编码 Agent 驱动）
 ├ runner    = 候选提示词 → PiAgent → 官方 orchestrator 真实重跑
 ├ evaluator = 官方 reward ∈ {0,1}
 └ casebook  = tau2 任务 → train/validation（每任务独立 group + 唯一 source ref）
                       │
 tau2-bench（固定 SHA 2174a60；用户模拟器走官方 litellm）
 └ 自定义 Agent（官方 Agent Developer Guide 接口）
    └ Python 薄桥（python/pi_agent.py）
       └─ 本地 HTTP ─▶ pi 桥服务（agent-node/src/server.mjs，pi-ai 模型层）
          ├ mock 模式：进程内确定性桩（mock-agent / mock-user / mock-judge）
          └ 真实模式：PI_AI_BASE_URL / PI_AI_API_KEY 指向任何 OpenAI 兼容端点
```

工具执行留在 tau2 官方 orchestrator（进入轨迹），因此 reward 的 action 检查保持
官方语义——pi 侧每轮只做一次模型决策（文本或 tool_calls）。

## 快速开始

```bash
./setup.sh                          # 克隆 tau2-bench（固定 SHA）+ uv venv + npm install

node agent-node/src/server.mjs &    # pi 桥服务（默认 :7891）

# 单任务真实运行（官方判分）
.vendor/tau2-bench/.venv/bin/python python/run_task.py \
  --domain mock --task-ids create_task_1 --prompt-file candidates/exact-title-v1.txt

# 全量 mock 基线
.vendor/tau2-bench/.venv/bin/python python/run_task.py --domain mock --output evidence/mock-baseline.json

# 导出 casebook 并冻结（scorer 绑定 tune_agent.py 指纹）
.vendor/tau2-bench/.venv/bin/python python/export_casebook.py \
  --domain mock --output evals/mock.json \
  --split-override '{"create_task_1_with_env_assertions": "validation", "create_task_1_nl_eval": "train"}'
.vendor/tau2-bench/.venv/bin/python ../../skills/fuju-tune/scripts/benchmark.py freeze \
  --input evals/mock.json --output benchmarks/mock-v1 --version v1 --scorer-file python/tune_agent.py

# fuju-tune 全流程：基线 → 候选比较（skill 真实命令）
PI_TAU2_CASEBOOK=benchmarks/mock-v1 .vendor/tau2-bench/.venv/bin/python \
  ../../skills/fuju-tune/scripts/run_experiment.py \
  --agent python.tune_agent:build_agent --workspace .yitrace-optimization \
  --benchmark benchmarks/mock-v1 --max-calls 40
# 第二次加 --candidate-file candidates/exact-title-v1.txt

# 测试（需要 setup.sh 已执行；缺前置自动 skip）
python3 -m unittest discover -s tests -v
```

## 已验证的结果（2026-09-15，确定性 mock 模型）

| 提示词 | mock 全量 | 训练 | 验证 |
|---|---:|---:|---:|
| 默认策略渲染 | 3/10 | 3/6 | 0/4 |
| exact-title 候选（加一句「使用用户提供的精确标题」） | 5/10 | 4/6 | 1/4 |

- 比较实验真实选出候选（`selectedTrialId=trial-1`，验证集严格改善且无回归）；
  `adoptable=false` 是正确语义——mock 环境不构成独立验收。
- 防过拟合守卫全程在工作：冻结后改 scorer 文件被拦截（要求新版本）、
  验证集同分不选候选（第一次演练如实拒绝）、每任务独立 group。
- 原工作树曾保留 `evidence/` 任务明细；本独立仓库未复制这些结果。

## mock 模型与真实模型的边界

- `mock-agent` 是**确定性桩**：它模拟「策略文档没写清 → 执行走样」的失败模式
  （不带 exact 指令时把任务标题转小写），仅用于证明管道与调优机制，**不代表真实模型收益**。
- `mock-user` 模拟 tau2 用户模拟器（注意 tau2 会翻转消息角色：客服的话以 role=user 到达）。
- 真实模型路径已接好：`PI_AI_BASE_URL`/`PI_AI_API_KEY`/`PI_AI_EXTRA_MODELS` 指向
  OpenAI 兼容服务，被测 Agent 模型与用户模拟器模型换成真实串即可；telecom 域
  （114 任务、gpt-4.1 基线 34%）按同一流程换 `--domain telecom`。

## 真实模型路径（GLM-5.3-Flash 已实测）

桥服务的 provider 机制接任何 OpenAI 兼容端点。智谱 **GLM Coding Plan** 订阅 key
走专用端点（`/api/coding/paas/v4`），不消耗按量余额：

```bash
# 桥服务注册真实模型（key 走环境变量，不要写入任何文件）
# 注意 glm-5.3-flash 是推理模型：reasoning:true + 足够 maxTokens
PI_AI_API_KEY="$ZHIPU_API_KEY" \
PI_AI_EXTRA_MODELS='[{"id":"glm-5.3-flash","name":"GLM 5.3 Flash","api":"openai-completions","provider":"yitrace","baseUrl":"https://open.bigmodel.cn/api/coding/paas/v4","reasoning":true,"input":["text"],"cost":{"input":0,"output":0,"cacheRead":0,"cacheWrite":0},"contextWindow":128000,"maxTokens":8192}]' \
node agent-node/src/server.mjs &

# 用户模拟器（官方 litellm）直连同一 coding 端点
OPENAI_API_KEY="$ZHIPU_API_KEY" OPENAI_BASE_URL=https://open.bigmodel.cn/api/coding/paas/v4 \
PI_TAU2_AGENT_MODEL=glm-5.3-flash PI_TAU2_USER_LLM=openai/glm-5.3-flash \
.vendor/tau2-bench/.venv/bin/python python/run_task.py --domain mock --output evidence/glm53flash-baseline.json
```

2026-09-15 实测（详见 `evidence/glm53flash/findings.md`）：mock 域 **8/10**（官方
reward，~28s/任务）；skill 调优演练训练集 6/6、候选同分被正确拒选；两道失败题
经诊断均为基准判分缺陷（communicate_info 子串匹配、隐式 DB 终态比对），非提示
词可修——这正是防过拟合流程「不为收益硬造收益」的真实行为。

桥服务已修复错误透传：pi-ai 把模型错误包装为 `stopReason=error` + 空 content 而
不抛异常，桥以 502 明确上抛，避免空消息掩盖真实故障。

## AppWorld 域（有余量的主战场）

AppWorld（ICML 2025，[appworld.dev](https://appworld.dev/)）：9 个日常 app、457 个 API、
FullCode agent 形态（写代码调 API）、**state 单元测试程序化判分**。dev 57 题已冻结为
开发包（`benchmarks/appworld-dev-v1`，29 train / 28 validation）；test_normal 167 /
test_challenge 416 留作独立验收材料（license 兼容：casebook 只存 task_id 引用）。

**GLM-5.3-flash 基线 ~54-60%（未饱和）**；失败模式已确诊四类（答案提交 null、
世界时间失明、空结果轻信、语义口径）。

### 关键实验结论（2026-09-17，详见 `evidence/appworld/EXPERIMENT-LOG.md`）

- 同一 baseline 提示词两次全量运行差 3 题（31 vs 34/57）——**单次采样噪声 ±3**；
- 候选 v1/v2/v3 同口径单次 A/B：v2 名义 +4（训练 5 修 1 退）、v3 -1，**全部落在
  噪声带内，验证集均同分 → skill「严格改善才选」规则每轮正确拒选**；
- **多次重跑组均（n=3 vs n=3）翻案**：v2 候选验证集 **+4.8pp**（56.0%→60.7%）、
  训练 +8.0pp、轮数 **-8%**（≈token/耗时 -8-10%）——逐次通过数 [34,35,33] vs
  [38,38,37] **分布不重叠**，双维度收益确认；
- skill 正式实验（单次判定）再次拒选（验证 -1，噪声内）——**三层口径闭环演示**
  了 yitrace 架构设计：搜索层单次快筛保守正确，验收层多次重跑才确认真实收益；
  「多次重跑协议是唯一能区分真改进和采样运气的方法」从 telecom 边缘题到
  调优 A/B 双场景实证；
### holdout 未见题收官（2026-09-17）

test_normal 抽样 56 题（`benchmarks/appworld-holdout-v1`，题目从未参与开发决策）
双臂 × 3 次验证：**64.3% → 69.6%（+5.4pp），与开发集 +4.8pp 一致——收益泛化**；
轮数 -3.1%、耗时 -3.9%。口径 workflow_only（题目未见，环境本机，无独立验收
签名）。**完整调优闭环走通：单次快筛（拒选）→ 多次重跑（确认）→ 未见题
（泛化验证）**。最终口径：GLM-5.3-flash + time-awareness-v2 提示词在 AppWorld
上 64% → 70%，同时省 3-4% 轮数/耗时。

**产品改进项**：效率维度（轮数/token/耗时）纳入候选选择标准——提案见
`docs/design/2026-09-17_efficiency-dimension-candidate-selection.md`。

### 工程要点

- `appworld_agent.py`：`--parallel N` 进程分片并行（实测 6 路稳定，端点并发无限制），
  逐题增量写盘 + 断点续跑；`--system-file` 注入候选提示词；
- `appworld_tune_agent.py`：spawn 子进程隔离（AppWorld freezegun/anyio 与 SDK
  实验线程同进程冲突，见 EXPERIMENT-LOG）；task_id 白名单校验；
- `export_appworld_casebook.py`：任务 → yitrace casebook（只存引用，license 兼容）。

## telecom 域（GLM-5.3-flash 实测汇总）

tau2 官方 split 与 yitrace 防过拟合流程天然对齐，直接复用：

| tau2 官方 split | 任务数 | yitrace 用途 | glm-5.3-flash | glm-4.5-air |
|---|---:|---|---:|---:|
| small | 20 | 快速校准（官方简单代表集） | 20/20 | — |
| train | 74 | 开发包：38 train + 36 validation | 72/74 = 97.3% | 70/74 = 94.6% |
| test | 40 | **holdout（未见题验收）** | **38/40 = 95.0%** | — |

对照论文口径（2025 模型完整 telecom pass^1）：gpt-4.1 34%、o4-mini 42%、
claude-3.7-sonnet 49%——**当前 GLM 家族在该基准双双接近饱和**。full 集难题线
（故障数 ≥6 探针 12 题、600s 宽口径）进一步确认：11/12 通过，唯一失败的 8 故障
题重跑即过——边缘任务单次 reward 有波动，**多次重跑 + 组均值才是稳定度量**
（yitrace 验收协议每题重跑 3 次的设计因此有实测支撑）。真实调优收益的舞台在
模型能力边缘的**真实业务**，不在公开基准——与 RSI 论文「能力余量越大、
改进杠杆越强」的判断一致：饱和的基准 = 剩余空间在基准之外。

冻结包：`benchmarks/telecom-train-v1`（开发）、`benchmarks/telecom-holdout-v1`
（独立验收材料）。运行参数对齐官方默认（`max_steps=200`、`max_errors=10`、
单题 300s 墙钟截断）。

```bash
# 基线 / 调优：同 mock 流程，环境变量 PI_TAU2_CASEBOOK=benchmarks/telecom-train-v1
... python/run_task.py --domain telecom --split train \
  --agent-model glm-5.3-flash --user-llm openai/glm-5.3-flash
```

## 目录

| 路径 | 作用 |
|---|---|
| `agent-node/src/server.mjs` | pi 桥服务：会话管理 + pi-ai 模型层 + 确定性 mock 端点 |
| `python/pi_agent.py` | tau2 自定义 Agent（官方半双工接口 → HTTP → pi-ai） |
| `python/run_task.py` | 单任务/全量运行 CLI，输出官方 reward JSON |
| `python/export_casebook.py` | tau2 任务 → yitrace casebook（分层 split + 显式覆盖） |
| `python/tune_agent.py` | fuju-tune 适配器（runner/evaluator/baseline） |
| `benchmarks/mock-v1/` | 冻结 Benchmark（scorer 指纹绑定 tune_agent.py） |
| `candidates/` | 基线与候选提示词文件 |
| `tests/test_pi_tau2.py` | 端到端冒烟（4 项，含完整调优演练） |
| `evidence/` | 运行证据 |
