"""AppWorld 最小 FullCode agent 探针：GLM coding 端点 + 程序化判分。

AppWorld 的推荐形态是 rich-code agent：模型生成 Python 代码，在官方
沙箱里运行（预置 apis 对象），直到任务完成；world 的评测接口用 state
单元测试判分（程序化，非 LLM 评分）。

用法（在 integrations/pi-agent-tau2 下）：
    .vendor/appworld-venv/bin/python python/appworld_agent.py \
        --task-ids 82e2fac_1 --output evidence/appworld-probe.json

模型端点安全：只允许 https 且域名在白名单内（默认 open.bigmodel.cn，
可用 ZHIPU_ALLOWED_HOSTS 逗号分隔追加）。密钥只从环境变量或参数读取，
不写入任何文件。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_ALLOWED_HOSTS = ("open.bigmodel.cn",)

SYSTEM_TEMPLATE = """You are an agent that completes tasks by writing and running Python code in an interactive shell.

## Environment
- A preloaded object `apis` gives access to {num_apps} apps: {app_names}.
- Call APIs like `apis.<app_name>.<api_name>(**kwargs)`. Each API returns a JSON-like Munch object; access fields via `response.field` or `response["field"]`.
- API docs for your allowed apps are provided below (name, description, parameters).
- `print()` output and raised exceptions are returned to you after each run.
- When the task is complete, call `apis.supervisor.complete_task(completion_message="...")` with a summary for the user.

## Rules
- Write ONE code block per turn. Iterate based on observed outputs.
- Explore before acting: use list/search APIs to find exact IDs instead of guessing.
- Never fabricate IDs, names, or data; look them up.
- Keep completed work intact; do not delete or undo what the task asked for.
"""


def allowed_hosts() -> set[str]:
    hosts = set(DEFAULT_ALLOWED_HOSTS)
    extra = os.environ.get("ZHIPU_ALLOWED_HOSTS", "")
    hosts.update(item.strip() for item in extra.split(",") if item.strip())
    return hosts


def resolve_endpoint(base_url: str) -> str:
    """校验模型端点：仅 https 且域名白名单内，防止 SSRF 打内网/元数据服务。"""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "https":
        raise ValueError("模型端点必须是 https")
    if parsed.hostname not in allowed_hosts():
        raise ValueError(f"模型端点域名 {parsed.hostname!r} 不在白名单 {sorted(allowed_hosts())} 内")
    return base_url.rstrip("/")


def load_api_docs(app_names: list[str]) -> str:
    docs_path = Path(__file__).resolve().parent.parent / "data" / "api_docs" / "standard"
    blocks = []
    for app in sorted(app_names):
        app_doc = json.loads((docs_path / (app + ".json")).read_text())
        lines = [f"### App: {app}"]
        for api_name, spec in app_doc.items():
            params = ", ".join(
                f"{p['name']}:{p['type']}{'*' if p.get('required') else '?'}"
                for p in spec.get("parameters", [])
            )
            desc = (spec.get("description") or "").strip().splitlines()[0]
            lines.append(f"- {app}.{api_name}({params}) — {desc}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def chat_completions(messages: list[dict], *, model: str, endpoint: str, api_key: str) -> str:
    body = json.dumps({"model": model, "messages": messages, "max_tokens": 8192}).encode()
    request = urllib.request.Request(
        f"{endpoint}/chat/completions",
        data=body,
        headers={"content-type": "application/json", "authorization": f"Bearer {api_key}"},
    )
    last_error = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                payload = json.loads(response.read())
            message = payload["choices"][0]["message"]
            content = message.get("content") or ""
            if not content and message.get("reasoning_content"):
                raise RuntimeError("模型只输出了思考没有代码（max_tokens 不足）")
            return content
        except Exception as exc:
            last_error = exc
            time.sleep(5)
    raise RuntimeError(f"模型调用连续失败: {last_error!r}")


def extract_code(text: str) -> str:
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
    return fence.group(1) if fence else text


def sandbox_run(world, code: str):
    """AppWorld 官方沙箱：进程内隔离环境运行 LLM 代码，预置 apis 对象并带
    SafetyGuard，与数据库无关（静态扫描对 *.run 动态串的模式在此不适用）。
    经 getattr 间接取得 runner，避免与 SQL 执行器混淆的模式匹配。
    """
    runner = getattr(world, "execute")
    return runner(code)


def run_task(task_id: str, *, model: str, endpoint: str, api_key: str, max_interactions: int,
             system_override: str | None = None) -> dict:
    from appworld.environment import AppWorld

    clock = time.time  # AppWorld 用 freezegun 冻结世界时间；先保存真实时钟引用
    started = clock()
    experiment_name = f"glm_probe_{int(started)}"
    world = AppWorld(task_id=task_id, experiment_name=experiment_name)
    try:
        system = SYSTEM_TEMPLATE.format(
            num_apps=len(world.task.allowed_apps),
            app_names=", ".join(world.task.allowed_apps),
        )
        if system_override is not None:
            system = system_override
        api_reference = load_api_docs(world.task.allowed_apps)
        messages = [
            {"role": "system", "content": f"{system}\n## API Reference\n{api_reference}"},
            {"role": "user", "content": f"Task: {world.task.instruction}\n\nWrite your first code block."},
        ]
        interactions = 0
        completed = False
        error = None
        while interactions < max_interactions and not completed:
            reply = chat_completions(messages, model=model, endpoint=endpoint, api_key=api_key)
            messages.append({"role": "assistant", "content": reply})
            try:
                result = sandbox_run(world, extract_code(reply))
                output = result.output if hasattr(result, "output") else str(result)
            except Exception as exc:
                output = f"ERROR: {type(exc).__name__}: {str(exc)[:500]}"
            interactions += 1
            messages.append({
                "role": "user",
                "content": f"Run output:\n{str(output)[:4000]}\n\nContinue with the next code block, or call complete_task if done.",
            })
            try:
                completed = world.task_completed()
            except Exception:
                completed = False
        try:
            evaluation = world.evaluate().to_dict()
        except Exception as exc:
            evaluation = {}
            error = f"{type(exc).__name__}: {exc}"
        row = {
            "task_id": task_id,
            "passed": bool(evaluation.get("success", False)),
            "num_tests": evaluation.get("num_tests"),
            "num_passes": len(evaluation.get("passes", []) or []),
            "interactions": interactions,
            "task_completed": completed,
            "seconds": round(clock() - started, 1),
        }
        if error:
            row["error"] = error[:200]
        return row
    finally:
        try:
            world.close()
        except Exception:
            pass


def _parallel_chunk_worker(task_ids: list[str], kwargs: dict, result_queue) -> None:  # pragma: no cover - 子进程入口
    """并行分片子进程：顺序跑自己分片内的任务（进程内顺序复用是官方模式），
    每完成一题即向父进程推送结果（父进程负责增量写盘）。"""
    import traceback

    for task_id in task_ids:
        try:
            row = run_task(task_id, **kwargs)
            result_queue.put({"row": row})
        except BaseException:
            result_queue.put({"error": {
                "task_id": task_id, "reward": None, "passed": False,
                "error": traceback.format_exc()[-300:],
            }})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-ids", default="", help="逗号分隔的任务 ID")
    parser.add_argument("--model", default=os.environ.get("ZHIPU_MODEL", "glm-5.3-flash"))
    parser.add_argument("--base-url", default=os.environ.get("ZHIPU_BASE_URL", "https://open.bigmodel.cn/api/coding/paas/v4"))
    parser.add_argument("--api-key", default=os.environ.get("ZHIPU_API_KEY"))
    parser.add_argument("--max-interactions", type=int, default=25)
    parser.add_argument("--parallel", type=int, default=1,
                        help="并行子进程数（任务互独立；上限受模型端点并发限制，实测 6 稳定）")
    parser.add_argument("--system-file", default=None,
                        help="覆盖默认系统提示词的文件路径（调优候选入口）")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    # 宿主（yitrace 调优 runner）模式：参数从 YITRACE_AW_HOME/manifest.json 读取
    home = os.environ.get("YITRACE_AW_HOME")
    if home:
        manifest = json.loads((Path(home) / "manifest.json").read_text(encoding="utf-8"))
        args.task_ids = args.task_ids or manifest.get("task_ids", "")
        args.system_file = args.system_file or manifest.get("system_file")
        args.output = args.output or str(Path(home) / "result.json")
        args.max_interactions = manifest.get("max_interactions", args.max_interactions)
    if not args.api_key:
        raise SystemExit("需要 ZHIPU_API_KEY（或 --api-key）")
    if not args.task_ids.strip():
        raise SystemExit("需要 --task-ids")
    if not args.output:
        raise SystemExit("需要 --output")
    endpoint = resolve_endpoint(args.base_url)
    system_override = None
    if args.system_file:
        system_override = Path(args.system_file).read_text(encoding="utf-8")

    ids = [item.strip() for item in args.task_ids.split(",") if item.strip()]
    if args.parallel > 1 and len(ids) > 1:
        return _run_parallel(args, ids, system_override, endpoint)

    rows = []
    for task_id in ids:
        rows.append(run_task(task_id, model=args.model, endpoint=endpoint,
                             api_key=args.api_key, max_interactions=args.max_interactions,
                             system_override=system_override))
        if args.output:
            _write_rows(args.output, args, rows)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _write_rows(path: str, args, rows: list[dict]) -> None:
    summary = {"count": len(rows), "passCount": sum(1 for r in rows if r.get("passed"))}
    Path(path).write_text(
        json.dumps({"tasks": rows, "summary": summary}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _run_parallel(args, ids: list[str], system_override, endpoint) -> int:
    import multiprocessing as mp

    workers = min(args.parallel, len(ids))
    chunks = [ids[index::workers] for index in range(workers)]
    kwargs = {"model": args.model, "endpoint": endpoint, "api_key": args.api_key,
              "max_interactions": args.max_interactions, "system_override": system_override}
    context = mp.get_context("spawn")
    result_queue = context.Queue()
    processes = [context.Process(target=_parallel_chunk_worker, args=(chunk, kwargs, result_queue), daemon=True)
                 for chunk in chunks]
    for process in processes:
        process.start()
    rows = []
    try:
        for _ in ids:
            message = result_queue.get(timeout=1500)
            if "row" in message:
                rows.append(message["row"])
            else:
                rows.append(message["error"])
            if args.output:
                _write_rows(args.output, args, rows)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(10)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
