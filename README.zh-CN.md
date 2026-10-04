# Fuju RSI

**让 Codex / Claude Code 帮你评测和改进已有的 AI Agent。**

接上程序真实入口，复现失败，比较限定范围的改动，得到包含结果和退步的报告。
先从**智能问数（text-to-SQL）**开始，继续使用业务项目原来的运行和发布方式。

[English](README.md) · 简体中文 · [反馈问题](https://github.com/vibeinging/fuju-rsi/issues) · [参与贡献](CONTRIBUTING.md)

## 一句话开始

在 Codex 或 Claude Code 中打开你的业务项目，复制这句话：

> 按 https://github.com/vibeinging/fuju-rsi 的说明，给当前项目接入 Fuju RSI，找到 Agent 的真实入口和已有测试，先跑一次原版评测并给我报告；缺少连接或标准答案依据时请列出来。

编码 Agent 按 Skill 指南在开发环境安装工具，找到现有函数、命令或测试接口，
填写适配器，再执行评测。接入命令负责生成骨架和检查缺项；工程绑定由编码 Agent
根据真实代码完成。业务所需的测试账号、授权和可信答案仍要实际提供。

首次产物是**原版报告**。之后可以提出候选并比较，再按业务项目原有流程审查和发布。
安装后，支持相应命令的宿主可用 Codex 的 `$fuju-tune`、Claude Code 的 `/fuju-tune`
显式选择。详见[安装说明](skills/fuju-tune/references/install.md)和[首次接入](skills/fuju-tune/references/onboarding.md)。

## 先试玩：不需要模型账号

需要 Python 3.8+ 和 Git。以下命令适用于 macOS / Linux：

```bash
git clone https://github.com/vibeinging/fuju-rsi.git
cd fuju-rsi
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/try_ask_data.py
```

Windows 使用已安装的 Python 命令，在 PowerShell 中通过
`.venv\Scripts\Activate.ps1` 激活环境。

试玩会真实执行 SQLite：原版把“金额求和”误做成“订单计数”，固定规则提供一个修正候选。
每次新建目录，打印报告位置；试玩本身不访问网络，不调用模型，不需要服务或 Trace DB。

| 方案 | 训练通过 | 验证通过 | 运行错误 |
| --- | ---: | ---: | ---: |
| 原版 | 0 / 1 | 0 / 1 | 0 |
| 演示候选 | 1 / 1 | 1 / 1 | 0 |

这是可复现的**合成协议示例**，用了 5 次 runner/proposer 回调，未执行独立验收，
保持 `adoptable=false`。它帮助理解报告和流程；真实客户效果还需要真实运行验证。
[查看试玩说明与报告示例](docs/guides/offline-demo.md)。

## 安装到业务项目

从已取得的仓库执行，选择你使用的宿主和现有业务目录：

```bash
python skills/fuju-tune/scripts/install.py --host codex --project /path/to/your-project
# Claude Code 改为 --host claude；两个宿主使用 --host both。
```

安装器把完整 Skill 复制到宿主标准项目目录，已有修改会保留。Skill 和 Python 包分别安装，
Python 包只用于开发、测试或 CI 环境；业务程序继续按原有方式运行。
[用户目录安装与其他选项](skills/fuju-tune/references/install.md)。

## 能得到什么

- **首次评测：** Markdown 报告和 JSON 汇总，准备不足时列出缺项。
- **提示词比较：** 原版、候选、差异、开发结果；独立验收另有明确入口。
- **配置比较：** 限定文件改动、原版与候选重跑、实际读取文件的摘要和原版恢复材料。目前只提供开发证据。

业务程序可以是 Python、Node、Go 等语言，由测试适配器调用原有函数、命令或测试 HTTP。
Skill 名是 `fuju-tune`；包和 CLI 是 `fuju-rsi`，Python 导入为 `fuju_rsi`。

## 当前范围

当前是**源码预览**，尚未发布 PyPI 包。请从仓库安装，不能假设 `pip install fuju-rsi` 已可用。

本地已检查干净包安装、复制 Skill、外部项目原版运行以及移除 RSI 后业务继续运行。
Codex / Claude 宿主实际发现和调用、真实客户问数效果仍待验证。
提示词搜索、限定配置比较已有实现；自动代码优化与外部场景代码插件尚未实现。

开发分数提高不能直接授予采用资格。独立案例由单独环境持有；最终交付普通提示词、
配置或代码差异，按业务项目原有测试、审查、发布与恢复流程处理。

## 指南与反馈

| 想做的事 | 入口 |
| --- | --- |
| 接上已有程序、首次跑原版 | [首次接入](skills/fuju-tune/references/onboarding.md) |
| 准备有来源、可复核的问数案例 | [问数场景指南](skills/fuju-tune/scenarios/ask-data/guide.md) |
| 判断该改指标、词典还是项目规则 | [问数调整对象](skills/fuju-tune/scenarios/ask-data/targets.md) |
| 比较普通配置文件 | [文件候选](skills/fuju-tune/references/file-candidates.md) |
| 独立验收与交付 | [验收流程](skills/fuju-tune/references/verification.md) |
| 安装失败或提出接入需求 | [问题反馈](https://github.com/vibeinging/fuju-rsi/issues/new/choose) |
| 贡献新场景和适配器 | [贡献指南](CONTRIBUTING.md) |

智能问数是已接入的领域场景；欢迎用可运行的公开或合成样例提出其他场景。
反馈时提供脱敏步骤，移除凭据、客户题库和私有结果。

[Fuju Trace](https://github.com/vibeinging/fuju-trace) 可选，用于定位运行问题；
[Fuju / 原 yiTrace](https://github.com/vibeinging/fuju) 提供 TraceDB 引擎与兼容包。
离线试玩与默认评测均不依赖它们。

## 开发

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
python3 skills/fuju-tune/scripts/scenarios.py check
```

控制台改动后构建 `console/` 并运行 `python scripts/sync_console.py`。
通过 `python -m build` 构建 wheel，再用 `scripts/verify_python_consumer.py` 检查干净安装。
[开发约定](AGENTS.md) · MIT 许可证。
