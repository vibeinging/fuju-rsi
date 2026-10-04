"""项目内首次接入：保存测试连接，检查缺项，再复用既有开发评测。

这里只生成连接骨架，不猜业务入口或答案。原版每次使用新实验目录，问数
验证账本则始终复用，避免旧 active prompt 和重复接入改变证据含义。
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib
import inspect
import json
import os
from pathlib import Path
import re
import sys
import traceback
import uuid

from .benchmark import encode, load_bundle, read_json
from .output import private_output


CONNECTION = Path(".fuju-rsi/connection")
PROFILE = CONNECTION / "connection.json"
FIELDS = {"schemaVersion", "kind", "agent", "adapterDir", "benchmark", "validationLedger"}
REFERENCE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*\Z", re.ASCII)


class ConnectionError(ValueError):
    """只存可展示的缺项说明；业务异常正文留在本地日志。"""


def add_parser(subparsers):
    parser = subparsers.add_parser("connect", help="inspect a project, prepare its test connection, and run a baseline")
    actions = parser.add_subparsers(dest="connect_action", required=True)
    for action in ("inspect", "init", "check", "baseline"):
        item = actions.add_parser(action)
        item.add_argument("--project", default=".", help="business project directory")
        if action == "init":
            item.add_argument("--kind", choices=["custom", "ask-data"], required=True)
            item.add_argument("--agent", help="reuse an existing local module:factory")
            item.add_argument("--adapter-dir", help="directory containing the existing local adapter")
            item.add_argument("--benchmark", help="existing frozen development bundle")
            item.add_argument("--validation-ledger", help="existing shared Ask Data validation ledger")
        if action == "baseline":
            item.add_argument("--max-calls", type=int, default=100, help="runner callback limit; not a money limit")
    return parser


def project_path(value):
    project = Path(value).expanduser().resolve(strict=True)
    if not project.is_dir():
        raise ConnectionError("project 必须是已存在的业务项目目录。")
    return project


def local_path(project, relative):
    """写入位置受项目约束，已有软链接不能把元数据导到别处。"""
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConnectionError("连接文件必须位于业务项目内。")
    path = project / relative
    current = path
    while current != project:
        if current.is_symlink():
            raise ConnectionError("连接目录或文件不能是软链接。")
        current = current.parent
    return path


def path_value(project, value):
    if not isinstance(value, str) or not value.strip():
        raise ConnectionError("连接路径必须是非空字符串。")
    path = Path(value).expanduser()
    return path if path.is_absolute() else project / path


def inspect_project(project):
    # 只枚举少量路径线索，不打开 .env、案例正文或未知脚本，不执行项目命令。
    markers = [name for name in ("AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml",
                                 "package.json", "go.mod", "Cargo.toml") if (project / name).is_file()]
    tests, entries = [], []
    excluded = {".git", ".fuju-rsi", ".venv", "venv", "node_modules", "vendor", "dist", "build", "__pycache__"}
    visited = 0
    for folder, directories, files in os.walk(str(project), followlinks=False):
        directories[:] = sorted(name for name in directories if name not in excluded and not name.startswith(".")
                                and not (Path(folder) / name).is_symlink())
        visited += 1
        if visited > 250:
            break
        for name in sorted(files):
            path = Path(folder) / name
            if path.is_symlink():
                continue
            relative = path.relative_to(project)
            if (name.startswith("test_") or ".test." in name or ".spec." in name
                    or name == "tests.json" or name.endswith("-tests.json")) and len(tests) < 30:
                tests.append(str(relative))
            if name in ("app.py", "main.py", "server.py", "agent.py", "main.go", "index.ts", "server.ts") and len(entries) < 20:
                entries.append(str(relative))
    return {"status": "inspected", "project": str(project), "sdkInstalled": True,
            "projectFiles": markers, "possibleEntries": entries, "existingTests": tests,
            "connectionExists": local_path(project, PROFILE).is_file(), "executedProjectCode": False,
            "message": "这些路径只是线索；由编码 Agent 核对真实入口、配置和预期依据。"}


def adapter_template(kind):
    return '''"""测试侧连接骨架；编码 Agent 按项目真实接口填写，业务程序不导入此文件。"""
import os
from pathlib import Path
from fuju_rsi import AgentSpec, Example, Prediction, Evaluation
from fuju_rsi.benchmark import load_bundle


def baseline_prompt():
    # 读取业务原有配置；配置候选比较另走 compare-files，不把 JSON 当提示词。
    raise NotImplementedError("填写原版提示词来源")


def development_examples():
    bundle = os.environ.get("FUJU_RSI_BENCHMARK")
    if bundle:
        _manifest, rows = load_bundle(bundle, role="development")
        return [Example(**row) for row in rows]
    # 复用已有测试及有来源的答案；不要在这里编造案例、答案或未见题。
    raise NotImplementedError("绑定已有开发案例")


def runner(prompt, request):
    # 调用真实函数/命令/测试 HTTP；隔离状态并读取完整结果，设置超时。
    # 业务失败返回 Prediction；认证、超时和执行异常抛异常。未知费用留空。
    raise NotImplementedError("绑定真实执行入口")


def evaluate(expected, prediction):
    # 复用独立业务评分；问数使用结构化评分并核对最终答复，勿比较 SQL 文本。
    raise NotImplementedError("绑定已有评分依据")


def propose(*args):
    raise RuntimeError("首次只跑原版；候选由编码 Agent 另行提供")


def build_agent():
    # 工厂只配置测试连接；导入/初始化不能执行模型或有副作用的业务操作。
    return AgentSpec(id="project-agent", name="项目 Agent", kind=%r,
                     baseline_prompt=baseline_prompt(), examples=development_examples(),
                     runner=runner, evaluator=evaluate, proposer=propose)
''' % kind


def load_profile(project):
    path = local_path(project, PROFILE)
    if not path.is_file():
        raise ConnectionError("尚未准备连接；先运行 connect init。")
    if path.stat().st_size > 32768:
        raise ConnectionError("连接配置过大。")
    profile = read_json(path)
    if (type(profile) is not dict or set(profile) != FIELDS or type(profile["schemaVersion"]) is not int
            or profile["schemaVersion"] != 1 or profile["kind"] not in ("custom", "ask-data")
            or not isinstance(profile["agent"], str) or not REFERENCE.fullmatch(profile["agent"])):
        raise ConnectionError("连接配置格式无效；只保存测试路径和 factory，不保存账号密钥。")
    adapter = path_value(project, profile["adapterDir"]).resolve(strict=True)
    if not adapter.is_dir() or (adapter != project and project not in adapter.parents):
        raise ConnectionError("适配器必须在业务项目内的测试目录。")
    for field in ("benchmark", "validationLedger"):
        if profile[field] is not None:
            path_value(project, profile[field])
    if profile["kind"] != "ask-data" and profile["validationLedger"] is not None:
        raise ConnectionError("验证账本只用于 ask-data；不能通过改 kind 绕过场景规则。")
    return profile, adapter


def initialize(project, args):
    path = local_path(project, PROFILE)
    if path.exists():
        profile, _adapter = load_profile(project)
        requested = {"agent": args.agent, "adapterDir": args.adapter_dir,
                     "benchmark": args.benchmark, "validationLedger": args.validation_ledger}
        if profile["kind"] != args.kind or any(value is not None and profile[key] != value for key, value in requested.items()):
            raise ConnectionError("已有连接与请求不同；检查现有配置，不覆盖它或重建账本。")
        return {"status": "reused", "connection": str(path), "adoptable": False,
                "message": "保留已有适配器、题集和账本；继续 connect check。"}
    if args.adapter_dir and not args.agent:
        raise ConnectionError("复用适配器目录时必须提供 --agent。")
    agent = args.agent or "fuju_connection_adapter:build_agent"
    if not REFERENCE.fullmatch(agent):
        raise ConnectionError("agent 必须使用 module:factory 格式。")
    adapter_dir = args.adapter_dir or ("." if args.agent else str(CONNECTION))
    adapter = path_value(project, adapter_dir).resolve()
    if adapter != project and project not in adapter.parents:
        raise ConnectionError("适配器必须在业务项目内的测试目录。")
    if args.agent and not adapter.is_dir():
        raise ConnectionError("复用的适配器目录必须已存在；请核对目录后重新接入。")
    if args.kind != "ask-data" and args.validation_ledger:
        raise ConnectionError("验证账本只用于 ask-data。")
    template = local_path(project, CONNECTION / "fuju_connection_adapter.py")
    if not args.agent and template.exists():
        raise ConnectionError("已有适配器骨架，不能覆盖；检查文件后复用 --agent。")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not args.agent:
        with template.open("x", encoding="utf-8") as stream:
            stream.write(adapter_template(args.kind))
    profile = {"schemaVersion": 1, "kind": args.kind, "agent": agent, "adapterDir": adapter_dir,
               "benchmark": args.benchmark, "validationLedger": args.validation_ledger}
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(profile, ensure_ascii=False, indent=2) + "\n")
    return {"status": "prepared", "connection": str(path),
            "adapter": str(template) if not args.agent else None, "adoptable": False,
            "message": "编码 Agent 填写真实入口、原版配置与已有评分后运行 connect check；骨架尚未接通。"}


def prerequisites(project, profile):
    benchmark = path_value(project, profile["benchmark"]).resolve() if profile["benchmark"] else None
    ledger = path_value(project, profile["validationLedger"]).resolve() if profile["validationLedger"] else None
    if profile["kind"] == "ask-data" and (not benchmark or not ledger):
        raise ConnectionError("问数首次运行需要已有冻结开发包和共用验证账本；不能生成假答案或重置次数。")
    manifest, rows = load_bundle(benchmark, role="development") if benchmark else (None, None)
    if profile["kind"] == "ask-data":
        if manifest["schemaVersion"] != 2 or manifest.get("scoring") != "ask-data-structured-v1":
            raise ConnectionError("问数连接需要 v2 结构化开发题集。")
        from .ask_data_validation import ValidationLedger
        ValidationLedger(ledger)  # 检查已有账本；不初始化、不预留、不清空。
    return benchmark, ledger, manifest, rows


@contextmanager
def project_imports(project, adapter, benchmark):
    previous_path, previous_cwd = list(sys.path), Path.cwd()
    previous_bundle = os.environ.get("FUJU_RSI_BENCHMARK")
    try:
        os.chdir(str(project))
        sys.path[:0] = [str(adapter), str(project)]
        if benchmark:
            os.environ["FUJU_RSI_BENCHMARK"] = str(benchmark)
        else:
            os.environ.pop("FUJU_RSI_BENCHMARK", None)
        importlib.invalidate_caches()
        yield
    finally:
        sys.path[:] = previous_path
        os.chdir(str(previous_cwd))
        if previous_bundle is None:
            os.environ.pop("FUJU_RSI_BENCHMARK", None)
        else:
            os.environ["FUJU_RSI_BENCHMARK"] = previous_bundle


def check_connection(project, profile, adapter, benchmark, manifest, rows):
    from .cli import load_agent
    from .core import validate_spec
    from .skill_runner import callback_is_stub
    logs = local_path(project, CONNECTION / "logs")
    logs.mkdir(exist_ok=True, mode=0o700)
    log = logs / ("check-" + uuid.uuid4().hex + ".log")
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream, private_output(stream), project_imports(project, adapter, benchmark):
            try:
                spec = load_agent(profile["agent"])
                validate_spec(spec)
                if spec.kind != profile["kind"] or spec.candidate_kind != "prompt":
                    raise ConnectionError("工厂类型与连接不符；文件候选另走 compare-files。")
                if callback_is_stub(spec.runner) or callback_is_stub(spec.evaluator):
                    raise ConnectionError("执行或评分骨架仍未填写。")
                if rows is not None:
                    keys = rows[0].keys()
                    if encode([{key: getattr(example, key) for key in keys} for example in spec.examples]) != encode(rows):
                        raise ConnectionError("工厂案例与冻结开发包不一致。")
                    scorer = inspect.getsourcefile(spec.evaluator)
                    if not scorer or Path(scorer).resolve() != Path(manifest["scorer"]["path"]).resolve():
                        raise ConnectionError("评分函数不是冻结开发包登记的文件。")
                counts = {split: sum(example.split == split for example in spec.examples)
                          for split in ("train", "validation")}
            except BaseException:
                traceback.print_exc(file=stream)
                raise
    except KeyboardInterrupt:
        raise
    except BaseException:
        return {"status": "blocked", "adoptable": False, "executionChecked": False,
                "runtimeLog": str(log), "message": "接入检查未通过；编码 Agent 查看本地日志补齐连接，尚未调用 runner。"}
    return {"status": "ready", "adoptable": False, "executionChecked": False,
            "counts": counts, "plannedRunnerCalls": sum(counts.values()), "runtimeLog": str(log),
            "message": "工厂和开发材料可加载；尚未执行业务或证明正确性。下一步运行 connect baseline。"}


def run(args):
    try:
        project = project_path(args.project)
        if args.connect_action == "inspect":
            result = inspect_project(project)
        elif args.connect_action == "init":
            result = initialize(project, args)
        else:
            profile, adapter = load_profile(project)
            # 冻结评分路径可能相对业务项目，不能按启动 CLI 的目录校验。
            with project_imports(project, adapter, None):
                benchmark, ledger, manifest, rows = prerequisites(project, profile)
            if args.connect_action == "check":
                result = check_connection(project, profile, adapter, benchmark, manifest, rows)
            else:
                if not 0 <= args.max_calls <= 10000:
                    raise ConnectionError("max-calls 必须在 0 到 10000 之间，它不是金额上限。")
                from .skill_runner import main as run_skill
                workspace = local_path(project, Path(".fuju-rsi/baselines") / uuid.uuid4().hex)
                argv = ["--agent", profile["agent"], "--workspace", str(workspace),
                        "--max-calls", str(args.max_calls), "--name", "项目原版评测"]
                if benchmark:
                    argv += ["--benchmark", str(benchmark)]
                if ledger:
                    argv += ["--validation-ledger", str(ledger)]
                if profile["kind"] == "ask-data":
                    argv += ["--scenario", "ask-data"]
                with project_imports(project, adapter, benchmark):
                    return run_skill(argv, baseline_only=True, expected_kind=profile["kind"])
    except KeyboardInterrupt:
        return 130
    except ConnectionError as exc:
        result = {"status": "blocked", "adoptable": False, "message": str(exc)}
    except (ValueError, OSError, RuntimeError, TypeError):
        result = {"status": "blocked", "adoptable": False,
                  "message": "项目、连接配置或开发材料不可用；请核对路径、格式和原有账本。"}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if result["status"] == "blocked" else 0
