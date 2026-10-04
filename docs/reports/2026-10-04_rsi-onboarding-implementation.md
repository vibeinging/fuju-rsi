# 一句话接入的首个本地实现

日期：2026-10-04。范围：独立 `fuju-rsi` 仓库的开发工具、可复制 Skill 和首次原版报告；未修改业务生产代码或 TraceDB。

## 用户入口与已实现内容

用户在业务项目里向编码 Agent 说：

> 给当前项目接入 Fuju RSI，找到 Agent 的真实入口和已有测试，先跑一次原版评测并给我报告。

编码 Agent 按 README / Skill 安装开发工具、查找入口、填写测试连接并运行。首次未安装时需提供可信仓库或指南链接，SDK 从源码或 wheel 安装到开发虚拟环境；仓库尚未发布到 PyPI。

- `skills/fuju-tune/scripts/install.py`：明确选择 Codex、Claude 或两者，复制完整资源到用户/项目目录；相同资源幂等，不覆盖用户修改，不拉远程脚本或调用模型。
- `fuju-rsi connect inspect / init / check / baseline`：列项目线索、保存连接、生成待填写骨架、检查缺项、执行原版。复制 Skill 后可通过 `scripts/connect.py` 使用同一入口。
- `.fuju-rsi/connection/connection.json`：保存类型、工厂与测试路径，复用已有适配器、题集和账本；认证沿用业务原有方式。
- `skill_runner.py`：首次接入和原 Skill 复用原有用途、评分文件、案例一致性、验证账本和输出守卫。原版不调用 proposer、不运行独立 test、不授予采用资格。
- 原版每次使用新实验目录，避免旧实验 active prompt 替代业务原版。问数共享账本保持原目录与上限，重试不重置使用历史。
- 报告使用 Fuju RSI 名称；原版报告不引导用户自行开始候选搜索，也不把运行完整当成业务全答对。

脚本生成工程骨架，业务绑定由编码 Agent 按源码填写；并非通用脚本能自动猜出任意项目的入口、答案或全部配置依赖。`check` 只加载工厂和开发材料，不调用 runner / proposer；factory 导入和初始化不得执行业务。`ready` 保留 `executionChecked=false`，实际运行与业务正确性还须核对。

## 复现和修复

| 问题 | 复现与修复 |
| --- | --- |
| 外部目录接入冻结问数项目失败 | 从业务 cwd 可通过，从其他 cwd 加 `--project` 失败；在业务目录上下文校验冻结评分相对路径，外部 cwd 子进程回归通过。 |
| 无效适配器目录留下坏配置 | 不存在的 `--adapter-dir` 原先仍落盘，重试无法复用；在写入前检查已有适配器目录，失败不留 profile。 |
| 复制 Skill 在 `python -I` 下找不到场景脚本 | 干净 consumer 复现抽取回归；按脚本自身位置显式加载同目录场景资源，并保持按需加载，普通/问数及文件路径回归通过。 |
| 部分骨架误运行或正常分支被误判 | 显式占位异常在 runner、评分和账本预留前拒绝；只检查顺序代码中的占位异常，不将正常接口的不支持分支视为整个骨架。兼容 Python 3.14 的 AST 接口。 |

工厂异常和输出收进本地日志，新建检查及运行日志使用 `0600`。这不构成独立验收的权限隔离，日志中的验证明细不能回流给调优提案。

## 最终验证

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 <skill-creator>/scripts/quick_validate.py skills/fuju-tune
python scripts/verify_python_consumer.py <本次构建的wheel>
git diff --check
```

- 源码基线 306 项通过；最终全量 **343 项通过**。新增测试覆盖双宿主目录复制、幂等/冲突、路径边界、复制与安装形态、原版隔离、骨架前置拒绝、问数用途/评分/账本和私有输出。
- Skill 结构检查及 diff 空白检查通过。
- 本次 wheel 在全新虚拟环境安装，`pip check` 通过。无源码 `PYTHONPATH` 的 `connect` CLI、复制 Skill、原有提示词 CLI/报告、文件比较和可移除性路径全部通过。普通业务进程在 `-I -S` 下运行，不依赖 RSI。
- 独立前向流程从无连接的临时项目开始，读取原始 README / 程序 / 配置 / 已有测试，填写四处业务绑定后完成原版报告。两次真实 SQLite 命令调用，训练 0/1、验证 0/1、运行异常 0；原版 COUNT 与固定求和答案不符被如实报告，`baselineComplete=true` 且 `adoptable=false`。产品四个源文件前后 SHA-256 相同。

前向流程和普通案例均为离线合成业务，说明工程流程可运行，不是客户效果或准确率证明。原版实际报告的通过数、完整性和异常统计见上面的证据摘要；临时本地报告不作为公开安装依赖。另有[可复现的离线试玩与报告摘录](../guides/offline-demo.md)。

## 完成边界

尚未验收 Codex / Claude Code 宿主实际发现与调用 Skill，尚未接通真实客户问数、调用云模型或完成配置独立验收。用户仍需实际可用的账号、测试条件和业务答案依据；编码 Agent 承担可确定的工程接入，不编造缺失材料。本记录覆盖本地验证；源码提交推送与包发布是后续单独的交付步骤。

本实现使用 `feature/repository-split`，未修改相邻 TraceDB 引擎或业务生产代码。
