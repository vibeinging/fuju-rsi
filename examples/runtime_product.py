"""纯标准库业务示例。按策略汇总数据；不导入 yitrace、不连接优化服务。

preview/sum_all 是预置协议测试策略，不是模型生成行为或真实用户业务收益。
"""
import json
from pathlib import Path
import sys


def calculate(strategy, values):
    return sum(values if strategy == "sum_all" else values[:1])


def main():
    try:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        prompt, version = config["prompt"], config["version"]
        if not isinstance(prompt, str) or not isinstance(version, str):
            raise ValueError("invalid config")
    except (OSError, ValueError, KeyError, TypeError):
        prompt, version = "preview", "built-in"
    values = json.loads(sys.argv[2])
    print(json.dumps({"version": version, "amount": calculate(prompt, values)}))


if __name__ == "__main__":
    main()
