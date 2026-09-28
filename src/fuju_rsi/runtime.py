"""可选的后台改进入口：固定开发包、有限运行、普通文件交付。

不启动网页或常驻服务。宿主用自己的任务系统执行 run_once；产品不读取实验账本。
摘要用于检查漂移，不构成权限隔离或对任意文件的可信签名。
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import inspect
import os
from pathlib import Path
import shutil
import stat
import uuid

from .benchmark import digest, load_bundle, read_json, slug, write_new
from .core import Example
from .verification import _files, _relative_identity
from .workspace import ExperimentManager, atomic_json, now


def _limit(value, maximum, label):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(label + " out of range")
    return value


def _checked(value, kind):
    if not isinstance(value, dict) or value.get("kind") != kind or value.get("schemaVersion") != 1:
        raise ValueError("Unsupported " + kind)
    if value.get("digest") != digest({k: v for k, v in value.items() if k != "digest"}):
        raise ValueError(kind + " digest mismatch")
    return value


def _sealed(value):
    return {**value, "digest": digest(value)}


def _development(directory):
    # 先读角色，连旧 v1 混合包也拒绝；不能先解析 test 正文再筛掉。
    header = read_json(Path(directory) / "manifest.json")
    if not isinstance(header, dict) or header.get("schemaVersion") != 2 or header.get("role") != "development":
        raise ValueError("Runtime requires a v2 development-only benchmark")
    return load_bundle(directory, role="development")


def export_pack(reference, development, output, *, source_files, max_trials=3,
                max_calls=100, max_runs=3, total_calls=300):
    """在项目根目录导出；源码保留在原项目，包只带声明文件的相对路径和摘要。"""
    from .cli import load_agent
    manifest, _ = _development(development)
    limits = {"maxTrials": _limit(max_trials, 20, "max_trials"),
              "maxCalls": _limit(max_calls, 10000, "max_calls"),
              "maxRuns": _limit(max_runs, 1000, "max_runs"),
              "totalCalls": _limit(total_calls, 10000000, "total_calls")}
    if limits["totalCalls"] < limits["maxCalls"]:
        raise ValueError("total_calls must cover one complete reservation")
    # load_agent 只在用户明确导出/运行时导入；factory 不应在 import 时执行付费工作。
    agent = load_agent(reference)
    identity = _relative_identity((agent.runner, agent.evaluator, agent.proposer), Path.cwd())
    factory = __import__(reference.partition(":")[0], fromlist=[reference.partition(":")[2]])
    factory_file = inspect.getsourcefile(getattr(factory, reference.partition(":")[2]))
    files = _files([*source_files, factory_file, manifest["scorer"]["path"],
                    *(item["file"] for item in identity)], Path.cwd())
    pack = _sealed({"kind": "recur-improvement-pack", "schemaVersion": 1,
                    "agentId": agent.id, "agentFactory": reference, "baselinePrompt": agent.baseline_prompt,
                    "benchmarkDigest": manifest["digest"], "files": files,
                    "callbackIdentity": identity, "limits": limits,
                    "editable": ["prompt"], "releaseMode": "review"})
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    try:
        target = output / "development"
        target.mkdir()
        for name in ("casebook.json", "examples.json", "manifest.json"):
            write_new(target / name, read_json(Path(development) / name))
        # 再核对复制结果，避免读源文件期间混入不同版本。
        if _development(target)[0]["digest"] != pack["benchmarkDigest"]:
            raise ValueError("Benchmark changed during export")
        write_new(output / "pack.json", pack)
        load_pack(output)
    except BaseException:
        shutil.rmtree(output)
        raise
    return pack


def load_pack(directory):
    """只校验文件，不导入或调用用户代码；从当前项目根目录运行。"""
    pack = _checked(read_json(Path(directory) / "pack.json"), "recur-improvement-pack")
    if pack.get("editable") != ["prompt"] or pack.get("releaseMode") != "review":
        raise ValueError("Only prompt search with reviewed release is supported")
    limits = pack.get("limits", {})
    for key, maximum in (("maxTrials", 20), ("maxCalls", 10000), ("maxRuns", 1000), ("totalCalls", 10000000)):
        _limit(limits.get(key), maximum, key)
    if limits["totalCalls"] < limits["maxCalls"]:
        raise ValueError("Invalid total call reservation")
    if not isinstance(pack.get("baselinePrompt"), str) or not pack["baselinePrompt"].strip():
        raise ValueError("Missing baseline prompt")
    if _files(pack.get("files", {}), Path.cwd()) != pack["files"]:
        raise ValueError("Project files changed; export a new improvement pack")
    manifest, _ = _development(Path(directory) / "development")
    if manifest["digest"] != pack.get("benchmarkDigest"):
        raise ValueError("Pack benchmark changed")
    return pack


def _agent(directory, pack):
    from .cli import load_agent
    agent = load_agent(pack["agentFactory"])
    if agent.id != pack["agentId"] or agent.baseline_prompt != pack["baselinePrompt"]:
        raise ValueError("Agent identity or baseline changed")
    identity = _relative_identity((agent.runner, agent.evaluator, agent.proposer), Path.cwd())
    if identity != pack["callbackIdentity"]:
        raise ValueError("Agent callbacks changed")
    _, examples = _development(Path(directory) / "development")
    return replace(agent, examples=[Example(**item) for item in examples])


def load_runtime_agent(directory):
    """供 freeze/import/publish 复用搜索时同一个开发包，而非 factory 中的占位案例。"""
    return _agent(directory, load_pack(directory))


def initialize_runtime(pack_dir, workspace):
    pack = load_pack(pack_dir)
    workspace = Path(workspace)
    # 只接受新目录。账本缺失时不能把旧工作区重新初始化成零预算。
    workspace.mkdir(parents=True, exist_ok=False)
    try:
        atomic_json(workspace / "runtime.json", {"schemaVersion": 1, "packDigest": pack["digest"],
                    "limits": pack["limits"], "jobs": {}, "createdAt": now()})
    except BaseException:
        shutil.rmtree(workspace)
        raise
    return {"status": "initialized", "workspace": str(workspace.resolve()), "packDigest": pack["digest"]}


def _state(workspace, pack):
    state = read_json(Path(workspace) / "runtime.json")
    if (not isinstance(state, dict) or state.get("schemaVersion") != 1 or
            state.get("packDigest") != pack["digest"] or state.get("limits") != pack["limits"] or
            not isinstance(state.get("jobs"), dict)):
        raise ValueError("Runtime ledger or pack mismatch")
    for key, job in state["jobs"].items():
        slug(key, "run_id")
        if (not isinstance(job, dict) or job.get("reservedCalls") != pack["limits"]["maxCalls"] or
                job.get("status") not in ("running", "completed", "failed", "cancelled", "interrupted", "budget_exhausted")):
            raise ValueError("Invalid runtime job ledger")
    return state


def runtime_status(pack_dir, workspace):
    pack = load_pack(pack_dir)
    state = _state(workspace, pack)
    return {"packDigest": pack["digest"], "reservedCalls": sum(j["reservedCalls"] for j in state["jobs"].values()),
            "limits": deepcopy(pack["limits"]), "jobs": deepcopy(state["jobs"])}


def run_once(pack_dir, workspace, run_id):
    """一次有限后台工作。相同 run_id 幂等；崩溃后的预算保守保留，不自动重跑。

    应由用户自己的后台进程/CI 调用。与现有实验器共用进程锁，拒绝同工作区并发。
    """
    slug(run_id, "run_id")
    pack = load_pack(pack_dir)
    _state(workspace, pack)  # 不存在/损坏时在导入业务代码之前失败。
    agent = _agent(pack_dir, pack)
    manager = ExperimentManager(workspace, [agent])
    try:
        state = _state(workspace, pack)
        if manager.active_prompt(agent.id)["prompt"] != pack["baselinePrompt"]:
            raise ValueError("Workspace baseline changed; create a new pack and workspace")
        for job in state["jobs"].values():
            if job["status"] == "running":
                job.update(status="interrupted", finishedAt=now(), message="Previous worker stopped; reservation retained")
        atomic_json(Path(workspace) / "runtime.json", state)
        if run_id in state["jobs"]:
            return deepcopy(state["jobs"][run_id])
        limits = pack["limits"]
        reserved = sum(job["reservedCalls"] for job in state["jobs"].values())
        if len(state["jobs"]) >= limits["maxRuns"] or reserved + limits["maxCalls"] > limits["totalCalls"]:
            raise ValueError("Runtime run/callback reservation limit reached")
        job = {"runId": run_id, "status": "running", "reservedCalls": limits["maxCalls"],
               "createdAt": now(), "experimentId": None, "artifacts": None, "adoptable": False}
        state["jobs"][run_id] = job
        atomic_json(Path(workspace) / "runtime.json", state)
        try:
            started = manager.start(agent_id=agent.id, name=run_id,
                                    max_trials=limits["maxTrials"], max_calls=limits["maxCalls"])
            job["experimentId"] = started["id"]
            atomic_json(Path(workspace) / "runtime.json", state)
            result = manager.wait(started["id"])
            # 回调运行中改动了声明文件时，不能交付旧条件下的候选。
            load_pack(pack_dir)
            job.update(status=result["status"], usedCalls=result["usedCalls"],
                       searchComplete=result["searchComplete"], evidenceStatus=result["evidenceStatus"],
                       artifacts=manager.export_report(started["id"]))
        except KeyboardInterrupt:
            if job["experimentId"]:
                manager.cancel(job["experimentId"])
            job.update(status="cancelled", message="Cancelled; reservation retained")
            raise
        except Exception as exc:
            job.update(status="failed", errorType=type(exc).__name__, message="Worker failed; inspect local logs")
            raise
        finally:
            job["finishedAt"] = now()
            atomic_json(Path(workspace) / "runtime.json", state)
        return deepcopy(job)
    finally:
        manager.close()


def _prompt_version(agent_id, prompt, previous=None, experiment_id=None):
    content = {"agentId": agent_id, "prompt": prompt, "experimentId": experiment_id, "revision": uuid.uuid4().hex}
    return {"schemaVersion": 1, "kind": "recur-prompt", **content,
            "version": digest(content), "previous": previous}


def prompt_status(path):
    """检查普通 JSON 配置；这是文件读回，不是宿主已加载的证明。"""
    value = read_json(path)
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("kind") != "recur-prompt":
        raise ValueError("Invalid prompt file")
    for snapshot in [value] + ([value["previous"]] if value.get("previous") is not None else []):
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("prompt"), str) or not snapshot["prompt"].strip():
            raise ValueError("Invalid prompt snapshot")
        content = {key: snapshot[key] for key in ("agentId", "prompt", "experimentId", "revision")}
        if snapshot.get("version") != digest(content) or snapshot["agentId"] != value["agentId"]:
            raise ValueError("Prompt snapshot changed")
    return value


def initialize_prompt(path, *, agent_id, prompt):
    """显式注册产品原版，不表示其经过独立验收。只写新文件。"""
    slug(agent_id, "agent_id")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Empty prompt")
    value = _prompt_version(agent_id, prompt)
    write_new(path, value)
    return value


class _PromptLock:
    """短期发布锁，与产品读取路径分开。进程退出后操作系统释放。"""
    def __init__(self, path):
        self.path = Path(path)

    def __enter__(self):
        self.stream = (self.path.parent / ("." + self.path.name + ".recur.lock")).open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self.stream.seek(0, 2) == 0:
                    self.stream.write(b"1")
                    self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.stream.close()
            raise
        return self

    def __exit__(self, *args):
        self.stream.close()


def publish_prompt(manager, experiment_id, path, *, expected_version):
    """人审发布入口：现场重核可信验收，原子写普通文件，不采用实验 active 指针。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Prompt target must be a regular file")
    with _PromptLock(path):
        current = prompt_status(path)
        record = manager.get(experiment_id)
        if not record["adoptable"] or record["status"] != "completed":
            raise ValueError("Current trusted independent verification is required")
        if (current["version"] != expected_version or current["agentId"] != record["agentId"] or
                current["prompt"] != record["baselinePrompt"]):
            raise ValueError("Product baseline/version changed")
        previous = {key: current[key] for key in ("agentId", "prompt", "experimentId", "revision", "version")}
        value = _prompt_version(record["agentId"], record["bestPrompt"], previous, experiment_id)
        atomic_json(path, value, mode=stat.S_IMODE(path.stat().st_mode))
        return prompt_status(path)


def rollback_prompt(path, *, expected_version):
    """仅回退这个文件的上一版本；不需要实验器或验收密钥。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Prompt target must be a regular file")
    with _PromptLock(path):
        current = prompt_status(path)
        if current["version"] != expected_version or not current.get("previous"):
            raise ValueError("Product version changed or no previous version")
        previous = current["previous"]
        atomic_json(path, _prompt_version(previous["agentId"], previous["prompt"], experiment_id=previous["experimentId"]),
                    mode=stat.S_IMODE(path.stat().st_mode))
        return prompt_status(path)
