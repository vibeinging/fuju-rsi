"""本机项目共用的保留题使用记录；先保存使用状态，再允许调用被测程序。

这里防止的是正常执行中的重复验收和历史文件意外损坏。文件校验不是签名；
手动重写或恢复整个目录、换机器、使用另一个未关联目录，均超出本地记录范围。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid


class RegistryError(ValueError):
    """历史缺失、内容冲突或状态不允许时，阻止继续验收。"""


_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TOKEN = re.compile(r"(?:input|source|group):[a-f0-9]{64}\Z")
_METADATA = "metadata.json"
_LEDGER = "ledger.json"
_LOCK = ".holdout.lock"
_METADATA_KEYS = {"schemaVersion", "registryId", "authorityId", "createdAt"}
_LEDGER_KEYS = {"schemaVersion", "registryId", "authorityId", "revision", "jobs"}
_JOB_KEYS = {"jobId", "fingerprint", "tokens", "status", "reservedAt", "updatedAt",
             "startedAt", "finishedAt", "result"}
_STATES = {"reserved", "running", "consumed", "interrupted", "released"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value, name):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise RegistryError(name + " 必须是 1–160 位字母、数字、下划线、连字符、点或冒号")
    return value


def _fingerprint(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise RegistryError("fingerprint 必须是小写 SHA-256 摘要")
    return value


def _tokens(values):
    if not isinstance(values, list) or not values:
        raise RegistryError("tokens 必须是非空列表")
    if any(not isinstance(value, str) or not _TOKEN.fullmatch(value) for value in values):
        raise RegistryError("token 必须使用 input/source/group 前缀和小写 SHA-256 摘要")
    # 调用方可重复提交同一来源；持久化后保持唯一且有序，确保快照比较稳定。
    return sorted(set(values))


def _check_json(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json(item)
        return
    raise RegistryError("记录只能包含有效的 JSON 值，且对象的键必须是字符串")


def _encoded(value):
    try:
        _check_json(value)
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise RegistryError("无法编码验收记录") from exc


def _envelope(payload):
    return {"payload": payload, "sha256": hashlib.sha256(_encoded(payload)).hexdigest()}


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RegistryError("验收记录包含重复字段")
        value[key] = item
    return value


def _regular_file(path):
    try:
        info = path.lstat()
    except OSError as exc:
        raise RegistryError("验收历史文件缺失或不可读：" + path.name) from exc
    if not stat.S_ISREG(info.st_mode):
        raise RegistryError("验收历史必须保存在普通文件中：" + path.name)
    return info


def _read_payload(path):
    _regular_file(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or set(value) != {"payload", "sha256"}:
            raise RegistryError("验收历史文件结构无效：" + path.name)
        if not isinstance(value["sha256"], str) or not _HASH.fullmatch(value["sha256"]):
            raise RegistryError("验收历史文件摘要无效：" + path.name)
        if hashlib.sha256(_encoded(value["payload"])).hexdigest() != value["sha256"]:
            raise RegistryError("验收历史文件内容校验失败：" + path.name)
        return value["payload"]
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, RegistryError):
            raise
        raise RegistryError("验收历史文件损坏或不可读：" + path.name) from exc


def _atomic_write_json(path, value):
    """临时文件与目标同目录；文件和目录均 fsync，不把内存成功当成落盘成功。"""
    data = _encoded(value) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _locked(directory, *, initialize=False):
    path = directory / _LOCK
    stream = None
    try:
        if initialize:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            stream = os.fdopen(fd, "r+b")
            stream.write(b"1")
            stream.flush()
            os.fsync(stream.fileno())
        else:
            _regular_file(path)
            # 不使用 a+b，缺失锁文件意味着历史不完整，不能自动新建另一把锁。
            stream = path.open("r+b")
    except OSError as exc:
        if stream is not None:
            stream.close()
        raise RegistryError("验收记录未初始化、已存在或无法打开本地锁") from exc
    try:
        if os.name == "nt":
            import msvcrt
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    time.sleep(0.025)
        elif os.name == "posix":
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        else:
            raise RegistryError("当前系统不支持验收记录需要的本地文件锁")
        actual = os.fstat(stream.fileno())
        current = _regular_file(path)
        if (actual.st_dev, actual.st_ino) != (current.st_dev, current.st_ino):
            raise RegistryError("等待期间验收锁文件被替换，拒绝继续")
        stream.seek(0)
        if stream.read() != b"1":
            raise RegistryError("验收锁文件损坏")
        yield
    except OSError as exc:
        raise RegistryError("无法锁定或持久化验收记录") from exc
    finally:
        # 关闭句柄释放 OS 锁；不删除锁文件，也不让锁覆盖整个模型调用。
        stream.close()


def _timestamp(value):
    if not isinstance(value, str):
        raise RegistryError("验收记录时间无效")
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
    except (ValueError, OverflowError):
        raise RegistryError("验收记录时间必须包含 UTC 时区") from None


def _validate_metadata(metadata):
    if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
        raise RegistryError("验收入口信息结构无效")
    if type(metadata["schemaVersion"]) is not int or metadata["schemaVersion"] != 1:
        raise RegistryError("不支持的验收记录版本")
    if not isinstance(metadata["registryId"], str) or not re.fullmatch(r"[a-f0-9]{32}", metadata["registryId"]):
        raise RegistryError("验收记录身份无效")
    _identifier(metadata["authorityId"], "authority_id")
    _timestamp(metadata["createdAt"])


def _validate_ledger(ledger, metadata):
    if not isinstance(ledger, dict) or set(ledger) != _LEDGER_KEYS:
        raise RegistryError("验收历史结构无效")
    if type(ledger["schemaVersion"]) is not int or ledger["schemaVersion"] != 1:
        raise RegistryError("不支持的验收历史版本")
    if any(ledger[key] != metadata[key] for key in ("registryId", "authorityId")):
        raise RegistryError("验收历史与当前入口不匹配")
    if type(ledger["revision"]) is not int or ledger["revision"] < 0 or not isinstance(ledger["jobs"], dict):
        raise RegistryError("验收历史修订号或任务结构无效")
    used = set()
    for job_id, row in ledger["jobs"].items():
        _identifier(job_id, "job_id")
        if not isinstance(row, dict) or set(row) != _JOB_KEYS or row["jobId"] != job_id:
            raise RegistryError("验收任务身份或结构无效")
        _fingerprint(row["fingerprint"])
        if _tokens(row["tokens"]) != row["tokens"]:
            raise RegistryError("验收历史 token 必须已排序且无重复")
        if not isinstance(row["status"], str) or row["status"] not in _STATES:
            raise RegistryError("验收任务状态无效")
        for key in ("reservedAt", "updatedAt"):
            _timestamp(row[key])
        for key in ("startedAt", "finishedAt"):
            if row[key] is not None:
                _timestamp(row[key])
        status = row["status"]
        if status in ("running", "consumed") and row["startedAt"] is None:
            raise RegistryError("验收任务缺少已开始记录")
        if status in ("reserved", "released") and row["startedAt"] is not None:
            raise RegistryError("未开始的验收任务含有错误的开始记录")
        if (status in ("consumed", "interrupted", "released")) != (row["finishedAt"] is not None):
            raise RegistryError("验收任务结束状态不一致")
        if status == "consumed":
            if not isinstance(row["result"], dict):
                raise RegistryError("已使用的验收任务缺少结果")
        elif row["result"] is not None:
            raise RegistryError("未完成的验收任务不能含有结果")
        if status != "released":
            if used.intersection(row["tokens"]):
                raise RegistryError("验收历史中出现重复占用的内容或来源")
            used.update(row["tokens"])


class HoldoutRegistry:
    """使用同一项目目录的进程共享历史。构造函数绝不创建缺失记录。

    reserve 的 tokens 是 input:<sha256> / source:<sha256> / group:<sha256>。
    调用方负责按内容和来源生成摘要；名称变化不能影响摘要。start 必须发生在
    读取保留题正文或调用外部程序之前。运行、已使用和中断任务永远不能释放。
    """

    @staticmethod
    def initialize(directory, authority_id: str) -> dict:
        """显式创建一个新的本地记录范围；已存在或初始化中断的目录拒绝覆盖。"""
        _identifier(authority_id, "authority_id")
        directory = Path(directory).resolve()
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # 专用目录中存在任何旧文件，都不能当作没有使用历史。
            if any(directory.iterdir()):
                raise RegistryError("验收记录目录非空；请打开已有记录，不得重新初始化")
        except OSError as exc:
            raise RegistryError("无法创建验收记录目录") from exc
        with _locked(directory, initialize=True):
            metadata = {"schemaVersion": 1, "registryId": uuid.uuid4().hex,
                        "authorityId": authority_id, "createdAt": _now()}
            ledger = {"schemaVersion": 1, "registryId": metadata["registryId"],
                      "authorityId": authority_id, "revision": 0, "jobs": {}}
            _atomic_write_json(directory / _METADATA, _envelope(metadata))
            _atomic_write_json(directory / _LEDGER, _envelope(ledger))
        return deepcopy(metadata)

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self._guard = threading.RLock()
        self._metadata = None
        self._revision = -1
        with self._guard, _locked(self.directory):
            self._read()

    @property
    def metadata(self):
        return deepcopy(self._metadata)

    @property
    def authority_id(self):
        return self._metadata["authorityId"]

    def _read(self):
        metadata = _read_payload(self.directory / _METADATA)
        _validate_metadata(metadata)
        if self._metadata is not None and metadata != self._metadata:
            raise RegistryError("当前进程使用的验收入口记录已被替换")
        ledger = _read_payload(self.directory / _LEDGER)
        _validate_ledger(ledger, metadata)
        if ledger["revision"] < self._revision:
            raise RegistryError("验收历史被回退到较早版本")
        self._metadata = metadata
        self._revision = ledger["revision"]
        return ledger

    def _save(self, ledger):
        ledger["revision"] += 1
        _validate_ledger(ledger, self._metadata)
        _atomic_write_json(self.directory / _LEDGER, _envelope(ledger))
        self._revision = ledger["revision"]

    @staticmethod
    def _job(ledger, job_id):
        row = ledger["jobs"].get(job_id)
        if row is None:
            raise RegistryError("验收任务不存在")
        return row

    def reserve(self, job_id: str, fingerprint: str, tokens: list[str]) -> dict:
        _identifier(job_id, "job_id")
        _fingerprint(fingerprint)
        tokens = _tokens(tokens)
        with self._guard, _locked(self.directory):
            ledger = self._read()
            previous = ledger["jobs"].get(job_id)
            if previous is not None:
                if previous["fingerprint"] != fingerprint or previous["tokens"] != tokens:
                    raise RegistryError("同一验收任务不能更换候选、规则、内容或来源")
                if previous["status"] in ("reserved", "consumed"):
                    return deepcopy(previous)
                raise RegistryError("验收任务已开始、中断或释放，不能再次执行")
            incoming = set(tokens)
            for previous in ledger["jobs"].values():
                if previous["status"] != "released" and incoming.intersection(previous["tokens"]):
                    raise RegistryError("保留题的内容或来源已预约、使用或暴露，不能用于新验收")
            now = _now()
            row = {"jobId": job_id, "fingerprint": fingerprint, "tokens": tokens,
                   "status": "reserved", "reservedAt": now, "updatedAt": now,
                   "startedAt": None, "finishedAt": None, "result": None}
            ledger["jobs"][job_id] = row
            self._save(ledger)
            return deepcopy(row)

    def get(self, job_id: str) -> dict:
        _identifier(job_id, "job_id")
        with self._guard, _locked(self.directory):
            return deepcopy(self._job(self._read(), job_id))

    def start(self, job_id: str) -> dict:
        _identifier(job_id, "job_id")
        with self._guard, _locked(self.directory):
            ledger = self._read()
            row = self._job(ledger, job_id)
            if row["status"] != "reserved":
                raise RegistryError("只有已预约且未开始的验收任务可以开始")
            now = _now()
            row.update(status="running", startedAt=now, updatedAt=now)
            self._save(ledger)
            return deepcopy(row)

    def finish(self, job_id: str, result: dict) -> dict:
        _identifier(job_id, "job_id")
        if type(result) is not dict:
            raise RegistryError("验收结果必须是 JSON 对象")
        # 保存调用时快照，防止调用者随后修改结果影响账本或重试比较。
        result = json.loads(_encoded(result))
        with self._guard, _locked(self.directory):
            ledger = self._read()
            row = self._job(ledger, job_id)
            if row["status"] == "consumed" and _encoded(row["result"]) == _encoded(result):
                return deepcopy(row)
            if row["status"] != "running":
                raise RegistryError("只有正在运行的验收任务可以保存最终结果")
            now = _now()
            row.update(status="consumed", finishedAt=now, updatedAt=now, result=result)
            self._save(ledger)
            return deepcopy(row)

    def interrupt(self, job_id: str) -> dict:
        _identifier(job_id, "job_id")
        with self._guard, _locked(self.directory):
            ledger = self._read()
            row = self._job(ledger, job_id)
            if row["status"] == "interrupted":
                return deepcopy(row)
            if row["status"] not in ("reserved", "running"):
                raise RegistryError("已结束的验收任务不能改为中断")
            now = _now()
            row.update(status="interrupted", finishedAt=now, updatedAt=now)
            self._save(ledger)
            return deepcopy(row)

    def release(self, job_id: str) -> dict:
        """调用此方法即声明尚未读取保留题正文；只释放从未 start 的预约。

        这是受信任调用方的声明，不是文件权限隔离。保留已释放任务记录，后续
        使用同一批内容须分配新的任务编号，不能把旧任务改成另一次实验。
        """
        _identifier(job_id, "job_id")
        with self._guard, _locked(self.directory):
            ledger = self._read()
            row = self._job(ledger, job_id)
            if row["status"] != "reserved":
                raise RegistryError("只有从未读取保留题的预约可以释放")
            now = _now()
            row.update(status="released", finishedAt=now, updatedAt=now)
            self._save(ledger)
            return deepcopy(row)
