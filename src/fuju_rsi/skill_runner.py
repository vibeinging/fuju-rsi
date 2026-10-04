#!/usr/bin/env python3
"""Skill 与首次接入共用的执行入口，只返回开发阶段允许看到的反馈。

场景说明由 Skill 显式传入；安装后的 SDK 只知道内置已接入的问数场景，
不读取开发仓库或编码工具目录。首次接入沿用同一套题集、评分与账本守卫。
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import sys
import textwrap
import traceback
import uuid


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, self.prog + ": error: " + message + "\n")


class SkillUsageError(ValueError):
    """可安全展示的 Skill 参数错误，不包含业务回调的异常正文。"""


def aggregate(batch):
    if batch is None:
        return None
    cases = batch["cases"]
    return {**{key: batch[key] for key in
               ("score", "passed", "total", "latencyMs", "tokens", "costUsd")},
            "evaluated": len(cases), "errors": sum(case["error"] is not None for case in cases)}


def complete(batch):
    return bool(batch and len(batch["cases"]) == batch["total"] and
                all(case["error"] is None for case in batch["cases"]))


def summarize(result, directory, run_log, candidate_count):
    baseline = next((item for item in result["trials"] if item["id"] == "baseline"), None)
    baseline_complete = bool(result["status"] == "completed" and baseline and
                             complete(baseline["training"]) and complete(baseline["validation"]))
    summary = {
        **{key: result[key] for key in
           ("id", "status", "message", "adoptable", "selectedTrialId", "usedCalls", "searchComplete", "stage", "evidenceStatus")},
        "mode": "candidates" if candidate_count else "baseline",
        "baselineComplete": baseline_complete,
        "record": str(directory / "experiments" / (result["id"] + ".json")),
        "runtimeLog": str(run_log),
        "trainingFeedback": [
            {"trialId": item["id"], "cases": item["training"]["cases"]}
            for item in result["trials"] if item["training"] is not None
        ],
        "trials": [{"id": item["id"], "label": item["label"],
                    "training": aggregate(item["training"]),
                    "validation": aggregate(item["validation"])} for item in result["trials"]],
        "baselineTest": aggregate(result["baselineTest"]),
        "candidateTest": aggregate(result["candidateTest"]),
    }
    if not candidate_count and baseline_complete:
        summary["message"] = "基线已完成，尚未比较候选或运行最终测试；请根据训练反馈生成候选。"
    if isinstance(result.get("askDataValidation"), dict):
        summary["askDataValidation"] = result["askDataValidation"]
    return summary


def builtin_scenarios():
    """SDK 内置执行范围，不把场景发现误当成任意插件执行权限。"""
    return [{"id": "ask-data", "status": "integrated"}]


def callback_is_stub(callback):
    """识别仍显式抛占位异常的适配器；通过本检查不等于已经接通业务。"""
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(callback)))
    except (OSError, TypeError, SyntaxError):
        return False
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) != 1:
        return False
    body = list(functions[0].body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    # 只识别顺序代码后必然抛占位异常的骨架，允许导入和诊断语句。
    # 遇到分支或返回就停止判断，某个不支持分支不能使整个接入被拒绝。
    for node in body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Expr, ast.Assign,
                             ast.AnnAssign, ast.AugAssign, ast.Pass)):
            continue
        if not isinstance(node, ast.Raise):
            return False
        exception = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(exception, ast.Name) and exception.id == "NotImplementedError":
            return True
        if (isinstance(exception, ast.Attribute) and exception.attr == "NotImplementedError"
                and isinstance(exception.value, ast.Name) and exception.value.id == "builtins"):
            return True
        return False
    return False


def _private_log(path, *, append=False):
    # factory 与异常日志可能包含业务材料，创建时即限制文件读取权限。
    # append 保留已有内容和权限；本地日志权限不构成独立验收的隔离证明。
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_EXCL)
    descriptor = os.open(str(path), flags, 0o600)
    return os.fdopen(descriptor, "a" if append else "w", encoding="utf-8")


def main(argv=None, *, scenario_loader=None, baseline_only=False, expected_kind=None):
    scenario_loader = scenario_loader or builtin_scenarios
    parser = Parser(description=__doc__)
    parser.add_argument("--agent", required=True, help="local module:factory returning AgentSpec")
    parser.add_argument("--scenario", help="explicit Skill scenario id; currently ask-data is executable")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--benchmark", help="optional frozen benchmark directory; verify cases and scorer before running")
    parser.add_argument("--validation-ledger", help="shared fixed-cap validation ledger; required for kind=ask-data")
    parser.add_argument("--candidate-file", action="append", default=[], help="UTF-8 full prompt; repeat for each candidate")
    parser.add_argument("--candidate-kind", choices=["prompt", "files"], default="prompt",
                        help="files uses versioned config candidate JSON and FileExperimentSpec")
    parser.add_argument("--max-calls", type=int, default=100, help="runner + proposer callbacks for this command, 0..10000")
    parser.add_argument("--name", help="experiment label, up to 120 characters")
    parser.add_argument("--report-dir", help="new output directory; default: a fresh workspace/reports snapshot")
    args = parser.parse_args(argv)
    if baseline_only and (args.candidate_file or args.candidate_kind != "prompt"):
        parser.error("baseline only accepts the original prompt; candidate files and file comparisons are not allowed")
    if not 0 <= args.max_calls <= 10000 or len(args.candidate_file) > 20:
        parser.error("max-calls must be 0..10000; at most 20 candidate files")
    if args.name is not None and len(args.name) > 120:
        parser.error("name must be at most 120 characters")
    if args.report_dir and (Path(args.report_dir).exists() or Path(args.report_dir).is_symlink()):
        parser.error("report-dir must be a new directory; use fuju-rsi report to export an existing experiment")
    if args.candidate_kind == "files":
        if args.scenario:
            selected = next((item for item in scenario_loader() if item['id'] == args.scenario), None)
            if selected is None or selected['status'] != 'integrated' or selected['id'] != 'ask-data':
                print(json.dumps({'status': 'failed', 'adoptable': False,
                                  'error': 'This Skill scenario has no executable integration yet.'}))
                return 1
        try:
            from fuju_rsi.file_cli import run as run_files
        except ImportError:
            print(json.dumps({'status': 'failed', 'error': 'Install the Fuju RSI package in this Python environment.'}))
            return 1
        args.ask_data_bundle = args.benchmark
        args.skill_feedback = True
        return run_files(args)
    candidates = []
    for path in args.candidate_file:
        try:
            with Path(path).open(encoding="utf-8") as stream:
                prompt = stream.read(32769)
            if not prompt.strip() or len(prompt) > 32768:
                raise ValueError("invalid prompt length")
        except (OSError, UnicodeError, ValueError):
            # 不回显文件内容或异常正文，避免把凭据/业务材料混进命令摘要。
            print(json.dumps({"status": "failed", "error": "Candidate files must contain 1..32768 characters of UTF-8 prompt text."}))
            return 1
        candidates.append(prompt)
    try:
        from fuju_rsi.cli import load_agent
        from fuju_rsi.workspace import ExperimentManager, atomic_json
        from fuju_rsi.output import private_output
    except ImportError:
        print(json.dumps({"status": "failed", "error": "Install the Fuju RSI package in this Python environment."}))
        return 1

    directory = Path(args.workspace).resolve()
    run_log = directory / "skill-runs" / (uuid.uuid4().hex + ".log")
    benchmark = None
    try:
        run_log.parent.mkdir(parents=True, exist_ok=True)
        # 业务函数可能 print 输入/答案；收进本地日志，不能泄漏到候选生成反馈中。
        with _private_log(run_log) as stream, private_output(stream):
            if args.scenario:
                selected = next((item for item in scenario_loader() if item["id"] == args.scenario), None)
                if selected is None:
                    raise SkillUsageError("Unknown Skill scenario.")
                if selected["status"] != "integrated" or selected["id"] != "ask-data":
                    raise SkillUsageError("This Skill scenario has no executable integration yet.")
            if args.benchmark:
                from fuju_rsi.benchmark import BenchmarkError, encode, load_bundle, read_json
                # role="development" 在读取案例正文之前就拒绝 holdout 包。
                manifest, benchmark_examples = load_bundle(args.benchmark, role="development")
            if args.benchmark:
                # factory 与 helper 读取同一个已校验开发包，避免接入方另配错版本。
                os.environ["FUJU_RSI_BENCHMARK"] = str(Path(args.benchmark).resolve())
            spec = load_agent(args.agent)
            if baseline_only and spec.candidate_kind != "prompt":
                raise SkillUsageError("The first baseline requires a prompt AgentSpec; configuration files use compare-files.")
            if baseline_only and (callback_is_stub(spec.runner) or callback_is_stub(spec.evaluator)):
                raise SkillUsageError("The runner or evaluator adapter is still a placeholder; connect the existing business entry and scoring before running a baseline.")
            if expected_kind is not None and spec.kind != expected_kind:
                raise SkillUsageError("AgentSpec.kind does not match the saved connection kind.")
            if args.scenario and spec.kind != args.scenario:
                raise SkillUsageError("The selected Skill scenario does not match AgentSpec.kind.")
            if spec.kind not in ("ask-data", "custom", "demo"):
                raise SkillUsageError("AgentSpec.kind has no executable Skill scenario.")
            if spec.kind == "ask-data":
                if not args.benchmark or not args.validation_ledger:
                    raise SkillUsageError("Ask Data requires --benchmark and --validation-ledger before any run.")
                if manifest["schemaVersion"] != 2 or manifest["scoring"] != "ask-data-structured-v1":
                    raise SkillUsageError("Ask Data requires a frozen v2 Ask Data development benchmark.")
            elif args.validation_ledger:
                raise SkillUsageError("--validation-ledger is only for kind=ask-data.")
            if args.benchmark:
                keys = benchmark_examples[0].keys()
                actual = [{key: getattr(example, key) for key in keys} for example in spec.examples]
                if encode(actual) != encode(benchmark_examples):
                    raise BenchmarkError("Factory examples must match the frozen benchmark")
                scorer_source = inspect.getsourcefile(spec.evaluator)
                if not scorer_source or Path(scorer_source).resolve() != Path(manifest["scorer"]["path"]).resolve():
                    raise BenchmarkError("Scorer file must define the registered evaluator function")
                if manifest["schemaVersion"] == 1:
                    # 旧包仍可开发回归；其中所有题已被 factory 读取，不能宣称盲测。
                    book = read_json(Path(args.benchmark) / "casebook.json")
                    by_id = {case["id"]: case for case in book["cases"]}
                    spec = replace(spec, examples=[replace(e, group_id=by_id[e.id]["group"], exposure="development") for e in spec.examples])
                benchmark = {key: manifest[key] for key in ("id", "version", "digest")}

            def proposer(_prompt, _training_feedback, trial_index):
                return candidates[trial_index - 1]

            spec = replace(spec, proposer=proposer)
            manager = ExperimentManager(
                directory, [spec],
                ask_data_bundle=args.benchmark if spec.kind == "ask-data" else None,
                validation_ledger=args.validation_ledger if spec.kind == "ask-data" else None)
            try:
                # 首次评测以产品 factory 读取到的原版为准。旧工作区的采用指针
                # 属于实验状态，不能悄悄替换产品当前配置；connect 使用新工作区。
                if baseline_only and manager.active_prompt(spec.id)["prompt"] != spec.baseline_prompt:
                    raise SkillUsageError("The workspace baseline differs from the product prompt; use a new workspace while reusing the shared validation ledger.")
                name = args.name
                if benchmark:
                    archived = directory / "benchmarks" / (benchmark["digest"] + ".json")
                    archived.parent.mkdir(exist_ok=True)
                    atomic_json(archived, manifest)
                    benchmark["manifest"] = str(archived)
                    name = (benchmark["id"] + "@" + benchmark["version"] + (" · " + name if name else ""))[:120]
                record = manager.start(agent_id=spec.id, name=name,
                                       max_trials=len(candidates), max_calls=args.max_calls)
                try:
                    if benchmark:
                        receipt = directory / "skill-runs" / (record["id"] + ".benchmark.json")
                        atomic_json(receipt, {"experimentId": record["id"], **benchmark})
                        benchmark["receipt"] = str(receipt)
                    result = manager.wait(record["id"])
                    artifacts = manager.export_report(record["id"], args.report_dir)
                except KeyboardInterrupt:
                    manager.cancel(record["id"])
                    raise
            finally:
                manager.close()
    except KeyboardInterrupt:
        print(json.dumps({"status": "cancelled", "adoptable": False, "runtimeLog": str(run_log)}))
        return 130
    except SkillUsageError as exc:
        print(json.dumps({"status": "failed", "adoptable": False, "error": str(exc),
                          "runtimeLog": str(run_log)}))
        return 1
    except Exception as exc:
        try:
            with _private_log(run_log, append=True) as stream:
                traceback.print_exc(file=stream)
        except OSError:
            pass
        print(json.dumps({"status": "failed", "adoptable": False, "errorType": type(exc).__name__,
                          "error": "Check the SDK, local factory, benchmark cases/scorer, and workspace lock. No prompt was adopted.",
                          "runtimeLog": str(run_log)}))
        return 1
    summary = summarize(result, directory, run_log, len(candidates))
    if baseline_only and summary["baselineComplete"]:
        summary["message"] = "原版评测已完成；请核对报告中的通过、失败和运行异常。本次未比较候选或执行独立验收。"
    summary["artifacts"] = artifacts
    if benchmark:
        summary["benchmark"] = benchmark
    print(json.dumps(summary, ensure_ascii=False, allow_nan=False))
    if result["status"] == "cancelled":
        return 130
    if result["status"] != "completed":
        return 1
    return 0 if result.get("searchComplete") else 2


if __name__ == "__main__":
    sys.exit(main())
