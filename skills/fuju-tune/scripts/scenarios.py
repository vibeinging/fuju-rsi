#!/usr/bin/env python3
"""只读取复制后 Skill 内的场景目录，供编码 Agent 选择指南。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1] / "scenarios"
FIELDS = {"schemaVersion", "id", "title", "description", "status", "guide",
          "targets", "automatedTargets"}
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,63}\Z")


class ScenarioError(ValueError):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ScenarioError("场景清单包含重复字段")
        result[key] = value
    return result


def _read(directory):
    if directory.is_symlink() or not directory.is_dir() or not IDENTIFIER.fullmatch(directory.name):
        raise ScenarioError("场景目录无效")
    manifest = directory / "manifest.json"
    if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 16384:
        raise ScenarioError("场景清单缺失或过大")
    try:
        content = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScenarioError("场景清单无法读取") from exc
    if (not isinstance(content, dict) or set(content) != FIELDS or
            type(content["schemaVersion"]) is not int or content["schemaVersion"] != 1):
        raise ScenarioError("场景清单版本或字段无效")
    if content["id"] != directory.name:
        raise ScenarioError("场景 id 必须与目录名一致")
    if content["status"] not in ("draft", "integrated"):
        raise ScenarioError("场景状态无效")
    targets, automated = content["targets"], content["automatedTargets"]
    for items in (targets, automated):
        if (type(items) is not list or len(items) > 16 or
                any(type(item) is not str or not IDENTIFIER.fullmatch(item) for item in items) or
                len(set(items)) != len(items)):
            raise ScenarioError("场景调整对象无效")
    if not targets or not set(automated).issubset(targets) or (content["status"] == "draft" and automated):
        raise ScenarioError("自动比较对象必须属于已接入的场景调整对象")
    for key, maximum in (("title", 80), ("description", 240)):
        value = content[key]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise ScenarioError("场景标题或说明无效")
    guide_name = content["guide"]
    if not isinstance(guide_name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+\.md", guide_name):
        raise ScenarioError("场景指南必须是目录内的 Markdown 文件")
    guide = directory / guide_name
    if guide.is_symlink() or not guide.is_file():
        raise ScenarioError("场景指南缺失或为符号链接")
    return {**content, "guidePath": str(guide)}


def load_scenarios():
    if not ROOT.is_dir() or ROOT.is_symlink():
        raise ScenarioError("Skill 缺少场景目录")
    return [_read(path) for path in sorted(ROOT.iterdir()) if path.is_dir() or path.is_symlink()]


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if not arguments or arguments[0] not in ("list", "show", "check") or len(arguments) != (2 if arguments[0] == "show" else 1):
        print("用法：scenarios.py list | show <场景 id> | check", file=sys.stderr)
        return 1
    try:
        scenarios = load_scenarios()
        if arguments[0] == "show":
            selected = next((item for item in scenarios if item["id"] == arguments[1]), None)
            if selected is None:
                raise ScenarioError("没有这个场景")
            output = selected
        else:
            output = {"schemaVersion": 1, "scenarios": scenarios}
    except (ScenarioError, OSError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
