# 固定候选后的独立验收

搜索完成只表示找到一个开发候选。独立验收固定唯一候选，比较新案例上的原版和候选，再把汇总回执交给可信登记环境。下面命令已对应当前 SDK 的 `fuju-rsi verify`；不会创建 CI 账号、配置文件权限或部署产品。

## 环境与身份

准备三个职责边界：开发环境生成候选；验收环境持有未见题、使用历史与签名密钥；可信登记环境核验回执并更新实验。后两者可以由同一受控 CI 管理。相同账号下的目录或子进程只能作为 workflow_only 的流程检查。

HMAC 使用共享密钥，拥有验签密钥的人也能签署结果。密钥和 authority-file 必须由项目管理者预先配置，不能随候选包或 skill 交给调优 Agent。签名证明回执来自登记的密钥持有者，不证明答案正确、数据独立或对方真的使用了声明的权限边界。

以下命令使用 `YT_VERIFY_HOME` 指向项目已经配置的验收私有目录，`YT_HOLDOUT_BUNDLE` 指向其 v2 holdout 包；这些变量由验收环境提供。所有命令都在项目根目录运行，相对源文件路径须与开发时相同；runner/evaluator 按角色绑定具体函数身份，不能换成同文件中的另一函数。复制源码到验收环境可以更换机器绝对路径，但必须保持文件内容和相对布局。

## 1. 显式建立验收记录

仅在首次建立这个项目的验收入口时，在验收账号运行：

```bash
fuju-rsi verify init --registry "$YT_VERIFY_HOME/history" --authority project-ci --key-file "$YT_VERIFY_HOME/project-ci.key"
```

同一项目的不同实验工作区继续共用该 history。已有、缺失部分文件或损坏的历史拒绝重新初始化；不能通过新建空目录冒充从未使用过测试集。中断任务继续占用这批内容。正常完成的同一固定候选可取回已保存结果，不重新跑题；其他候选不能复用重叠输入、来源或分组。

本地记录不能全局识别另建项目、换机器、恶意重写或整体历史回滚。报告其实际覆盖的项目和环境，不能把“没找到历史”当成独立性证明。

## 2. 在开发环境固定唯一候选

先按 SKILL.md 完成搜索，取得 stdout 的 id。准备非空 `evals/environment.json`，记录实际模型、工具、数据、运行方式等条件。示例命令沿用 `tune_agent.py` 适配器、`app.py` 业务入口与原实验工作区；把实验 id 替换为真实结果：

```bash
fuju-rsi verify freeze --workspace .yitrace-optimization --agent tune_agent:build_agent --experiment <实验id> --source-file tune_agent.py --source-file app.py --environment-file evals/environment.json --output artifacts/candidate.json
```

命令要求完整搜索选出的新候选，基线、Example 元数据和 runner/evaluator 文件均未变化。候选包固定提示词、开发输入与来源摘要、代码文件、运行条件及验收策略；不读取 holdout。

`--source-file` 可重复，必须包含定义实际 runner、evaluator 的文件，并显式列出影响结果的其他源文件。工具不会自动追踪全部依赖。环境 JSON 是声明，不是自动采集或证明。将 artifacts/candidate.json 及固定代码交给已有验收流程，不附带修改过的评分或新的提示词。

默认使用 `AcceptancePolicy()`。若项目要自定义，搜索结束且未查看验收题或结果前准备 policy JSON，并给 freeze 增加 `--policy-file evals/policy.json`；验收 factory 必须使用完全相同的策略。已固定实验不能事后换策略或环境，输出文件也不能被其他候选覆盖。

## 3. 验收环境运行固定计划

在验收环境配置明确的 `verify_agent:build_verifier`。以下骨架复用已固定的运行和评分函数；holdout 包必须由实际独立流程建立并核对，不能让编码 Agent 为当前候选生成已知答案的“盲测”：

```python
# verify_agent.py，留在验收环境
import os
from pathlib import Path
from fuju_rsi import AcceptancePolicy, Example, VerificationSpec
from fuju_rsi.benchmark import load_bundle, read_json
from tune_agent import runner, evaluate, reset


def build_verifier():
    # role="holdout" 必须由验收侧显式声明：load_bundle 默认拒绝验收包，在读取案例正文前失败。
    manifest, cases = load_bundle(os.environ["YT_HOLDOUT_BUNDLE"], role="holdout")
    return VerificationSpec(
        examples=[Example(**item) for item in cases],
        runner=runner, evaluator=evaluate, reset=reset,
        policy=AcceptancePolicy(),
        isolation=os.environ.get("YT_VERIFICATION_ISOLATION", "workflow_only"),
        provenance=os.environ.get("YT_VERIFICATION_PROVENANCE", ""),
        environment=read_json(Path("evals/environment.json")),
    )
```

默认 workflow_only，即使分数改善也不能采用。只有项目已建立实际独立账号/CI 边界后，验收环境才声明 `YT_VERIFICATION_ISOLATION=independent`，并在 `YT_VERIFICATION_PROVENANCE` 中写出可核对的数据来源、访问范围和建立方式。这两个字段不能替代实际权限设置。自定义策略时，将 factory 的 policy 改为读取与 freeze 相同的固定文件。

```bash
fuju-rsi verify run --candidate artifacts/candidate.json --verifier verify_agent:build_verifier --registry "$YT_VERIFY_HOME/history" --key-file "$YT_VERIFY_HOME/project-ci.key" --max-calls 600 --output artifacts/receipt.json
```

每题每次重复都分别运行原版与候选，先 reset 状态，并交替执行顺序。默认每题两侧各 3 次，所以 runner 预算必须覆盖 `2×验收题数×3`；30 题需 180 次。预算不足会在回调前拒绝，不靠部分评分给出采用结论。runner、evaluator、reset 必须自行配置 I/O 超时。

验收 `--max-calls` **只统计 runner**，与搜索中的 runner+proposer 口径不同。评分、reset 及 runner 内部模型调用的全部费用不在这个上限内。回执 tokens/costUsd 来自 runner 已上报的用量，未知或未完成时保持未知。

题目、答案、输出和逐次评分保存在 history/private-logs 内；stdout 和回执只提供判断汇总、原因与记录摘要，不返回这些明细。退步原因只包含数量，不返回案例名或来源组名。加载 factory 的输出同样留在验收侧。不要把私有日志交给候选生成者。

run 退出码：`0` 通过并可采用；`2` 已有结果但不可采用，包括证据不足或业务退步；`1` 配置、历史或保存失败；`130` 取消。运行或取消已开始后不返还这批题的使用资格。

## 4. 可信登记环境导入回执

由项目管理者事先保存 authority-file，格式为“验收入口 id → 共享密钥文件的绝对路径”，例如：

```json
{"project-ci": "/private-verification/project-ci.key"}
```

实际路径替换为登记环境受控密钥位置。不能接受由候选生成者随回执附送的任意 authority-file 或密钥。在持有原搜索工作区、固定源码和受控密钥的登记环境执行：

```bash
fuju-rsi verify import --workspace .yitrace-optimization --agent tune_agent:build_agent --receipt artifacts/receipt.json --authority-file /private-verification/authorities.json
```

导入核对签名、实验、固定候选与当前文件摘要；同一候选不能用另一个结果覆盖。导入同时生成独立的 report.md、result.json 与普通提示词/差异文件，stdout 的 artifacts 返回路径；可用 --report-dir 指定新目录。只有当前资格通过时才额外生成 verified-prompt.txt。导入退出码 0 只表示结果已登记并导出，仍需检查返回的 adoptable。手改 JSON 中的布尔字段、旧版 adoptable=true 或未登记的回执都不能授予采用资格。

无需前端的重新导出：

```bash
fuju-rsi report --workspace .yitrace-optimization --agent tune_agent:build_agent --experiment <实验ID> --authority-file /private-verification/authorities.json --output reports/acceptance-review
```

这只读已有实验，不重跑题目。可选本地查看器也要在可信登记环境配置相同入口：

```bash
fuju-rsi optimize --workspace .yitrace-optimization --agent tune_agent:build_agent --serve --authority-file /private-verification/authorities.json
```

页面只消费已有结果，当前 HTTP 不接受任意 verifier 模块或验收数据上传。工作区采用是可选实验状态，不能当成产品已上线。

## 判定与证据边界

默认策略：至少 30 个独立来源组、每侧每题重复 3 次、置信水平 0.95、最低收益 0、允许组平均退步 0。先对案例重复运行取均值，再对同源组取均值，最后各组等权。重复次数和同源变体不会增加独立组数。

采用要求组平均收益的单侧 Hoeffding 保守下界大于预先固定的最低收益，同时无关键案例平均退步、无超出容许值的组平均退步。这个判断依赖来源组独立、评分在 [0,1] 内，以及候选和规则在验收前固定。30 组只是最低门槛，收益小或数据不稳定时仍可能证据不足。bootstrap 区间只作描述，不参与采用判断，也不能用很窄的区间跳过保守检查。

只在 `stage=verification`、`evidenceStatus=improved`、可信回执核验成功且 `adoptable=true` 时交付对应候选。同分、证据不足、退步、无效结果与中断均保留原版。合成协议测试只能证明运行与拒绝规则按预期工作，不能证明真实业务收益或独立来源声明为真。

看过汇总后继续修改，就需要新独立验收材料；原题可留作已知回归，不能重复选优直到通过。删除工作区、更换文件名或多跑几轮不改变这个要求。最终交付普通提示词和可追溯证据，业务运行与回退不依赖 yiTrace。
