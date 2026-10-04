"""在固定预算内重跑开发样本并选择提示词候选。

这里不调用模型服务，也不修改应用里的提示词。调用方提供运行、提案和评分
函数；搜索不读取保留题，也不授予采用资格，独立验收由 verification 完成。
"""
from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .benchmark import callback_identity, callback_snapshot, digest


@dataclass(frozen=True)
class Example:
    id: str
    input: object
    expected: object
    split: str
    group_id: Optional[str] = None
    source_id: Optional[str] = None
    exposure: str = "unknown"
    critical: bool = False


@dataclass
class Prediction:
    output: object
    tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    trace_id: Optional[str] = None


@dataclass
class Evaluation:
    score: float
    reason: str = ""


@dataclass
class AgentSpec:
    id: str
    name: str
    baseline_prompt: str
    examples: List[Example]
    runner: Callable[[str, object], Prediction]
    proposer: Callable[[str, List[dict], int], str]
    evaluator: Callable[[object, Prediction], Evaluation]
    description: str = ""
    kind: str = "custom"
    # 内部仍复用成对开发比较；文件包不能沿旧提示词发布路径采用。
    candidate_kind: str = "prompt"

    def __post_init__(self) -> None:
        validate_spec(self)


def _json_value(value: Any, field: str) -> Any:
    """只保存可独立重放的 JSON 值，拒绝 NaN 和隐式改变类型的字典键。"""
    def check(item: Any) -> None:
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("%s 必须使用有限数值" % field)
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("%s 的对象键必须为字符串" % field)
                check(child)
            return
        raise ValueError("%s 必须为 JSON 值" % field)

    try:
        check(value)
        # 序列化后恢复，避免回调持有的可变对象进入持久状态。
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (RecursionError, OverflowError) as exc:
        raise ValueError("%s 不是有效的 JSON 值" % field) from exc


def _text(value: Any, field: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ValueError("%s 必须为%s字符串" % (field, "" if allow_empty else "非空"))


def _normalized_input(value: Any) -> str:
    # 键顺序和 JSON 空白不构成独立样本。保留内容与数组顺序，不猜测语义相似性。
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def validate_spec(spec: AgentSpec) -> None:
    """供服务注册入口复用；optimize 也会再次检查可能被调用方改动的配置。"""
    if not isinstance(spec, AgentSpec):
        raise ValueError("agent 必须为 AgentSpec")
    for field in ("id", "name", "baseline_prompt", "kind"):
        _text(getattr(spec, field), field)
    _text(spec.description, "description", allow_empty=True)
    if spec.candidate_kind not in ("prompt", "files"):
        raise ValueError("candidate_kind 必须为 prompt 或 files")
    for field in ("runner", "proposer", "evaluator"):
        if not callable(getattr(spec, field)):
            raise ValueError("%s 必须可调用" % field)
    if not isinstance(spec.examples, list):
        raise ValueError("examples 必须为列表")
    ids = set()
    inputs: Dict[str, str] = {}
    counts = {"train": 0, "validation": 0, "test": 0}
    groups: Dict[str, str] = {}
    for example in spec.examples:
        if not isinstance(example, Example):
            raise ValueError("examples 中每项必须为 Example")
        _text(example.id, "example.id")
        if example.id in ids:
            raise ValueError("样本 id 不能重复")
        ids.add(example.id)
        if not isinstance(example.split, str) or example.split not in counts:
            raise ValueError("split 必须为 train、validation 或 test")
        for field in ("group_id", "source_id"):
            group = getattr(example, field)
            if group is not None:
                _text(group, field)
                key = field + ":" + group
                if key in groups and groups[key] != example.split:
                    raise ValueError("同源案例不能跨 train、validation、test")
                groups[key] = example.split
        if example.exposure not in ("unknown", "development", "unseen") or type(example.critical) is not bool:
            raise ValueError("样本暴露情况或关键案例标记无效")
        value = _json_value(example.input, "example.input")
        _json_value(example.expected, "example.expected")
        normalized = _normalized_input(value)
        if normalized in inputs and inputs[normalized] != example.split:
            raise ValueError("train、validation、test 不能包含完全相同的输入")
        inputs[normalized] = example.split
        counts[example.split] += 1
    if not counts["train"] or not counts["validation"]:
        raise ValueError("train、validation 都必须包含样本；独立验收题不进入开发入口")


class _BudgetExhausted(Exception):
    pass


class _Cancelled(Exception):
    pass


class _InfrastructureFailure(Exception):
    pass


def _finite_number(value: Any, field: str, minimum: float = 0.0, maximum: Optional[float] = None) -> float:
    if type(value) not in (int, float):
        raise ValueError("%s 必须为数字" % field)
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("%s 必须为有限数值" % field) from exc
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError("%s 超出允许范围" % field)
    return number


def _prediction(value: Any) -> Prediction:
    if not isinstance(value, Prediction):
        raise ValueError("runner 必须返回 Prediction")
    result = Prediction(output=_json_value(value.output, "prediction.output"))
    if value.tokens is not None:
        if type(value.tokens) is not int or value.tokens < 0:
            raise ValueError("tokens 必须为非负整数或 None")
        result.tokens = value.tokens
    if value.cost_usd is not None:
        result.cost_usd = _finite_number(value.cost_usd, "cost_usd")
    if value.trace_id is not None:
        _text(value.trace_id, "trace_id")
        result.trace_id = value.trace_id
    return result


def _batch(total: int) -> dict:
    return {"score": 0.0, "passed": 0, "total": total, "latencyMs": 0.0,
            "tokens": None, "costUsd": None, "cases": []}


def _summarize(batch: dict) -> None:
    cases = batch["cases"]
    # 分母包含尚未完成的样本，取消或中断的半批结果不能呈现成满分。
    batch["score"] = sum(case["score"] / batch["total"] for case in cases)
    batch["passed"] = sum(case["passed"] for case in cases)
    batch["latencyMs"] = _finite_number(sum(case["latencyMs"] for case in cases), "latencyMs")
    for target in ("tokens", "costUsd"):
        values = [case[target] for case in cases]
        total = sum(values) if len(cases) == batch["total"] and all(value is not None for value in values) else None
        batch[target] = _finite_number(total, "costUsd") if target == "costUsd" and total is not None else total


def _no_regression(candidate: dict, baseline: dict) -> bool:
    if len(candidate["cases"]) != candidate["total"] or len(baseline["cases"]) != baseline["total"]:
        return False
    # 网络、凭据、超时等运行异常没有说明提示词的任务质量。即使两边都计零分，
    # 也不能把这种缺失的比较当成没有退步；业务失败应由 Prediction + 评分表达。
    if any(case["error"] is not None for batch in (candidate, baseline) for case in batch["cases"]):
        return False
    reference = {case["id"]: case["score"] for case in baseline["cases"]}
    return len(reference) == len(candidate["cases"]) and all(
        case["id"] in reference and case["score"] >= reference[case["id"]]
        for case in candidate["cases"]
    )


def development_examples(examples):
    """开发视图：按原顺序剔除独立验收题（split == "test"）。

    摘要与令牌只能由开发题派生。把 test/holdout 内容算进 `developmentDigest`，等于
    给搜索侧一个确认未见题内容的预言机：改一道验收题就会改变持久化的摘要。
    v1 旧包把 test 和开发题放在同一列表里，这里只忽略、不报错，保持读取兼容。
    """
    return [example for example in examples if example.split != "test"]


def optimize(
    spec: AgentSpec,
    *,
    max_trials: int = 3,
    max_calls: int = 100,
    baseline_prompt: Optional[str] = None,
    on_update: Optional[Callable[[dict], None]] = None,
    is_cancelled: Optional[Callable[[], bool]] = None,
) -> dict:
    """执行一次实验；预算只统计 runner 和 proposer 的实际调用。

    旧 test 字段只用于兼容和污染检查，不运行、不返回题目；搜索完成仍不可采用。
    回调必须自行设置网络超时；取消会在每次回调前后检查，不能杀死外部调用。
    配置错误抛 ValueError，执行中的错误保存在返回结果中。
    """
    validate_spec(spec)
    for field, value in (("max_trials", max_trials), ("max_calls", max_calls)):
        if type(value) is not int or value < 0:
            raise ValueError("%s 必须为非负整数" % field)
    prompt = spec.baseline_prompt if baseline_prompt is None else baseline_prompt
    _text(prompt, "baseline_prompt")
    if on_update is not None and not callable(on_update):
        raise ValueError("on_update 必须可调用")
    if is_cancelled is not None and not callable(is_cancelled):
        raise ValueError("is_cancelled 必须可调用")

    # 从此不再读取 spec；回调修改注册对象也不能改动本次实验的数据和函数。
    development = development_examples(spec.examples)
    examples = {split: [] for split in ("train", "validation", "test")}
    for example in development:
        examples[example.split].append(Example(example.id, _json_value(example.input, "input"),
                                               _json_value(example.expected, "expected"), example.split))
    runner, proposer, evaluator = spec.runner, spec.proposer, spec.evaluator
    state = {"agentId": spec.id, "status": "running", "baselinePrompt": prompt,
             "candidateKind": spec.candidate_kind,
             "bestPrompt": prompt, "selectedTrialId": None, "adoptable": False,
             "message": "正在运行原版，建立比较基线。", "usedCalls": 0, "trials": [],
             "baselineTest": None, "candidateTest": None, "stage": "search",
             "evidenceStatus": "not_verified", "acceptancePolicyVersion": None,
             "verificationId": None, "searchComplete": False}
    state["callbackSnapshot"] = callback_snapshot(runner, evaluator)
    state["callbackIdentity"] = callback_identity(runner, evaluator)
    state["developmentDigest"] = digest([vars(e) for e in development])
    reserve = 0

    def cancelled() -> None:
        if is_cancelled is not None:
            try:
                value = is_cancelled()
            except Exception as exc:
                raise _InfrastructureFailure("无法读取取消状态（%s）。" % type(exc).__name__) from exc
            if value:
                raise _Cancelled()

    def publish() -> None:
        if on_update is not None:
            try:
                on_update(copy.deepcopy(state))
            except Exception as exc:
                raise _InfrastructureFailure("无法保存实验进度（%s）。" % type(exc).__name__) from exc

    def claim_call(test: bool = False) -> None:
        cancelled()
        if state["usedCalls"] >= max_calls - (0 if test else reserve):
            raise _BudgetExhausted()
        state["usedCalls"] += 1

    def evaluate(prompt_value: str, split: str, owner: dict, key: str, test: bool = False) -> dict:
        batch = _batch(len(examples[split]))
        owner[key] = batch
        publish()
        for example in examples[split]:
            claim_call(test)
            case = {"id": example.id, "input": copy.deepcopy(example.input),
                    "expected": copy.deepcopy(example.expected), "output": None,
                    "score": 0.0, "passed": False, "reason": "", "error": None,
                    "latencyMs": 0.0, "tokens": None, "costUsd": None, "traceId": None}
            started = time.perf_counter()
            prediction = None
            failure = None
            try:
                # runner 和 evaluator 都只拿到副本，不能串改评分答案或前次记录。
                try:
                    raw_prediction = runner(prompt_value, copy.deepcopy(example.input))
                except Exception as exc:
                    case["error"] = "运行失败（%s）。" % type(exc).__name__
                else:
                    try:
                        prediction = _prediction(raw_prediction)
                    except Exception as exc:
                        case["error"] = "运行结果格式无效（%s）。" % type(exc).__name__
                        failure = _InfrastructureFailure("运行函数返回的结果格式无效，实验不能采用候选（%s）。" % type(exc).__name__)
                        if isinstance(raw_prediction, Prediction):
                            try:
                                case["output"] = _json_value(raw_prediction.output, "prediction.output")
                            except ValueError:
                                pass
                case["latencyMs"] = _finite_number((time.perf_counter() - started) * 1000, "latencyMs")
                if prediction is not None:
                    case.update(output=copy.deepcopy(prediction.output), tokens=prediction.tokens,
                                costUsd=prediction.cost_usd, traceId=prediction.trace_id)
                cancelled()
                if prediction is not None:
                    try:
                        assessment = evaluator(copy.deepcopy(example.expected), copy.deepcopy(prediction))
                        if not isinstance(assessment, Evaluation):
                            raise ValueError("evaluator 必须返回 Evaluation")
                        score = _finite_number(assessment.score, "score", maximum=1.0)
                        _text(assessment.reason, "reason", allow_empty=True)
                        case.update(score=score, passed=(score == 1.0), reason=assessment.reason)
                    except Exception as exc:
                        case["error"] = "评分失败（%s）。" % type(exc).__name__
                        failure = _InfrastructureFailure("评分函数失败，实验不能采用候选（%s）。" % type(exc).__name__)
                    cancelled()
            finally:
                batch["cases"].append(case)
                _summarize(batch)
                publish()
            if failure is not None:
                raise failure
            cancelled()
        return batch

    def trial(identifier: str, label: str, value: str) -> dict:
        item = {"id": identifier, "label": label, "prompt": value, "training": None, "validation": None}
        state["trials"].append(item)
        return item

    try:
        cancelled()
        publish()
        baseline = trial("baseline", "原版", prompt)
        state["selectedTrialId"] = "baseline"
        evaluate(prompt, "train", baseline, "training")
        evaluate(prompt, "validation", baseline, "validation")
        best = baseline
        seen = {prompt.strip()}
        exhausted = False
        for index in range(1, max_trials + 1):
            state["message"] = "正在生成并重跑候选 %d。" % index
            publish()
            try:
                claim_call()
                try:
                    proposal = proposer(best["prompt"], copy.deepcopy(best["training"]["cases"]), index)
                    _text(proposal, "候选提示词")
                except Exception as exc:
                    raise _InfrastructureFailure("候选生成失败（%s）。" % type(exc).__name__) from exc
                cancelled()
                item = trial("trial-%d" % index, "候选 %d" % index, proposal)
                if proposal.strip() in seen:
                    item["label"] += "（重复，已跳过）"
                    publish()
                    continue
                seen.add(proposal.strip())
                evaluate(proposal, "train", item, "training")
                evaluate(proposal, "validation", item, "validation")
                validation = item["validation"]
                if validation["score"] > best["validation"]["score"] and _no_regression(validation, baseline["validation"]):
                    best = item
                    state["bestPrompt"] = item["prompt"]
                    state["selectedTrialId"] = item["id"]
                publish()
            except _BudgetExhausted:
                exhausted = True
                break
        if best is baseline:
            validation_errors = any(
                case["error"] is not None for item in state["trials"] if item["validation"] is not None
                for case in item["validation"]["cases"])
            if validation_errors:
                state["message"] = "验证集中有运行错误，未能完成有效的改善比较，继续使用原版。"
            else:
                state["message"] = ("调用预算已用尽，没有完整验证通过的候选，继续使用原版。"
                                    if exhausted else "没有候选在验证集上严格改善且保持每个样本不退步，继续使用原版。")
        else:
            state["message"] = "候选在开发验证集上改善；请固定候选并在独立验收环境运行 verify，当前尚不可采用。"
        cancelled()
        state["status"] = "completed"
        state["searchComplete"] = not exhausted and all(
            batch is not None and len(batch["cases"]) == batch["total"] and
            all(case["error"] is None for case in batch["cases"])
            for item in state["trials"] if item["training"] is not None
            for batch in (item["training"], item["validation"]))
        publish()
        cancelled()
    except _BudgetExhausted:
        state.update(status="completed", adoptable=False,
                     message="调用预算不足以完成比较，已保留运行记录，继续使用原版。")
    except _Cancelled:
        state.update(status="cancelled", adoptable=False, message="实验已取消，已保留运行记录，当前应用未更改。")
    except Exception as exc:
        state.update(status="failed", adoptable=False,
                     message=str(exc) if isinstance(exc, _InfrastructureFailure) else "实验执行失败（%s），已保留运行记录。" % type(exc).__name__)
    # 终态也必须是快照；保存失败对调用方可见，不能返回可采用的成功结果。
    try:
        publish()
        cancelled()
    except _Cancelled:
        state.update(status="cancelled", adoptable=False, message="实验已取消，已保留运行记录，当前应用未更改。")
        try:
            publish()
        except _InfrastructureFailure as exc:
            state.update(status="failed", adoptable=False, message=str(exc))
    except _InfrastructureFailure as exc:
        state.update(status="failed", adoptable=False, message=str(exc))
    if state["status"] != "completed":
        state["searchComplete"] = False
    return copy.deepcopy(state)
