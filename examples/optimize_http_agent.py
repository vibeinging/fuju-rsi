"""把现有 Python/Node HTTP Agent 接入优化器；配置只在服务端读取。

运行：fuju-rsi optimize --workspace ./optimization-data --agent optimize_http_agent:build_agent --serve
需要把本文件所在目录放进 PYTHONPATH，或从该目录启动。详见 Python SDK README。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

from fuju_rsi import AgentSpec, Evaluation, Example, Prediction
from fuju_rsi.providers import ChatCompletionProposer


def build_agent():
    config = json.loads(Path(os.environ["YT_OPTIMIZATION_CONFIG"]).read_text(encoding="utf-8"))
    endpoint = config["agentUrl"]
    url = urllib.parse.urlsplit(endpoint)
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("agentUrl 必须是无凭据和查询参数的 HTTP(S) 地址")
    if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("远程 Agent 请使用 HTTPS")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    def runner(prompt, item):
        headers = {"Content-Type": "application/json"}
        token = os.environ.get("YT_AGENT_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = json.dumps({"prompt": prompt, "input": item}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=45) as response:
                raw = response.read(2_000_001)
        except (urllib.error.URLError, OSError):
            raise RuntimeError("Agent 调用失败或超时") from None
        if len(raw) > 2_000_000:
            raise RuntimeError("Agent 返回内容过大，请在业务侧提取必要结果")
        value = json.loads(raw)
        return Prediction(output=value["output"], tokens=value.get("tokens"), cost_usd=value.get("costUsd"), trace_id=value.get("traceId"))

    def evaluator(expected, prediction):
        # 示例使用 JSON 结果完全相等；问数/客服等实际业务可替换成自己的确定性检查。
        matches = prediction.output == expected
        return Evaluation(1.0 if matches else 0.0, "输出与期望 JSON 一致" if matches else "输出与期望 JSON 不一致")

    return AgentSpec(
        id=config["id"], name=config["name"], baseline_prompt=config["baselinePrompt"],
        examples=[Example(**item) for item in config["examples"]], runner=runner, evaluator=evaluator,
        proposer=ChatCompletionProposer(base_url=os.environ["YT_OPTIMIZER_BASE_URL"], model=os.environ["YT_OPTIMIZER_MODEL"], api_key=os.environ.get("YT_OPTIMIZER_API_KEY", "")),
        description="真实 HTTP Agent；模型生成候选，业务结果独立评分。",
    )
