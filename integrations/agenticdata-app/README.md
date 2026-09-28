# AgenticData 外部测试适配器

这个目录放在 Fuju RSI 一侧，用户通过 `--project-root` 指向自己的 agenticdata-app 源码。业务项目不需要添加 Fuju RSI import、AgentSpec、服务或生产依赖。

当前交付有两个入口：

- `check`：运行 AgenticData 已有的两组无模型测试，覆盖真实 SQLite/DuckDB 查询执行及合成问数的评分合同。
- `model`：在新建隔离 App 中，复用 `makeDriver` 和现有合成业务任务；给新项目设置普通 `instructions`，随后运行一个真实 Agent 场景。需要明确模型调用授权和配置库路径。**本次没有执行这个入口，不能把命令准备完成当成真实 Agent 验收。**

## 无模型费用检查

需要目标项目已安装依赖，并满足其 Node/native 架构要求。适配器使用目标仓库的 `scripts/run-with-project-node.mjs` 选择正确 Node，不安装依赖、不运行 bootstrap。

从 yiTrace 根目录运行：

```bash
node integrations/agenticdata-app/adapter.mjs check \
  --project-root /path/to/agenticdata-app \
  --output /path/to/new-no-model-result.json

node --test integrations/agenticdata-app/adapter.test.mjs
```

`check` 实际调用：

```bash
node scripts/run-with-project-node.mjs node \
  --import ./eval/tests/setup.mjs --test --test-concurrency=1 --test-reporter=tap \
  eval/tests/query-execution-evidence.test.mjs \
  eval/tests/ask-data-synthetic-multiturn.test.mjs
```

适配器先创建自己的临时目录，固定 `AHA_DATA_ROOT`、`DB_SQLITE_PATH`、运行目录和 Skill 目录；清除可能复用旧数据、窗口或安装包的环境覆盖。结果包含测试数量、子进程退出状态以及声明源文件的前后 SHA-256。完整测试日志保存在结果路径加 `.log`，原报告不能覆盖。临时目录在子进程退出后清理。

这两组测试混合包含真实数据库执行和构造的评分/证据反例。通过说明这些代码路径和判分规则通过测试，不说明真实模型完成了业务。源文件摘要只覆盖报告列出的文件，不是对整个项目“从未写入”的证明。

## 已有业务案例及防止过拟合的边界

适配目标 `eval/tasks/87-ask-data-synthetic-multiturn.task.mjs` 当前包含三个来源场景、七个对话回合：

| 来源场景 | 业务检查 | 数据与答案 |
| --- | --- | --- |
| `ask-data-synthetic-revenue-multiturn` | 先确认口径；只计 paid 订单、只减 approved 退款；更改展示时复用既有结果 | 本地生成两个 SQLite 库，答案由规则独立计算 |
| `ask-data-synthetic-precision-empty` | BIGINT、18 位小数精确显示；空结果保留表头，不编造数据 | 本地生成 DuckDB 精确账本 |
| `ask-data-synthetic-coverage-multiturn` | 部分清单明确范围；1205 条全量统计不能被 1000 行预览上限替代 | 本地生成 SQLite 运营清单 |

**这三个场景是已知合成回归题。** 源码已包含问题和答案，不能作为调优者从未见过的最终验收集。七个回合、重复运行、同源改写以及几千行表数据，都不会增加独立来源组数。它们不满足 yiTrace 默认 30 个独立来源组的采用条件。

本适配器的报告始终使用 `evidenceStatus: not_verified`、`adoptable: false`。即使真实模型场景通过，也只表示这个固定提示词通过了这批已知回归。新的独立业务数据、业务负责人确认的答案、来源分组以及 yiTrace 的独立验收登记应另行准备。

## 准备好的真实模型入口

确认要调用模型后，再明确提供配置库。入口不会自动查找或读取日常模型配置：

```bash
node integrations/agenticdata-app/adapter.mjs model \
  --project-root /path/to/agenticdata-app \
  --scenario ask-data-synthetic-revenue-multiturn \
  --prompt-file /path/to/candidate-project-instructions.txt \
  --model-source-db /path/to/explicitly-authorized-model-config.db \
  --allow-model-calls \
  --cdp-port 9433 \
  --output /path/to/new-model-result.json
```

该命令会读取明确指定配置库中的已启用模型，再通过目标项目现有逻辑复制到新建的隔离测试库，可能产生模型费用。不要直接运行内部的 `model-run.mjs`；外层命令负责正确 Node、临时目录和环境配置。

每次命令只执行一个提示词、一个已知场景，不自动搜索候选，不在失败后自动重跑。原版和候选需分别运行，在各自新建项目、会话中比较；跨进程反复调用仍可能反复使用已知题，因此这个入口不会出具独立验收回执。

模型费用没有硬金额上限。AgenticData 的一次业务回合可能调用多次模型；任务等待超时不是金额预算。当前 `driver.buildAgentRequestBody` 没有转发 `settings.maxQueryTurns`，不能把任务里的该参数描述成实际生效的调用或费用上限。模型使用量需要从实际运行证据另行核对。

## 实际接入与写入范围

真实 Agent 路径为：

```text
createSyntheticFixtures
  → makeDriver.importDatabase（正式导入接口）
  → 新项目挂载 Ask Data
  → 新项目 instructions = 普通提示词文本
  → makeDriver.continueAgent
  → CDP → Renderer electronAPI → IPC → 应用服务进程
  → 实际查询、submit_data_answer、accepted 最终回答
  → 原有 inspectSyntheticStage 独立检查
```

`driver.raw.api` 虽然使用 `/api/...` 路径，当前实际传输是 CDP/IPC，**不是普通 HTTP 客户端**。

`driver-wrapper.mjs` 不调用原来的按名字复用项目方法。它检查完整已有项目列表，直接创建唯一新项目，只有确认返回的新 ID 不属于旧项目后，才允许 `PUT /api/projects/:id {instructions}`。每次模型回合前再次读取并确认固定指令；其他项目和全局设置写入被拒绝。该检查预防接入错误，不是同进程恶意代码的权限沙箱。

目标项目正式运行时由 `loadProjectPromptContext` 读取 `projects.instructions`，所以不需要改动业务源码。移除整个适配器目录不改变线上运行；所需优化结果仍是普通项目指令文本。模型命令只修改自己启动的隔离 App 数据；数据准备、关闭和清理仍依赖目标项目已有 Eval 实现。此真实窗口路径尚需后续实际运行验收。

## 当前验证记录

- 适配器自身的协议与防误操作测试：8 项，使用测试 driver，不能当成真实 UI/Agent 证据。
- 原工作树的 2026-09-08 检查记录显示目标模块 44 项通过。`results/` 与原始日志未迁入本独立仓库；在当前业务代码上使用前须重新运行并核对结果。
- 没有读取 KDD 数据、客户记录、日常凭据，没有运行真实模型或修改业务源码。

报告中的具体绝对路径是该次运行记录；适配器代码不含开发机项目路径。
