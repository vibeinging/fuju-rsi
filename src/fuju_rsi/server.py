"""本地优化 API 和控制台。执行入口只能在服务启动时注册。"""
from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import urllib.parse

from .workspace import ExperimentError


def create_server(manager, *, host="127.0.0.1", port=7880, static_dir=None, trace_db=None):
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("优化服务仅支持绑定 127.0.0.1 或 localhost")
    assets = Path(static_dir or Path(__file__).parent / "console_dist").resolve()

    class Handler(BaseHTTPRequestHandler):
        server_version = "FujuRSI"

        def log_message(self, *_args):
            pass

        def _reply(self, status, body, content_type="application/json; charset=utf-8"):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _local_request(self):
            # Host 防 DNS rebinding；Origin 和 Fetch Metadata 防其他网页触发本地付费实验。
            allowed = {"127.0.0.1:" + str(self.server.server_port), "localhost:" + str(self.server.server_port)}
            if self.headers.get("Host", "") not in allowed:
                raise ExperimentError("请求 Host 不属于此本地服务", 403)
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + h for h in allowed}:
                raise ExperimentError("仅允许同源页面访问本地优化服务", 403)
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise ExperimentError("不接受跨站请求", 403)

        def _body(self):
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise ExperimentError("请求体必须是 application/json", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ExperimentError("Content-Length 无效") from None
            if not 0 <= size <= 1_000_000:
                raise ExperimentError("请求体最多 1 MB", 413)
            try:
                data = json.loads(self.rfile.read(size), parse_constant=lambda _v: (_ for _ in ()).throw(ValueError())) if size else {}
            except (ValueError, UnicodeError):
                raise ExperimentError("请求体不是有效 JSON") from None
            if not isinstance(data, dict):
                raise ExperimentError("请求体必须是 JSON 对象")
            return data

        def _handle(self):
            self.connection.settimeout(10)
            try:
                self._local_request()
                path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
                prefix = "/v1/optimizations"
                if path == prefix + "/capabilities" and self.command == "GET":
                    self._reply(200, manager.capabilities(trace_db is not None))
                elif path == prefix:
                    if self.command == "GET":
                        self._reply(200, manager.list())
                    elif self.command == "POST":
                        body = self._body()
                        if set(body) - {"agentId", "name", "maxTrials", "maxCalls"}:
                            raise ExperimentError("请求包含不支持的参数")
                        if not isinstance(body.get("agentId"), str):
                            raise ExperimentError("agentId 必须是已注册的字符串 id")
                        self._reply(202, manager.start(agent_id=body["agentId"], name=body.get("name"),
                                                      max_trials=body.get("maxTrials", 3), max_calls=body.get("maxCalls", 100)))
                    else:
                        raise ExperimentError("方法不支持", 405)
                elif path.startswith(prefix + "/agents/") and path.endswith("/prompt") and self.command == "GET":
                    agent_id = urllib.parse.unquote(path[len(prefix + "/agents/"):-len("/prompt")])
                    self._reply(200, manager.active_prompt(agent_id))
                elif path.startswith(prefix + "/"):
                    parts = path[len(prefix) + 1:].split("/")
                    if not re.fullmatch(r"[a-f0-9]{32}", parts[0]):
                        raise ExperimentError("实验不存在", 404)
                    if len(parts) == 1 and self.command == "GET":
                        self._reply(200, manager.get(parts[0]))
                    elif len(parts) == 2 and self.command == "POST" and parts[1] == "verification":
                        body = self._body()
                        receipt = body.get("receipt")
                        if (set(body) != {"receipt"} or not isinstance(receipt, dict) or not isinstance(receipt.get("body"), dict) or
                                receipt["body"].get("experimentId") != parts[0]):
                            raise ExperimentError("需要与当前实验绑定的签名回执")
                        self._reply(200, manager.import_verification(receipt))
                    elif len(parts) == 2 and self.command == "POST" and parts[1] in ("cancel", "adopt", "rollback"):
                        if self._body():
                            raise ExperimentError("此操作不接受额外参数")
                        self._reply(200, getattr(manager, parts[1])(parts[0]))
                    else:
                        raise ExperimentError("端点不存在", 404)
                elif path.startswith("/v1/"):
                    if trace_db is None:
                        raise ExperimentError("本服务未连接 trace DB；启动时可指定 --trace-db DIR", 404)
                    body = self._body() if self.command != "GET" else None
                    tenant = self.headers.get("X-Tenant-Id")
                    try:
                        result = trace_db.route_json(self.command, self.path, json.dumps(body, ensure_ascii=False) if body is not None else "", tenant_id=tenant)
                    except RuntimeError as err:
                        match = re.search(r"status=(\d+)", str(err))
                        status = int(match.group(1)) if match else 500
                        raise ExperimentError("Trace API 请求失败", status if 400 <= status <= 599 else 500) from None
                    self._reply(200, result)
                elif self.command == "GET":
                    relative = "index.html" if path in ("/", "/index.html") else urllib.parse.unquote(path).lstrip("/")
                    target = (assets / relative).resolve()
                    try:
                        target.relative_to(assets)
                    except ValueError:
                        raise ExperimentError("资源不存在", 404) from None
                    if not target.is_file():
                        raise ExperimentError("控制台资源不存在，请使用包含控制台构建产物的 SDK", 404)
                    self._reply(200, target.read_bytes(), mimetypes.guess_type(str(target))[0] or "application/octet-stream")
                else:
                    raise ExperimentError("方法不支持", 405)
            except ExperimentError as err:
                self._reply(err.status, {"error": str(err)})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                self._reply(500, {"error": "服务内部错误；实验记录已保留，请检查本地配置。"})

        do_GET = _handle
        do_POST = _handle
        do_PATCH = _handle
        do_DELETE = _handle

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
