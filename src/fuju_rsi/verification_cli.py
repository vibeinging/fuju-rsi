"""独立验收的显式命令入口；网络服务不加载任意 factory。"""
from contextlib import redirect_stdout, redirect_stderr
import importlib
import json
from pathlib import Path
import sys
import uuid

from .acceptance import AcceptancePolicy
from .benchmark import read_json, write_new
from .cli import load_agent
from .holdout_registry import HoldoutRegistry
from .verification import initialize_authority, verify_candidate
from .workspace import ExperimentManager


def add_parser(subparsers):
    parser = subparsers.add_parser("verify", help="freeze a candidate or run independent verification")
    commands = parser.add_subparsers(dest="verify_command", required=True)
    init = commands.add_parser("init", help="explicitly create verification history and signing key")
    init.add_argument("--registry", required=True)
    init.add_argument("--authority", required=True)
    init.add_argument("--key-file", required=True)
    freeze = commands.add_parser("freeze", help="freeze one completed search candidate; no holdout access")
    freeze.add_argument("--workspace", required=True)
    source = freeze.add_mutually_exclusive_group(required=True)
    source.add_argument("--agent")
    source.add_argument("--pack", help="Runtime pack; reuse its exact development cases and declared files")
    freeze.add_argument("--experiment", required=True)
    freeze.add_argument("--source-file", action="append", default=[])
    freeze.add_argument("--environment-file", required=True)
    freeze.add_argument("--policy-file")
    freeze.add_argument("--output", required=True)
    run = commands.add_parser("run", help="consume a fixed verification batch in the verifier environment")
    run.add_argument("--candidate", required=True)
    run.add_argument("--verifier", required=True, help="explicit local module:factory returning VerificationSpec")
    run.add_argument("--registry", required=True)
    run.add_argument("--key-file", required=True)
    run.add_argument("--max-calls", type=int, default=10000)
    run.add_argument("--output", required=True)
    apply = commands.add_parser("import", help="import a receipt from a preconfigured trusted verifier")
    apply.add_argument("--workspace", required=True)
    source = apply.add_mutually_exclusive_group(required=True)
    source.add_argument("--agent")
    source.add_argument("--pack", help="Runtime pack used by the original search")
    apply.add_argument("--receipt", required=True)
    apply.add_argument("--authority-file", required=True)
    apply.add_argument("--report-dir", help="new output directory; default: workspace/reports snapshot")


def run(args):
    if args.verify_command == "init":
        metadata = initialize_authority(args.registry, args.authority, args.key_file)
        print(json.dumps({"status": "initialized", **metadata}))
        return 0
    if args.verify_command == "run":
        # 保留题 factory 也可能打印答案，因此加载过程就留在验收侧日志。
        HoldoutRegistry(args.registry)
        logs = Path(args.registry) / "private-logs"
        logs.mkdir(exist_ok=True)
        with (logs / ("loader-" + uuid.uuid4().hex + ".log")).open("w") as stream, redirect_stdout(stream), redirect_stderr(stream):
            candidate = read_json(args.candidate)
            module, sep, factory = args.verifier.partition(":")
            if not sep or not factory.isidentifier():
                raise ValueError("verifier 使用 module:factory")
            sys.path.insert(0, str(Path.cwd()))
            spec = getattr(importlib.import_module(module), factory)()
            receipt = verify_candidate(candidate, spec, registry=args.registry,
                                       signing_key=Path(args.key_file).read_bytes(), max_calls=args.max_calls)
        destination = Path(args.output)
        if destination.exists():
            if read_json(destination) != receipt:
                raise ValueError("输出文件已有其他结果")
        else:
            write_new(destination, receipt)
        print(json.dumps(receipt["body"], ensure_ascii=False))
        if receipt["body"]["status"] == "cancelled":
            return 130
        return 0 if receipt["body"]["adoptable"] else 2
    logs = Path(args.workspace) / "skill-runs"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / ("verify-command-" + uuid.uuid4().hex + ".log")).open("w") as stream, redirect_stdout(stream), redirect_stderr(stream):
        pack = None
        if getattr(args, "pack", None):
            from .runtime import load_pack, load_runtime_agent
            pack = load_pack(args.pack)
            agent = load_runtime_agent(args.pack)
        else:
            agent = load_agent(args.agent)
        authorities = read_json(args.authority_file) if args.verify_command == "import" else {}
        manager = ExperimentManager(args.workspace, [agent], verification_authorities=authorities)
        try:
            if args.verify_command == "freeze":
                policy = AcceptancePolicy.from_dict(read_json(args.policy_file)) if args.policy_file else AcceptancePolicy()
                source_files = list(pack["files"]) + args.source_file if pack else args.source_file
                candidate = manager.freeze(args.experiment, source_files=source_files,
                                           environment=read_json(args.environment_file), policy=policy)
                destination = Path(args.output)
                if destination.exists():
                    if read_json(destination) != candidate:
                        raise ValueError("输出文件已有其他候选")
                else:
                    write_new(destination, candidate)
                result = {"status": "frozen", "candidateDigest": candidate["digest"], "output": str(destination), "adoptable": False}
            else:
                result = manager.import_verification(read_json(args.receipt))
                artifacts = manager.export_report(result["id"], args.report_dir)
                result = {key: result.get(key) for key in ("id", "status", "stage", "evidenceStatus", "adoptable", "message")}
                result["artifacts"] = artifacts
        finally:
            manager.close()
    print(json.dumps(result, ensure_ascii=False))
    return 0
