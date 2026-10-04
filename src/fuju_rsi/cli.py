"""自动优化 CLI；只从本地显式提供的 factory 注册 Agent。"""
from __future__ import annotations

import importlib
from .output import private_output
import json
import os
from pathlib import Path
import sys
import uuid

from .core import AgentSpec
from .demo import build_demo_agent
from .server import create_server
from .workspace import ExperimentManager


def add_parser(subparsers):
    parser = subparsers.add_parser("optimize", help="run bounded prompt optimization or open its local console")
    parser.add_argument("--workspace", required=True, help="directory for experiments and active prompts")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--demo", action="store_true", help="offline SQLite example with rule-generated candidates; no cloud model")
    source.add_argument("--agent", help="explicit local module:factory returning AgentSpec")
    parser.add_argument("--serve", action="store_true", help="serve the optimization console and API")
    parser.add_argument("--port", type=int, default=7880)
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"])
    parser.add_argument("--trace-db", help="optional embedded Fuju Trace data directory; requires fuju-trace-db")
    parser.add_argument("--telemetry", choices=["auto", "log"], default="auto", help="use fuju-trace when available, otherwise local JSONL")
    parser.add_argument("--trace-sqlite", help="telemetry SQLite file; default: workspace/telemetry/trace.sqlite")
    parser.add_argument("--authority-file", help="local JSON mapping trusted authority IDs to signing key files")
    parser.add_argument("--max-trials", type=int, default=3, help="candidate attempts, maximum 20")
    parser.add_argument("--max-calls", type=int, default=100, help="runner + proposer calls, maximum 10000")
    parser.add_argument("--ask-data-bundle", help="frozen v2 development bundle for kind=ask-data")
    parser.add_argument("--validation-ledger", help="shared Ask Data validation-use ledger")
    parser.add_argument("--name", help="experiment name in one-shot mode")
    parser.add_argument("--report-dir", help="new report directory in one-shot mode; default: workspace/reports snapshot")
    return parser


def load_agent(reference):
    module_name, separator, factory_name = reference.partition(":")
    if not separator or not module_name or not factory_name.isidentifier():
        raise ValueError("--agent 使用 module:factory 格式")
    # 仅 CLI 用户明确指定的模块会被导入；HTTP 不提供模块加载能力。
    current = str(Path.cwd())
    if current not in sys.path:
        sys.path.insert(0, current)
    factory = getattr(importlib.import_module(module_name), factory_name)
    agent = factory()
    if not isinstance(agent, AgentSpec):
        raise ValueError("Agent factory 必须返回 AgentSpec")
    return agent


def run(args):
    if not 1 <= args.port <= 65535:
        raise ValueError("port 必须在 1 到 65535 之间")
    if args.serve and args.report_dir:
        raise ValueError("--report-dir 用于单次执行；已有实验使用 fuju-rsi report 导出")
    if args.report_dir and (Path(args.report_dir).exists() or Path(args.report_dir).is_symlink()):
        raise ValueError("报告目录已存在；请使用新目录，不需要重跑已有实验")
    if args.ask_data_bundle:
        os.environ["FUJU_RSI_BENCHMARK"] = str(Path(args.ask_data_bundle).resolve())
    agent = build_demo_agent() if args.demo else load_agent(args.agent)
    authorities = json.loads(Path(args.authority_file).read_text()) if args.authority_file else {}
    manager = ExperimentManager(args.workspace, [agent], verification_authorities=authorities,
                                telemetry_mode=args.telemetry, trace_sqlite_path=args.trace_sqlite,
                                ask_data_bundle=args.ask_data_bundle,
                                validation_ledger=args.validation_ledger)
    db, server = None, None
    try:
        if args.trace_db:
            from fuju_trace import connect
            db = connect(path=args.trace_db)
        if args.serve:
            server = create_server(manager, host=args.host, port=args.port, trace_db=db)
            print("Fuju RSI 控制台：http://" + args.host + ":" + str(server.server_port) + "/", flush=True)
            print("工作区：" + str(manager.directory), flush=True)
            if agent.kind == "demo":
                print("离线示例：实际执行 SQLite，规则生成候选，不调用云模型。", flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            return 0
        record = manager.start(agent_id=agent.id, name=args.name, max_trials=args.max_trials, max_calls=args.max_calls)
        try:
            result = manager.wait(record["id"])
        except KeyboardInterrupt:
            manager.cancel(record["id"])
            return 130
        print(json.dumps({"id": result["id"], "status": result["status"], "message": result["message"],
                          "stage": result["stage"], "searchComplete": result["searchComplete"],
                          "evidenceStatus": result["evidenceStatus"],
                          "adoptable": result["adoptable"], "usedCalls": result["usedCalls"],
                          "artifacts": manager.export_report(result["id"], args.report_dir),
                          "record": str(manager.records_dir / (result["id"] + ".json"))}, ensure_ascii=False))
        return 0 if result["searchComplete"] else 2
    finally:
        if server:
            server.server_close()
        manager.close()
        if db:
            db.close()


def add_report_parser(subparsers):
    parser = subparsers.add_parser("report", help="export a completed experiment without running an agent or web server")
    parser.add_argument("--workspace", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--agent", help="local module:factory used by the original experiment")
    source.add_argument("--demo", action="store_true")
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--ask-data-bundle", help="frozen development bundle for an Ask Data agent factory")
    parser.add_argument("--output", help="new output directory; default: a fresh workspace/reports snapshot")
    parser.add_argument("--authority-file", help="preconfigured trusted authority IDs mapped to key files")
    return parser


def run_report(args):
    from .benchmark import read_json
    logs = Path(args.workspace) / "skill-runs"
    logs.mkdir(parents=True, exist_ok=True)
    # factory 初始化也可能打印业务信息；命令的标准输出仅包含报告文件位置。
    with (logs / ("report-" + uuid.uuid4().hex + ".log")).open("w", encoding="utf-8") as stream, private_output(stream):
        if args.ask_data_bundle:
            os.environ["FUJU_RSI_BENCHMARK"] = str(Path(args.ask_data_bundle).resolve())
        agent = build_demo_agent() if args.demo else load_agent(args.agent)
        authorities = read_json(args.authority_file) if args.authority_file else {}
        manager = ExperimentManager(args.workspace, [agent], verification_authorities=authorities)
        try:
            artifacts = manager.export_report(args.experiment, args.output)
        finally:
            manager.close()
    print(json.dumps({"id": args.experiment, "status": "exported", "artifacts": artifacts}, ensure_ascii=False))
    return 0
