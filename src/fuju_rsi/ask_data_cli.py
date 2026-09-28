"""问数目录准备和验证集使用账本。"""
from __future__ import annotations

import json

from .ask_data import build_casebook
from .ask_data_validation import ValidationLedger
from .benchmark import freeze_book, inspect_book, read_json, write_new


def add_parser(subparsers):
    parser = subparsers.add_parser("ask-data", help="prepare Ask Data cases and validation history")
    commands = parser.add_subparsers(dest="ask_data_command", required=True)
    prepare = commands.add_parser("prepare", help="convert a reviewed catalog to a new RSI v2 casebook")
    prepare.add_argument("--catalog", required=True)
    prepare.add_argument("--role", choices=("development", "holdout"), required=True,
                         help="authoring role; use holdout only inside the verifier environment")
    prepare.add_argument("--output", required=True)
    freeze = commands.add_parser("freeze", help="freeze a reviewed Ask Data casebook")
    freeze.add_argument("--casebook", required=True)
    freeze.add_argument("--role", choices=("development", "holdout"), required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--version", required=True)
    freeze.add_argument("--scorer-file", required=True)
    init = commands.add_parser("init-validation", help="create a shared, fixed-cap validation ledger")
    init.add_argument("--directory", required=True)
    init.add_argument("--max-exposures", type=int, required=True,
                      help="maximum total validation sweeps per source across experiments")
    return parser


def run(args):
    if args.ask_data_command == "init-validation":
        ledger = ValidationLedger.initialize(args.directory, max_exposures=args.max_exposures)
        print(json.dumps({"status": "initialized", **ledger.metadata}, ensure_ascii=False))
        return 0
    if args.ask_data_command == "freeze":
        book = read_json(args.casebook)
        if book.get("role") != args.role or book.get("scoring") != "ask-data-structured-v1":
            raise ValueError("问数案例用途或评分协议与显式参数不一致")
        result = freeze_book(book, output=args.output, version=args.version,
                             scorer_file=args.scorer_file)
        print(json.dumps({"status": "frozen", "digest": result["digest"],
                          "role": result["role"], "caseCount": result["caseCount"],
                          "output": args.output}, ensure_ascii=False))
        return 0
    catalog = read_json(args.catalog)
    if catalog.get("role") != args.role:
        raise ValueError("目录声明用途与显式 --role 不一致")
    book = build_casebook(catalog)
    summary = inspect_book(book)
    write_new(args.output, book)
    print(json.dumps({"status": "prepared", **summary, "output": args.output},
                     ensure_ascii=False))
    return 0
