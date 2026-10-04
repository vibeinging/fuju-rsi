# 比较普通配置文件

先找到一个真实失败及允许修改的文件。首版支持词典、项目规则和指标上下文的已知案例或开发集比较；不改业务公式、评分答案和验收政策。结果始终 `adoptable=false`。文件候选的独立验收、正式发布、代码候选和自动生成逻辑仍待接入。

用户项目只需提供测试适配器：从给定配置目录调用原有业务入口，并返回真正读入的文件字节摘要。每个案例使用新的临时目录；源项目不被修改。业务输出、缓存和数据库使用另外的路径。临时目录是防止共享配置串改的工具，不是操作系统权限沙箱；哈希读回是适配器提供的证据，不能单独证明业务正确。

## 一个不调用模型的本地例子

在 Fuju RSI 仓库运行，临时工作目录每次新建：

```bash
experiment_dir="$(mktemp -d)"
PYTHONPATH=src:examples python3 examples/file_config_fixture.py \
  --candidate-output "$experiment_dir/candidate.json"
PYTHONPATH=src:examples python3 -m fuju_rsi compare-files \
  --agent file_config_fixture:build_agent \
  --workspace "$experiment_dir/workspace" \
  --candidate-file "$experiment_dir/candidate.json" --max-calls 5
```

这个 SQLite 合成例子只把“销售额/营业额”等金额别名指向既有金额指标；计数与金额 SQL、单位和评分答案保持固定。两份配置一起参与身份和实际读取检查；报告保留原版，业务不依赖 RSI。它检查协议和已知回归，不是真实客户收益或独立验收。

## 接入已有项目

开发侧 `module:factory` 返回 `FileExperimentSpec`，不修改生产导入：

```python
from fuju_rsi import (
    FileTarget, FileBaseline, FileCandidate, FileRunResult,
    FileExperimentSpec, Prediction,
)

baseline = FileBaseline.capture(
    "./test-config",
    [FileTarget("dictionary.json", "dictionary", ("/aliases",)),
     FileTarget("rules.json", "project-rules", ("/displayHints",))],
    context_files=["data-version.txt"],
)
candidate = FileCandidate.propose(
    baseline, {"dictionary.json": changed_dictionary_text},
    rationale="修正等价别名；不改公式、单位或答案含义。",
)
```

`runner(context, input)` 从 `context.root` 实际读取配置，调用业务函数、命令或测试接口，返回 `FileRunResult(Prediction(output), loaded_hashes)`。`loaded_hashes` 是路径到实际读取字节 SHA256 的映射，必须包含全部声明文件；不要把 `context.expected_hashes` 原样复制来代替读取证据。`evaluator(expected, prediction)` 使用独立答案与原评分规则，返回 `Evaluation`。两个函数必须有可记录的源文件；源码、函数代码、原版和候选输入在运行期间改变都会拒绝完整成功。

配置输出只接受规范相对路径、UTF-8 普通文件，单文件至多 1 MiB、全部内容至多 4 MiB。不跟随声明路径的符号链接，禁止未声明文件、大小写/Unicode 名称冲突、只读上下文改动和新增/删除文件。JSON 指针限定实际可变化的字段；未指定指针的文本文件可整体替换。`preserving` 是提案声明，不能自动证明业务等价。正式口径变化须另行业务确认、更新答案和 Benchmark、重建基线。

把 `candidate.to_json()` 保存为候选文件。CLI 可重复传 `--candidate-file`，按现有评分提高与退步约束比较；`maxCalls` 计 runner/proposer 回调，不是金额上限。没有候选时只跑原版；预算不足、异常、取消或条件漂移都不会获得完整成功或采用资格。

## 问数和 Skill 入口

问数 factory 返回 `FileExperimentSpec(kind="ask-data")`，案例来自 `FUJU_RSI_BENCHMARK` 指定的冻结开发包。评分函数须与冻结评分文件相符。CLI 同时传 `--ask-data-bundle` 与 `--validation-ledger`，复用现有共享验证额度和来源隔离，不允许独立验收包进入开发。

复制 Skill 后使用同一入口：

```bash
python <skill目录>/scripts/run_experiment.py \
  --scenario ask-data --candidate-kind files \
  --agent business_adapter:build_file_agent --workspace ./experiments \
  --benchmark ./benchmarks/development-v1 --validation-ledger ./validation-history \
  --candidate-file ./candidate.json --max-calls 100
```

Skill 只返回训练明细和验证汇总；普通 CLI 和公开 `result.json` 不输出题目、答案、运行明细或业务打印。完整记录和诊断日志留在本地私有工作目录。本地记录本身不构成对运行账号的访问隔离。

## 交付与恢复

- `report.md`、`result.json`：状态、每方案汇总、用量及证据范围。
- `candidate/`：按选中方案导出的普通配置；未选新方案时保持原版内容。
- `baseline/`：原版文件，沿业务项目原有方式恢复。
- `changes.diff`：逐文件差异。
- `candidate.json`：候选和原版恢复包；可再次作为 `compare-files --candidate-file` 输入，原版发生变化时拒绝。

先核对完整比较与真实结果，再按项目自己的验收、应用和恢复流程使用普通文件。配置比较不会生成 `verified-prompt.txt`，也不会更新 active prompt 或调用提示词 Runtime 发布。当前交付检查证明移除 RSI 后仍可读取配置；尚无真实问数产品效果验收。
