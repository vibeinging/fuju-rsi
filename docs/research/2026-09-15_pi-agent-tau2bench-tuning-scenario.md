# 调研：pi-agent × τ²-bench × yitrace-tune 真实调优场景

> 日期：2026-09-15
> 需求：给 yiTrace 配置一个真实 agent 项目（pi-agent 底座），选一个 RSI 领域好验证、有真实 Benchmark 的场景，用 yitrace-tune skill 做真实调优。
> 结论先行：**推荐「pi-agent-core 客服 Agent × τ²-bench telecom 域 × yitrace-tune 调 policy 系统提示词」**。这是工具 Agent（论文 HCI 39.9，剩余空间全场最大）+ 真实基准 + 程序化判分 + 已被独立证实提示词敏感的组合，且 pi 的 TS 栈与 `@yitrace/trace-sdk` / `@yitrace/db` 同栈，trace 闭环可以直接吃自家狗粮。

## 1. 三个组件的事实核实

### 1.1 pi-agent（底座）

- 即 Mario Zechner（badlogic）的 `pi-mono`，现已迁到 **earendil-works** 命名空间（[github.com/earendil-works/pi](https://github.com/earendil-works/pi)），是 OpenClaw 的引擎，MIT，TypeScript/Node。
- 相关包：**`pi-agent-core`**（agent runtime：tool calling + 状态管理，可编程 headless 使用——这就是"底座"）、`pi-ai`（多提供商统一 LLM API）、`pi-coding-agent`（CLI）、`pi-telemetry`（厂商中立遥测契约）。
- **没有自带 eval 套件**：维护者明确偏好真实任务而非玩具基准——所以"用 pi 自带 benchmark"这条路不存在，必须外接基准。
- 注意：无权限系统，工具必须白名单化（本方案只暴露 τ²-bench 领域工具，不给 shell/文件系统）。

### 1.2 τ²-bench（真实 Benchmark，推荐主战场）

- Sierra Research，[github.com/sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench)，论文 [arXiv:2506.07982](https://arxiv.org/abs/2506.07982)（ICML 2026），MIT，Python ≥3.12，LiteLLM 接任意模型。
- 任务量（论文 Table 1）：**retail 115 / airline 50 / telecom 114**（telecom 另有 2285 条生成集）。telecom 细分：service_issue 29、mobile_data_issue 36、mms_issue 49。
- 判分：程序化 reward（0/1）——数据库状态断言 + 动作匹配（每条 solution 动作须出现在轨迹中）+ 状态/通信断言，**不需要 LLM 评分**。
- 用户模拟器：LLM 扮演用户（`--user-llm` 可指定）；官方协议 temp 0、每任务跑 4 次报 pass^k。
- **基线低、空间大**（telecom dual-control pass^1）：gpt-4.1 **34%**、o4-mini 42%、claude-3.7-sonnet 49%、gpt-4.1-mini ~50%。
- 官方支持自定义被测 Agent：**Agent Developer Guide**（"Build and evaluate your own agent"）+ `examples/agents` + `tau2 evaluate-trajs` 复评命令——这是接 pi-agent 的正规扩展点，不用 fork。
- 质量修正版：[amazon-agi/tau2-bench-verified](https://github.com/amazon-agi/tau2-bench-verified)（修了 50+ 任务、去歧义）；注意上游 v0.1 有 grading update 改分——**固定 commit SHA** 是必须的。

### 1.3 提示词敏感性已被独立证实（关键先例）

- **Quesma 实验**（[博客](https://quesma.com/blog/tau2-benchmark-improving-results-smaller-models/)，HN 头条）：把 policy 文档重写成 step-by-step 工作流式的系统提示词，GPT-5-mini 在 τ² 上 **~45% → 67.5%（+22pp），API 调用减少 ~50%**。
- τ²-bench 一作 Victor Barres 本人对 GPT-4.1 复现出 **~+20pp**。
- 含义：这个基准上「提示词的呈现方式」是最大杠杆（比换模型还大）；Quesma 是**手工**重写——**自动化搜索 + 独立验收**（yitrace-tune 做的事）在此没有正式发表先例，是空白点，也是机会。
- 佐证：GEPA（[arXiv:2507.19457](https://arxiv.org/abs/2507.19457)，Databricks）证明提示词优化器在 agent 任务上可比 RL 便宜 35×；Databricks 企业实践里 GEPA 让 GPT-OSS-120B 超 Claude Opus 4.1。但 GEPA 未在 τ² 上发表数字。

## 2. 为什么这是 RSI 领域「好验证」的场景

1. **正中剩余空间最大的领域**：RSI 论文 HCI 2026——工具调用 Agent 39.9（全场最低）、SWE 52.6；论文明确「部署工作流薄弱的环节」是 RSI 杠杆最强、工程上最易切入的入口。τ²-bench 就是工具 Agent 基准。
2. **改进循环形态 = 论文的 L2**：目标与评估器固定（官方 reward），候选生成（policy 提示词）自主，更新持久化（casebook/工作区账本）——和 GEPA、Agent Lightning 同档，正好复用 [RSI 结合分析](../analysis/2026-09-15_rsi-paper-yitrace-integration.md) 的叙事。
3. **四问全部可答**：可衡量（官方 reward）/ 可保持（已知回归题重跑）/ 可迁移（telecom 调优的候选拿到 airline/retail 测迁移）/ 可回滚（工作区 active 指针）。
4. **来源组天然独立**：每 task 一个来源组，telecom 114 条足够 train/validation/holdout 切分后验收侧仍有 ≥30 独立组（当前验收门槛）。

## 3. 架构

```
yitrace-tune skill（编码 Agent 驱动）
 ├ runner   = 子进程跑 tau2-bench 单任务（官方 harness）
 ├ evaluator= 官方 reward ∈ {0,1}（DB 状态 + 动作匹配）
 ├ proposer = 编码 Agent 自身 或 Chat-Completions proposer
 └ casebook = telecom 114 任务 → train / validation / holdout
              group_id = task_id, source_id = tau2:telecom:<task_id>
                       │
 tau2-bench（Python ≥3.12，固定 SHA；用户模拟器固定模型、temp 0）
 └ 自定义 Agent（Agent Developer Guide 接口）
    └ Python 薄适配器 ──本地 HTTP──▶ pi-agent-core 服务（Node 子进程）
        ├ 系统提示词 = 候选文件（per-request 注入，即 --prompt-file 形态）
        ├ 工具 = tau2-bench telecom tool schema（白名单，无 shell/FS）
        └ 可选：@yitrace/trace-sdk 打 span → @yitrace/db embedded
                （调优全程进 yiTrace 控制台，可回放）
```

边界自检（对照 AGENTS.md）：
- 优化器仍只在 Python SDK / Skill；被测 agent 是外部真实程序，提示词经参数注入——与 SQLite demo 同形（`--prompt-file`），符合「黑盒不能注入就只能评测」的规则。
- TS/Python 桥走本地回环 HTTP，只在测试适配器里，不进生产依赖。
- 预算口径不变：maxCalls 计 runner/proposer 回调；真实模型费用单独预算（见 §5）。

## 4. 实施顺序

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1. 搭 agent（2–3 天） | pi-agent-core headless 服务（暴露 τ² telecom 工具白名单 + 可注入系统提示词）+ tau2-bench 自定义 Agent 适配器 | 官方 harness 跑通 1 个任务，trajectory 落盘 |
| 2. 基线校准（1 天） | gpt-4.1-mini 跑 telecom 114 任务 pass^1 | 结果与论文同模型档位（~50%）量级一致——校准证明 harness 没跑偏，这一步的数字同时是 casebook 的初始基线 |
| 3. 建 casebook（0.5 天） | 114 任务导入 draft → 每 task 独立 group → 冻结 train/val/holdout 包 | `benchmark.py inspect` 通过；验收包 ≥30 独立组 |
| 4. 真实调优（1–2 天） | skill 跑 optimize（预算 maxCalls）→ 冻结候选 → verify 独立验收 | report.md + verified-prompt.txt（或如实「无提升」） |
| 5. 报告与迁移（1 天） | 候选在 airline/retail 上测迁移；trace 控制台回放调优过程 | 四问逐项有答案；演示素材 |

该场景同时是 [产品路线](../plans/2026-09-15_product-roadmap-next-stage.md) P1 需要的「第一份可信证据」的自有版本：agenticdata AF-14 是外部业务证据，这个是自有、可控、可公开写的证据（对外可写「yiTrace 把一个 pi-agent 客服 Agent 在 τ²-bench telecom 上的表现自动调优了 X 点，经独立验收」）。

## 5. 风险与对策

1. **用户模拟器方差**：telecom 用户模拟器错误率 16%（关键 6%）。对策：固定 user-llm 与温度；验收每题 3 重跑取组均值（现有 acceptance 流程本就如此）；判分用官方 reward，不受模拟器措辞影响。
2. **跨语言桥**：Agent Developer Guide 是官方扩展点，HTTP 本地回环即可；不要 fork tau2-bench。
3. **基准版本漂移**：固定 commit SHA；如遇任务质量问题换 amazon-agi/tau2-bench-verified 并在报告注明。
4. **公开基准的「未见」边界**：任务公开，holdout 防的是调优过程过拟合，不防模型预训练见过——报告如实标注（现有规则本就这样定义：「不能保证永不过拟合」）。生成集 2285 条若用于扩充，同源变体必须同组。
5. **成本**：gpt-4.1-mini 档；粗算 baseline 114 + 搜索（~40 训练 × N 候选）+ 验收（30 × 2 arm × 3 rep）≈ 数千次对话级调用，几十美元量级。Quesma 已证明小模型可跑出显著差异。
6. **pi-agent 无权限系统**：只注册 τ² 领域工具，进程内无 shell/文件工具；如需更强隔离套 Docker。

## 6. 备选场景（为何不选）

| 备选 | 放弃原因 |
|---|---|
| BFCL V4 multi-turn（800 条，判分最确定、最便宜） | 工具调用是「单 agent 单轮/短程」，agent 工作流味弱；作降级备胎保留 |
| Terminal-Bench | pi 是终端 agent 的天然场景，但任务少（~90）、Docker 重、单次成本高，不适合多候选×多重复的调优循环 |
| SWE-bench Verified | 单次轨迹长、Docker per-instance，调优循环成本爆炸，「好验证」不成立 |
| pi 自带 eval | 不存在（维护者明确不做玩具基准） |

## 7. 实施结果（2026-09-15 当天完成第 1–4 步的 mock 版）

底座已落地在 `integrations/pi-agent-tau2/`（README 含完整命令）：

- **pi 桥服务**（`agent-node/src/server.mjs`）：pi-ai 模型层 + 确定性 mock 端点（OpenAI 兼容、SSE 流式），真实模型路径经 `PI_AI_BASE_URL` 即可切换。tau2 用户模拟器的角色翻转（客服以 role=user 到达）和 pi-ai 的强制流式两个坑都已处理。
- **tau2 自定义 Agent**（`python/pi_agent.py`）：官方 Agent Developer Guide 接口，每轮一次模型决策，工具执行留在官方 orchestrator（reward 的 action 检查因此有效）。
- **端到端已验证**：mock 域 10 任务，默认策略 3/10 → exact-title 候选 5/10（官方 reward，无退步）；yitrace-tune 全流程演练真实选出候选（验证集 0/4 → 1/4 严格改善），`adoptable=false` 语义正确。防过拟合守卫全程在工作（scorer 改动被拦截、第一次演练验证集同分如实拒选）。
- **测试**：`python3 -m unittest discover -s tests -v`（4 项，11 秒，含完整调优演练）。

**边界**：mock-agent 是确定性桩，只证明管道与调优机制，不代表真实模型收益。剩余步骤：真实模型下重跑 §4 第 2 步基线校准（gpt-4.1-mini vs 论文 ~50%），然后 telecom 域（114 任务）按同一流程换域。这也是[产品路线](../plans/2026-09-15_product-roadmap-next-stage.md) P1 自有证据的可控版本。

**真实模型实测（同日追加，GLM-5.3-Flash）**：智谱 GLM Coding Plan 订阅 key 走 `/api/coding/paas/v4` 专用端点（不消耗按量余额；glm-5.3-flash 为推理模型，需 `reasoning:true`）。mock 域全量 **8/10**（官方 reward，~28s/任务）；skill 调优演练训练集 6/6 满分，service-guidelines 候选验证集同分被正确拒选。两道失败题经轨迹诊断均为基准判分缺陷（communicate_info 子串匹配要求逐字复读；user_tools 题 ACTION/ENV_ASSERTION 全过但隐式 DB 终态比对失败），非提示词可修——与 tau2-bench-verified 修正 50+ 任务的背景一致。证据：`integrations/pi-agent-tau2/evidence/glm53flash/findings.md`。

**telecom 域完整实测（2026-09-16 追加）**：small 20/20、train 72/74（97.3%）、**holdout（官方 test split，未见题）38/40（95.0%）**——对照论文 2025 模型完整 telecom 口径（gpt-4.1 34%、claude-3.7-sonnet 49%）是模型代差。关键结论：GLM-5.3-flash 在该基准开发包上饱和（验证集全对 ⇒ 候选必被拒选），**无提示词调优空间**；演示真实调优收益需弱模型（glm-4.5-air / glm-5-turbo 已确认可用）或更难任务集——印证 RSI 论文「能力余量越大、改进杠杆越强」：调优工具的价值在弱模型与高难任务上。冻结包 `telecom-train-v1`（74 题开发）与 `telecom-holdout-v1`（40 题独立验收材料）已就绪。

**弱模型线最终结论（2026-09-16 收口）**：glm-4.5-air 探针 6/8 系抽样偏差——全量 train 74 题（300s 口径）**70/74 = 94.6%**，失败全为多重故障超时题（600s 下多数可过）；效率候选 A/B 验证不压缩步数。「弱模型有真实失败率」假设证伪，**该基准对当前 GLM 家族整体饱和**。按「证据不足不硬造收益」止损，不投入 8 小时 skill 全量实验复现拒选。真实调优收益的出路：tau2 full 2285 / telecom-workflow 更难任务集，或回到 agenticdata 真实业务（产品路线 P1 本意）。工程副产品：run_task 健壮性修复（单题容错、每题增量写盘、断点续跑、HTTP 超时 240s），经 5 小时连续真实运行验证。

**full 难题线与场景终局（2026-09-16）**：full 集 2285 唯一场景、故障数峰值 5-6、极端 9 重；难题探针（故障数 ≥6 采样 12 题、600s 宽口径、5.3-flash）**11/12**，唯一失败的 8 故障题**重跑即过**（reward 0→1）。三条线交叉确认基准饱和终局；同时得到一个方法论发现：边缘任务单次 reward 波动明显，**多次重跑 + 组均值才是稳定度量**——为 yitrace 验收协议（每题每版本重跑 3 次、独立组等权）的必要性提供了实测支撑，可直接用于对外叙事。真实调优收益的舞台在模型能力边缘的真实业务（agenticdata P1），不在公开基准。

**AppWorld 战场与最终实验结论（2026-09-16/17 追加）**：tau2 全域饱和后转入 AppWorld（ICML 2025，457 API、FullCode agent、程序化 state 单元测试）。dev 57 题冻结开发包，GLM-5.3-flash 基线 54-60%（未饱和），四类失败模式确诊（答案 null、世界时间失明、空结果轻信、语义口径）。三轮候选同口径单次 A/B（v1 无效 / v2 名义 +4 / v3 -1）：**同一基线两次运行差 3 题，候选效应全部落在 ±3 噪声带内，验证集均同分 → skill 每轮正确拒选**。核心发现：该设置下提示词真实效应小于单次采样噪声，继续单次迭代=过拟合开发集——**调优场景直接实证了多次重跑协议的必要性**（与 telecom 边缘题发现互为印证）。工程沉淀：spawn 子进程隔离（AppWorld freezegun 与 SDK 实验线程冲突）、`--parallel 6` 进程分片（端点并发实测无限制）、Mimosa 安全加固。完整记录：`integrations/pi-agent-tau2/evidence/appworld/EXPERIMENT-LOG.md`。下一步：多次重跑 A/B（每题 3 次）口径下重检候选，通过后才进 skill 正式实验与 test_normal 验收。

## 8. 来源

- [earendil-works/pi（pi-mono 新家）](https://github.com/earendil-works/pi) · [Armin Ronacher 介绍](https://lucumr.pocoo.org/2026/1/31/pi/) · [作者博客](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/)
- [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) · [论文 arXiv:2506.07982](https://arxiv.org/abs/2506.07982) · [taubench.com](https://taubench.com/) · [amazon-agi/tau2-bench-verified](https://github.com/amazon-agi/tau2-bench-verified)
- [Quesma：τ² 提示词重写 +22pp](https://quesma.com/blog/tau2-benchmark-improving-results-smaller-models/) · [一作确认 GPT-4.1 +20pp](https://www.linkedin.com/posts/victor-barres_tau%25C2%25B2-benchmark-how-a-prompt-rewrite-boosted-activity-7374548141527764992-0rS5)
- [GEPA 论文](https://arxiv.org/abs/2507.19457) · [Databricks 提示词优化实践](https://www.databricks.com/blog/building-state-art-enterprise-agents-90x-cheaper-automated-prompt-optimization) · [BFCL V4](https://gorilla.cs.berkeley.edu/leaderboard.html)
