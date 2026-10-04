"""Runtime 的有限作业和显式文件发布入口；stdout 仅输出汇总。"""
from .output import private_output
import json
from pathlib import Path
import tempfile

from . import runtime


def add_parser(subparsers):
    parser = subparsers.add_parser("runtime", help="export a project improvement pack, run a bounded job or publish a verified prompt")
    commands = parser.add_subparsers(dest="runtime_command", required=True)
    export = commands.add_parser("export", help="export development-only pack from the project root")
    export.add_argument("--agent", required=True)
    export.add_argument("--benchmark", required=True)
    export.add_argument("--source-file", action="append", default=[])
    export.add_argument("--output", required=True)
    export.add_argument("--max-trials", type=int, default=3)
    export.add_argument("--max-calls", type=int, default=100)
    export.add_argument("--max-runs", type=int, default=3)
    export.add_argument("--total-calls", type=int, default=300)
    for name in ("init", "run", "status"):
        command = commands.add_parser(name)
        command.add_argument("--pack", required=True)
        command.add_argument("--workspace", required=True)
        if name == "run":
            command.add_argument("--run-id", required=True)
        if name == "init":
            command.add_argument("--validation-ledger", help="shared validation history required for Ask Data packs")
    initial = commands.add_parser("prompt-init", help="register the existing product baseline as a new ordinary JSON file")
    initial.add_argument("--target", required=True)
    initial.add_argument("--agent-id", required=True)
    initial.add_argument("--prompt-file", required=True)
    publish = commands.add_parser("publish", help="explicitly publish a currently verified prompt to a product file")
    publish.add_argument("--workspace", required=True)
    source = publish.add_mutually_exclusive_group(required=True)
    source.add_argument("--agent")
    source.add_argument("--pack")
    publish.add_argument("--experiment", required=True)
    publish.add_argument("--authority-file", required=True)
    publish.add_argument("--target", required=True)
    publish.add_argument("--expected-version", required=True)
    rollback = commands.add_parser("rollback", help="restore the previous prompt file version")
    rollback.add_argument("--target", required=True)
    rollback.add_argument("--expected-version", required=True)
    status = commands.add_parser("prompt-status", help="read back the file version; does not prove the host loaded it")
    status.add_argument("--target", required=True)


def _file_result(value, target):
    return {"status": "file_ready", "agentId": value["agentId"], "version": value["version"],
            "target": str(Path(target).resolve()), "hostLoaded": None,
            "previousVersion": value["previous"]["version"] if value.get("previous") else None}


def _execute(args):
    command = args.runtime_command
    if command == "export":
        pack = runtime.export_pack(args.agent, args.benchmark, args.output, source_files=args.source_file,
                                   max_trials=args.max_trials, max_calls=args.max_calls,
                                   max_runs=args.max_runs, total_calls=args.total_calls)
        return {"status": "exported", "packDigest": pack["digest"], "limits": pack["limits"],
                "directory": str(Path(args.output).resolve())}
    if command == "init":
        return runtime.initialize_runtime(args.pack, args.workspace,
                                          validation_ledger=args.validation_ledger)
    if command == "run":
        return runtime.run_once(args.pack, args.workspace, args.run_id)
    if command == "status":
        return runtime.runtime_status(args.pack, args.workspace)
    if command == "prompt-init":
        return _file_result(runtime.initialize_prompt(args.target, agent_id=args.agent_id,
                            prompt=Path(args.prompt_file).read_text(encoding="utf-8")), args.target)
    if command == "prompt-status":
        return _file_result(runtime.prompt_status(args.target), args.target)
    if command == "rollback":
        return _file_result(runtime.rollback_prompt(args.target, expected_version=args.expected_version), args.target)
    if command == "publish":
        from .cli import load_agent
        from .benchmark import read_json
        from .workspace import ExperimentManager
        agent = runtime.load_runtime_agent(args.pack) if args.pack else load_agent(args.agent)
        manager = ExperimentManager(args.workspace, [agent], verification_authorities=read_json(args.authority_file))
        try:
            return _file_result(runtime.publish_prompt(manager, args.experiment, args.target,
                                                       expected_version=args.expected_version), args.target)
        finally:
            manager.close()
    raise ValueError("Unknown runtime command")


def run(args):
    # factory/runner 的打印可能带业务内容，放入仅本机可读的诊断文件。
    # 当前命令是独立进程；不在多任务宿主中全局 redirect_stdout。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="yitrace-runtime-", suffix=".log", delete=False) as log:
        with private_output(log):
            try:
                result = _execute(args)
                code = 0 if result.get("status") not in ("failed", "cancelled", "interrupted", "budget_exhausted") else 2
                if args.runtime_command == "run" and not result.get("searchComplete"):
                    code = 2
            except KeyboardInterrupt:
                result, code = {"status": "cancelled", "adoptable": False}, 130
            except Exception as exc:
                result, code = {"status": "failed", "adoptable": False, "errorType": type(exc).__name__,
                                "message": "Runtime 未完成；检查开发包、工作区、预算或当前可信验收。"}, 1
        result["diagnosticLog"] = log.name
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return code
