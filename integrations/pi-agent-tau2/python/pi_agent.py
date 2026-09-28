"""tau2-bench 自定义 Agent：把被测 Agent 的模型层放到 pi 栈（pi-ai）桥服务上。

协议对齐 tau2 的半双工约定：generate_next_message 每次返回一条
AssistantMessage（纯文本或 tool_calls）；工具由官方 orchestrator 执行并计入
轨迹，reward 的 action 检查因此保持官方语义。本模块只做三件事：
- 把 tau2 Tool 转成 pi-ai 的工具定义；
- 把默认策略渲染成系统提示词（或注入候选提示词文件，调优入口）；
- 与 Node 桥服务交换 user 文本 / 工具结果 / 模型决策。
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import List, Optional

from tau2.agent.base_agent import HalfDuplexAgent, ValidAgentInputMessage
from tau2.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from tau2.environment.tool import Tool

DEFAULT_PROMPT_TEMPLATE = "You are a customer service agent.\n\n# Policy\n{policy}"


def default_prompt(domain_policy: str) -> str:
    return DEFAULT_PROMPT_TEMPLATE.format(policy=domain_policy)


class _BridgeError(RuntimeError):
    pass


class PiAgent(HalfDuplexAgent[dict]):
    """模型决策经由 pi-ai 桥服务的 tau2 半双工 Agent。"""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        *,
        endpoint: str = "http://127.0.0.1:7891",
        model: str = "mock-agent",
        prompt_override: Optional[str] = None,
        timeout_seconds: float = 240.0,
    ) -> None:
        super().__init__(tools=tools, domain_policy=domain_policy)
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.prompt_override = prompt_override
        self.timeout_seconds = timeout_seconds
        self.system_prompt = (
            prompt_override if prompt_override is not None else default_prompt(domain_policy)
        )
        self._pi_tools = [self._convert_tool(tool) for tool in tools]

    @staticmethod
    def _convert_tool(tool: Tool) -> dict:
        schema = tool.openai_schema
        return {
            "name": tool.name,
            "description": tool.short_desc or tool.name,
            "parameters": schema["function"]["parameters"],
        }

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> tuple[int, bytes]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.endpoint + path,
            data=data,
            method=method,
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            return response.status, response.read()

    def _post(self, path: str, body: Optional[dict] = None) -> dict:
        _, payload = self._request("POST", path, body)
        return json.loads(payload) if payload else {}

    def get_init_state(self, message_history: Optional[List[Message]] = None) -> dict:
        created = self._post(
            "/v1/agent/sessions",
            {"model": self.model, "systemPrompt": self.system_prompt, "tools": self._pi_tools},
        )
        state = {"sessionId": created["sessionId"], "turns": 0}
        for message in message_history or []:
            self._replay(message, state)
        return state

    def _replay(self, message: Message, state: dict) -> None:
        if isinstance(message, UserMessage):
            self._post(
                f"/v1/agent/sessions/{state['sessionId']}/messages",
                {"events": [{"type": "user", "text": str(message.content)}]},
            )
        elif isinstance(message, (ToolMessage, MultiToolMessage)):
            self._send_tool_results(message, state)

    def _send_tool_results(self, message: ToolMessage | MultiToolMessage, state: dict) -> None:
        items = message.tool_messages if isinstance(message, MultiToolMessage) else [message]
        results = [
            {
                "toolCallId": item.id,
                "text": item.content if item.content is not None else "",
                "error": bool(item.error),
            }
            for item in items
        ]
        self._post(
            f"/v1/agent/sessions/{state['sessionId']}/messages",
            {"events": [{"type": "toolResults", "results": results}]},
        )

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: dict
    ) -> tuple[AssistantMessage, dict]:
        session_id = state["sessionId"]
        if isinstance(message, UserMessage):
            self._post(
                f"/v1/agent/sessions/{session_id}/messages",
                {"events": [{"type": "user", "text": str(message.content)}]},
            )
        elif isinstance(message, (ToolMessage, MultiToolMessage)):
            self._send_tool_results(message, state)
        else:  # pragma: no cover - 官方输入类型只有以上两类
            raise _BridgeError(f"unsupported input message {type(message).__name__}")

        decision = self._post(f"/v1/agent/sessions/{session_id}/complete")
        text_parts: List[str] = []
        tool_calls: List[ToolCall] = []
        for block in decision.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "toolCall":
                tool_calls.append(
                    ToolCall(id=block.get("id") or "", name=block["name"], arguments=block.get("arguments") or {})
                )
        assistant = AssistantMessage.text(" ".join(p for p in text_parts if p), tool_calls=tool_calls or None)
        state["turns"] = state.get("turns", 0) + 1
        return assistant, state


def load_prompt_override(path: Optional[str]) -> Optional[str]:
    """读取候选提示词文件；未提供时返回 None，Agent 使用默认策略渲染。"""
    if not path:
        return None
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()
