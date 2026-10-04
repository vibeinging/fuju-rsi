# 第一次接入：先交付原版报告

适用于“接入当前项目”“先评测一下”“给我一份原版报告”等请求。用户描述目标，编码 Agent 完成工程接入，不要求用户手写适配器或理解内部账本。这里只接开发测试环境；不扩展为自动改动、搜索、采用或部署。

## 安装和检查

未安装 Skill 时按[安装说明](install.md)选择当前宿主。Python SDK 安装在业务项目的开发虚拟环境，先复用已有测试 Python；没有时创建独立测试环境。从用户提供或项目记录的 Fuju RSI checkout / wheel 安装，不把依赖加入生产清单。仓库尚未发布 PyPI，不能假设 `pip install fuju-rsi` 可用。

```bash
python3 -m venv .fuju-rsi/venv
.fuju-rsi/venv/bin/python -m pip install /path/to/fuju-rsi-checkout
```

以上是 POSIX 示例，Windows 使用虚拟环境的 `Scripts/python.exe`。已建环境不重复创建；安装来源需要由用户提供的链接或项目记录确定。下面均使用已安装 SDK 的同一个测试 Python，`<skill目录>` 是当前 SKILL.md 所在目录。

```bash
python <skill目录>/scripts/connect.py inspect --project /path/to/business
```

`inspect` 只列少量项目文件、入口和测试路径线索，不执行项目脚本，不打开 `.env` 或案例正文。编码 Agent 再按项目约定读取相关代码，确定真正的运行入口、配置来源、完整结果、测试隔离方式与已有预期依据。路径线索不是自动确认的入口。

## 准备和填写连接

优先复用项目已经存在的适配器；没有时准备测试骨架：

```bash
python <skill目录>/scripts/connect.py init --project /path/to/business --kind ask-data
```

通用业务使用 `--kind custom`。真实问数必须选择 `ask-data`，不能因为暂缺材料而改成 custom。已有工厂使用 `--agent local_adapter:build_agent`，必要时加 `--adapter-dir evaluation-tools`。

`init` 在业务项目创建 `.fuju-rsi/connection/connection.json` 及测试适配器骨架。编码 Agent 填写它，用户不手写 `module:factory`。已有连接重复执行返回 `reused`，保留适配器、题集和账本；冲突拒绝覆盖。配置只保存类型、工厂和路径，认证仍走业务原有方式，不把凭据写入连接文件。

按[接入契约](integration.md)把骨架绑定到真实函数、命令或测试 HTTP，读取产品原有提示词、隔离每次运行状态并设置超时。配置文件候选另走[文件比较](file-candidates.md)，不能把词典 JSON 填成提示词。原版报告不要求提案模型，不能用返回 expected 的假 runner 让检查通过。

通用已有测试可以提供 `Example` 与固定评分，报告只声明已知案例范围。问数按[场景指南](../scenarios/ask-data/guide.md)复用或冻结有来源的 v2 开发包，使用实际 evaluator 定义文件作为冻结评分文件；在连接 JSON 中填写 `benchmark` 和 `validationLedger`。也可在首次 init 时使用 `--benchmark benchmarks/development-v1 --validation-ledger validation-history`。答案来源不明确则保持草案，不能自动编成已确认答案。

共用验证账本优先复用项目已有目录。首次创建按既有 `ask-data init-validation` 协议固定上限，向用户说明原版也使用一次验证；不能因重试或首次检查失败而换账本。独立验收包不交给接入/搜索流程。

## 检查后运行

```bash
python <skill目录>/scripts/connect.py check --project /path/to/business
python <skill目录>/scripts/connect.py baseline --project /path/to/business --max-calls 40
```

同样的命令可用 `fuju-rsi connect` 执行。`check` 检查工厂、类型、开发包、评分文件和已有账本，不调用 runner / proposer，不消耗验证次数；factory 只配置连接，不得在导入或初始化时执行业务或调用模型。骨架未填写、缺开发材料或加载异常时返回 `blocked` 和本地日志位置。`ready` 只代表连接材料可加载，`executionChecked=false`；实际生效和业务正确性仍需运行核对。

运行前核对 `plannedRunnerCalls`、测试范围和已知计费条件，沿用用户授权。`--max-calls` 只限制 runner 回调数量，不限制金额或回调内模型调用。已有范围和依据足够时继续完成原版；只有缺账号、运行条件、真实调用授权或无法从资料确定的业务依据时，补问必要信息。

`baseline` 每次使用新的 `.fuju-rsi/baselines/<id>`，读取 factory 的产品原版，避免旧实验 active prompt 污染；继续复用连接中的题集与账本。它只执行训练/验证原版，不生成候选、不执行独立 test、不授予采用资格。账本耗尽会在 runner 前拒绝，不能清空或换目录继续同一批题。

命令返回原有 `baselineComplete`、计数、训练反馈、验证汇总和 `artifacts`。把报告路径、已知案例结果、运行异常、未计量费用与缺项交付给用户。`baselineComplete` 表示运行完整，不代表业务题全答对。先交付这一份报告；用户另行要求改进时才进入候选比较。

日志和实验记录留在业务项目；工厂输出/异常不回显到公共命令摘要，新建检查及执行日志使用私有文件权限。诊断日志只用于定位接入错误，不能把其中验证明细或独立验收内容回流用于提案；调优继续只读允许的训练反馈和验证汇总。目录分开不是独立验收的文件权限隔离。首次接入工具不自动证明所有环境依赖、正确答案或产品配置确实生效，需要编码 Agent 核对真实结果与配置读取。
