"""把已核验的公开实验记录导出为普通文件，不启动服务或修改业务配置。

输入应来自 ExperimentManager.get；本模块不验证回执，也不把导出的 JSON
作为资格凭据。只挑选汇总字段，避免把案例内容带进交付报告。
"""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import html
import json
import math
from pathlib import Path
import re
import shutil


_STATUSES = {"running", "completed", "failed", "cancelled", "interrupted"}
_EVIDENCE = {"not_verified", "invalid", "insufficient", "regressed", "no_improvement", "improved"}
_CONCLUSIONS = {
    "invalid": "独立验收无效，不能采用候选。",
    "insufficient": "独立验收证据不足，不能采用候选。",
    "regressed": "独立验收发现退步，不能采用候选。",
    "no_improvement": "独立验收未证明收益，继续使用原版。",
}


def _text(value):
    return value if isinstance(value, str) else None


def _number(value, minimum=0, maximum=None, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)):
        return None
    try:
        valid = math.isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
    except OverflowError:
        valid = False
    return value if valid else None


def _markdown(value):
    """元数据只能作为文字，不能注入链接、HTML 或新的报告段落。"""
    value = html.escape(str(value), quote=True).replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]()#+.!|>-])", r"\\\1", value)


def _format(value):
    return "未知" if value is None else format(value, ".6g")


def _batch(value):
    if not isinstance(value, dict):
        return None
    cases = value.get("cases")
    total = _number(value.get("total"), integer=True)
    completed = len(cases) if isinstance(cases, list) else None
    errors = sum(not isinstance(case, dict) or case.get("error") is not None for case in cases) if isinstance(cases, list) else None
    return {
        "score": _number(value.get("score"), maximum=1),
        "passed": _number(value.get("passed"), integer=True),
        "total": total, "completed": completed, "errors": errors,
        "complete": total is not None and total > 0 and completed == total and errors == 0,
        "latencyMs": _number(value.get("latencyMs")),
        "tokens": _number(value.get("tokens"), integer=True),
        "costUsd": _number(value.get("costUsd")),
    }


def _trial(value):
    if value is None:
        return None
    return {key: _batch(value.get(key)) for key in ("training", "validation")}


def _sum_known(batches, field):
    values = [batch[field] for batch in batches]
    if not values or any(value is None for value in values):
        return None
    return _number(sum(values), integer=field == "tokens")


def _interval(value):
    if not isinstance(value, dict):
        return None
    # 只有实际使用的主判断方法才能展示为保守下界，任意标签不得继承这个含义。
    method = value.get("method")
    if method != "paired-group-hoeffding-v1":
        return {"lower": None, "upper": None, "confidence": None, "method": None}
    return {
        "lower": _number(value.get("lower"), -1, 1),
        "upper": _number(value.get("upper"), -1, 1),
        "confidence": _number(value.get("confidence"), 0, 1),
        "method": method,
    }


def _summarize(record):
    trials = [trial for trial in record.get("trials", []) if isinstance(trial, dict)]
    baseline = next((trial for trial in trials if trial.get("id") == "baseline"), None)
    selected_id = _text(record.get("selectedTrialId"))
    selected = [trial for trial in trials if selected_id and trial.get("id") == selected_id]
    candidate = selected[0] if len(selected) == 1 and selected_id != "baseline" else None
    candidate_prompt = _text(candidate.get("prompt")) if candidate is not None else None
    if candidate_prompt == record["baselinePrompt"]:
        candidate = candidate_prompt = None
    stage = record.get("stage") if record.get("stage") in {"search", "verification"} else "legacy"
    status = record.get("status") if record.get("status") in _STATUSES else "unknown"
    evidence_status = record.get("evidenceStatus") if record.get("evidenceStatus") in _EVIDENCE else "not_verified"
    if stage != "verification":
        evidence_status = "not_verified"
    policy = _text(record.get("acceptancePolicyVersion"))
    verification_id = _text(record.get("verificationId"))
    qualified = bool(stage == "verification" and status == "completed" and record.get("adoptable") is True
                     and record.get("searchComplete") is True
                     and evidence_status == "improved" and policy and policy.strip()
                     and verification_id and verification_id.strip() and candidate_prompt is not None
                     and candidate_prompt == record.get("bestPrompt"))
    all_batches = [batch for trial in trials for field in ("training", "validation")
                   if (batch := _batch(trial.get(field))) is not None]
    evidence = record.get("evidence")
    verification = None
    if stage == "verification" and isinstance(evidence, dict):
        verification = {key: _number(evidence.get(key), integer=True)
                        for key in ("independentGroups", "caseCount", "runCount")}
        verification.update({key: _number(evidence.get(key), maximum=1)
                             for key in ("baselineScore", "candidateScore")})
        verification.update(delta=_number(evidence.get("delta"), -1, 1),
                            interval=_interval(evidence.get("interval")))
    config = record.get("config") if isinstance(record.get("config"), dict) else {}
    summary = {
        "schemaVersion": 1, "exportedAt": datetime.now(timezone.utc).isoformat(),
        "id": _text(record.get("id")), "experimentId": _text(record.get("id")), "agentId": _text(record.get("agentId")),
        "name": _text(record.get("name")), "stage": stage, "status": status,
        "kind": record.get("kind") if record.get("kind") in {"demo", "custom"} else "unknown",
        "evidenceStatus": evidence_status, "searchComplete": record.get("searchComplete") is True,
        "adoptable": qualified, "selectedTrialId": selected_id,
        "candidateAvailable": candidate_prompt is not None,
        "acceptancePolicyVersion": policy if stage == "verification" else None,
        "verificationId": verification_id if stage == "verification" else None,
        "development": {"baseline": _trial(baseline), "candidate": _trial(candidate)},
        "trials": [{"id": _text(trial.get("id")), "selected": trial.get("id") == selected_id,
                    **_trial(trial)} for trial in trials],
        "verification": verification,
        "usage": {
            "search": {"usedCalls": _number(record.get("usedCalls"), integer=True),
                       "maxCalls": _number(config.get("maxCalls"), integer=True),
                       "tokens": _sum_known(all_batches, "tokens"), "costUsd": _sum_known(all_batches, "costUsd"),
                       "usageScope": "runner_only"},
            "verification": {"usedCalls": _number(record.get("verificationUsedCalls"), integer=True),
                             "plannedCalls": _number(record.get("verificationPlannedCalls"), integer=True),
                             "tokens": _number(record.get("verificationTokens"), integer=True),
                             "costUsd": _number(record.get("verificationCostUsd")), "usageScope": "runner_only"},
        },
        "snapshotOnly": True,
    }
    return summary, candidate_prompt


def _conclusion(summary):
    if summary["adoptable"]:
        return "导出时已通过独立验收，可将已验收提示词作为普通配置交付业务测试。"
    if summary["status"] != "completed":
        return "实验未完成或执行异常，当前结果不能作为采用依据。"
    if summary["stage"] == "legacy":
        return "这是旧版实验记录，尚未获得当前独立验收资格。"
    if summary["stage"] == "verification":
        return _CONCLUSIONS.get(summary["evidenceStatus"], "当前独立验收资格未通过核验，不能采用候选。")
    if not summary["searchComplete"]:
        return "开发搜索未完整完成，可能存在预算中断或运行错误，不能采用候选。"
    if not any(trial["id"] != "baseline" for trial in summary["trials"]):
        return "仅建立原版基线，尚未比较候选，不能据此判断是否存在更好的方案。"
    if summary["candidateAvailable"]:
        return "已选出开发候选，尚未通过独立验收，不能据此宣称业务提升。"
    return "开发验证集没有选出更好的候选，继续使用原版。"


def _report(summary):
    scope = ("本次为离线 SQLite 与规则候选示例，用于检查工具流程，不代表云模型或真实产品的收益。"
             if summary["kind"] == "demo" else
             "本报告只覆盖所接 runner 和已声明的业务范围；没有据此推断运行使用了真实模型。")
    lines = ["# yiTrace 调优报告", "", _conclusion(summary), "", scope, "",
             "这是导出时的快照，不是长期有效的验收凭据。源文件、环境、基线或规则变化后须重新核验。", "",
             "- 实验：" + _markdown(summary["name"] or summary["experimentId"] or "未知"),
             "- 阶段 / 状态：" + _markdown(summary["stage"] + " / " + summary["status"]),
             "- 导出时间：" + _markdown(summary["exportedAt"]), "",
             "## 开发集比较", "", "开发训练题用于调优，开发验证题用于选择候选；两者都不能证明未见业务的收益。", "",
             "| 方案 | 选中 | 数据集 | 平均评分 | 通过 / 总数 | 已运行 / 总数 | 累计耗时 ms | 运行错误 | 完整比较 |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"]
    for trial in summary["trials"]:
        label = "原版" if trial["id"] == "baseline" else _markdown(trial["id"] or "未知候选")
        for split, split_label in (("training", "训练"), ("validation", "验证")):
            batch = trial.get(split) or {}
            lines.append("| %s | %s | %s | %s | %s / %s | %s / %s | %s | %s | %s |" % (
                label, "是" if trial["selected"] else "否", split_label, _format(batch.get("score")),
                _format(batch.get("passed")), _format(batch.get("total")), _format(batch.get("completed")),
                _format(batch.get("total")), _format(batch.get("latencyMs")), _format(batch.get("errors")),
                "是" if batch.get("complete") else "否 / 未知"))
    if not summary["trials"]:
        lines.append("| 无运行记录 | — | — | 未知 | 未知 | 未知 | 未知 | 未知 | 否 / 未知 |")
    lines += ["", "上表包括未选中的候选。累计耗时是批次内运行耗时之和，不是单次平均延迟。",
              "不完整批次的分数包含未完成案例的影响，仅供排查，不代表完成验收。", "", "## 独立验收", ""]
    evidence = summary["verification"]
    if evidence is None:
        lines.append("没有可展示的独立验收汇总。新候选需要先固定快照，再交给独立验收环境。")
    else:
        interval = evidence.get("interval") or {}
        lines += ["- 证据状态：" + _markdown(summary["evidenceStatus"]),
                  "- 验收规则：" + _markdown(summary["acceptancePolicyVersion"] or "未知"),
                  "- 独立来源组：%s；案例数：%s；已提供评分条目：%s。" % tuple(
                      _format(evidence.get(key)) for key in ("independentGroups", "caseCount", "runCount")),
                  "- 原版评分：%s；候选评分：%s；平均差值：%s。" % tuple(
                      _format(evidence.get(key)) for key in ("baselineScore", "candidateScore", "delta")),
                  "- 保守收益下界：%s；置信水平：%s。" % (_format(interval.get("lower")), _format(interval.get("confidence"))),
                  "", "重复运行次数不是独立样本数，同源改写应归入同一组。来源独立性、题目未泄漏及业务覆盖仍须验收者确认。",
                  "收益下界依赖独立来源组与事先固定的候选和规则，不能保证所有业务都会改善。"]
    lines += ["", "## 调用和用量", "", "| 阶段 | 已用回调 | 上限或计划 | 已记录 runner token | 已记录 runner 费用 USD |",
              "| --- | ---: | ---: | ---: | ---: |"]
    for key, label, planned in (("search", "搜索", "maxCalls"), ("verification", "验收", "plannedCalls")):
        usage = summary["usage"][key]
        lines.append("| %s | %s | %s | %s | %s |" % (label, *(_format(usage.get(field)) for field in ("usedCalls", planned, "tokens", "costUsd"))))
    lines += ["", "搜索回调计 runner 和 proposer；验收回调只计 runner。调用次数不是金额上限。",
              "token 与费用只覆盖 runner 已上报的记录，不含 proposer、评分器或其他环境费用；未知不等于零。", "",
              "## 交付文件和下一步", "", "- `baseline-prompt.txt`：原版提示词，可按原有业务配置使用。",
              "- `result.json`：供其他 Agent 或脚本读取的汇总，不包含案例输入、答案或输出。"]
    if summary["candidateAvailable"]:
        lines.append("- `candidate-prompt.txt` 和 `prompt.diff`：选定候选及修改内容，供审查。")
    if summary["adoptable"]:
        lines.append("- `verified-prompt.txt`：导出时通过核验的候选。接入普通提示词或配置后，仍需运行业务测试和移除 yiTrace 后的运行检查。")
    else:
        lines.append("- 本次不交付 `verified-prompt.txt`；继续使用原版。完整开发候选可固定后交给独立验收，证据不足或失败时不能采用。")
    lines += ["", "报告导出不启动浏览器、HTTP 或 DB，也不写入产品配置。交付物是普通文件；网站和控制台均不是运行依赖。",
              "采用资格要求独立验收完成、证据为 improved、可信回执及当前快照核验有效。报告本身不会授予资格，也不代表产品已上线。", ""]
    return "\n".join(lines)


def _diff(baseline, candidate):
    lines = difflib.unified_diff(baseline.splitlines(keepends=True), candidate.splitlines(keepends=True),
                                 fromfile="baseline-prompt.txt", tofile="candidate-prompt.txt")
    return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)


def write_report(record, output_dir):
    """导出 manager.get 返回的公开快照；目标目录必须是本次新建。

    返回所有文件的绝对路径，不存在的候选或已验收文件以 None 表示。
    任何写入失败都会清理本次目录，已有输出永不覆盖。
    """
    if not isinstance(record, dict) or not isinstance(record.get("baselinePrompt"), str):
        raise ValueError("报告需要已核验的公开实验记录及原版提示词")
    if not isinstance(record.get("trials", []), list):
        raise ValueError("实验 trials 必须为列表")
    summary, candidate = _summarize(record)
    payloads = {"report": ("report.md", _report(summary)),
                "result": ("result.json", json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n"),
                "baselinePrompt": ("baseline-prompt.txt", record["baselinePrompt"])}
    if candidate is not None:
        payloads.update(candidatePrompt=("candidate-prompt.txt", candidate),
                        promptDiff=("prompt.diff", _diff(record["baselinePrompt"], candidate)))
    if summary["adoptable"]:
        payloads["verifiedPrompt"] = ("verified-prompt.txt", candidate)
    requested = Path(output_dir).expanduser()
    # 保留最后一层路径，让 mkdir 也拒绝已有的悬空符号链接。
    directory = requested.parent.resolve() / requested.name
    directory.mkdir(parents=True, exist_ok=False)
    try:
        paths = {key: None for key in ("report", "result", "baselinePrompt", "candidatePrompt", "promptDiff", "verifiedPrompt")}
        for key, (name, content) in payloads.items():
            path = directory / name
            with path.open("xb") as stream:
                stream.write(content.encode("utf-8"))
            paths[key] = str(path)
        return {"directory": str(directory), **paths}
    except BaseException:
        shutil.rmtree(directory)
        raise
