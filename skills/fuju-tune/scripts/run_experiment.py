#!/usr/bin/env python3
"""复制后的 Skill 调用已安装 SDK，与首次接入共用执行守卫。"""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import sys

try:
    from fuju_rsi.skill_runner import aggregate, complete, summarize
    from fuju_rsi.skill_runner import main as run
except ImportError:
    if __name__ == "__main__":
        print(json.dumps({"status": "failed", "error": "Install the Fuju RSI package in this Python environment."}))
        sys.exit(1)
    raise


def load_scenarios():
    # -I 不把脚本目录加入 sys.path；按本文件定位同目录模块，复制后仍可用。
    # 只有显式选择场景时才读取清单，不要求普通基线发现其他场景。
    path = Path(__file__).resolve().with_name("scenarios.py")
    spec = importlib.util.spec_from_file_location("fuju_tune_scenarios", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.load_scenarios()


def main(argv=None):
    return run(argv, scenario_loader=load_scenarios)


if __name__ == "__main__":
    sys.exit(main())
