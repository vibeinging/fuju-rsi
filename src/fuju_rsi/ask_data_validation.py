"""问数开发题的跨实验使用账本。

一次搜索开始前，保守预留基线和全部候选对验证题的访问次数。失败、取消、
重复候选都不退还；换 Benchmark 版本但复用原题、来源或家族也继承历史。
本机账本不是文件权限或对恶意操作者的防护。
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from .benchmark import case_tokens, load_bundle, read_json
from .core import AgentSpec, Example, optimize, validate_spec


class ValidationUseError(ValueError):
    pass


def _save(path, value):
    path = Path(path)
    content = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(",", ":")) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".validation-", dir=str(path.parent))
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _lock(path):
    stream = open(path, "a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"1")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield
    finally:
        if acquired and os.name == "nt":
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


class ValidationLedger:
    """同一问数项目应长期复用一个目录；上限创建时固定。"""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.metadata_path = self.directory / "metadata.json"
        self.uses_path = self.directory / "uses.json"
        self.lock_path = self.directory / ".validation.lock"
        metadata = read_json(self.metadata_path)
        if (type(metadata) is not dict or set(metadata) !=
                {"schemaVersion", "ledgerId", "maxExposures"} or
                metadata["schemaVersion"] != 1 or
                type(metadata["ledgerId"]) is not str or
                not re.fullmatch(r"[a-f0-9]{32}", metadata["ledgerId"]) or
                type(metadata["maxExposures"]) is not int or metadata["maxExposures"] < 1):
            raise ValidationUseError("问数验证账本元数据无效")
        self.metadata = metadata
        self._read_uses()

    @classmethod
    def initialize(cls, directory, *, max_exposures):
        if type(max_exposures) is not int or max_exposures < 1:
            raise ValidationUseError("max_exposures 必须是正整数")
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        metadata = {"schemaVersion": 1, "ledgerId": uuid.uuid4().hex,
                    "maxExposures": max_exposures}
        _save(directory / "metadata.json", metadata)
        _save(directory / "uses.json", {"schemaVersion": 1,
                                        "ledgerId": metadata["ledgerId"],
                                        "revision": 0, "tokens": {}})
        return cls(directory)

    def _read_uses(self):
        uses = read_json(self.uses_path)
        if (type(uses) is not dict or set(uses) !=
                {"schemaVersion", "ledgerId", "revision", "tokens"} or
                uses["schemaVersion"] != 1 or uses["ledgerId"] != self.metadata["ledgerId"] or
                type(uses["revision"]) is not int or uses["revision"] < 0 or
                type(uses["tokens"]) is not dict):
            raise ValidationUseError("问数验证使用记录无效")
        for token, row in uses["tokens"].items():
            if (type(token) is not str or
                    not re.fullmatch(r"(?:input|source|group):[a-f0-9]{64}", token) or
                    type(row) is not dict or set(row) != {"split", "uses"} or
                    row["split"] not in ("train", "validation") or
                    type(row["uses"]) is not int or row["uses"] < 0 or
                    row["uses"] > self.metadata["maxExposures"] or
                    (row["split"] == "train" and row["uses"] != 0)):
                raise ValidationUseError("问数验证使用记录条目无效")
        return uses

    def reserve_run(self, examples, *, planned_exposures):
        if type(planned_exposures) is not int or planned_exposures < 1:
            raise ValidationUseError("计划的验证次数必须是正整数")
        if type(examples) is not list or not examples:
            raise ValidationUseError("需要完整的开发案例")
        current = {}
        for example in examples:
            if (not isinstance(example, Example) or
                    example.split not in ("train", "validation") or
                    not example.source_id or not example.group_id):
                raise ValidationUseError("问数案例需要开发用途、稳定来源和家族")
            for token in case_tokens(example):
                if token in current and current[token] != example.split:
                    raise ValidationUseError("同一输入、来源或家族不能跨训练和验证")
                current[token] = example.split
        if "validation" not in current.values():
            raise ValidationUseError("缺少验证来源")
        with _lock(self.lock_path):
            uses = self._read_uses()
            for token, split in current.items():
                previous = uses["tokens"].get(token)
                if previous and previous["split"] != split:
                    raise ValidationUseError("旧版本已将同一输入、来源或家族用于另一用途")
                count = previous["uses"] if previous else 0
                if split == "validation" and count + planned_exposures > self.metadata["maxExposures"]:
                    raise ValidationUseError("验证题访问次数已达固定上限；需要新的独立来源")
            for token, split in current.items():
                previous = uses["tokens"].get(token)
                uses["tokens"][token] = {"split": split,
                                         "uses": (previous["uses"] if previous else 0) +
                                         (planned_exposures if split == "validation" else 0)}
            uses["revision"] += 1
            _save(self.uses_path, uses)
        validation_counts = [uses["tokens"][token]["uses"] for token, split in current.items()
                             if split == "validation"]
        return {"ledgerId": self.metadata["ledgerId"], "revision": uses["revision"],
                "plannedExposures": planned_exposures,
                "validationTokenCount": sum(split == "validation" for split in current.values()),
                "maxUsedExposures": max(validation_counts),
                "maxExposures": self.metadata["maxExposures"]}

    def development_tokens(self):
        with _lock(self.lock_path):
            return sorted(self._read_uses()["tokens"])


def optimize_ask_data(spec: AgentSpec, *, bundle_dir, ledger,
                      max_trials=3, max_calls=100, baseline_prompt=None,
                      on_update=None, is_cancelled=None):
    """仅对已冻结的开发包运行，先记下全部可能的验证集访问。"""
    validate_spec(spec)
    if spec.kind != "ask-data":
        raise ValidationUseError("AgentSpec.kind 必须是 ask-data")
    if type(max_trials) is not int or max_trials < 0:
        raise ValidationUseError("max_trials 必须是非负整数")
    if type(max_calls) is not int or max_calls < 0:
        raise ValidationUseError("max_calls 必须是非负整数")
    manifest, rows = load_bundle(bundle_dir)
    if manifest.get("role") != "development" or manifest.get("schemaVersion") != 2:
        raise ValidationUseError("问数搜索只接受 v2 开发包")
    if [vars(example) for example in spec.examples] != rows:
        raise ValidationUseError("Agent 案例必须与冻结开发包逐项一致")
    registry = ledger if isinstance(ledger, ValidationLedger) else ValidationLedger(ledger)
    reservation = registry.reserve_run(spec.examples, planned_exposures=max_trials + 1)
    result = optimize(spec, max_trials=max_trials, max_calls=max_calls,
                      baseline_prompt=baseline_prompt, on_update=on_update,
                      is_cancelled=is_cancelled)
    result["askDataValidation"] = {"benchmarkDigest": manifest["digest"], **reservation}
    result["developmentHistoryTokens"] = registry.development_tokens()
    return result
