#!/usr/bin/env python3
"""使用另行安装的 SDK 检查、追加和冻结业务案例。"""
import sys
try:
    from fuju_rsi.benchmark import *
except ImportError:
    if __name__ == "__main__":
        print('{"status":"failed","error":"Install the Fuju RSI package with Benchmark support first."}')
        sys.exit(1)
    raise

if __name__ == "__main__":
    sys.exit(main())
