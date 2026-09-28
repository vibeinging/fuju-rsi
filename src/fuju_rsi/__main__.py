"""独立 RSI 命令行入口。"""
from __future__ import annotations

import argparse
from typing import Sequence


def main(argv: Sequence[str] = None) -> int:
    parser = argparse.ArgumentParser(prog="fuju-rsi")
    sub = parser.add_subparsers(dest="command", required=True)
    from .cli import add_parser as add_optimize, add_report_parser
    from .runtime_cli import add_parser as add_runtime
    from .verification_cli import add_parser as add_verify
    from .ask_data_cli import add_parser as add_ask_data
    add_optimize(sub)
    add_report_parser(sub)
    add_runtime(sub)
    add_verify(sub)
    add_ask_data(sub)
    args = parser.parse_args(argv)
    if args.command == "optimize":
        from .cli import run
    elif args.command == "report":
        from .cli import run_report as run
    elif args.command == "runtime":
        from .runtime_cli import run
    elif args.command == "ask-data":
        from .ask_data_cli import run
    else:
        from .verification_cli import run
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, ImportError, AttributeError, OSError) as err:
        # 验收回调异常可能包含未见题内容；不把异常正文写到终端。
        if args.command == "verify":
            print('{"status":"failed","adoptable":false,"message":"验收未完成；请检查本地诊断日志。"}')
            return 1
        parser.error(str(err))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
