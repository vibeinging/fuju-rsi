#!/usr/bin/env python3
"""把当前 Skill 完整复制到指定宿主；已有不同内容时保留用户版本。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


SKILL_NAME = "fuju-tune"


class InstallError(ValueError):
    pass


def _absolute(value):
    # 先检查原路径，不能 resolve 后把用户给出的符号链接悄悄隐藏。
    return Path(os.path.abspath(os.path.expanduser(str(value))))


def _check_path(path):
    for part in (path, *path.parents):
        if part.is_symlink():
            raise InstallError("安装路径或源路径包含符号链接，请使用真实目录")
        if part.exists() and not part.is_dir() and part != path:
            raise InstallError("安装路径的上级不是目录")


def _inventory(root):
    """比较实际资源字节；运行后生成的 Python 缓存不是分发资源。"""
    _check_path(root)
    if not root.is_dir():
        raise InstallError("Skill 源目录或已有安装不是目录")
    result = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        current = Path(directory)
        kept = []
        for name in sorted(directories):
            item = current / name
            if item.is_symlink():
                raise InstallError("Skill 资源包含符号链接，未进行复制")
            if name != "__pycache__":
                kept.append(name)
                result[item.relative_to(root).as_posix()] = ("directory", None)
        directories[:] = kept
        for name in sorted(files):
            item = current / name
            mode = item.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise InstallError("Skill 资源必须是普通文件或目录")
            if name.endswith((".pyc", ".pyo")):
                continue
            with item.open("rb") as stream:
                digest = hashlib.sha256()
                for chunk in iter(lambda: stream.read(65536), b""):
                    digest.update(chunk)
            result[item.relative_to(root).as_posix()] = ("file", digest.hexdigest())
    return result


def _nested(first, second):
    try:
        first.relative_to(second)
    except ValueError:
        return False
    return True


def _targets(arguments):
    hosts = ("codex", "claude") if arguments.host == "both" else (arguments.host,)
    if arguments.destination:
        base = _absolute(arguments.destination)
        return [(host, (base / host if len(hosts) == 2 else base) / SKILL_NAME)
                for host in hosts]
    base = _absolute(arguments.project) if arguments.project else _absolute(Path.home())
    _check_path(base)
    if not base.is_dir():
        raise InstallError("项目或用户目录必须已经存在")
    return [(host, base / (".agents" if host == "codex" else ".claude") / "skills" / SKILL_NAME)
            for host in hosts]


def install(arguments):
    source = _absolute(__file__).parents[1]
    inventory = _inventory(source)
    if "SKILL.md" not in inventory or inventory["SKILL.md"][0] != "file":
        raise InstallError("源目录缺少 SKILL.md")
    targets = _targets(arguments)
    installations = []
    # 两个宿主都先检查：一处用户改动不能导致另一处已经被更新。
    for host, destination in targets:
        _check_path(destination)
        if _nested(destination, source) or _nested(source, destination):
            raise InstallError("源目录和安装目录不能相同或互相包含")
        existing = destination.exists()
        if existing and _inventory(destination) != inventory:
            raise InstallError("已有 Skill 内容不同，保留原目录；请选择新的安装位置")
        installations.append({"host": host, "directory": str(destination),
                              "status": "unchanged" if existing else "installed"})

    staged = []
    try:
        for (_, destination), summary in zip(targets, installations):
            if summary["status"] == "unchanged":
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            _check_path(destination.parent)
            temporary = Path(tempfile.mkdtemp(prefix=".fuju-tune-install-", dir=destination.parent))
            staged.append((temporary, destination))
            # 先复制到临时目录并核对，失败不会留下半份可发现的 Skill。
            shutil.copytree(source, temporary / SKILL_NAME,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            if _inventory(temporary / SKILL_NAME) != inventory:
                raise InstallError("源资源在安装期间发生变化，未完成安装")
        for temporary, destination in staged:
            _check_path(destination)
            if destination.exists():
                raise InstallError("安装目录在复制期间出现，请检查后重新运行")
            (temporary / SKILL_NAME).rename(destination)
    finally:
        for temporary, _ in staged:
            shutil.rmtree(temporary, ignore_errors=True)

    invocations = []
    if arguments.host in ("codex", "both"):
        invocations.append("Codex：$fuju-tune 给当前项目接入评测，先跑原版并给我报告")
    if arguments.host in ("claude", "both"):
        invocations.append("Claude Code：/fuju-tune 给当前项目接入评测，先跑原版并给我报告")
    return {"status": "ready", "skill": SKILL_NAME, "installations": installations,
            "sdkInstalled": False,
            "nextSteps": ["在开发或测试虚拟环境中安装 Fuju RSI 源码或 wheel；本脚本只安装 Skill",
                          *invocations,
                          "自定义 --destination 仅复制资源；请确认宿主实际读取该目录"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description="安装 Fuju RSI Skill，保留已有不同内容")
    parser.add_argument("--host", choices=("codex", "claude", "both"), required=True,
                        help="明确选择要安装的编码工具")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--project", help="已有业务项目；安装到项目的标准 Skill 目录")
    location.add_argument("--destination", help="自定义父目录；both 时分为 codex/ 和 claude/ 子目录")
    arguments = parser.parse_args(argv)
    try:
        output = install(arguments)
    except InstallError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    except OSError:
        # 文件异常可能含私有路径，不把底层异常直接输出给编码 Agent。
        print(json.dumps({"status": "failed", "error": "无法读写安装目录，请检查路径和权限后重新运行"},
                         ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
