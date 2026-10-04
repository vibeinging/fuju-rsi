"""一条命令试跑公开 SQLite 案例；真实执行 SQL，规则生成候选，不调用模型。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new directory; default: a fresh temporary directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    # 每次演示独立创建账本和工作区，避免把上次的实验或验证次数带进结果。
    # 真实业务必须复用自己的共用账本，不能用这个演示脚本重置它。
    if args.output:
        output = args.output.absolute()
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error("output already exists; choose a new directory")
    else:
        output = Path(tempfile.mkdtemp(prefix="fuju-rsi-demo-")).resolve()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "examples") + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")

    def run(*arguments):
        result = subprocess.run([sys.executable, "-m", "fuju_rsi", *map(str, arguments)],
                                cwd=root, env=environment, capture_output=True, text=True,
                                timeout=60)
        if result.returncode:
            sys.stderr.write(result.stderr or result.stdout)
            raise SystemExit(result.returncode)
        return json.loads(result.stdout)

    book, bundle, ledger = output / "casebook.json", output / "development-v1", output / "validation-history"
    run("ask-data", "prepare", "--catalog", root / "examples/ask_data_catalog.json",
        "--role", "development", "--output", book)
    run("ask-data", "freeze", "--casebook", book, "--role", "development",
        "--output", bundle, "--version", "v1", "--scorer-file", root / "examples/ask_data_fixture.py")
    run("ask-data", "init-validation", "--directory", ledger, "--max-exposures", "2")
    result = run("optimize", "--agent", "ask_data_fixture:build_agent",
                 "--workspace", output / "workspace", "--ask-data-bundle", bundle,
                 "--validation-ledger", ledger, "--max-trials", "1", "--max-calls", "5",
                 "--telemetry", "log")
    print("SQLite demo complete / SQLite 演示完成")
    print("Runner + proposer callbacks / 回调次数: " + str(result["usedCalls"]))
    print("Independent acceptance / 独立验收: not run; adoptable=" + str(result["adoptable"]).lower())
    print("Report / 报告: " + result["artifacts"]["report"])
    print("Workspace / 工作区: " + str(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
