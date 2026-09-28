"""在验收环境运行固定候选；结果签名绑定快照，开发环境不接收题目。

签名证明结果来自登记的验收者，不证明该验收者的业务答案或隔离声明真实。
同账号的本地运行只能标为 workflow_only。独立性需要用户的 CI/账号边界。
"""
from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import os
import re
from pathlib import Path
import secrets
import time
from typing import Callable, List
import uuid

from .acceptance import AcceptancePolicy, POLICY_VERSION, compare
from .benchmark import (BenchmarkError, callback_identity, callback_snapshot, case_tokens, digest, encode,
                        read_json, text, write_new)
from .core import Example, Evaluation, _prediction, _finite_number
from .holdout_registry import HoldoutRegistry


@dataclass
class VerificationSpec:
    examples: List[Example]
    runner: Callable
    evaluator: Callable
    reset: Callable
    policy: AcceptancePolicy = field(default_factory=AcceptancePolicy)
    isolation: str = "workflow_only"
    provenance: str = ""
    environment: dict = field(default_factory=dict)


def _check_key(key):
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError("验收签名密钥至少需要 32 字节")


def initialize_authority(directory, authority_id, key_file):
    """显式初始化；密钥置于验收/登记环境，不随候选或 skill 分发。"""
    key_path = Path(key_file).resolve()
    if key_path.exists():
        raise ValueError("签名密钥文件已存在")
    metadata = HoldoutRegistry.initialize(directory, authority_id)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    import os
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(key)
        stream.flush()
        os.fsync(stream.fileno())
    return metadata


def _files(paths, root):
    root = Path(root).resolve()
    values = {}
    for filename in paths:
        path = Path(filename)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        try:
            relative = path.relative_to(root).as_posix()
            values[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except (ValueError, OSError) as exc:
            raise ValueError("快照文件必须可读且在指定项目目录内") from exc
    if not values:
        raise ValueError("至少声明目标和评分源文件")
    return dict(sorted(values.items()))


def validate_candidate(candidate, root):
    if not isinstance(candidate, dict) or candidate.get("schemaVersion") != 2:
        raise ValueError("候选包格式无效")
    if candidate.get("digest") != digest({k: v for k, v in candidate.items() if k != "digest"}):
        raise ValueError("候选包内容摘要不一致")
    for key in ("id", "experimentId", "agentId", "baselinePrompt", "candidatePrompt", "selectedTrialId"):
        text(candidate.get(key), key)
    if candidate["selectedTrialId"] == "baseline" or candidate["baselinePrompt"] == candidate["candidatePrompt"]:
        raise ValueError("没有新的固定候选")
    if _files(candidate.get("files", {}), root) != candidate["files"]:
        raise ValueError("代码或评分文件已改变，旧候选包失效")
    AcceptancePolicy.from_dict(candidate.get("policy"))
    if not isinstance(candidate.get("environment"), dict) or not candidate["environment"]:
        raise ValueError("缺少运行条件")
    if not isinstance(candidate.get("developmentTokens"), list) or not candidate["developmentTokens"]:
        raise ValueError("缺少开发样本来源记录")
    identities = candidate.get("callbackIdentity")
    if not isinstance(identities, list) or len(identities) != 2:
        raise ValueError("缺少固定的 runner/evaluator 函数身份")


def _relative_identity(callbacks, root):
    values = callback_identity(*callbacks)
    if not values:
        raise ValueError("无法确定 runner/evaluator 函数身份")
    for value in values:
        value["file"] = Path(value["file"]).relative_to(Path(root).resolve()).as_posix()
    return values


def freeze_candidate(spec, result, *, source_files, environment, policy=None, root=".",
                     development_history_tokens=None):
    """仅固定已完整搜索选出的一个候选；不会加载独立验收数据。"""
    from .core import development_examples, validate_spec
    validate_spec(spec)
    # 摘要和令牌都只由开发题派生，与 optimize 用同一份视图，验收题不参与。
    development = development_examples(spec.examples)
    if (result.get("status") != "completed" or not result.get("searchComplete") or
            result.get("stage") != "search" or result.get("selectedTrialId") in (None, "baseline")):
        raise ValueError("需要完整开发搜索选出的候选")
    selected = next((t for t in result.get("trials", []) if t["id"] == result["selectedTrialId"]), None)
    baseline = next((t for t in result.get("trials", []) if t["id"] == "baseline"), None)
    if (not selected or not baseline or selected["prompt"] != result.get("bestPrompt") or
            baseline["prompt"] != result.get("baselinePrompt") or
            any(not selected.get(k) or len(selected[k]["cases"]) != selected[k]["total"] or
                any(c["error"] is not None for c in selected[k]["cases"]) for k in ("training", "validation"))):
        raise ValueError("固定候选必须与已完成搜索记录一致")
    if result.get("agentId") != spec.id or result.get("developmentDigest") != digest([vars(e) for e in development]):
        raise ValueError("开发数据与搜索时不一致")
    snapshot = callback_snapshot(spec.runner, spec.evaluator)
    if not snapshot or snapshot != result.get("callbackSnapshot"):
        raise ValueError("运行或评分源文件与搜索时不一致")
    if not result.get("callbackIdentity") or callback_identity(spec.runner, spec.evaluator) != result["callbackIdentity"]:
        raise ValueError("运行或评分函数与搜索时不一致")
    files = _files(source_files, root)
    absolute = {str((Path(root) / p).resolve()): v for p, v in files.items()}
    if any(absolute.get(path) != value for path, value in snapshot.items()):
        raise ValueError("须包含定义 runner 和 evaluator 的源文件及明确声明的依赖")
    if not isinstance(environment, dict) or not environment:
        raise ValueError("需明确声明运行条件")
    plan = policy or AcceptancePolicy()
    if not isinstance(plan, AcceptancePolicy):
        raise ValueError("policy 必须为 AcceptancePolicy")
    current_tokens = {token for example in development for token in case_tokens(example)}
    if spec.kind == "ask-data":
        observed = result.get("askDataValidation")
        if not isinstance(observed, dict) or not isinstance(observed.get("benchmarkDigest"), str):
            raise ValueError("问数候选缺少冻结开发包和验证账本记录")
        if (type(development_history_tokens) is not list or
                any(type(token) is not str or
                    not re.fullmatch(r"(?:input|source|group):[a-f0-9]{64}", token)
                    for token in development_history_tokens) or
                not current_tokens.issubset(development_history_tokens)):
            raise ValueError("问数候选缺少完整开发来源历史")
        current_tokens.update(development_history_tokens)
    package = {"schemaVersion": 2, "id": uuid.uuid4().hex, "experimentId": result["id"],
               "agentId": spec.id, "selectedTrialId": result["selectedTrialId"],
               "baselinePrompt": result["baselinePrompt"], "candidatePrompt": result["bestPrompt"],
               "files": files, "environment": deepcopy(environment), "policy": plan.to_dict(),
               "callbackIdentity": _relative_identity((spec.runner, spec.evaluator), root),
               "developmentTokens": sorted(current_tokens),
               "developmentDigest": result["developmentDigest"]}
    if spec.kind == "ask-data":
        package["askDataValidation"] = deepcopy(result["askDataValidation"])
    package["digest"] = digest(package)
    return package


def _sign(body, key):
    _check_key(key)
    return {"body": body, "signature": hmac.new(key, encode(body), hashlib.sha256).hexdigest()}


def validate_receipt(receipt, key):
    _check_key(key)
    if not isinstance(receipt, dict) or set(receipt) != {"body", "signature"}:
        raise ValueError("验收回执格式无效")
    if not isinstance(receipt["signature"], str) or not re.fullmatch(r"[a-f0-9]{64}", receipt["signature"]) or not hmac.compare_digest(
            receipt["signature"], hmac.new(key, encode(receipt["body"]), hashlib.sha256).hexdigest()):
        raise ValueError("验收回执签名无效")
    body = receipt["body"]
    if (not isinstance(body, dict) or body.get("schemaVersion") != 2 or
            body.get("acceptancePolicyVersion") != POLICY_VERSION):
        raise ValueError("验收策略版本不支持")
    return deepcopy(body)


def verify_candidate(candidate, spec, *, registry, signing_key, root=".", max_calls=10000,
                     is_cancelled=None):
    """回调前预约并标记使用；失败、取消和中断不返还测试资格。

    max_calls 只统计 runner；评分/reset 回调与外部模型费用另计。
    调用方须让回调自行配置 I/O 超时；没有通用 Python 强制中断。
    """
    candidate = deepcopy(candidate)
    validate_candidate(candidate, root)
    _check_key(signing_key)
    if not isinstance(spec, VerificationSpec) or not all(callable(fn) for fn in (spec.runner, spec.evaluator, spec.reset)):
        raise ValueError("需要 VerificationSpec 和显式 reset 回调")
    if spec.isolation not in ("workflow_only", "independent") or not isinstance(spec.provenance, str):
        raise ValueError("隔离方式无效")
    if spec.isolation == "independent" and not spec.provenance.strip():
        raise ValueError("独立验收须提供可核对的环境与数据来源说明")
    if not isinstance(spec.policy, AcceptancePolicy) or spec.policy.to_dict() != candidate["policy"]:
        raise ValueError("验收规则与固定候选不一致")
    if spec.environment != candidate["environment"]:
        raise ValueError("运行条件与固定候选不一致")
    policy = AcceptancePolicy.from_dict(candidate["policy"])
    isolation, provenance = spec.isolation, spec.provenance
    runner, evaluator, reset = spec.runner, spec.evaluator, spec.reset
    snapshots = callback_snapshot(spec.runner, spec.evaluator)
    actual = {str((Path(root) / p).resolve()): h for p, h in candidate["files"].items()}
    if not snapshots or any(actual.get(p) != h for p, h in snapshots.items()):
        raise ValueError("实际运行/评分回调必须定义于已固定的源文件")
    if _relative_identity((runner, evaluator), root) != candidate["callbackIdentity"]:
        raise ValueError("验收 runner/evaluator 必须与固定候选的函数身份一致")
    examples = deepcopy(spec.examples)
    if not examples:
        raise ValueError("没有独立验收题")
    ids, inputs, tokens, source_groups, input_groups = set(), set(), set(), {}, {}
    for ex in examples:
        if not isinstance(ex, Example) or ex.split != "test" or ex.exposure != "unseen":
            raise ValueError("验收数据必须明确为 test/unseen；旧 test 不能自动升级")
        for value, name in ((ex.id, "id"), (ex.group_id, "group_id"), (ex.source_id, "source_id")):
            text(value, name)
        if type(ex.critical) is not bool or ex.id in ids or digest(ex.input) in inputs:
            raise ValueError("重复验收题或无效关键案例标记")
        if ex.source_id in source_groups and source_groups[ex.source_id] != ex.group_id:
            raise ValueError("同一来源不能伪装为多个独立样本组")
        source_groups[ex.source_id] = ex.group_id
        ids.add(ex.id)
        inputs.add(digest(ex.input))
        case_fingerprints = case_tokens(ex)
        for token in case_fingerprints:
            if token.startswith("input:"):
                if token in input_groups and input_groups[token] != ex.group_id:
                    raise ValueError("相同或已识别的近似输入不能拆成多个独立样本组")
                input_groups[token] = ex.group_id
        tokens.update(case_fingerprints)
        encode(ex.expected)
    if tokens.intersection(candidate["developmentTokens"]):
        raise ValueError("验收题与开发时见过的输入或来源重叠")
    if type(max_calls) is not int or not 0 <= max_calls <= 1_000_000:
        raise ValueError("调用预算无效")
    required = 2 * len(examples) * policy.repeats
    if max_calls < required:
        raise ValueError("预算不足以完成固定的成对验收计划；未执行验收")
    if is_cancelled is not None and not callable(is_cancelled):
        raise ValueError("is_cancelled 必须可调用")
    ledger = registry if isinstance(registry, HoldoutRegistry) else HoldoutRegistry(registry)
    fingerprint = digest({"candidate": candidate["digest"], "cases": [vars(e) for e in examples],
                          "provenance": provenance, "isolation": isolation,
                          "reset": callback_snapshot(reset)})
    job_id = candidate["id"]
    record = ledger.reserve(job_id, fingerprint, sorted(tokens))
    if record["status"] == "consumed":
        validate_receipt(record["result"], signing_key)
        return record["result"]
    used = 0
    tokens_total, cost_total = 0, 0.0
    tokens_known, cost_known = True, True
    audit_path = None
    batches = []
    interrupted = False
    started = time.time()

    def cancelled():
        if is_cancelled and is_cancelled():
            raise InterruptedError("验收已取消")

    # start 失败的竞争者不拥有执行权，不能在错误处理中中断正在运行的获胜者。
    ledger.start(job_id)
    try:
        # 从这里起，即使回调或持久化失败也不返还这批题。
        logs = Path(registry if not isinstance(registry, HoldoutRegistry) else ledger.directory) / "private-logs"
        logs.mkdir(exist_ok=True)
        audit_path = logs / (job_id + ".jsonl")
        with (logs / (job_id + ".log")).open("a", encoding="utf-8") as stream, audit_path.open("x", encoding="utf-8") as audit, redirect_stdout(stream), redirect_stderr(stream):
            for index, ex in enumerate(examples):
                row = {"id": ex.id, "groupId": ex.group_id, "baselineScores": [], "candidateScores": [], "critical": ex.critical}
                for repeat in range(policy.repeats):
                    arms = (("baselineScores", candidate["baselinePrompt"]), ("candidateScores", candidate["candidatePrompt"]))
                    if (index + repeat) % 2:
                        arms = arms[::-1]
                    for arm, prompt in arms:
                        cancelled()
                        reset()
                        cancelled()
                        used += 1
                        measured = {"caseId": ex.id, "groupId": ex.group_id, "repeat": repeat, "arm": arm,
                                    "input": ex.input, "expected": ex.expected, "output": None, "score": None,
                                    "tokens": None, "costUsd": None, "complete": False}
                        try:
                            prediction = _prediction(runner(prompt, deepcopy(ex.input)))
                            measured.update(output=prediction.output, tokens=prediction.tokens, costUsd=prediction.cost_usd)
                            if prediction.tokens is None:
                                tokens_known = False
                            else:
                                tokens_total += prediction.tokens
                            if prediction.cost_usd is None:
                                cost_known = False
                            else:
                                cost_total += prediction.cost_usd
                                _finite_number(cost_total, "totalCost")
                            cancelled()
                            assessment = evaluator(deepcopy(ex.expected), deepcopy(prediction))
                            if not isinstance(assessment, Evaluation):
                                raise ValueError("无效评分")
                            score = _finite_number(assessment.score, "score", maximum=1.0)
                            row[arm].append(score)
                            measured.update(score=score, complete=True)
                        finally:
                            audit.write(encode(measured).decode("utf-8") + "\n")
                            audit.flush()
                            os.fsync(audit.fileno())
                        cancelled()
                batches.append(row)
        validate_candidate(candidate, root)
        evidence = compare(batches, policy)
        if evidence["evidenceStatus"] == "improved" and isolation != "independent":
            evidence.update(evidenceStatus="insufficient")
            evidence["reasons"].append("本地流程隔离尚不能证明验收数据对调优者独立")
        status = "completed"
    except (KeyboardInterrupt, InterruptedError):
        interrupted = True
        status = "cancelled"
        evidence = {"evidenceStatus": "invalid", "reasons": ["验收已取消；该批次不能重新用于选方案"]}
    except Exception:
        interrupted = True
        status = "failed"
        evidence = {"evidenceStatus": "invalid", "reasons": ["运行、评分或快照校验失败；未采用，验收批次不返还"]}
    body = {"schemaVersion": 2, "verificationId": job_id, "experimentId": candidate["experimentId"],
            "candidateDigest": candidate["digest"], "authorityId": ledger.authority_id,
            "status": status, "stage": "verification", "acceptancePolicyVersion": POLICY_VERSION,
            "evidenceStatus": evidence["evidenceStatus"], "evidence": evidence,
            "isolation": isolation, "provenance": provenance,
            "environment": candidate["environment"], "usedCalls": used, "plannedCalls": required,
            "tokens": tokens_total if tokens_known and status == "completed" else None,
            "costUsd": cost_total if cost_known and status == "completed" else None,
            "usageScope": "runner_only",
            "recordsDigest": hashlib.sha256(audit_path.read_bytes()).hexdigest() if audit_path and audit_path.is_file() else None,
            "startedAt": started, "finishedAt": time.time(),
            "adoptable": status == "completed" and evidence["evidenceStatus"] == "improved" and isolation == "independent"}
    receipt = _sign(body, signing_key)
    try:
        if interrupted:
            ledger.interrupt(job_id)
        else:
            cancelled()
            ledger.finish(job_id, receipt)
    except BaseException:
        try:
            ledger.interrupt(job_id)
        except Exception:
            pass
        # 不发布可能未完整持久化的可采用回执。
        raise RuntimeError("无法完成验收记录；不返回可采用结果，不能重复消费该批次") from None
    return receipt
