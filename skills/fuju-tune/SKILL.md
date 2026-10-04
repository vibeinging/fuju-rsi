---
name: fuju-tune
description: "Connect an existing business Agent to Fuju RSI, produce its first baseline report, or improve it through real reruns and bounded configuration or prompt changes. Includes Ask Data and independent verification."
---

# Fuju RSI · Agent 评测与改进

你是使用本 Skill 的编码 Agent；用户的 Agent 程序是被测对象。先从真实失败、业务依据和可运行入口建立原版结果，再提出范围明确的候选并重跑。调整对象由场景和失败原因决定；提示词支持自动搜索，词典、项目规则和指标上下文支持已给定文件候选的开发比较，配置独立验收尚未开放。交付目标是能复现的业务改善，不能把开发集涨分说成对新案例也有效。智能问数是项目随带、已接入的第一个场景；其他场景可以由社区贡献。

## 选择场景

先运行 `python <skill目录>/scripts/scenarios.py list` 查看本份 Skill 带有哪些场景。根据用户实际任务选择 `id`，然后只读返回的 `guidePath`。当前问数场景的指南在[scenarios/ask-data/guide.md](scenarios/ask-data/guide.md)。如果没有匹配场景，使用[通用接入契约](references/integration.md)完成开发评测，或提出新场景；不要把某个场景的判分规则套到不相干任务上。

`manifest.json` 只用于发现和阅读说明，不执行其中的内容。`status=integrated` 表示仓库有对应接入流程，不代表用户项目已接通或业务验收已通过；`draft` 只用于设计与接入准备。运行已选场景时给 helper 传 `--scenario <id>`，让它核对场景与 `AgentSpec.kind`；目前只有 `ask-data` 有可执行领域接入。每次运行仍需检查真实业务入口、冻结 Benchmark、评分器和适配器；缺少场景必需的验证账本或执行能力时，在调用 runner 前停止。不能通过改 `kind` 或改清单状态退回较弱路径来绕开场景规则。新增场景按[场景贡献契约](references/scenarios.md)组织。

**默认无前端执行。** 官网用于介绍、安装与文档；Skill 不登录网站、不启动本地控制台，也不把业务样本上传到官网。CLI/SDK 执行并输出独立文件，控制台只是可选查看器。

**默认零线上依赖。** SDK 只安装在开发、测试或 CI 环境，适配器调用业务原有函数、命令或测试 HTTP。交付普通提示词、配置或代码差异，沿用产品的启动、发布和回退方式。线上 trace SDK、DB 和工作区提示词读取都是独立可选集成。

## 第一次接入

用户说“给当前项目接入 Fuju RSI，先跑原版并给我报告”时，读[首次接入](references/onboarding.md)。由你检查测试环境、寻找真实入口和已有测试、填写测试适配器，使用 `scripts/connect.py inspect / init / check / baseline` 完成第一份报告。首次安装读[安装入口](references/install.md)，使用已确认来源的 SDK 并安装到开发环境。

复用已有连接、案例依据和共享账本；只补问现有资料无法回答的必要信息，不要求用户手写工厂或内部参数。骨架未填写就报告缺项，不能换演示数据伪装接通。问数仍选 `ask-data` 并遵守冻结题集/账本规则；`check` 不执行业务，原版运行仍需核对已有授权和调用范围。首次只请求接入时，交付原版报告即可，不自行搜索候选或采用改动。

## 准备

1. 核对当前项目规则、未提交改动、真实执行入口、提示词来源与已有测试。确认候选提示词会到达最终调用；保留其他人的改动。
2. SDK 另行安装。本 skill 需要当前开发版的 `fuju_rsi`，旧版安装不保证包含新验收协议。先检查 `from fuju_rsi import AgentSpec, VerificationSpec`；缺少时，使用用户已提供或项目记录的源码或 wheel 安装到测试环境，不加入生产依赖。整个 skill 目录可以复制到别处。
3. 目标是问数 Agent 时，读[问数场景](scenarios/ask-data/guide.md)；需要修改配置时再读[调整对象与口径边界](scenarios/ask-data/targets.md)。按来源、答案和验证账本规则准备真实运行入口；其他已接入场景按各自指南执行。无匹配场景才读[接入契约](references/integration.md)并编写测试用 `module:factory`。优先复用现有接口。黑盒无法接受候选配置时，先报告评测结果，不能声称调优已经接通。

比较词典、规则或指标上下文时读[文件候选比较](references/file-candidates.md)，使用 `FileExperimentSpec` 与 `--candidate-kind files`。每次从独立目录运行并读回实际加载摘要；首版只给开发/回归证据，不能把配置 JSON 当提示词交给旧验收或发布入口。
4. 通用业务需要生成或补充案例时，读[业务评测](references/business-evals.md)。根据真实规则、案例和失败记录生成草案，用脚本检查并固定版本。没有预期依据的题保持 draft；已有明确规则或参考执行时直接核对。只要求补充 Eval 时，交付案例和运行入口，不扩大到修改产品。

## 防止过拟合

- **搜索与独立验收分开。** 开发包只放 train、validation；最终 test 留在验收环境。训练明细解释失败，验证汇总选择候选。搜索不会执行 test，永远返回 `adoptable=false`。
- 同一会话生成、读过或从结果推断过的题不能标为未见题。同源案例和改写放在同一组；不能通过改名、换版本、换工作区或添加少量新题重置使用历史。
- 先固定唯一候选、业务评分、代码文件、运行条件与验收规则，再运行验收。看过验收结果后继续调优，需要新的独立验收材料；旧题只用于已知回归。只返回汇总分也不能支持反复选优。
- 不为提高分数改答案、删失败题或把网络异常算成业务失败。发现判分本身错误时单独修正规则，并重新建立基线。
- 本地文件分开不是权限隔离。`workflow_only` 可以检查执行流程，不能授予采用资格。只有实际建立独立账号或 CI 数据边界并登记可信验收者后，才使用 `independent`。详见 [独立验收](references/verification.md)。

## 搜索候选

以下是通用 Agent 命令；问数 Agent 按[问数场景](scenarios/ask-data/guide.md)传入冻结开发包和共用验证账本。下面命令在能导入 factory 的测试工程目录执行；`<skill目录>` 指本文件所在目录。

```bash
python <skill目录>/scripts/run_experiment.py --agent tune_agent:build_agent --workspace .fuju-rsi --benchmark benchmarks/development-v1 --max-calls 40
```

无候选文件时只跑原版 train、validation。读取 stdout 的 `trainingFeedback`；验证只看汇总，不打开工作区的验证明细来写下一版。`baselineComplete` 代表完整执行且没有运行异常，不代表业务题全部答对。

根据训练失败和业务代码编写完整提示词文件，再比较这一批候选：

```bash
python <skill目录>/scripts/run_experiment.py --agent tune_agent:build_agent --workspace .fuju-rsi --benchmark benchmarks/development-v1 --candidate-file prompt-v1.txt --candidate-file prompt-v2.txt --max-calls 100
```

helper 用文件替换 factory 的 proposer，无需额外初始化一个付费提案模型；被测程序自己的凭据仍然需要。没有固定 Benchmark 时可省略 `--benchmark`，但独立验收仍需可信来源记录。

`searchComplete=true` 说明这一轮搜索完成。即使选出候选，`baselineTest`、`candidateTest` 仍为 null，`adoptable` 仍为 false。无改进也是完整搜索的有效结果；不要仅凭退出码 0 交付所谓已验证优化。

搜索 `--max-calls` 统计 runner 和 proposer 回调，最多需要 `(1+C)×(训练题数+验证题数)+C` 次，C 为候选数。另跑一次基线还需训练题数加验证题数。它不限制金额，不包含评分器、当前会话生成候选或 runner 内部多次模型调用的全部费用。业务回调必须设置超时，有副作用的工具只使用已授权的测试环境。

搜索退出码：`0` 完整搜索，包括未找到改进；`2` 预算中断或运行异常导致搜索不完整；`1` 配置或启动失败；`130` 取消。

## 验收与交付

有新候选后，按 [独立验收](references/verification.md) 执行 `verify init / freeze / run / import`。默认验收要求至少 30 个独立来源组、每题每个版本重跑 3 次；来源独立性、业务答案和覆盖仍需要实际证据。只降低数量配置不会让数据变得充分。

只有可信回执与固定候选对应、`evidenceStatus=improved` 且 `adoptable=true` 时，才把该提示词作为已验证改进交付。证据不足、同分、关键案例退步、运行异常或中断都不能推荐采用。报告样本与来源组数、原版和候选表现、退步、收益下界、调用次数、已知用量与未知费用；合成示例或协议测试与真实业务验收分开说明。

交付普通提示词文本及原因说明。用户已授权修改产品时，写回原有配置并跑项目回归，不添加 RSI 实验配置读取调用。需要改代码的修复，按原有流程修改并重新建立基线。工作区采用/回退只管理实验状态，不代表产品已部署或回滚。

用原有启动方式和生产依赖验证：停用适配器和实验服务后，交付的提示词仍生效，原有配置回退仍可用。优先在隔离测试或构建产物里移除本次工具，不卸载其他项目使用的 SDK。

## 默认输出文件

每次有实验记录的搜索结束后，helper 自动生成报告并在 stdout 的 `artifacts` 返回绝对路径。默认目录为工作区 `reports/<实验ID>/<快照>/`；可用 `--report-dir 新目录` 指定。输出目录不覆盖，报告导出不会再调用 runner 或 proposer。

- `report.md`：面向人的结论、原版/候选汇总、样本量、退步、调用和已知用量、未知项及下一步。
- `result.json`：面向后续 Agent/CI 的结构化汇总，不含案例输入、答案、输出或私有日志。
- `baseline-prompt.txt`：普通原版文本；选出新候选时另有 `candidate-prompt.txt` 与 `prompt.diff`，供审查。
- `verified-prompt.txt`：只有当前可信独立验收仍满足采用条件时才生成；开发涨分、失败、同分和证据不足都没有这个文件。

业务案例及冻结 Benchmark 仍保存在评测目录，不重复塞进结果包；独立验收原始回执和私有审计保留在验收侧。可信 `verify import` 后会自动生成新的验收报告。配置/启动失败而未形成实验时，先交付错误与诊断路径，不能伪造完整评测报告。

报告是导出时的快照，修改产品/评分/环境或改变基线后不能当作仍有效的采用凭据。用户无需网页即可阅读 Markdown、用 JSON 接续流程或审查普通提示词差异。没有新候选/独立证据时，报告本身就是交付结果，说明缺什么，不强行输出“已验证优化”。

已有记录可重新导出，无需重跑：

```bash
fuju-rsi report --workspace .fuju-rsi --agent tune_agent:build_agent --experiment <实验ID> --output reports/review-1
```

问数 Agent 重新导出时还要传原冻结开发包的 `--ask-data-bundle`，使 factory 读取相同案例。可信验收报告另加项目预先登记的 `--authority-file`，不能接受候选生成方自带密钥。复制报告不自动应用改动。

## 可选的后台改进

用户希望调试后把改进流程接到产品时，阅读 [可选后台改进](references/runtime.md)。导出开发包，由用户的后台任务运行有限比较；可信验收后的发布是显式步骤。当前只支持提示词，文件写入不代表宿主已经加载，默认零线上依赖用法保持。

## 可选的本地查看器

helper 退出并释放工作区锁后，可以查看本地证据页面：

```bash
fuju-rsi optimize --workspace .fuju-rsi --agent tune_agent:build_agent --serve --port 7880
```

打开 `http://127.0.0.1:7880/#optimize`。要核验已导入结果的采用资格，服务必须在可信登记环境启动，并配置 `--authority-file`，见验收文档；不把密钥交给调优 Agent。没有可信配置时，不能凭历史 `adoptable` 字段重新授予资格。固定文件模式的 factory 不提供自动 proposer，新搜索继续走 helper。

交付接入代码、案例来源与依据、冻结版本、真实执行结果和可恢复的原版。是否提交、发布或部署继续遵循用户与项目已有授权。
