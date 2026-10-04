# Offline Ask Data demo / 离线问数试玩

From a checkout with the Python package installed / 在已安装 Python 包的仓库中执行：

```bash
python scripts/try_ask_data.py
```

Each run creates a fresh temporary directory and prints its report path. To retain
the files at a chosen location, provide a **new** directory / 每次新建临时目录并打印报告位置，也可指定新的保存目录：

```bash
python scripts/try_ask_data.py --output /path/to/new-demo-directory
```

An existing output is refused. The script can run from another working directory;
it locates the checked-out examples relative to its own file. The package must be
installed in the Python environment running the script.

已有输出目录会被拒绝。脚本按自身位置寻找仓库样例，可以从其他工作目录启动；
运行它的 Python 环境需先安装本仓库包。

## What happens / 实际执行

1. Prepare and freeze the public synthetic catalog in `examples/ask_data_catalog.json`.
2. Create a demo-only validation ledger allowing two sweeps: original and candidate.
3. Execute the original `COUNT(*)` and the rule-generated `SUM(amount)` candidate against in-memory SQLite.
4. Check complete results and answer text with the registered scorer; export the report and ordinary prompt diff.

The source values are A: `40 + 60 = 100`, B: `30 + 50 = 80`. Counting gives `2` for
both. The scorer therefore records two completed wrong answers for the original,
then two correct answers for the candidate.

原始数据为 A：`40 + 60 = 100`，B：`30 + 50 = 80`。计数都得到 `2`。
因此原版的两次业务回答都错误，但运行完成且没有异常；候选两次回答正确。

## Observed report excerpt / 实际报告摘录

Reproduced on 2026-10-04 with an installed package, with no source-package `PYTHONPATH`.
This table omits variable timings and experiment IDs / 2026-10-04 使用已安装包复跑；表格略去每次变化的耗时和实验编号：

| Plan / 方案 | Split / 用途 | Passed / 通过 | Run / 已运行 | Runtime errors / 运行错误 |
| --- | --- | ---: | ---: | ---: |
| Original / 原版 | Training / 训练 | 0 / 1 | 1 / 1 | 0 |
| Original / 原版 | Validation / 验证 | 0 / 1 | 1 / 1 | 0 |
| Demo candidate / 演示候选 | Training / 训练 | 1 / 1 | 1 / 1 | 0 |
| Demo candidate / 演示候选 | Validation / 验证 | 1 / 1 | 1 / 1 | 0 |

Five callbacks are used: four runner calls and one proposer call. This cap measures
callbacks, not money. No model is called in this example; the fixture does not report
token or monetary usage, so those report fields remain unknown.

共 5 次回调：4 次 runner、1 次 proposer。回调上限不是金额上限。
样例不调用模型，fixture 未上报 token 或费用，因此报告中保留未知。

## Reading the result / 如何理解

- The result demonstrates local reruns, scoring, regression comparison, and exported files.
- The cases and proposal are known synthetic fixtures. No customer model or business system was tested.
- No independent holdout is run; `adoptable=false`. A candidate file is review material, not permission to deploy.
- The report, `result.json`, `baseline-prompt.txt`, `candidate-prompt.txt`, and `prompt.diff` remain in the printed directory.

演示能证明本地重跑、评分和文件交付链路。它没有证明真实客户问数准确率提升，
也没有执行独立验收或写入业务配置。

The fresh ledger belongs only to each isolated demo. For a real project, establish
the limit before first validation use and reuse its shared ledger across experiments.
Follow [first connection](../../skills/fuju-tune/references/onboarding.md) and the
[Ask Data guide](../../skills/fuju-tune/scenarios/ask-data/guide.md).

真实业务应在首次使用验证题前确定上限，跨实验复用共用账本，不能用试玩重置次数。
