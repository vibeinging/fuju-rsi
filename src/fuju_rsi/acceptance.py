"""用独立来源组比较原版和固定候选，避免把重跑次数当成样本量。

先对每个案例的重复运行取均值，再对同一来源组的案例取均值，最后让各组
等权参与比较。分组由调用方提供：本模块无法从组名证明来源独立，也不能
补救题目泄漏、错误答案或业务覆盖不足。相似改写仍应放在同一个组。

采用判断使用单侧 Hoeffding 下界。假设各来源组独立、评分在 [0, 1] 内、
候选及规则在查看本次验收结果前已固定，则每组差值在 [-1, 1] 内，均值
减去 sqrt(2 * log(1 / (1 - confidence)) / 组数) 是保守的下界。
这不是总体泛化的保证；反复查看同一保留集再选优也不受这一次判断保护。
下界针对独立组均值的期望，报告中的上界 1 只是差值的取值边界。

成对组级 bootstrap 基本区间只作辅助描述，不参与采用判断。它直接重采样
已经计算的组差值，不调用 runner，也不把重跑或同源改写视为独立观测。
小样本和退化分布的 bootstrap 区间可能很窄，不能据此跳过主判断。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Dict, List


POLICY_VERSION = "paired-group-hoeffding-v1"
_BOOTSTRAP_METHOD = "paired-group-bootstrap-basic-v1"
_FIELDS = {
    "repeats": "repeats",
    "minGroups": "min_groups",
    "minGain": "min_gain",
    "confidence": "confidence",
    "maxGroupRegression": "max_group_regression",
    "resamples": "resamples",
    "seed": "seed",
}


def _number(value: Any, field: str, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise ValueError("%s 必须为数字，不能是布尔值" % field)
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("%s 必须为有限数字" % field) from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError("%s 必须在 [%s, %s] 范围内且为有限数字" % (field, minimum, maximum))
    return result


def _integer(value: Any, field: str, minimum: int) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("%s 必须为不小于 %s 的整数，不能是布尔值" % (field, minimum))


@dataclass(frozen=True)
class AcceptancePolicy:
    """在验收前固定的规则；降低最低组数不代表数据因此足够或独立。"""

    repeats: int = 3
    min_groups: int = 30
    min_gain: float = 0.0
    confidence: float = 0.95
    max_group_regression: float = 0.0
    resamples: int = 2000
    seed: int = 1729

    def __post_init__(self) -> None:
        _integer(self.repeats, "repeats", 1)
        _integer(self.min_groups, "minGroups", 2)
        _integer(self.resamples, "resamples", 1)
        _integer(self.seed, "seed", 0)
        for name, field in (("min_gain", "minGain"), ("max_group_regression", "maxGroupRegression")):
            object.__setattr__(self, name, _number(getattr(self, name), field, 0.0, 1.0))
        confidence = _number(self.confidence, "confidence", 0.0, 1.0)
        if not 0.0 < confidence < 1.0:
            raise ValueError("confidence 必须大于 0 且小于 1")
        object.__setattr__(self, "confidence", confidence)

    def to_dict(self) -> dict:
        """持久化和 HTTP 使用 camelCase；返回副本，不允许改动已固定规则。"""
        return {field: getattr(self, name) for field, name in _FIELDS.items()}

    @classmethod
    def from_dict(cls, value: dict) -> "AcceptancePolicy":
        """未提供的规则用明确默认值，拒绝拼错字段和隐式类型转换。"""
        if type(value) is not dict:
            raise ValueError("acceptancePolicy 必须为对象")
        if any(type(key) is not str or key not in _FIELDS for key in value):
            raise ValueError("acceptancePolicy 包含未知字段")
        return cls(**{_FIELDS[key]: item for key, item in value.items()})


def _mean(values: List[float]) -> float:
    return math.fsum(values) / len(values)


def _quantile(ordered: List[float], probability: float) -> float:
    """线性插值，明确算法以便重复运行及跨实现对账。"""
    position = (len(ordered) - 1) * probability
    left = int(math.floor(position))
    right = int(math.ceil(position))
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def _bounded(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _bootstrap(deltas: List[float], mean: float, policy: AcceptancePolicy) -> dict:
    rng = random.Random(policy.seed)
    count = len(deltas)
    means = sorted(_mean([deltas[rng.randrange(count)] for _ in range(count)])
                   for _ in range(policy.resamples))
    tail = (1.0 - policy.confidence) / 2.0
    return {
        "lower": _bounded(2.0 * mean - _quantile(means, 1.0 - tail)),
        "upper": _bounded(2.0 * mean - _quantile(means, tail)),
        "confidence": policy.confidence,
        "method": _BOOTSTRAP_METHOD,
        "resamples": policy.resamples,
        "seed": policy.seed,
        "descriptiveOnly": True,
    }


def compare(cases: List[dict], policy: AcceptancePolicy) -> dict:
    """比较已经完成的评分，不执行或补跑任何案例。

    每项包含 id、groupId、baselineScores、candidateScores、critical。
    两侧评分列表都必须恰好含 repeats 个 [0, 1] 内的有限数字。
    无效输入不返回部分均值，避免把未完成验收显示成成功。
    runCount 是提供的评分条目数；无效输入时它不表示已验证的有效运行次数。
    """
    if not isinstance(policy, AcceptancePolicy):
        raise ValueError("policy 必须为 AcceptancePolicy")
    result = {
        "evidenceStatus": "invalid",
        "reasons": [],
        "independentGroups": 0,
        "caseCount": len(cases) if type(cases) is list else 0,
        "runCount": 0,
        "baselineScore": None,
        "candidateScore": None,
        "delta": None,
        "interval": {"lower": None, "upper": None, "confidence": policy.confidence,
                     "method": POLICY_VERSION},
        "bootstrapInterval": None,
        "acceptancePolicyVersion": POLICY_VERSION,
    }
    if type(cases) is not list:
        result["reasons"].append("验收案例必须为列表")
        return result
    if not cases:
        result["reasons"].append("没有完成的验收案例")
        return result

    groups: Dict[str, List[tuple]] = {}
    ids = set()
    critical_regressions = []
    for index, case in enumerate(cases):
        if type(case) is dict:
            result["runCount"] += sum(len(case[field]) for field in ("baselineScores", "candidateScores")
                                      if type(case.get(field)) is list)
        try:
            if type(case) is not dict:
                raise ValueError("案例必须为对象")
            for field in ("id", "groupId"):
                if type(case.get(field)) is not str or not case[field].strip():
                    raise ValueError("%s 必须为非空字符串" % field)
            if case["id"] in ids:
                raise ValueError("案例 id 不能重复")
            ids.add(case["id"])
            if type(case.get("critical")) is not bool:
                raise ValueError("critical 必须为布尔值")
            scores = []
            for field in ("baselineScores", "candidateScores"):
                values = case.get(field)
                if type(values) is not list or len(values) != policy.repeats:
                    raise ValueError("%s 必须恰好包含 %s 次评分" % (field, policy.repeats))
                scores.append(_mean([_number(value, field, 0.0, 1.0) for value in values]))
            baseline, candidate = scores
            groups.setdefault(case["groupId"], []).append((baseline, candidate))
            if case["critical"] and candidate < baseline:
                critical_regressions.append(case["id"])
        except ValueError as exc:
            result["reasons"].append("案例 %s 无效：%s" % (index + 1, exc))
    if result["reasons"]:
        return result

    # 排序只固定计算与重采样顺序，不修改分组或从名称推断独立性。
    group_means = []
    for group_id in sorted(groups):
        values = groups[group_id]
        group_means.append((group_id, _mean([value[0] for value in values]),
                            _mean([value[1] for value in values])))
    deltas = [candidate - baseline for _, baseline, candidate in group_means]
    count = len(deltas)
    delta = _mean(deltas)
    # log1p 避免非常接近零的 confidence 在 1-confidence 中丢失精度。
    radius = math.sqrt(2.0 * -math.log1p(-policy.confidence) / count)
    lower = _bounded(delta - radius)
    result.update({
        "independentGroups": count,
        "baselineScore": _mean([baseline for _, baseline, _ in group_means]),
        "candidateScore": _mean([candidate for _, _, candidate in group_means]),
        "delta": delta,
        "interval": {"lower": lower, "upper": 1.0, "confidence": policy.confidence,
                     "method": POLICY_VERSION},
        "bootstrapInterval": _bootstrap(deltas, delta, policy),
    })
    reasons = result["reasons"]
    # 对外只返回汇总；来源名或案例编号也可能包含业务内容，不能用于下一轮调优。
    if critical_regressions:
        reasons.append("%s 个关键案例的平均评分退步" % len(critical_regressions))
    regressed_groups = sum(delta < -policy.max_group_regression for delta in deltas)
    if regressed_groups:
        reasons.append("%s 个来源组的平均评分退步超过允许值" % regressed_groups)
    if reasons:
        result["evidenceStatus"] = "regressed"
    elif delta <= policy.min_gain:
        result["evidenceStatus"] = "no_improvement"
        reasons.append("平均收益没有超过预先固定的最低收益")
    elif count < policy.min_groups:
        result["evidenceStatus"] = "insufficient"
        reasons.append("独立来源组数量少于预先固定的最低组数")
    elif lower <= policy.min_gain:
        result["evidenceStatus"] = "insufficient"
        reasons.append("保守收益下界没有超过预先固定的最低收益")
    else:
        result["evidenceStatus"] = "improved"
        reasons.append("在已声明的来源独立性和固定验收规则前提下，通过收益与退步检查")
    return result
