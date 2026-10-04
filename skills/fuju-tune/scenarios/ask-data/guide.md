# 问数场景

当目标是改进已有问数 Agent 时使用本流程。先找到产品实际接收问题、执行 SQL、保存完整结果和生成最终答复的路径；适配器调用这条路径。任务完成、SQL 可执行、页面预览或模型自评都不能代替实际业务结果。

发现失败后先读[调整对象与口径边界](targets.md)，判断应修改指标、词典、项目规则、提示词还是代码。提示词支持自动搜索；词典、项目规则和指标上下文可按[文件候选比较](../../references/file-candidates.md)做开发比较，配置独立验收尚未开放。代码候选仍按业务项目原有测试路径处理。

## 准备有来源的案例

先登记每道原题的 `sourceId` 和 `familyId`，按原始来源分配 train、validation 或独立验收用途，然后再创建改写题。同源改写继承用途。改变时间、维度、过滤、公式、排序或输出列时要重新计算答案。业务规则、数据快照、完整预期结果和复核方法由业务资料或独立计算提供；无法确认的题保留草案，不进入冻结包。

开发目录是 JSON，包含 `schemaVersion: 1`、`role: "development"`、`id/name`、`environment.dataSnapshot`、`environment.contextRevision`、`sources[]` 和 `cases[]`。每个来源声明 `id/kind/ref/familyId/split`；每道 ready 题声明 `id/sourceId/question` 或 `turns`、非空 `tags`、`status: "ready"`、`expected` 与 `oracle`。答数题的 `expected` 要有 `behavior: "answer"` 与 `result.columns/rows/ordered`，必要时标单位、数值容差和交付要求；澄清或缺数据题要写 `rubric/basisRefs`。`oracle` 要有 `verified/method/evidence/dataSnapshot/contextRevision`。`prepare` 会检查结构和版本，业务方仍须复核计算依据；工具不能证明业务口径正确。

```bash
fuju-rsi ask-data prepare --catalog evals/ask-data-catalog.json \
  --role development --output evals/ask-data-casebook.json
fuju-rsi ask-data freeze --casebook evals/ask-data-casebook.json \
  --role development --output evals/ask-data-dev-v1 \
  --version v1 --scorer-file tests/ask_data_adapter.py
```

验收来源和答案由独立验收环境持有。开发 Skill 不读取私有验收包；即使只看过验收汇总，下一轮用于选优的验收来源也不再是未见题。

## 接入真实 Python 程序

在测试工程中提供 `module:factory`，返回 `AgentSpec(kind="ask-data")`；从已冻结开发包读取原样 `Example` 列表。Skill 命令会把已校验的 `--benchmark` 绝对路径放入 `FUJU_RSI_BENCHMARK` 环境变量，供 factory 使用。直接使用 `fuju-rsi optimize/report` 时传 `--ask-data-bundle`，命令同样会设置该变量。`runner(prompt, input)` 要把候选提示词送到产品实际调用，建立隔离会话或重置状态，并读取每轮**已持久化的完整结果**与最终答复。返回 `Prediction`，其 `output` 遵守问数评分协议：每轮有状态、行为；答数结果有 `columns/rows/complete`。多轮输入按同一会话执行。

`evaluator` 用 `fuju_rsi.ask_data.evaluate` 核对完整结果。需要判断澄清行为或最终答复时，接入独立的 `behavior_judge`、`delivery_judge`；不能用被测 Agent 自己给的标签判自己正确。找不到真实结果读取路径时，先报告接入缺口，不能用模拟结果宣布改善。普通接入结构见[接入契约](../../references/integration.md)。

如果候选提示词需要写入项目级或业务级共享配置，先使用独立测试项目或明确的会话级覆盖入口；一次只运行一个候选。记录配置原值与版本，写入后读回确认，运行结束恢复并再次读回。进程崩溃时 `finally` 不保证执行，恢复失败必须停止后续实验并由人核对配置。不能在其他用户会话可能同时读取该配置的环境里直接比较候选。

页面表格和 SSE 中的结果可能只是预览。通过当前会话的结果句柄分页读取时，逐页核对句柄、总行数、是否截断、是否还有下一页；句柄过期、总行数不精确或只拿到部分页时，不得把 `complete` 设为 `true`。

## 原版与候选重跑

首次查看验证题前，在该项目长期使用的目录中设定固定上限。上限要覆盖预定实验中的原版和每个候选验证次数；上限耗尽后使用新的独立来源，不能另建账本目录来当作未用过。已有目录直接复用，不重新初始化。

```bash
fuju-rsi ask-data init-validation --directory evals/validation-history \
  --max-exposures 4
python <skill目录>/scripts/run_experiment.py \
  --scenario ask-data \
  --agent tests.ask_data_adapter:build_agent \
  --workspace .fuju-rsi \
  --benchmark evals/ask-data-dev-v1 \
  --validation-ledger evals/validation-history \
  --max-calls 100
```

没有候选文件时只做原版重跑。提示词模式比较已有候选时追加一个或多个 `--candidate-file`，每个文件是完整提示词；文件模式使用 `--candidate-kind files` 和版本化候选 JSON，详见文件候选指南。单次实验先预留 `1 + 候选数` 次验证访问，失败、取消或重复候选不退还。运行前确认 `max-calls` 可覆盖计划的 runner 调用；它只统计 runner/proposer 回调，不是模型费用上限。

helper 的标准输出只给训练明细、验证汇总和报告路径；不要读工作区里的验证明细来写下一版。`askDataValidation` 说明本次预留的验证次数。`searchComplete=true` 只说明开发比较完成，`adoptable=false` 仍表示没有独立验收。报告要分别说明真实运行、协议检查、已知回归和独立验收的证据范围。固定候选与可信验收见[独立验收](../../references/verification.md)。
