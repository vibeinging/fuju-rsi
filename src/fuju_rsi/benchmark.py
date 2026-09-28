#!/usr/bin/env python3
"""检查、追加业务评测草案，并固定可追溯的 Benchmark 版本；不生成或猜测答案。"""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import hashlib
import inspect
import json
from pathlib import Path
import re
import shutil
import sys
import unicodedata


class BenchmarkError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BenchmarkError("JSON contains duplicate object keys")
            result[key] = value
        return result

    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique)
        encode(value)
        return value
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise BenchmarkError("Cannot read a valid finite JSON document") from exc


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkError(label + " must be a nonempty string")
    return value


def slug(value, label):
    text(value, label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise BenchmarkError(label + " must use 1..80 letters, digits, underscores or hyphens")
    return value


def inspect_book(book, *, frozen=False):
    if not isinstance(book, dict) or type(book.get("schemaVersion")) is not int or book["schemaVersion"] not in (1, 2):
        raise BenchmarkError("Casebook requires schemaVersion=1 or 2")
    role = book.get("role", "legacy")
    if book["schemaVersion"] == 2 and role not in ("development", "holdout"):
        raise BenchmarkError("Version 2 requires development or holdout role")
    slug(book.get("id"), "id")
    text(book.get("name"), "name")
    cases = book.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkError("cases must be a nonempty list")
    ids, inputs, groups, sources, approximate = set(), set(), {}, {}, {}
    warnings = []
    splits, coverage = Counter(), Counter()
    drafts = 0
    for case in cases:
        if not isinstance(case, dict):
            raise BenchmarkError("Every case must be an object")
        identifier = text(case.get("id"), "case.id")
        if identifier in ids:
            raise BenchmarkError("Duplicate case id")
        ids.add(identifier)
        if "input" not in case:
            raise BenchmarkError("Every case requires input")
        key = encode(case["input"])
        if key in inputs:
            raise BenchmarkError("Duplicate normalized input; merge its checks instead of inflating case count")
        inputs.add(key)
        group = text(case.get("group"), "case.group")
        split = case.get("split")
        if split is not None and split not in ("train", "validation", "test"):
            raise BenchmarkError("split must be train, validation or test")
        if book["schemaVersion"] == 2 and split is not None:
            if (role == "development" and split == "test") or (role == "holdout" and split != "test"):
                raise BenchmarkError("Development and holdout cases must be separate bundles")
        if split:
            if group in groups and groups[group] != split:
                raise BenchmarkError("Related cases in one group cannot cross splits")
            groups[group] = split
            splits[split] += 1
        tags = case.get("tags")
        if not isinstance(tags, list) or not tags or any(not isinstance(t, str) or not t.strip() for t in tags):
            raise BenchmarkError("Every case requires nonempty coverage tags")
        coverage.update(set(tags))
        source = case.get("source")
        if not isinstance(source, dict):
            raise BenchmarkError("Every case requires a source")
        text(source.get("kind"), "source.kind")
        text(source.get("ref"), "source.ref")
        if book["schemaVersion"] == 2:
            origin = source["ref"]
            if split and origin in sources and sources[origin] != split:
                raise BenchmarkError("Cases from one source cannot cross splits")
            sources[origin] = split
            if isinstance(case["input"], str):
                similar = near_key(case["input"])
                if similar and similar in approximate and approximate[similar] != group:
                    warnings.append("Near-identical text uses different groups; review provenance")
                approximate[similar] = group
        status = case.get("expectedStatus", "draft")
        if status not in ("draft", "verified"):
            raise BenchmarkError("expectedStatus must be draft or verified")
        if status == "verified":
            if "expected" not in case:
                raise BenchmarkError("Verified case requires expected; explicit null is allowed")
            text(case.get("evidence"), "case.evidence")
        else:
            drafts += 1
        if frozen and (not split or status != "verified"):
            raise BenchmarkError("Frozen cases must have a split and verified expectations")
    required = ("train", "validation", "test") if book["schemaVersion"] == 1 else (("train", "validation") if role == "development" else ("test",))
    if frozen and any(not splits[s] for s in required):
        raise BenchmarkError("Frozen benchmark requires its role's nonempty splits")
    if frozen and warnings:
        raise BenchmarkError("Near-identical text uses different groups; resolve provenance before freezing")
    result = {"id": book["id"], "name": book["name"], "caseCount": len(cases),
            "draftCount": drafts, "verifiedCount": len(cases) - drafts,
            "splits": dict(splits), "coverage": dict(sorted(coverage.items()))}
    if book["schemaVersion"] == 2:
        result.update(role=role, warnings=warnings)
    return result


def near_key(value):
    """仅识别大小写、标点与空白改写，不声称语义去重。数字保留业务含义。"""
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def case_tokens(example):
    """输入与来源分别存指纹；改名、答案变更不能重置输入使用历史。"""
    result = {"input:" + digest(example.input)}
    for field, prefix in (("group_id", "group"), ("source_id", "source")):
        value = getattr(example, field, None)
        if value:
            result.add(prefix + ":" + digest(value))
    if isinstance(example.input, str) and near_key(example.input):
        result.add("input:" + digest({"nearText": near_key(example.input)}))
    return sorted(result)


def callback_snapshot(*callbacks):
    paths = {}
    for callback in callbacks:
        try:
            path = inspect.getsourcefile(callback)
        except (TypeError, OSError):
            return None
        if not path or not Path(path).is_file():
            return None
        resolved = Path(path).resolve()
        paths[str(resolved)] = hashlib.sha256(resolved.read_bytes()).hexdigest()
    return paths


def callback_identity(*callbacks):
    """按调用角色保留函数身份；同一文件中的另一个评分器不能替换已选入口。"""
    values = []
    for callback in callbacks:
        try:
            path = inspect.getsourcefile(callback)
            function = getattr(callback, "__func__", callback)
            code = function.__code__
            values.append({"file": str(Path(path).resolve()), "name": function.__qualname__,
                           "line": code.co_firstlineno})
        except (AttributeError, TypeError, OSError):
            return None
    return values


def examples_from(book):
    rows = []
    for case in book["cases"]:
        row = {key: case[key] for key in ("id", "input", "expected", "split")}
        if book["schemaVersion"] == 2:
            row.update(group_id=case["group"], source_id=case["source"]["ref"],
                       exposure="unseen" if book["role"] == "holdout" else "development",
                       critical="critical" in case["tags"])
        rows.append(row)
    return rows


def merge_book(book, additions):
    inspect_book(book)
    if not isinstance(additions, list) or not additions:
        raise BenchmarkError("Additions must be a nonempty array of cases")
    result = deepcopy(book)
    result["cases"].extend(deepcopy(additions))
    inspect_book(result)
    return result


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encode(value) + b"\n")


def scorer_snapshot(path):
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(Path.cwd().resolve())
        return {"path": relative.as_posix(), "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest()}
    except (ValueError, OSError) as exc:
        raise BenchmarkError("Scorer file must be readable inside the current project") from exc


def freeze_book(book, *, output, version, scorer_file):
    summary = inspect_book(book, frozen=True)
    slug(version, "version")
    environment = book.get("environment")
    if not isinstance(environment, dict) or not environment:
        raise BenchmarkError("Declare nonempty environment metadata before freezing")
    text(book.get("scoring"), "scoring")
    scorer = scorer_snapshot(scorer_file)
    examples = examples_from(book)
    manifest = {"schemaVersion": book["schemaVersion"], **summary, "version": version, "scoring": book["scoring"],
                "environment": deepcopy(environment), "scorer": scorer,
                "files": {"casebook.json": digest(book), "examples.json": digest(examples)}}
    manifest["digest"] = digest(manifest)
    output = Path(output)
    # mkdir 是排他的版本预留；manifest 最后写，读取方不接受半成品。
    output.mkdir(parents=True, exist_ok=False)
    try:
        write_new(output / "casebook.json", book)
        write_new(output / "examples.json", examples)
        write_new(output / "manifest.json", manifest)
    except BaseException:
        shutil.rmtree(output)
        raise
    return manifest


def load_bundle(directory, *, role=None):
    """读取冻结 Benchmark，并在读取案例正文之前校验用途。

    独立验收题（`role="holdout"`）必须由验收方显式传 `role="holdout"` 才读得出：
    搜索路径即使漏检，也会在读到答案之前失败，而不是先解析再拒绝。默认只接受开发包。
    v1 旧包没有 role，按开发包兼容。
    """
    directory = Path(directory)
    manifest = read_json(directory / "manifest.json")
    if not isinstance(manifest, dict):
        raise BenchmarkError("Invalid benchmark manifest")
    content = {k: v for k, v in manifest.items() if k != "digest"}
    if manifest.get("digest") != digest(content):
        raise BenchmarkError("Benchmark manifest digest mismatch")
    # role 只在 manifest 里，不含案例正文；校验放在这里才挡得住"先读后拒"。
    actual = manifest.get("role") or "legacy"
    allowed = ("holdout",) if role == "holdout" else ("development", "legacy")
    if actual not in allowed:
        if actual == "holdout":
            raise BenchmarkError("Holdout bundle requires an explicit role='holdout' load")
        raise BenchmarkError("Expected a holdout bundle, got role=%s" % actual)
    book = read_json(directory / "casebook.json")
    examples = read_json(directory / "examples.json")
    summary = inspect_book(book, frozen=True)
    expected_files = {"casebook.json": digest(book), "examples.json": digest(examples)}
    if manifest.get("files") != expected_files or encode(examples) != encode(examples_from(book)):
        raise BenchmarkError("Frozen benchmark cases changed")
    if any(manifest.get(key) != value for key, value in summary.items()):
        raise BenchmarkError("Benchmark summary does not match its cases")
    if manifest.get("environment") != book.get("environment") or manifest.get("scoring") != book.get("scoring"):
        raise BenchmarkError("Benchmark conditions do not match its casebook")
    slug(manifest.get("version"), "version")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != book["schemaVersion"]:
        raise BenchmarkError("Unsupported benchmark version")
    scorer = manifest.get("scorer")
    if not isinstance(scorer, dict) or scorer_snapshot(scorer.get("path", "")) != scorer:
        raise BenchmarkError("Scorer file changed; freeze a new benchmark version")
    return manifest, examples


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, self.prog + ": error: " + message + "\n")


def main(argv=None):
    parser = Parser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="report draft status and coverage without disclosing cases")
    inspect.add_argument("--input", required=True)
    merge = commands.add_parser("merge", help="append cases to a new draft file; never overwrite")
    merge.add_argument("--input", required=True)
    merge.add_argument("--add", required=True)
    merge.add_argument("--output", required=True)
    freeze = commands.add_parser("freeze", help="create a fixed benchmark directory; never overwrite")
    freeze.add_argument("--input", required=True)
    freeze.add_argument("--output", required=True)
    freeze.add_argument("--version", required=True)
    freeze.add_argument("--scorer-file", required=True)
    args = parser.parse_args(argv)
    try:
        book = read_json(args.input)
        if args.command == "merge":
            book = merge_book(book, read_json(args.add))
            write_new(args.output, book)
        if args.command == "freeze":
            result = freeze_book(book, output=args.output, version=args.version, scorer_file=args.scorer_file)
        else:
            result = inspect_book(book)
        print(json.dumps({"status": "ok", **result}, ensure_ascii=False, allow_nan=False))
        return 0
    except (BenchmarkError, OSError, TypeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, BenchmarkError) else "Cannot write output; check paths and use a new destination"
        print(json.dumps({"status": "failed", "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
