# 可选的产品后台改进

用户要求调试后继续运行改进时，使用本流程。当前自动修改范围仍只有提示词；经验提取、反馈自动入库、通用代码优化与自动灰度发布尚未提供。默认无常驻服务，由用户自己的后台任务或 CI 执行一次有限作业。

安装 `fuju-rsi` 后使用 `fuju-rsi runtime` 命令，无需网站、HTTP 或 TraceDB。始终在业务测试项目根目录执行，工具安装于测试环境。

## 导出与运行

复用已调试的 `module:factory` 和 v2 development Benchmark。factory/proposer 在后台必须可调用，不依赖当前聊天会话；factory 导入时不执行付费任务。回调仅接收训练反馈，模型凭据通过用户原有配置提供，不写入包。

```bash
fuju-rsi runtime export --agent tune_agent:build_agent --benchmark benchmarks/development-v1 --source-file app/agent.py --output improvement-v1 --max-trials 3 --max-calls 100 --max-runs 3 --total-calls 300
fuju-rsi runtime init --pack improvement-v1 --workspace worker-v1
fuju-rsi runtime run --pack improvement-v1 --workspace worker-v1 --run-id change-001
fuju-rsi runtime status --pack improvement-v1 --workspace worker-v1
```

包只复制开发 Benchmark 的已知 JSON 文件，不复制目录内的其他文件。源码、依赖和环境留在项目中；复制项目后保持相对路径可复用。runner/evaluator/proposer 与 factory 的源文件自动纳入摘要，额外业务依赖用 `--source-file` 声明。摘要不能证明所有环境依赖；代码或评分改变后导出新版本。

每次 `run` 是一个独立后台作业，命令等待作业结束，stdout 返回汇总与报告路径。相同 `run-id` 只返回原记录，不重跑 runner/proposer；不同编号消耗新的预留额度。不要为了获得更好结果不断换编号重试。

`max-calls` 是单轮 runner/proposer 回调限制。`total-calls` 按每轮的整个上限预留，失败、取消或崩溃也不退还；实际调用数另列。评分、验收、回调内部模型调用不在此额度内，不是金额硬上限。预算仅管理此工作区；新建工作区不构成全局限额。没有取消回调内部阻塞 I/O 的能力，业务回调须设置超时；进程强制停止交给宿主任务系统。

同工作区拒绝并发 worker。账本缺失或损坏时停止，不能重新初始化旧目录。重启后未完成的作业标为中断，保留预留额度；明确调查后决定新作业，不能自动重试可能有副作用的调用。

问数开发包在初始化时还必须传 `--validation-ledger PATH`，绑定项目已经使用的共用验证账本。后续 `runtime run` 使用该绑定，不会创建新账本或重置验证次数；同一 run-id 仍然幂等。账本身份或固定上限变化时停止运行。Runtime 导出和运行会把已校验的开发包路径提供给 factory，无需另设 `FUJU_RSI_BENCHMARK`。

退出码：0 完整作业（包括无提升），2 未完成/中断，1 配置或预算等入口错误，130 主动取消。报告继续只包含开发/验收汇总，原始打印留在本地诊断日志。

## 验收沿用同一个包

```bash
fuju-rsi verify freeze --pack improvement-v1 --workspace worker-v1 --experiment <experimentId> --environment-file verification-environment.json --output frozen-candidate.json
```

`--pack` 自动复用搜索中的开发案例与声明源文件，避免切回 factory 内的占位题。运行条件仍由项目明确声明。后续 `verify run` 使用[独立验收流程](verification.md)；`verify import` 可以用 `--pack improvement-v1` 代替 `--agent`。独立验收文件、密钥与数据不进入改进包，不能把本地同账号协议测试描述为已建立独立业务环境。

## 显式发布与产品读回

产品已有普通配置发布方式时优先使用。SDK 另提供普通 JSON 提示词文件适配，适合愿意选择该文件格式的项目：

```bash
fuju-rsi runtime prompt-init --target product-prompt.json --agent-id <agentId> --prompt-file baseline-prompt.txt
fuju-rsi runtime prompt-status --target product-prompt.json
fuju-rsi runtime publish --pack improvement-v1 --workspace worker-v1 --experiment <experimentId> --authority-file trusted-authorities.json --target product-prompt.json --expected-version <currentVersion>
```

`prompt-init` 仅注册当前产品原版，不证明它已验收；新文件不会覆盖旧文件。`publish` 必须在持有预先登记信任配置的环境执行，现场重新核对独立验收、声明文件和产品基线。开发候选、未知签发者或产品版本变化均拒绝写入。此命令是用户明确选择发布后的步骤，不因创建改进包而自动执行。

JSON 包含 `prompt` 和 `version`，业务可用自己的 JSON 读取代码加载，无需 import Recur。每个请求固定一次读取结果，加载失败时按产品规则使用已知原版。发布使用同目录临时文件与原子替换，所有发布者须遵循同一锁与版本检查协议；本地锁不约束绕过协议的外部编辑。

命令返回 `hostLoaded=null`：只完成文件写入与读回。还要通过业务原有入口检查实际加载版本和业务结果，不能把文件发布当成部署完成。

```bash
fuju-rsi runtime rollback --target product-prompt.json --expected-version <publishedVersion>
```

只恢复上一版普通文件；回退后获得新的版本编号，防止旧发布请求覆盖它。无需原实验工作区或密钥，业务仍须再次读回。初版只保留一层回退快照，长期版本管理沿用产品原有系统。

产品采用了新基线后，更新测试 factory 并建立新的改进包；不能继续使用旧包声称在比较当前生产版本。关闭后台或移除 Recur 后，产品应继续读取普通配置运行。参考项目中的无依赖业务示例并运行真实移除验收；不要卸载用户其他项目使用的 SDK。
