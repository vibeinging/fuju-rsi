#!/usr/bin/env python3
"""让编码 Agent 用固定候选文件调用真实优化器，只返回可用于提案的反馈。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import inspect
import json
import os
from pathlib import Path
import sys
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


def main(argv=None):
    parser = Parser(description=__doc__)
    parser.add_argument("--agent", required=True, help="local module:factory returning AgentSpec")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--benchmark", help="optional frozen benchmark directory; verify cases and scorer before running")
    parser.add_argument("--validation-ledger", help="shared fixed-cap validation ledger; required for kind=ask-data")
    parser.add_argument("--candidate-file", action="append", default=[], help="UTF-8 full prompt; repeat for each candidate")
    parser.add_argument("--max-calls", type=int, default=100, help="runner + proposer callbacks for this command, 0..10000")
    parser.add_argument("--name", help="experiment label, up to 120 characters")
    parser.add_argument("--report-dir", help="new output directory; default: a fresh workspace/reports snapshot")
    args = parser.parse_args(argv)
    if not 0 <= args.max_calls <= 10000 or len(args.candidate_file) > 20:
        parser.error("max-calls must be 0..10000; at most 20 candidate files")
    if args.name is not None and len(args.name) > 120:
        parser.error("name must be at most 120 characters")
    if args.report_dir and (Path(args.report_dir).exists() or Path(args.report_dir).is_symlink()):
        parser.error("report-dir must be a new directory; use fuju-rsi report to export an existing experiment")
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
    except ImportError:
        print(json.dumps({"status": "failed", "error": "Install the Fuju RSI package in this Python environment."}))
        return 1

    directory = Path(args.workspace).resolve()
    run_log = directory / "skill-runs" / (uuid.uuid4().hex + ".log")
    benchmark = None
    try:
        run_log.parent.mkdir(parents=True, exist_ok=True)
        # 业务函数可能 print 输入/答案；收进本地日志，不能泄漏到候选生成反馈中。
        with run_log.open("w", encoding="utf-8") as stream, redirect_stdout(stream), redirect_stderr(stream):
            if args.benchmark:
                from fuju_rsi.benchmark import BenchmarkError, encode, load_bundle, read_json
                # role="development" 在读取案例正文之前就拒绝 holdout 包。
                manifest, benchmark_examples = load_bundle(args.benchmark, role="development")
            if args.benchmark:
                # factory 与 helper 读取同一个已校验开发包，避免接入方另配错版本。
                os.environ["FUJU_RSI_BENCHMARK"] = str(Path(args.benchmark).resolve())
            spec = load_agent(args.agent)
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
            with run_log.open("a", encoding="utf-8") as stream:
                traceback.print_exc(file=stream)
        except OSError:
            pass
        print(json.dumps({"status": "failed", "adoptable": False, "errorType": type(exc).__name__,
                          "error": "Check the SDK, local factory, benchmark cases/scorer, and workspace lock. No prompt was adopted.",
                          "runtimeLog": str(run_log)}))
        return 1
    summary = summarize(result, directory, run_log, len(candidates))
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
