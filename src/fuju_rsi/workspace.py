"""优化实验的本地账本。原子文件保存结果，active 指针单独作为采用的提交点。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from .core import AgentSpec, optimize
from .telemetry import Telemetry


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value, *, mode=None):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            # 产品配置替换保留原读取权限；账本仍使用 mkstemp 的私有默认权限。
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        if os.name == "posix":
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ExperimentError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class ExperimentManager:
    def __init__(self, directory, agents, *, verification_authorities=None, project_root=None,
                 telemetry_mode="auto", telemetry_plugin=None, trace_sqlite_path=None,
                 ask_data_bundle=None, validation_ledger=None):
        agents = list(agents)
        if any(isinstance(agent, AgentSpec) and agent.candidate_kind != "prompt" for agent in agents):
            raise ValueError("配置候选请使用 compare-files，不能注册到提示词工作区")
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.telemetry = Telemetry(self.directory, mode=telemetry_mode, plugin=telemetry_plugin,
                                   trace_sqlite_path=trace_sqlite_path)
        self.records_dir = self.directory / "experiments"
        self.records_dir.mkdir(exist_ok=True)
        self._guard = threading.RLock()
        self._jobs = {}
        self._cancellations = {}
        self._closing = False
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.ask_data_bundle = Path(ask_data_bundle).resolve() if ask_data_bundle else None
        self.validation_ledger = Path(validation_ledger).resolve() if validation_ledger else None
        self.verification_authorities = dict(verification_authorities or {})
        self.agents = {}
        for agent in agents:
            if not isinstance(agent, AgentSpec) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", agent.id):
                raise ValueError("Agent 必须是 AgentSpec，id 仅允许字母、数字、下划线和连字符")
            if agent.candidate_kind != "prompt":
                raise ValueError("配置候选请使用 compare-files，不能注册到提示词工作区")
            if agent.id in self.agents:
                raise ValueError("Agent id 重复")
            self.agents[agent.id] = agent
        if not self.agents:
            raise ValueError("至少注册一个 Agent")
        self._lock_stream = open(self.directory / ".optimizer.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self._lock_stream.seek(0, os.SEEK_END) == 0:
                    self._lock_stream.write(b"1")
                    self._lock_stream.flush()
                self._lock_stream.seek(0)
                msvcrt.locking(self._lock_stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            self.telemetry.close()
            self._lock_stream.close()
            raise RuntimeError("优化工作区已被其他进程使用，或当前文件系统不支持本地锁") from None
        try:
            self._records = {}
            for path in self.records_dir.glob("*.json"):
                with path.open(encoding="utf-8") as stream:
                    record = json.load(stream)
                if record.get("id") != path.stem or not re.fullmatch(r"[a-f0-9]{32}", path.stem):
                    raise ValueError("实验记录 id 与文件名不一致")
                if record["status"] == "running":
                    record.update(status="interrupted", adoptable=False, message="上次服务退出时实验未完成；原版保持生效。", updatedAt=now())
                    atomic_json(path, record)
                self._records[path.stem] = record
            self._active_path = self.directory / "active-prompts.json"
            self._active = json.loads(self._active_path.read_text(encoding="utf-8")) if self._active_path.exists() else {}
        except BaseException:
            self.telemetry.close()
            self._lock_stream.close()
            raise

    def _save(self, record):
        atomic_json(self.records_dir / (record["id"] + ".json"), record)
        self._records[record["id"]] = deepcopy(record)

    def _agent(self, agent_id):
        if agent_id not in self.agents:
            raise ExperimentError("该 Agent 未在服务启动时注册", 404)
        return self.agents[agent_id]

    def _writable(self):
        if self._closing or self._lock_stream.closed:
            raise ExperimentError("服务正在停止或工作区已关闭", 503)

    def capabilities(self, trace_available=False):
        return {"available": True, "protocolVersion": 2, "verificationAvailable": False,
                "traceAvailable": trace_available, "telemetry": self.telemetry.health(), "agents": [
            {"id": a.id, "name": a.name, "description": a.description, "kind": a.kind,
             "baselinePrompt": self.active_prompt(a.id)["prompt"],
             "counts": {split: sum(ex.split == split for ex in a.examples) for split in ("train", "validation", "test")}}
            for a in self.agents.values()
        ]}

    def active_prompt(self, agent_id):
        with self._guard:
            agent = self._agent(agent_id)
            return deepcopy(self._active.get(agent_id, {"agentId": agent_id, "prompt": agent.baseline_prompt, "experimentId": None}))

    def get(self, experiment_id):
        with self._guard:
            record = self._records.get(experiment_id)
            if record is None:
                raise ExperimentError("实验不存在", 404)
            result = deepcopy(record)
            result.pop("previousActive", None)
            result.pop("callbackSnapshot", None)
            result.pop("callbackIdentity", None)
            result.pop("verificationReceipt", None)
            result.pop("frozenCandidate", None)
            result["legacyAdoptable"] = bool(record.get("adoptable")) if not record.get("stage") else False
            result["adoptable"] = self._qualified(record)
            if not record.get("stage"):
                result.update(evidenceStatus="not_verified", acceptancePolicyVersion=None, verificationId=None)
            result["adopted"] = self._active.get(record["agentId"], {}).get("experimentId") == experiment_id
            return result

    def _qualified(self, record):
        """所有入口使用当前证据判定；历史布尔值或客户端传值不能授予新资格。"""
        from .verification import validate_candidate, validate_receipt
        try:
            if record.get("candidateKind", "prompt") != "prompt":
                return False
            if record.get("status") != "completed" or record.get("stage") != "verification":
                return False
            receipt, candidate = record["verificationReceipt"], record["frozenCandidate"]
            authority = receipt["body"]["authorityId"]
            key = Path(self.verification_authorities[authority]).read_bytes()
            body = validate_receipt(receipt, key)
            validate_candidate(candidate, self.project_root)
            active = self._active.get(record["agentId"])
            if active and active.get("experimentId") != record["id"] and active["prompt"] != record["baselinePrompt"]:
                return False
            return bool(body["status"] == "completed" and body["adoptable"] and
                        body["evidenceStatus"] == "improved" and body["isolation"] == "independent" and
                        body["candidateDigest"] == candidate["digest"] and body["experimentId"] == record["id"] and
                        body["verificationId"] == candidate["id"] and
                        record["bestPrompt"] == candidate["candidatePrompt"] and
                        record["baselinePrompt"] == candidate["baselinePrompt"])
        except (KeyError, ValueError, TypeError, OSError):
            return False

    def freeze(self, experiment_id, *, source_files, environment, policy=None):
        from .verification import freeze_candidate, validate_candidate
        with self._guard:
            self._writable()
            self.get(experiment_id)
            record = deepcopy(self._records[experiment_id])
            if self.active_prompt(record["agentId"])["prompt"] != record["baselinePrompt"]:
                raise ExperimentError("当前基线已变化，需要重新搜索", 409)
            if record.get("frozenCandidate"):
                previous = record["frozenCandidate"]
                validate_candidate(previous, self.project_root)
                from .acceptance import AcceptancePolicy
                from .verification import _files
                if (previous["environment"] != environment or previous["policy"] != (policy or AcceptancePolicy()).to_dict() or
                        previous["files"] != _files(source_files, self.project_root)):
                    raise ExperimentError("该实验已固定候选，不能事后更换策略或环境", 409)
                return deepcopy(previous)
            agent = self._agent(record["agentId"])
            history_tokens = None
            if agent.kind == "ask-data":
                from .ask_data_validation import ValidationLedger
                if not record.get("validationLedger"):
                    raise ExperimentError("问数实验缺少共用验证账本", 409)
                ledger = ValidationLedger(record["validationLedger"])
                observed = record.get("askDataValidation")
                if (not isinstance(observed, dict) or
                        observed.get("ledgerId") != ledger.metadata["ledgerId"]):
                    raise ExperimentError("问数验证账本与实验记录不一致", 409)
                history_tokens = ledger.development_tokens()
            candidate = freeze_candidate(agent, record, source_files=source_files,
                                         environment=environment, policy=policy, root=self.project_root,
                                         development_history_tokens=history_tokens)
            record["frozenCandidate"] = candidate
            self._save(record)
            return deepcopy(candidate)

    def import_verification(self, receipt):
        from .verification import validate_candidate, validate_receipt
        with self._guard:
            self._writable()
            try:
                authority = receipt["body"]["authorityId"]
                key = Path(self.verification_authorities[authority]).read_bytes()
                body = validate_receipt(receipt, key)
                self.get(body["experimentId"])
                record = deepcopy(self._records[body["experimentId"]])
                candidate = record["frozenCandidate"]
                validate_candidate(candidate, self.project_root)
                if body["candidateDigest"] != candidate["digest"] or body["verificationId"] != candidate["id"]:
                    raise ValueError("验收结果不属于该候选")
                if record.get("verificationReceipt") and record["verificationReceipt"] != receipt:
                    raise ValueError("同一固定候选不能替换验收结论")
                if self.active_prompt(record["agentId"])["prompt"] != record["baselinePrompt"]:
                    raise ValueError("当前基线已变化")
            except (KeyError, ValueError, TypeError, OSError) as exc:
                raise ExperimentError("回执未获已登记验收者确认，或与当前快照不符", 409) from exc
            record.update(stage="verification", evidenceStatus=body["evidenceStatus"],
                          acceptancePolicyVersion=body["acceptancePolicyVersion"], verificationId=body["verificationId"],
                          evidence=body["evidence"], verificationReceipt=deepcopy(receipt),
                          verificationUsedCalls=body["usedCalls"], verificationPlannedCalls=body["plannedCalls"],
                          verificationTokens=body["tokens"], verificationCostUsd=body["costUsd"],
                          status=body["status"], adoptable=body["adoptable"],
                          message="；".join(body["evidence"].get("reasons", [])), updatedAt=now())
            self._save(record)
            return self.get(record["id"])

    def list(self):
        with self._guard:
            return {"items": [self.get(key) for key in sorted(self._records, key=lambda key: self._records[key]["createdAt"], reverse=True)]}

    def export_report(self, experiment_id, output=None):
        """导出当时核验的结果快照；只读实验，不重跑业务，也不需要 HTTP 服务。"""
        from .reporting import write_report
        with self._guard:
            self._writable()
            record = self.get(experiment_id)
            if record["status"] == "running":
                raise ExperimentError("请等待实验结束或取消后再导出报告", 409)
            directory = Path(output) if output is not None else (
                self.directory / "reports" / experiment_id /
                ((record.get("stage") or "legacy") + "-" + uuid.uuid4().hex[:12]))
            return write_report(record, directory)

    def start(self, *, agent_id, name=None, max_trials=3, max_calls=100):
        for value, maximum, label in [(max_trials, 20, "候选次数"), (max_calls, 10000, "调用次数")]:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
                raise ExperimentError(label + "必须是 0 到 " + str(maximum) + " 之间的整数")
        if name is not None and (not isinstance(name, str) or len(name) > 120):
            raise ExperimentError("实验名称最多 120 个字符")
        with self._guard:
            self._writable()
            agent = self._agent(agent_id)
            if agent.kind == "ask-data" and (self.ask_data_bundle is None or self.validation_ledger is None):
                raise ExperimentError("问数优化必须指定冻结开发包和共用验证账本")
            if any(r["agentId"] == agent_id and r["status"] == "running" for r in self._records.values()):
                raise ExperimentError("该 Agent 已有运行中的实验", 409)
            if len(self._jobs) >= 4:
                raise ExperimentError("同时最多运行 4 个实验", 429)
            eid = uuid.uuid4().hex
            prompt = self.active_prompt(agent_id)["prompt"]
            record = {"id": eid, "agentId": agent_id, "name": (name or "").strip() or agent.name + "的优化实验",
                      "createdAt": now(), "updatedAt": now(), "config": {"maxTrials": max_trials, "maxCalls": max_calls},
                      "kind": agent.kind, "status": "running", "baselinePrompt": prompt, "bestPrompt": prompt,
                      "selectedTrialId": "baseline", "adoptable": False, "adopted": False, "usedCalls": 0,
                      "message": "正在运行原版基线", "trials": [], "baselineTest": None, "candidateTest": None}
            record.update(stage="search", evidenceStatus="not_verified", acceptancePolicyVersion=None,
                          verificationId=None, searchComplete=False)
            if agent.kind == "ask-data":
                record["askDataBundle"] = str(self.ask_data_bundle)
                record["validationLedger"] = str(self.validation_ledger)
            self._save(record)
            self.telemetry.emit("experiment.started", experiment_id=eid, agent_id=agent_id,
                                status="running", used_calls=0)
            cancel = threading.Event()
            self._cancellations[eid] = cancel

            def update(snapshot, final=False):
                with self._guard:
                    current = deepcopy(self._records[eid])
                    current.update(snapshot)
                    # 核心的终态通知仍可能遇到取消或落盘异常；函数返回才发布采用资格。
                    if not final and current["status"] != "running":
                        current.update(status="running", adoptable=False, message="正在保存实验结果。")
                    if final and cancel.is_set():
                        current.update(status="cancelled", adoptable=False, message="实验已取消，原版保持生效。")
                    current["updatedAt"] = now()
                    self._save(current)

            def work():
                try:
                    if agent.kind == "ask-data":
                        from .ask_data_validation import optimize_ask_data
                        result = optimize_ask_data(
                            agent, bundle_dir=self.ask_data_bundle, ledger=self.validation_ledger,
                            max_trials=max_trials, max_calls=max_calls, baseline_prompt=prompt,
                            on_update=update, is_cancelled=cancel.is_set)
                    else:
                        result = optimize(agent, max_trials=max_trials, max_calls=max_calls,
                                          baseline_prompt=prompt, on_update=update,
                                          is_cancelled=cancel.is_set)
                    update(result, final=True)
                    self.telemetry.emit("experiment.finished", experiment_id=eid, agent_id=agent_id,
                                        status=result["status"], used_calls=result.get("usedCalls"))
                except BaseException as exc:
                    # 用户回调可能把 URL 或凭据带进异常；HTTP 账本不保存未经控制的堆栈。
                    from .ask_data_validation import ValidationUseError
                    message = (str(exc) if isinstance(exc, ValidationUseError) else
                               "实验执行失败，请检查 Agent、数据集或评分函数配置。")
                    status = "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed"
                    failure = {"status": status, "adoptable": False, "message": message}
                    try:
                        update(failure, final=True)
                    except BaseException:
                        # 磁盘满时仍要在本进程结束 running；磁盘上的旧记录重启后标为 interrupted。
                        with self._guard:
                            self._records[eid].update(status="failed", adoptable=False, updatedAt=now(),
                                                     message="无法保存实验结果，请检查工作区磁盘空间和写入权限；候选未采用。")
                    self.telemetry.emit("experiment.finished", experiment_id=eid, agent_id=agent_id,
                                        status=status)
                finally:
                    with self._guard:
                        self._jobs.pop(eid, None)
                        self._cancellations.pop(eid, None)
                        if self._closing and not self._jobs:
                            self.telemetry.close()
                            self._lock_stream.close()

            thread = threading.Thread(target=work, name="fuju-rsi-optimize-" + eid[:8], daemon=True)
            self._jobs[eid] = thread
            thread.start()
            return self.get(eid)

    def cancel(self, experiment_id):
        with self._guard:
            self._writable()
            record = self.get(experiment_id)
            event = self._cancellations.get(experiment_id)
            if event and record["status"] == "running":
                event.set()
                current = deepcopy(self._records[experiment_id])
                current.update(message="正在取消；等待当前调用返回。", updatedAt=now())
                self._save(current)
                record = self.get(experiment_id)
            return record

    def adopt(self, experiment_id):
        with self._guard:
            self._writable()
            record = self.get(experiment_id)
            if record.get("candidateKind", "prompt") != "prompt":
                raise ExperimentError("配置候选不能作为提示词采用", 409)
            active = self.active_prompt(record["agentId"])
            if active["experimentId"] == experiment_id:
                return record
            if record["status"] != "completed" or not record["adoptable"]:
                raise ExperimentError("只有完成独立验收并改善的候选可以采用", 409)
            if active["prompt"] != record["baselinePrompt"]:
                raise ExperimentError("生效版本已改变，请基于当前版本重新运行实验", 409)
            stored = deepcopy(self._records[experiment_id])
            stored["previousActive"] = active
            stored["updatedAt"] = now()
            self._save(stored)
            # 先保存回退依据，再原子切 active；active 是唯一生效判断依据。
            new_active = deepcopy(self._active)
            new_active[record["agentId"]] = {"agentId": record["agentId"], "prompt": record["bestPrompt"], "experimentId": experiment_id}
            atomic_json(self._active_path, new_active)
            self._active = new_active
            return self.get(experiment_id)

    def rollback(self, experiment_id):
        with self._guard:
            self._writable()
            record = self.get(experiment_id)
            if self.active_prompt(record["agentId"])["experimentId"] != experiment_id:
                raise ExperimentError("该实验当前未生效，不能覆盖其他已采用版本", 409)
            previous = self._records[experiment_id].get("previousActive")
            if not previous:
                raise ExperimentError("没有可回退的版本", 409)
            new_active = deepcopy(self._active)
            new_active[record["agentId"]] = previous
            atomic_json(self._active_path, new_active)
            self._active = new_active
            return self.get(experiment_id)

    def wait(self, experiment_id, timeout=None):
        with self._guard:
            thread = self._jobs.get(experiment_id)
        if thread:
            thread.join(timeout)
        return self.get(experiment_id)

    def close(self, timeout=5.0):
        with self._guard:
            self._closing = True
            for event in self._cancellations.values():
                event.set()
            jobs = list(self._jobs.values())
        deadline = time.monotonic() + timeout
        for thread in jobs:
            thread.join(max(0, deadline - time.monotonic()))
        with self._guard:
            if not self._jobs:
                self.telemetry.close()
                self._lock_stream.close()


def read_active_prompt(workspace, agent_id, default):
    """业务应用可无锁读取原子 active 指针；采用不修改业务源文件或部署配置。"""
    path = Path(workspace) / "active-prompts.json"
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get(agent_id, {}).get("prompt", default)
