# Fuju RSI 第一轮可用性与可靠性优化

日期：2026-09-26。目标：让独立拆出的 RSI 可以使用当前已发布的 Fuju Trace，观测失败不影响实验，并能验证实际 wheel 的安装与运行。

## 原实现的问题

1. 默认插件导入 `HttpExporter`，向 7878 端口发送事件；当前 Fuju Trace 已移除 HTTP exporter 和独立服务。安装新 SDK 后插件不能使用，所有观测都会退回日志。
2. `trace-db` extra 使用 `fuju-trace[db]`，当前发布的基础 SDK 没有该 extra，不能借此安装 native DB。
3. 自定义插件得到原始事件对象，若修改后返回失败或抛错，附加的字段会进入回退 JSONL。
4. log 模式写日志失败后，健康状态仍可能报告 `degraded=False`，也没有丢弃计数。
5. 接入测试使用了模拟的旧 `HttpExporter`，不能验证当前已发布 SDK；此前缺少独立的 wheel 安装验收命令。

## 已完成的修改

- `.[trace]` 依赖 `fuju-trace[sqlite]>=0.1.10,<0.2`。RSI 核心继续只用标准库；不装插件也能运行。
- `--telemetry auto` 的默认插件直接写工作区 `telemetry/trace.sqlite`，新增 `--trace-sqlite` 文件参数。移除 `--trace-url` 和 `FUJU_TRACE_URL`。
- 数据库延迟到第一条观测时打开，复用连接与 Tracer；每条观测生成的 start/end 事件一次批量提交。成功返回以数据库写入无异常为准。
- 支持 `FujuTracePlugin(db=...)` 注入已经打开的 Trace store，可用于 VexDB 等后端。插件不关闭借来的连接；默认 SQLite 连接由插件关闭。自定义插件由调用方负责关闭。
- 工作区停止时等待已有 worker；尚未结束的 worker 会在最终状态观测完成后关闭默认插件，避免过早释放连接。
- 插件只能收到状态事件副本，回退 JSONL 保留原始白名单字段。异常健康状态只记录异常类型，不记录异常文本。
- 健康状态新增 `sentEvents`、`loggedEvents`、`droppedEvents`、`closed`。log 模式写入失败会报告降级，恢复后清除当前错误，累计丢弃数仍保留。
- 保留 `--trace-db` 的可选 native DB 查询入口，但明确需另从 Fuju Trace 源码构建，不属于 `.[trace]`。删除误导性的 `trace-db` extra。
- 更新 README，并提供 `scripts/verify_python_consumer.py` 验证真实 wheel。
- 固定构建工具为 `hatchling==1.27.0`。原来未固定的版本生成 Metadata 2.5，当前 Twine 检查拒绝；重建为兼容元数据后再交付本地包。

## 验证证据

| 检查 | 结果 |
| --- | --- |
| 修改前全部 Python 测试 | 206 项通过 |
| 修改后全部 Python 测试 | 211 项通过，含 Skill 移除后运行、验收隔离及 Runtime 回归 |
| wheel / sdist 格式检查 | 固定 Hatchling 1.27.0 后，两个产物均通过 `twine check` |
| 核心 wheel 的全新 Python 3.12 环境 | 安装、`pip check`、CLI 实验、报告、控制台首页和实际静态资产通过；环境中没有 Trace SDK |
| 可选插件的全新 Python 3.12 环境 | 从官方 PyPI 安装 Trace 0.1.10 与 SQL 0.1.10，`pip check` 通过；开始/结束两条观测形成两个完整 span，SQLite 重开读取通过 |
| 注入现有数据库 | 插件关闭后数据库仍可读取；每次 ingest 批次包含 start/end 两条事件 |
| 数据库第一次写入抛错、第二次恢复 | 首条进入 JSONL；第二条进入数据库；错误批次没有残留到成功批次，健康状态恢复 |
| 已装 Trace SDK，卸载 SQL 插件 | 实际安装环境继续完成实验、写 JSONL，并成功提供控制台资源 |
| 状态隐私与生命周期 | 插件修改事件并抛错不污染日志；默认插件只关闭一次；工作区等待 worker 后关闭插件；写日志失败计入丢弃数 |

全量测试运行于 Python 3.14.6；wheel 消费验证运行于 Python 3.12。最初受沙箱限制的本地监听和测试目录写入错误，放开相应测试权限后全部通过。

复现命令：

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
python -m build
python scripts/verify_python_consumer.py dist/fuju_rsi-0.1.0-py3-none-any.whl
python scripts/verify_python_consumer.py dist/fuju_rsi-0.1.0-py3-none-any.whl --with-trace
```

## 本轮范围

此次没有改变候选选择、评分、调用预算、独立验收资格和产品提示词发布规则，也没有调用付费模型或连接真实 VexDB。VexDB 的注入示例基于同一 store 接口；本轮真实存储验证使用 SQLite，不能等同于 VexDB 联调验收。自定义插件的 `emit` 是同步调用，远程连接和调用超时由插件或调用方配置。

仓库仍处于首次拆分的未提交状态，没有执行 commit、push 或 PyPI 发布。新增的是本地测试包与消费验收工具。
