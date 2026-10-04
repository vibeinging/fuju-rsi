"""保存配置原版和有限候选；摘要只能证明内容身份，不能证明业务口径正确。

首版仅承载词典、项目规则和指标上下文，不运行文件中的代码，也不修改
业务项目。声明 preserving 是提案者的说明，仍需原有业务判分和人工复核。
"""
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Tuple


MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * MAX_FILE_BYTES
SCHEMA_VERSION = 1
_KINDS = frozenset(("dictionary", "project-rules", "metric-context"))


class FileCandidateError(ValueError):
    """候选、范围、原版身份或文件边界不符合声明。"""


def _path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise FileCandidateError("文件路径须为规范相对路径")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise FileCandidateError("文件路径不能包含绝对路径、空目录、. 或 ..")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise FileCandidateError("文件路径须为有效 UTF-8") from exc
    return value


def _pointer(value: str) -> Tuple[str, ...]:
    if not isinstance(value, str) or (value and not value.startswith("/")):
        raise FileCandidateError("JSON 指针须为空字符串或以 / 开头")
    if not value:
        return ()
    tokens = []
    for encoded in value[1:].split("/"):
        index = 0
        while index < len(encoded):
            if encoded[index] == "~":
                if index + 1 >= len(encoded) or encoded[index + 1] not in "01":
                    raise FileCandidateError("JSON 指针仅允许 ~0 和 ~1 转义")
                index += 2
            else:
                index += 1
        tokens.append(encoded.replace("~1", "/").replace("~0", "~"))
    return tuple(tokens)


@dataclass(frozen=True)
class FileTarget:
    path: str
    kind: str
    json_pointers: Tuple[str, ...] = ()

    def __post_init__(self):
        _path(self.path)
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise FileCandidateError("目标类型只允许 dictionary/project-rules/metric-context")
        if isinstance(self.json_pointers, str):
            raise FileCandidateError("json_pointers 须为指针列表")
        try:
            pointers = tuple(self.json_pointers)
        except TypeError as exc:
            raise FileCandidateError("json_pointers 须为指针列表") from exc
        for pointer in pointers:
            _pointer(pointer)
        if len(set(pointers)) != len(pointers):
            raise FileCandidateError("JSON 指针不能重复")
        object.__setattr__(self, "json_pointers", tuple(sorted(pointers)))


def _declarations(targets: Iterable[FileTarget], context_files: Iterable[str]):
    if isinstance(targets, (str, bytes)) or isinstance(context_files, (str, bytes)):
        raise FileCandidateError("目标和只读文件须为列表")
    try:
        targets = tuple(targets)
        contexts = tuple(context_files)
    except TypeError as exc:
        raise FileCandidateError("目标和只读文件须为列表") from exc
    if any(not isinstance(target, FileTarget) for target in targets):
        raise FileCandidateError("每个目标须为 FileTarget")
    paths = [target.path for target in targets] + [_path(path) for path in contexts]
    if len(paths) != len(set(paths)):
        raise FileCandidateError("目标和只读文件路径不能重复")
    prefixes = {}
    for path in paths:
        parts = tuple(path.split("/"))
        for length in range(1, len(parts) + 1):
            prefix = parts[:length]
            # macOS 文件系统可能把组合字符和预组合字符当成同一个名字。
            folded = tuple(unicodedata.normalize("NFC", part).casefold() for part in prefix)
            previous = prefixes.get(folded)
            if previous is not None and previous != prefix:
                raise FileCandidateError("文件或目录路径不能存在大小写冲突")
            prefixes[folded] = prefix
        if any("/".join(parts[:length]) in paths for length in range(1, len(parts))):
            raise FileCandidateError("声明文件不能同时作为另一个文件的父目录")
    return tuple(sorted(targets, key=lambda target: target.path)), tuple(sorted(contexts))


def _same_file(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _is_link(item):
    # Windows 的目录 junction 也是路径转向，不能绕过只读范围检查。
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(item.st_mode) or bool(getattr(item, "st_file_attributes", 0) & reparse)


def _check_chain(root: Path, path: str):
    """lstat 不跟随链接，逐级确认声明路径的目录和文件。"""
    chain = []
    current = root
    root_stat = current.lstat()
    if not stat.S_ISDIR(root_stat.st_mode) or _is_link(root_stat):
        raise FileCandidateError("项目根目录须为非符号链接目录")
    chain.append((current, root_stat))
    parts = path.split("/")
    for index, part in enumerate(parts):
        current = current / part
        item = current.lstat()
        wanted = stat.S_ISREG if index == len(parts) - 1 else stat.S_ISDIR
        if _is_link(item) or not wanted(item.st_mode):
            raise FileCandidateError("声明路径只能经过普通目录并指向普通文件: " + path)
        chain.append((current, item))
    if chain[-1][1].st_size > MAX_FILE_BYTES:
        raise FileCandidateError("单个文件超过 1 MiB: " + path)
    return chain


def _read_file(root: Path, path: str) -> str:
    directory_fd = None
    file_fd = None
    try:
        chain = _check_chain(root, path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        file_flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
        if os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY"):
            # 持有每级目录句柄，防止检查后父目录被换成链接而读到范围外内容。
            directory_flags = os.O_RDONLY | os.O_DIRECTORY | nofollow
            directory_fd = os.open(str(root), directory_flags)
            if not _same_file(os.fstat(directory_fd), chain[0][1]):
                raise FileCandidateError("读取期间项目根目录发生变化")
            for index, part in enumerate(path.split("/")[:-1], 1):
                next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
                if not _same_file(os.fstat(directory_fd), chain[index][1]):
                    raise FileCandidateError("读取期间父目录发生变化: " + path)
            file_fd = os.open(path.split("/")[-1], file_flags, dir_fd=directory_fd)
        else:
            file_fd = os.open(str(root / path), file_flags)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode) or not _same_file(before, chain[-1][1]):
            raise FileCandidateError("读取期间文件发生替换: " + path)
        if before.st_size > MAX_FILE_BYTES:
            raise FileCandidateError("单个文件超过 1 MiB: " + path)
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_FILE_BYTES:
            raise FileCandidateError("单个文件超过 1 MiB: " + path)
        after = os.fstat(file_fd)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise FileCandidateError("读取期间文件内容发生变化: " + path)
        for (previous_path, previous_stat), (current_path, current_stat) in zip(
                chain, _check_chain(root, path)):
            if previous_path != current_path or not _same_file(previous_stat, current_stat):
                raise FileCandidateError("读取期间声明路径发生变化: " + path)
        return data.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise FileCandidateError("声明文件缺失、无法读取或不是有效 UTF-8: " + path) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if directory_fd is not None:
            os.close(directory_fd)


def _duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise FileCandidateError("JSON 不能有重复字段")
        value[key] = item
    return value


def _invalid_constant(value):
    raise FileCandidateError("JSON 不能包含 NaN 或 Infinity")


def _finite(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise FileCandidateError("JSON 数值须为有限值")
    if isinstance(value, dict):
        for item in value.values():
            _finite(item)
    elif isinstance(value, list):
        for item in value:
            _finite(item)


def _json(text: str):
    if not isinstance(text, str):
        raise FileCandidateError("JSON 内容须为文本")
    try:
        value = json.loads(text, object_pairs_hook=_duplicate_keys, parse_constant=_invalid_constant)
        _finite(value)
        return value
    except (ValueError, RecursionError) as exc:
        if isinstance(exc, FileCandidateError):
            raise
        raise FileCandidateError("JSON 内容无法解析") from exc


def _changes(before, after, prefix=()):
    if type(before) is not type(after):
        yield prefix
    elif isinstance(before, dict):
        for key in sorted(set(before) | set(after)):
            child = prefix + (key,)
            if key not in before or key not in after:
                yield child
            else:
                yield from _changes(before[key], after[key], child)
    elif isinstance(before, list):
        for index in range(max(len(before), len(after))):
            child = prefix + (str(index),)
            if index >= len(before) or index >= len(after):
                yield child
            else:
                yield from _changes(before[index], after[index], child)
    elif before != after:
        yield prefix


def _check_json_scope(target: FileTarget, before: str, after: str):
    if not target.json_pointers:
        return
    original = _json(before)
    proposed = _json(after)
    allowed = tuple(_pointer(pointer) for pointer in target.json_pointers)
    try:
        for change in _changes(original, proposed):
            if not any(change[:len(scope)] == scope for scope in allowed):
                raise FileCandidateError("JSON 改动超出许可范围: " + target.path)
    except RecursionError as exc:
        raise FileCandidateError("JSON 结构嵌套过深") from exc


def _check_files(files: Mapping[str, str]):
    total = 0
    for path, text in files.items():
        _path(path)
        if not isinstance(text, str):
            raise FileCandidateError("文件内容须为 UTF-8 文本: " + path)
        try:
            length = len(text.encode("utf-8"))
        except UnicodeError as exc:
            raise FileCandidateError("文件内容须为有效 UTF-8: " + path) from exc
        if length > MAX_FILE_BYTES:
            raise FileCandidateError("单个文件超过 1 MiB: " + path)
        total += length
        if total > MAX_TOTAL_BYTES:
            raise FileCandidateError("声明文件总量超过 4 MiB")


def _target_documents(targets):
    return [{"path": target.path, "kind": target.kind, "jsonPointers": list(target.json_pointers)}
            for target in targets]


def _serialize(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise FileCandidateError("候选内容无法编码") from exc


def _digest(value):
    try:
        return hashlib.sha256(_serialize(value).encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise FileCandidateError("候选元数据须为有效 UTF-8") from exc


@dataclass(frozen=True, init=False)
class FileBaseline:
    root: Path
    targets: Tuple[FileTarget, ...]
    context_files: Tuple[str, ...]
    files: Mapping[str, str]
    digest: str

    @classmethod
    def capture(cls, root, targets, context_files=()):
        targets, contexts = _declarations(targets, context_files)
        try:
            root = Path(root).absolute()
            root_stat = root.lstat()
        except (OSError, TypeError, ValueError) as exc:
            raise FileCandidateError("项目根目录不存在或无效") from exc
        if _is_link(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
            raise FileCandidateError("项目根目录须为非符号链接目录")
        files = {}
        total = 0
        for path in sorted([target.path for target in targets] + list(contexts)):
            text = _read_file(root, path)
            total += len(text.encode("utf-8"))
            if total > MAX_TOTAL_BYTES:
                raise FileCandidateError("声明文件总量超过 4 MiB")
            files[path] = text
        for target in targets:
            _check_json_scope(target, files[target.path], files[target.path])
        payload = {"schemaVersion": SCHEMA_VERSION, "type": "file-baseline",
                   "targets": _target_documents(targets), "contextFiles": list(contexts), "files": files}
        result = object.__new__(cls)
        for name, value in (("root", root), ("targets", targets), ("context_files", contexts),
                            ("files", MappingProxyType(files)), ("digest", _digest(payload))):
            object.__setattr__(result, name, value)
        return result

    def assert_current(self):
        try:
            current = self.root.lstat()
        except OSError as exc:
            raise FileCandidateError("原项目根目录已缺失") from exc
        if _is_link(current) or not stat.S_ISDIR(current.st_mode):
            raise FileCandidateError("原项目根目录须保持为非符号链接目录")
        for path, text in self.files.items():
            if _read_file(self.root, path) != text:
                raise FileCandidateError("原版文件内容已变化，请重新捕获原版: " + path)

    def as_candidate(self):
        return FileCandidate.propose(self, {}, rationale="原版对照，不修改业务内容。")


@dataclass(frozen=True, init=False)
class FileCandidate:
    baseline_digest: str
    targets: Tuple[FileTarget, ...]
    context_files: Tuple[str, ...]
    files: Mapping[str, str]
    semantics: str
    rationale: str
    digest: str

    @classmethod
    def propose(cls, baseline: FileBaseline, replacements: Dict[str, str], *,
                semantics="preserving", rationale):
        if not isinstance(baseline, FileBaseline):
            raise FileCandidateError("原版须通过 FileBaseline.capture 生成")
        baseline.assert_current()
        if semantics != "preserving":
            raise FileCandidateError("首版只允许声明 preserving；业务含义变化须另行确认与建基线")
        if not isinstance(rationale, str) or not rationale.strip():
            raise FileCandidateError("候选须提供非空修改理由")
        if not isinstance(replacements, dict):
            raise FileCandidateError("替换内容须为 path→text 字典")
        files = dict(baseline.files)
        targets = {target.path: target for target in baseline.targets}
        for path, text in replacements.items():
            _path(path)
            if path not in targets:
                raise FileCandidateError("只能替换已声明目标，不能修改只读文件或新增文件: " + path)
            if not isinstance(text, str):
                raise FileCandidateError("替换内容须为文本，不能删除文件: " + path)
            files[path] = text
        _check_files(files)
        for target in baseline.targets:
            _check_json_scope(target, baseline.files[target.path], files[target.path])
        result = object.__new__(cls)
        for name, value in (("baseline_digest", baseline.digest), ("targets", baseline.targets),
                            ("context_files", baseline.context_files), ("files", MappingProxyType(files)),
                            ("semantics", semantics), ("rationale", rationale)):
            object.__setattr__(result, name, value)
        object.__setattr__(result, "digest", _digest(result._payload()))
        return result

    def _payload(self):
        return {"schemaVersion": SCHEMA_VERSION, "type": "file-candidate",
                "baselineDigest": self.baseline_digest, "targets": _target_documents(self.targets),
                "contextFiles": list(self.context_files), "files": dict(self.files),
                "semantics": self.semantics, "rationale": self.rationale}

    def to_dict(self) -> Dict[str, Any]:
        document = self._payload()
        document["digest"] = self.digest
        return document

    def to_json(self) -> str:
        return _serialize(self.to_dict())

    @classmethod
    def from_json(cls, text: str, baseline: FileBaseline):
        document = _json(text)
        expected = {"schemaVersion", "type", "baselineDigest", "targets", "contextFiles", "files",
                    "semantics", "rationale", "digest"}
        if not isinstance(document, dict) or set(document) != expected:
            raise FileCandidateError("候选文档字段与 schemaVersion=1 不一致")
        if (type(document["schemaVersion"]) is not int or document["schemaVersion"] != SCHEMA_VERSION
                or document["type"] != "file-candidate"):
            raise FileCandidateError("不支持的候选类型或版本")
        if not isinstance(baseline, FileBaseline):
            raise FileCandidateError("原版须通过 FileBaseline.capture 生成")
        if document["baselineDigest"] != baseline.digest:
            raise FileCandidateError("候选绑定的原版摘要不一致")
        if (document["targets"] != _target_documents(baseline.targets)
                or document["contextFiles"] != list(baseline.context_files)):
            raise FileCandidateError("候选绑定的目标或只读范围不一致")
        files = document["files"]
        if not isinstance(files, dict) or set(files) != set(baseline.files):
            raise FileCandidateError("候选文件集合不一致，不能新增或删除文件")
        _check_files(files)
        if any(files[path] != baseline.files[path] for path in baseline.context_files):
            raise FileCandidateError("候选不能修改只读上下文")
        replacements = {target.path: files[target.path] for target in baseline.targets
                        if files[target.path] != baseline.files[target.path]}
        result = cls.propose(baseline, replacements, semantics=document["semantics"],
                             rationale=document["rationale"])
        if document["digest"] != result.digest:
            raise FileCandidateError("候选摘要校验失败")
        return result
