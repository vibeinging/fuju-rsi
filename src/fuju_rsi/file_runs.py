"""在独立临时目录中比较普通配置文件候选。

这里检查候选被业务实际读入，并在每例结束后检查目录没有变化。它不是操作
系统权限隔离：业务回调仍以当前用户权限运行，不应交给不可信代码。输出、
数据库与缓存由业务放在另外的路径，本目录只放明确登记的输入文件。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import MappingProxyType
from typing import Callable, Dict, List, Mapping

from .ask_data_validation import optimize_ask_data
from .benchmark import callback_identity, callback_snapshot, load_bundle
from .core import AgentSpec, Evaluation, Example, Prediction, optimize
from .file_candidates import FileBaseline, FileCandidate


class FileRunError(ValueError):
    pass


@dataclass(frozen=True)
class FileRunContext:
    root: Path
    candidate_digest: str
    expected_hashes: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "expected_hashes", MappingProxyType(dict(self.expected_hashes)))


@dataclass
class FileRunResult:
    prediction: Prediction
    loaded_hashes: Dict[str, str]


@dataclass(frozen=True)
class FileExperimentSpec:
    id: str
    name: str
    baseline: FileBaseline
    examples: List[Example]
    runner: Callable[[FileRunContext, object], FileRunResult]
    evaluator: Callable[[object, Prediction], Evaluation]
    kind: str = "custom"


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _package(candidate):
    return json.loads(candidate.to_json())


def _tree_fingerprint(root, declared_files):
    """不跟随链接；目录条目、字节、权限和文件身份均需保持。"""
    from .file_candidates import MAX_FILE_BYTES
    directories = {""}
    for name in declared_files:
        parts = name.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    entries = {}
    stack = [(root, "")]
    while stack:
        path, relative = stack.pop()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise FileRunError("候选目录不能包含符号链接")
        metadata = (info.st_mode, info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns)
        if stat.S_ISREG(info.st_mode):
            if relative not in declared_files or info.st_size > MAX_FILE_BYTES:
                raise FileRunError("候选目录包含未声明或超出上限的文件")
            entries[relative] = ("file", metadata, _hash(path.read_bytes()))
        elif stat.S_ISDIR(info.st_mode):
            if relative not in directories:
                raise FileRunError("候选目录包含未声明的目录")
            entries[relative] = ("directory", metadata)
            with os.scandir(str(path)) as children:
                for child in children:
                    name = child.name if not relative else relative + "/" + child.name
                    stack.append((path / child.name, name))
        else:
            raise FileRunError("候选目录只允许普通文件和目录")
    return entries


def _materialize(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
    # 权限用于防止意外写入，不构成当前用户的权限隔离。
    for directory, _, names in os.walk(str(root), topdown=False):
        for name in names:
            (Path(directory) / name).chmod(0o400)
        Path(directory).chmod(0o500)


def _validate_export_files(files):
    # 公共导出入口可能接收被调用方修改的字典，写入前先验证路径和字节上限。
    from .file_candidates import FileTarget, MAX_FILE_BYTES, MAX_TOTAL_BYTES
    total = 0
    if type(files) is not dict:
        raise FileRunError("原版文件包无效")
    for name, text in files.items():
        FileTarget(name, "dictionary")
        if type(text) is not str:
            raise FileRunError("原版文件内容必须为 UTF-8 文本")
        size = len(text.encode("utf-8"))
        if size > MAX_FILE_BYTES:
            raise FileRunError("原版文件超过单文件上限")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise FileRunError("原版文件超过总量上限")


def _clean_scratch(root):
    # 回调可能改权限或放入链接；清理仅处理这次创建的临时根目录，不跟随链接。
    if root.is_symlink():
        root.unlink()
        return
    if root.exists():
        if not root.is_dir():
            root.unlink()
            return
        for directory, names, files in os.walk(str(root), followlinks=False):
            Path(directory).chmod(0o700)
            for name in names + files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700 if path.is_dir() else 0o600)
        shutil.rmtree(str(root))


def _function_code(callback):
    return getattr(getattr(callback, "__func__", callback), "__code__", None)


def _validate_ask_data(spec, bundle, ledger):
    if bundle is None or ledger is None:
        raise FileRunError("问数文件比较需要冻结开发包和共享验证账本")
    manifest, rows = load_bundle(bundle)
    if manifest.get("role") != "development" or manifest.get("schemaVersion") != 2:
        raise FileRunError("问数文件比较只接受 v2 冻结开发包")
    if [vars(example) for example in spec.examples] != rows:
        raise FileRunError("问数案例必须与冻结开发包逐项一致")
    identity = callback_identity(spec.evaluator)
    snapshot = callback_snapshot(spec.evaluator)
    scorer = manifest["scorer"]
    expected = str(Path(scorer["path"]).resolve())
    if (not identity or identity[0]["file"] != expected or not snapshot or
            snapshot.get(expected) != scorer["sha256"]):
        raise FileRunError("实际业务评分函数必须来自冻结开发包的评分文件")


def compare_file_candidates(
    spec, candidates, *, max_calls=100, ask_data_bundle=None,
    validation_ledger=None, is_cancelled=None, on_update=None,
):
    """固定候选逐一重跑；返回私有完整记录，始终不授予正式采用资格。"""
    if not isinstance(spec, FileExperimentSpec) or not isinstance(spec.baseline, FileBaseline):
        raise FileRunError("spec 必须为带 FileBaseline 的 FileExperimentSpec")
    if not callable(spec.runner) or not callable(spec.evaluator):
        raise FileRunError("runner 和 evaluator 必须可调用")
    if type(candidates) is not list:
        raise FileRunError("candidates 必须为 FileCandidate 列表")
    if type(max_calls) is not int or max_calls < 0:
        raise FileRunError("max_calls 必须为非负整数")
    if on_update is not None and not callable(on_update):
        raise FileRunError("on_update 必须可调用")
    if spec.kind != "ask-data" and (ask_data_bundle is not None or validation_ledger is not None):
        raise FileRunError("提供问数开发包或账本时 kind 必须为 ask-data")
    if spec.kind == "ask-data":
        _validate_ask_data(spec, ask_data_bundle, validation_ledger)
    spec.baseline.assert_current()
    baseline_candidate = spec.baseline.as_candidate()
    verified = [baseline_candidate]
    for candidate in candidates:
        if not isinstance(candidate, FileCandidate):
            raise FileRunError("候选必须为 FileCandidate")
        checked = FileCandidate.from_json(candidate.to_json(), spec.baseline)
        if checked.digest != candidate.digest or candidate.baseline_digest != spec.baseline.digest:
            raise FileRunError("候选身份或原版摘要不匹配")
        verified.append(checked)
    tokens = [candidate.to_json() for candidate in verified]
    token_candidates = dict(zip(tokens, verified))
    packages = {candidate.digest: _package(candidate) for candidate in verified}
    runner, evaluator = spec.runner, spec.evaluator
    snapshots = callback_snapshot(runner, evaluator)
    identities = callback_identity(runner, evaluator)
    codes = (_function_code(runner), _function_code(evaluator))
    if snapshots is None or identities is None:
        raise FileRunError("业务运行与评分函数需要可记录的源码和函数身份")
    integrity_error = []

    def check_conditions():
        try:
            spec.baseline.assert_current()
            if (callback_snapshot(runner, evaluator) != snapshots or
                    callback_identity(runner, evaluator) != identities or
                    (_function_code(runner), _function_code(evaluator)) != codes):
                raise FileRunError("业务运行或评分函数在比较期间发生变化")
        except Exception as exc:
            integrity_error.append(type(exc).__name__)
            raise FileRunError("文件比较条件发生变化，不能返回完整成功") from exc

    def enrich(state):
        result = copy.deepcopy(state)
        result.update(candidateKind="files", fileCandidates=copy.deepcopy(packages),
                      callbackSnapshot=copy.deepcopy(snapshots),
                      callbackIdentity=copy.deepcopy(identities), adoptable=False,
                      baselineFileCandidateDigest=baseline_candidate.digest,
                      fileBaselineDigest=spec.baseline.digest)
        selected = token_candidates.get(result.get("bestPrompt"))
        result["selectedFileCandidateDigest"] = selected.digest if selected is not None else None
        return result

    def publish(state):
        check_conditions()
        if on_update is not None:
            on_update(enrich(state))
        check_conditions()

    def run_files(token, input_value):
        check_conditions()
        candidate = token_candidates.get(token)
        if candidate is None:
            integrity_error.append("candidate-identity")
            raise FileRunError("内部文件候选身份不匹配")
        root = Path(tempfile.mkdtemp(prefix="fuju-rsi-files-"))
        try:
            _materialize(root, candidate.files)
            before = _tree_fingerprint(root, candidate.files)
            expected = {name: _hash(content.encode("utf-8")) for name, content in candidate.files.items()}
            context = FileRunContext(root, candidate.digest, expected)
            try:
                value = runner(context, input_value)
            finally:
                # 即使业务抛出异常或取消，也不能漏掉数据漂移检查与清理。
                try:
                    check_conditions()
                    if _tree_fingerprint(root, candidate.files) != before:
                        raise FileRunError("候选输入目录被修改；输出和缓存请放在另外路径")
                except Exception:
                    integrity_error.append("scratch-or-source-drift")
                    raise
            if not isinstance(value, FileRunResult) or not isinstance(value.prediction, Prediction):
                raise FileRunError("runner 必须返回 FileRunResult(Prediction, loaded_hashes)")
            if type(value.loaded_hashes) is not dict or value.loaded_hashes != expected:
                raise FileRunError("实际已加载文件摘要缺失或与候选不一致")
            return value.prediction
        finally:
            _clean_scratch(root)

    def propose_files(previous, feedback, index):
        check_conditions()
        return tokens[index]

    agent = AgentSpec(spec.id, spec.name, tokens[0], spec.examples, run_files,
                      propose_files, evaluator, kind=spec.kind, candidate_kind="files")
    options = dict(max_trials=len(candidates), max_calls=max_calls,
                   on_update=publish, is_cancelled=is_cancelled)
    if spec.kind == "ask-data":
        result = optimize_ask_data(agent, bundle_dir=ask_data_bundle,
                                   ledger=validation_ledger, **options)
    else:
        result = optimize(agent, **options)
    result = enrich(result)
    try:
        check_conditions()
    except FileRunError:
        pass
    if integrity_error:
        result.update(status="failed", searchComplete=False, adoptable=False,
                      message="原版、候选目录或业务回调发生变化，文件比较不能返回完整成功。")
    if on_update is not None:
        try:
            on_update(copy.deepcopy(result))
            check_conditions()
        except Exception:
            result.update(status="failed", searchComplete=False,
                          message="无法保存文件比较终态，已保留本地记录。")
    if is_cancelled is not None:
        try:
            if is_cancelled():
                result.update(status="cancelled", searchComplete=False, adoptable=False,
                              message="文件比较已取消，当前业务文件未由工具修改。")
        except Exception:
            result.update(status="failed", searchComplete=False, adoptable=False,
                          message="无法读取取消状态，文件比较不能返回完整成功。")
    try:
        check_conditions()
    except FileRunError:
        result.update(status="failed", searchComplete=False, adoptable=False,
                      message="文件比较结束时原版或业务回调发生变化，不能返回完整成功。")
    return result


def _public_batch(batch):
    if batch is None:
        return None
    value = {key: batch[key] for key in ("score", "passed", "total", "latencyMs", "tokens", "costUsd")}
    value.update(completedCases=len(batch["cases"]),
                 runtimeErrors=sum(item["error"] is not None for item in batch["cases"]))
    return value


def _public_result(result):
    value = {key: copy.deepcopy(result[key]) for key in (
        "agentId", "status", "searchComplete", "adoptable", "usedCalls", "message",
        "candidateKind", "selectedTrialId", "selectedFileCandidateDigest",
        "baselineFileCandidateDigest", "fileBaselineDigest", "evidenceStatus",
    ) if key in result}
    value["adoptable"] = False
    value["trials"] = [{"id": item["id"], "label": item["label"],
                        "training": _public_batch(item.get("training")),
                        "validation": _public_batch(item.get("validation"))}
                       for item in result.get("trials", [])]
    if "askDataValidation" in result:
        value["askDataValidation"] = copy.deepcopy(result["askDataValidation"])
    return value


def _safe_destination(path):
    path = Path(path).absolute()
    for component in (path,) + tuple(path.parents):
        if component.is_symlink():
            # macOS 的标准临时目录经过 /var 或 /tmp 系统别名；允许这两个已知
            # 系统入口，仍拒绝用户报告目录中的链接。最终输出使用规范绝对路径。
            if (component != path and sys.platform == "darwin" and
                    str(component) in ("/var", "/tmp") and
                    str(component.resolve()) in ("/private/var", "/private/tmp")):
                continue
            raise FileRunError("报告目录及父目录不能是符号链接")
    if path.exists():
        raise FileExistsError(str(path))
    if not path.parent.is_dir():
        raise FileRunError("报告父目录必须已存在")
    return path.parent.resolve() / path.name


def export_file_report(result, output_dir):
    """输出普通配置与原版恢复材料；公开摘要不包含题目、答案和结果明细。"""
    if type(result) is not dict or result.get("candidateKind") != "files":
        raise FileRunError("只能导出文件候选比较结果")
    packages = result.get("fileCandidates")
    if type(packages) is not dict:
        raise FileRunError("文件候选包缺失")
    selected = result.get("selectedFileCandidateDigest")
    baseline_id = result.get("baselineFileCandidateDigest")
    if selected not in packages or baseline_id not in packages:
        raise FileRunError("选中候选或原版包缺失")
    # 重新解析模型并验证两份包，不信任调用方改写后的 result 字典。
    output = _safe_destination(output_dir)
    baseline_package = packages[baseline_id]
    candidate_package = packages[selected]
    # 模型公开包保留原版，from_json 需要原目录快照。导出时使用私有临时原版目录。
    temporary_root = Path(tempfile.mkdtemp(prefix="fuju-rsi-export-input-"))
    staging = None
    reserved = None
    try:
        baseline_files = baseline_package.get("files")
        _validate_export_files(baseline_files)
        _materialize(temporary_root, baseline_files)
        from .file_candidates import FileTarget
        targets = [FileTarget(target["path"], target["kind"], target["jsonPointers"])
                   for target in baseline_package["targets"]]
        baseline = FileBaseline.capture(temporary_root, targets,
                                        context_files=baseline_package.get("contextFiles", []))
        baseline_candidate = FileCandidate.from_json(json.dumps(baseline_package, ensure_ascii=False), baseline)
        candidate = FileCandidate.from_json(json.dumps(candidate_package, ensure_ascii=False), baseline)
        if baseline_candidate.digest != baseline_id or candidate.digest != selected:
            raise FileRunError("报告选中候选身份不匹配")
        for field, expected_candidate in (("baselinePrompt", baseline_candidate), ("bestPrompt", candidate)):
            encoded = result.get(field)
            if not isinstance(encoded, str):
                raise FileRunError("文件比较记录缺少内部候选身份")
            recorded = FileCandidate.from_json(encoded, baseline)
            if recorded.to_dict() != expected_candidate.to_dict():
                raise FileRunError("报告选中候选与比较记录不一致")
        selected_trials = [item for item in result.get("trials", [])
                           if item.get("id") == result.get("selectedTrialId")]
        if len(selected_trials) != 1 or selected_trials[0].get("prompt") != result["bestPrompt"]:
            raise FileRunError("报告选中方案与比较记录不一致")
        staging = Path(tempfile.mkdtemp(prefix=".fuju-rsi-report-", dir=str(output.parent)))
        for label, files in (("baseline", baseline.files), ("candidate", candidate.files)):
            directory = staging / label
            directory.mkdir()
            for name, content in files.items():
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
        changes = []
        for name in sorted(baseline.files):
            changes.extend(difflib.unified_diff(
                baseline.files[name].splitlines(keepends=True),
                candidate.files[name].splitlines(keepends=True),
                fromfile="baseline/" + name, tofile="candidate/" + name))
        (staging / "changes.diff").write_text("".join(changes), encoding="utf-8")
        delivery = {"schemaVersion": 1, "type": "file-comparison-delivery",
                    "candidate": candidate.to_dict(), "baseline": baseline_candidate.to_dict()}
        (staging / "candidate.json").write_text(json.dumps(delivery, ensure_ascii=False,
                                                          sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")
        public = _public_result(result)
        (staging / "result.json").write_text(json.dumps(public, ensure_ascii=False,
                                                       allow_nan=False, indent=2) + "\n", encoding="utf-8")
        report = ["# 文件候选开发比较", "", "这份记录只说明已知案例或开发集比较，不是正式独立验收。",
                  "当前 adoptable=false；业务采用前仍需自己的验收、生效检查与恢复流程。",
                  "业务运行只需 candidate/ 中的普通文件，不需要安装 RSI。",
                  "baseline/ 保存原版文件；candidate.json 保存候选及原版恢复材料。",
                  "目录检查与文件摘要不是操作系统权限隔离，也不证明真实产品效果。", "",
                  "状态：%s；完整比较：%s；回调次数：%s。" % (
                      public.get("status"), public.get("searchComplete"), public.get("usedCalls")),
                  "", "|方案|训练评分|验证评分|运行错误数|", "|---|---:|---:|---:|"]
        for item in public["trials"]:
            report.append("|%s|%s|%s|%s|" % (item["label"],
                          item["training"]["score"] if item["training"] else "未完成",
                          item["validation"]["score"] if item["validation"] else "未完成",
                          sum(batch["runtimeErrors"] for batch in (item["training"], item["validation"])
                              if batch is not None)))
        report.append("\n交付摘要：%s；未选方案只在汇总中记录，不作为交付候选。\n" % selected)
        (staging / "report.md").write_text("\n".join(report), encoding="utf-8")
        # 排他预留，避免覆盖并发创建的目录；最终替换的目录只属于本次导出。
        output.mkdir(mode=0o700)
        reserved = output
        os.replace(str(staging), str(output))
        staging = None
        reserved = None
        return {"outputDir": str(output), "report": str(output / "report.md"),
                "result": str(output / "result.json"), "candidate": str(output / "candidate.json")}
    finally:
        _clean_scratch(temporary_root)
        if staging is not None:
            shutil.rmtree(str(staging))
        if reserved is not None:
            reserved.rmdir()
