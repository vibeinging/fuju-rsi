# ask-data-benchmark Skill 审查

日期：2026-09-26

## 结论

现有 Skill 的定位和评测方法合理，适合作为问数评测的资料整理、交互出题、答案准备和诊断入口。建议保留，不需要把它改成 RSI 的搜索执行器。

如果要把它产出的题集直接交给 fuju-rsi 自动调优和独立验收，还需要补齐三个交接约束：独立验收题的冻结与使用规则、标准答案的复核条件、题集与评分器的转换。它目前是一套人工和 Agent 遵循的工作规范，不是已经实现的自动评测流水线。

此次只审查文件和当前 RSI 接口，没有修改 Skill，没有调用业务模型、执行真实问数或连接数据库。Skill 元数据校验通过，5 个本地 Markdown 链接均存在；这些检查不证明题集或产品准确率。

## 检查范围

- `/Users/Four/.codex/skills/ask-data-benchmark/SKILL.md`
- 该 Skill 的 interaction、generation-and-scoring、agenticdata、trace-diagnosis 四份参考文件、上下文模板和调用配置。
- `/Users/Four/PersonalProjects/fuju-rsi/src/fuju_rsi/benchmark.py` 中的校验、冻结和加载接口。

## 已经做对的地方

| 设计 | 当前依据 | 评价 |
| --- | --- | --- |
| 分阶段补问，只隔离受缺口影响的案例 | SKILL.md 的启动、交互与口径整理部分；interaction.md | 能继续做独立工作，同时避免暗自补业务规则。 |
| 语义变化重新计算答案 | generation-and-scoring.md:5-21 | 同义改写与指标、维度、时间、粒度变化分开，避免拿原题答案给新问题判分。 |
| 独立准备标准答案 | SKILL.md:60；generation-and-scoring.md:48-54 | 不用被测产品的输出作标准答案，不把逐题答案注入产品。 |
| 按完整结果判分 | SKILL.md:64；generation-and-scoring.md:49-53 | 覆盖重复行、并列、单位、容差和空集，能避免“接口成功就算答对”。 |
| 区分准备、运行和通过 | generation-and-scoring.md:40-44、71-87 | ready 只表示判分材料准备好；超时、评测器错误、未运行分别记录，分母明确。 |
| Trace 用于定位，结果用于判分 | SKILL.md:66、78；trace-diagnosis.md | 关联实际请求和 Trace，缺 Trace 只影响归因证据，不凭最后一句话猜原因。 |
| 保留来源、知识版本和失效历史 | interaction.md 的知识状态与版本更新流程 | 有助于区分产品改进和标准答案变化。 |
| 限定执行和环境范围 | SKILL.md:62；agents/openai.yaml | 保留手动调用、现有入口和授权题量，不把评测变成部署操作。 |

第一批 30 题和 6–10 个代表性示例适合作为起步方式。Skill 已要求报告语义家族数和覆盖范围，因此不能把 30 道同义变体解释成 30 种独立能力，也不能以小样本直接宣称泛化提升。

## 需要补齐的三处

### 1. 将“保留验证集”明确为开发验证与独立验收

当前 SKILL.md:68 要求按原题/语义家族划分修复集与保留验证集。这能减少同义题泄漏，但没有定义哪些题可用于选择候选、哪些题只用于最终验收，也没有规定查看验收失败明细后的复用方式。

风险：开发者反复查看保留题失败原因并据此修复，后续仍把相同题报告为未见过的独立验收。虽然没有直接把答案塞进 Prompt，验收反馈已经参与了方案选择。

建议补充：

- 开始调优前按原题来源和语义家族分配 train、validation、holdout；同源变体不能跨集合。
- train 可用于修复；validation 可用于选择方案；holdout 只由验收流程读取。
- 开发题与验收题分别冻结成 RSI schemaVersion 2 的 development 和 holdout 包。
- 验收失败后可以诊断，但被用于下一轮改进的案例转为开发数据；下一轮独立验收需要新的未见题，保留使用历史。
- RSI 的 `load_bundle(..., role="holdout")` 是用途守卫，不是文件权限或对同一个 Agent 的保密隔离。需要真正保密时由单独验收流程掌握验收文件。

现成接缝：benchmark.py:89-114 校验家族与来源不跨 split；254-275 在解析题目之前检查包用途。建议复用，而非仅靠文字提醒。

### 2. 明确标准答案标记 verified 的复核依据

Skill 已要求参考 SQL 实算、核对关联基数，并用隔离数据覆盖能区分对错的情形。这些原则正确。缺口在于 `oracle.verified` 没有固定的证据交付要求；不同执行者可能把“SQL 成功执行并保存了结果”解释成答案已经验证。

风险：COUNT 和 COUNT DISTINCT、平均比率和总体比率、JOIN 重复累加等错误 SQL 都能执行成功。标准答案错误会让正确产品被判错，或奖励错误实现。

建议将 verified 的依据写成明确规则：

- 有确认的口径、输入数据版本、参考计算和匹配规则。
- 有至少一种可记录的复核依据，例如人工核算代表案例、独立实现对算，或能够区分常见错误的固定数据。
- 对高风险计算优先检查重复关联、NULL/0、空集、并列、权重和时间边界。
- 只有执行记录、没有语义复核证据时，说明检查范围，不自动升级为已验证答案。

不是要求每道简单题都实现两套系统，而是让“验证过”有可追溯的含义。行为题应保存规则依据和评分样例，不能因没有数值 SQL 而随意给分。

### 3. 补 Ask Data 题集到 RSI 的转换与评分接口

当前 Skill 建议使用 cases.jsonl，字段包括 case_id、question/turns、family_id、status、oracle。RSI 接受 casebook.json，要求 id、input、group、split、tags、source、expectedStatus、expected、evidence 等。当前 src、tests、skills 中未发现 Ask Data 专用导入器。

这是未实现的集成交接，不代表当前人工工作流不能使用。直接把 Skill 产物传入 RSI 不能满足接口；现场临时转换还容易丢失家族、证据和多轮契约。

建议提供一个薄适配器：

| Skill 数据 | RSI 数据 | 转换约束 |
| --- | --- | --- |
| case_id | id | 唯一、稳定。 |
| question / turns | input | 多轮保留每轮输入与预期，runner 明确同一会话执行。 |
| family_id | group | 同义改写和同源语义变体归组。 |
| directions | tags | 可增加题型、难度和行为标签。 |
| source_refs / parent_case_ids | source 及附加来源记录 | source.ref 表示原始题的来源身份；文件位置另行保留。不能把整个工作簿路径当所有题相同的来源身份，否则不同原题也无法跨 split。 |
| status / oracle.verified | expectedStatus | ready 不直接映射 verified；draft、needs_context、stale、excluded 不进入冻结验收包。 |
| oracle / gold | expected / evidence | 解析并绑定标准结果、判分规则和依据，防止运行时 gold 文件被替换。 |

评分器必须分别处理数值结果、应澄清、应说明缺数据，以及逐轮结果；不能统一比较自然语言字符串。Skill 已写出的空结果、重复行、容差和失败分母规则应落实到评分器。

使用 RSI 的冻结接口绑定题集和评分器内容摘要。数据库数据、知识配置和外部 gold 还应记录可靠的版本或快照身份；现有冻结接口不会自动替用户冻结外部数据库。准备完成、冻结成功和真实产品评测通过仍应分别报告。

## 建议的分工

```text
ask-data-benchmark
  确认业务口径 → 生成题集 → 复核标准答案 → 输出开发/验收材料
                          ↓
fuju-rsi
  校验与冻结 → 开发题搜索 → 验证题选方案 → 独立验收
                          ↓
fuju-trace（可选）
  关联业务执行链路 → 支持定位与解释
```

业务系统原有的详细 Trace 可用于授权诊断。RSI 自身的普通观测事件仍只记录状态和计数，不能为了诊断把题目、标准答案、提示词或私有验收明细写进普通 Trace/JSONL。

## 处理顺序

1. 先补开发验证与独立验收的定义，以及 verified 需要的复核证据。
2. 再增加可校验的 Ask Data 导入格式和评分适配器，复用 RSI 现有冻结与用途守卫。
3. 用小型固定数据演练完整链路，包含重复行、空结果、比例、并列和多轮；之后再由用户指定真实数据与业务入口做实测。

不建议因为 RSI 拆分就推翻这个 Skill。更合适的是保留它的交互和业务评测方法，把需要稳定执行的规则落到适配器中。
