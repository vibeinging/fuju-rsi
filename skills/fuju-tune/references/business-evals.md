# 从业务建立评测

Eval 记录如何运行业务任务、如何判断结果。Benchmark 把案例、评分与运行条件固定成版本。目标是能重复检查用户业务，不是生成一个看起来很高的分数。

## 先确定正确答案的依据

从获授权的业务规则、已有测试、客户案例和失败记录生成草案，并保留成功案例作为回归保护。用户只要求添加 Eval 时，不自动修改产品提示词。

- 明确规则：补充正常情况、边界、例外和信息不足。规则未定义的口径保留待确认。
- 真实失败：提取可复现输入与必要环境。原 Agent 输出是观察结果，不等于标准答案。
- 已有测试：保留原输入与预期，补充有业务意义的缺口；不覆盖旧答案提高分数。
- 判断真实结果：SQL 能执行不等于金额正确，回答“已退款”不等于退款记录存在。

优先用固定数据上的参考计算、已有可靠断言或业务确认作为 evidence。开放回答可以把必要事实、禁止项和引用要求放进 expected，由自定义 evaluator 评分。模型裁判需用可信样本校准；让造题模型再答一次不算独立依据。评分器内的模型费用不包含在 runner/proposer 回调预算内。

## v2 草案格式

开发数据使用 `schemaVersion: 2, role: "development"`。下面只有一条草案，不能直接冻结或作为充分覆盖的证明：

```json
{
  "schemaVersion": 2,
  "role": "development",
  "id": "sales-agent",
  "name": "销售助手开发评测",
  "scoring": "对照固定订单数据核对金额，单位为分",
  "environment": {"fixture": "orders-fixture-v1", "runtime": "python-stdlib"},
  "cases": [
    {
      "id": "paid-orders-001",
      "input": {"question": "查询北区销售额", "region": "north"},
      "group": "customer-request-024",
      "split": "train",
      "tags": ["付款口径", "正常情况"],
      "source": {"kind": "user-case", "ref": "cases/customer-request-024"},
      "expectedStatus": "draft"
    }
  ]
}
```

`source.ref` 记录实际原始案例或数据来源，同一来源的变体不能靠更换 ref 变成独立样本。通用业务规则可引用在 evidence 中，不能为了跨集合分题伪造来源。`group` 把同源案例、改写与变体放在一起；同一 source 或 group 不能跨 train/validation/test，验收时同一 source 也不能拆成多个独立组。

`expectedStatus` 默认为 draft；有可靠依据后填写 expected、verified 与非空 evidence。显式 null 可以是合法答案，缺失答案不能用 null 代替。verified 记录已完成的依据核对，不代表工具自动证明答案正确。材料充分时直接核对；材料有歧义时保留草案并指出具体缺项。

`tags` 是覆盖标签，包含 `critical` 时标记为关键案例。冻结后的 Example 保留 `group_id/source_id/exposure/critical`；开发包的 exposure 为 development。真正的未见题由独立验收者使用 `role: "holdout"` 的另一份包保存，其中仅有 test，exposure 输出为 unseen。该字段只是声明，不能把同一会话造过或读过的题变成盲测。

## 检查与追加

在适配器项目根目录执行，`<skill目录>` 指 SKILL.md 所在目录：

```bash
python <skill目录>/scripts/benchmark.py inspect --input evals/development.json
python <skill目录>/scripts/benchmark.py merge --input evals/development.json --add evals/new-cases.json --output evals/development-next.json
```

new-cases.json 是案例数组。merge 追加到新文件，不覆盖旧案例。重复 id、相同规范化 JSON 输入、同源跨集合会报错。v2 对大小写、全半角、标点与空白造成的近似文本重复给出检查提示；未解决分组问题时拒绝冻结。这不是语义去重，仍需检查看似不同但来自同一业务问题的题。

inspect 返回草案数、已核对数、各集合数量、覆盖标签及相关提示，不返回题目答案。只有一个案例或缺少来源时，先交付草案与覆盖缺口，不用改写、重复运行或随机组名凑出独立样本量。

## 固定版本

所有纳入的案例须有 verified 预期及依据。开发包要求 train、validation 均非空；验收包只含非空 test。验收包的创建和保存留在实际验收环境：

```bash
python <skill目录>/scripts/benchmark.py freeze --input evals/development-next.json --output benchmarks/development-v1 --version v1 --scorer-file tune_agent.py
```

`--scorer-file` 必须是定义实际 evaluator 的项目内文件。若评分器在 grader.py 中，就指定 grader.py。结果包含：

- casebook.json：案例、来源、预期依据和覆盖信息。
- examples.json：可传给 Example 的数据与分组、来源、暴露情况、关键案例标记。
- manifest.json：版本、角色、案例摘要、评分文件摘要、声明的运行条件与数量。

已有目录不可覆盖。新增案例冻结到新版本；新版本不会清除原来内容的使用历史。不同题集或评分标准的总分不能直接作为同一基准上的涨跌。若单独冻结已验证子集，说明尚未纳入的草案，不能默默删掉失败题。

Benchmark 冻结只校验评分文件摘要；候选冻结还能显式记录其他代码文件。两者均不自动验证全部依赖、实际模型版本或外部数据状态。environment 中记录真实条件，并由项目测试设施保持一致。

## 搜索和迁移

factory 从 v2 开发包读取原样 Example 列表，参见 [接入契约](integration.md)：

```bash
python <skill目录>/scripts/run_experiment.py --agent tune_agent:build_agent --workspace .yitrace-optimization --benchmark benchmarks/development-v1 --candidate-file prompt-v1.txt --max-calls 100
```

helper 核对案例与顺序、评分源文件、冻结版本，并留下 manifest 副本和实验对应记录。holdout 包不能传给搜索入口：`load_bundle` 默认拒绝验收包，只有验收侧显式声明 `role="holdout"` 才读得出，并在读取案例正文之前就失败；搜索不运行 test，也不授予采用资格。独立验收走 [单独的验收命令](verification.md)。

v1 四字段包保留读取兼容，旧 train/validation/test 三集合格式不变。搜索仅执行 train、validation；旧 test 的历史暴露未知，不能直接升级为独立验收题。迁移时复制成新 v2 开发包，保留原答案、补充可信来源并重新搜索；新建的 holdout 必须来自调优者未见且有依据的材料。

案例与评分器留在开发、测试或 CI；业务生产程序不加载这些文件或 yiTrace。交付业务覆盖、未决规则、冻结版本与真实运行证据。生成题数、可信答案题数、已执行题数和独立来源组数分开报告。
