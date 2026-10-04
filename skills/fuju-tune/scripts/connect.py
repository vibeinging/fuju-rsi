#!/usr/bin/env python3
"""复制后的 Skill 也可检查项目、保存连接并运行首次原版；SDK 单独安装。"""
import json
import sys

try:
    from fuju_rsi import onboarding  # 旧包存在主 CLI，但未必包含接入能力。
    from fuju_rsi.__main__ import main
except ImportError:
    print(json.dumps({"status": "blocked", "adoptable": False,
                      "message": "请先把当前 Fuju RSI 源码或 wheel 安装到开发/测试 Python 环境。"}, ensure_ascii=False))
    raise SystemExit(1)

if __name__ == "__main__":
    raise SystemExit(main(["connect"] + sys.argv[1:]))
