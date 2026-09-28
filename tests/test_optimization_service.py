"""真实离线 SQL、HTTP 生命周期、持久化和模型适配边界回归。"""
from __future__ import annotations

import http.client
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fuju_rsi.demo import build_demo_agent
from fuju_rsi.core import optimize
from fuju_rsi.providers import ChatCompletionProposer
from fuju_rsi.server import create_server
from fuju_rsi.workspace import ExperimentError, ExperimentManager, atomic_json, read_active_prompt


class OptimizationServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.agent = build_demo_agent()
        self.manager = ExperimentManager(self.tmp.name, [self.agent])

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def completed(self):
        record = self.manager.start(agent_id=self.agent.id)
        result = self.manager.wait(record["id"], timeout=10)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(result["adoptable"], result)
        self.assertTrue(result["searchComplete"], result)
        self.assertEqual(result["stage"], "search")
        self.assertEqual(result["evidenceStatus"], "not_verified")
        self.assertLessEqual(result["usedCalls"], 100)
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        self.assertEqual(result["trials"][0]["validation"]["score"], 0.5)
        self.assertEqual(result["trials"][1]["validation"]["score"], 1.0)
        self.assertEqual(result["kind"], "demo")
        return result

    def historical_active(self):
        # 模拟升级前已落盘的旧版采用记录；不调用新版 adopt，也不伪造验收回执。
        identifier = "a" * 32
        baseline = self.agent.baseline_prompt
        prompt = baseline + "\n使用 orders 表真实存在的 buyer_id 作为客户标识。"
        old = {"id": identifier, "agentId": self.agent.id, "name": "升级前已采用的 SQL 修正",
               "createdAt": "2026-07-14T08:00:00+00:00", "updatedAt": "2026-07-14T08:01:00+00:00",
               "config": {"maxTrials": 1, "maxCalls": 100}, "kind": "demo", "status": "completed",
               "baselinePrompt": baseline, "bestPrompt": prompt, "selectedTrialId": "trial-1",
               "adoptable": True, "adopted": True, "usedCalls": 13,
               "message": "旧版验证与保留测试已通过。",
               "trials": [{"id": "baseline", "label": "原版", "prompt": baseline,
                           "training": None, "validation": None},
                          {"id": "trial-1", "label": "候选 1", "prompt": prompt,
                           "training": None, "validation": None}],
               "baselineTest": None, "candidateTest": None,
               "previousActive": {"agentId": self.agent.id, "prompt": baseline, "experimentId": None}}
        self.manager.close()
        atomic_json(Path(self.tmp.name) / "experiments" / (identifier + ".json"), old)
        atomic_json(Path(self.tmp.name) / "active-prompts.json", {
            self.agent.id: {"agentId": self.agent.id, "prompt": prompt, "experimentId": identifier}})
        self.manager = ExperimentManager(self.tmp.name, [self.agent])
        return old

    def test_search_cannot_adopt_and_remains_readable_after_restart(self):
        result = self.completed()
        baseline = self.agent.baseline_prompt
        with self.assertRaises(ExperimentError) as error:
            self.manager.adopt(result["id"])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(read_active_prompt(self.tmp.name, self.agent.id, baseline), baseline)
        self.manager.close()
        self.manager = ExperimentManager(self.tmp.name, [self.agent])
        reopened = self.manager.get(result["id"])
        self.assertFalse(reopened["adoptable"])
        self.assertFalse(reopened["adopted"])
        self.assertEqual(reopened["bestPrompt"], result["bestPrompt"])
        self.assertEqual(reopened["trials"], result["trials"])

    def test_historical_adoption_survives_upgrade_and_can_rollback(self):
        old = self.historical_active()
        historical = self.manager.get(old["id"])
        self.assertTrue(historical["adopted"])
        self.assertFalse(historical["adoptable"])
        self.assertEqual(self.manager.list()["items"][0]["id"], old["id"])
        self.assertEqual(read_active_prompt(self.tmp.name, self.agent.id, "fallback"), old["bestPrompt"])
        restored = self.manager.rollback(old["id"])
        self.assertFalse(restored["adopted"])
        self.assertFalse(restored["adoptable"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], old["baselinePrompt"])
        self.assertEqual(self.manager.get(old["id"])["trials"], old["trials"])
        with self.assertRaises(ExperimentError) as error:
            self.manager.adopt(old["id"])
        self.assertEqual(error.exception.status, 409)

    def test_unverified_adoption_and_unadopted_rollback_do_not_overwrite_history(self):
        old = self.historical_active()
        record = self.manager.start(agent_id=self.agent.id)
        second = self.manager.wait(record["id"], timeout=10)
        with self.assertRaises(ExperimentError) as err:
            self.manager.adopt(second["id"])
        self.assertEqual(err.exception.status, 409)
        with self.assertRaises(ExperimentError):
            self.manager.rollback(second["id"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["experimentId"], old["id"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], old["bestPrompt"])

    def test_historical_active_prompt_becomes_next_search_baseline(self):
        old = self.historical_active()
        record = self.manager.start(agent_id=self.agent.id)
        next_result = self.manager.wait(record["id"], timeout=10)
        self.assertEqual(next_result["baselinePrompt"], old["bestPrompt"])
        self.assertFalse(next_result["adoptable"])
        self.assertTrue(next_result["searchComplete"])
        self.assertEqual(next_result["selectedTrialId"], "baseline")
        self.assertEqual(next_result["trials"][0]["validation"]["score"], 1.0)

    def test_workspace_lock_and_interrupted_recovery(self):
        with self.assertRaises(RuntimeError):
            ExperimentManager(self.tmp.name, [self.agent])
        result = self.completed()
        path = self.manager.records_dir / (result["id"] + ".json")
        result.update(status="running", adoptable=True)
        atomic_json(path, result)
        self.manager.close()
        self.manager = ExperimentManager(self.tmp.name, [self.agent])
        recovered = self.manager.get(result["id"])
        self.assertEqual(recovered["status"], "interrupted")
        self.assertFalse(recovered["adoptable"])

    def test_same_agent_busy_and_cancel(self):
        entered, release = threading.Event(), threading.Event()
        original = self.agent.runner
        def runner(prompt, item):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(prompt, item)
        # AgentSpec 允许用户提供回调，不依赖服务里持有可变 spec。
        from dataclasses import replace
        blocked = replace(self.agent, runner=runner)
        self.manager.agents[blocked.id] = blocked
        record = self.manager.start(agent_id=blocked.id)
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ExperimentError) as err:
                self.manager.start(agent_id=blocked.id)
            self.assertEqual(err.exception.status, 409)
            self.manager.cancel(record["id"])
        finally:
            release.set()
        result = self.manager.wait(record["id"], timeout=5)
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["adoptable"])

    def test_zero_budget_never_produces_an_adoptable_version(self):
        record = self.manager.start(agent_id=self.agent.id, max_calls=0)
        result = self.manager.wait(record["id"], timeout=5)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 0)
        with self.assertRaises(ExperimentError):
            self.manager.adopt(record["id"])

    def test_failed_persistence_does_not_leave_permanent_running_state(self):
        save = self.manager._save
        writes = []
        def full_disk(record):
            writes.append(record["id"])
            if len(writes) > 1:
                raise OSError("fixture disk full")
            return save(record)
        with patch.object(self.manager, "_save", side_effect=full_disk):
            record = self.manager.start(agent_id=self.agent.id)
            result = self.manager.wait(record["id"], timeout=5)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertIn("保存", result["message"])
        self.completed()  # 磁盘恢复后能创建下一次实验，不被幽灵 running 阻塞。

    def test_closed_manager_cannot_overwrite_new_owner(self):
        result = self.historical_active()
        old = self.manager
        old.close()
        self.manager = ExperimentManager(self.tmp.name, [self.agent])
        for method in (old.adopt, old.rollback, old.cancel):
            with self.assertRaises(ExperimentError) as err:
                method(result["id"])
            self.assertEqual(err.exception.status, 503)
        self.assertEqual(read_active_prompt(self.tmp.name, self.agent.id, "fallback"), result["bestPrompt"])

    def test_server_refuses_non_loopback_bind(self):
        """优化服务只允许绑回环。这条约束是代码强制的，用测试盯住，不能只靠约定。"""
        for host in ("0.0.0.0", "192.168.1.5", "::", "", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                create_server(self.manager, host=host, port=0)
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                server = create_server(self.manager, host=host, port=0)
                try:
                    self.assertIn(server.server_address[0], ("127.0.0.1", "::1"))
                finally:
                    server.server_close()

    def test_http_workflow_origin_guards_and_static_paths(self):
        static = Path(self.tmp.name) / "static"
        static.mkdir()
        (static / "index.html").write_text("<h1>yiTrace console</h1>", encoding="utf-8")
        server = create_server(self.manager, port=0, static_dir=static)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, extra=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            headers = {"Content-Type": "application/json", **(extra or {})}
            connection.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            status, content_type, data = response.status, response.getheader("Content-Type"), response.read()
            connection.close()
            return status, json.loads(data) if "json" in content_type else data.decode()
        try:
            status, caps = request("GET", "/v1/optimizations/capabilities")
            self.assertEqual(status, 200)
            self.assertFalse(caps["traceAvailable"])
            self.assertEqual(caps["protocolVersion"], 2)
            self.assertFalse(caps["verificationAvailable"])
            self.assertEqual(caps["agents"][0]["counts"]["train"], 2)
            self.assertEqual(caps["agents"][0]["counts"]["validation"], 2)
            status, result = request("POST", "/v1/optimizations", {"agentId": self.agent.id, "maxTrials": 3, "maxCalls": 100})
            self.assertEqual(status, 202, result)
            self.manager.wait(result["id"], 10)
            status, result = request("GET", "/v1/optimizations/" + result["id"])
            self.assertFalse(result["adoptable"])
            self.assertTrue(result["searchComplete"])
            self.assertEqual(result["stage"], "search")
            self.assertEqual(result["evidenceStatus"], "not_verified")
            self.assertIsNone(result["baselineTest"])
            self.assertIsNone(result["candidateTest"])
            status, rejection = request("POST", "/v1/optimizations/" + result["id"] + "/adopt", {})
            self.assertEqual(status, 409, rejection)
            self.assertIsNone(self.manager.active_prompt(self.agent.id)["experimentId"])
            self.assertEqual(request("GET", "/")[0], 200)
            self.assertEqual(request("GET", "/%2e%2e/active-prompts.json")[0], 404)
            self.assertEqual(request("GET", "/v1/sessions")[0], 404)
            self.assertEqual(request("POST", "/v1/optimizations", {"agentId": self.agent.id}, {"Origin": "https://example.com"})[0], 403)
            self.assertEqual(request("POST", "/v1/optimizations", {}, {"Sec-Fetch-Site": "cross-site"})[0], 403)
            self.assertEqual(request("GET", "/v1/optimizations", extra={"Host": "evil.example"})[0], 403)
            self.assertEqual(request("POST", "/v1/optimizations", {"agentId": ["invalid"]})[0], 400)
            self.assertEqual(request("POST", "/v1/optimizations", {"agentId": self.agent.id, "maxCalls": True})[0], 400)
            self.assertEqual(request("POST", "/v1/optimizations", {"agentId": self.agent.id, "module": "os:system"})[0], 400)
            self.assertEqual(request("POST", "/v1/optimizations", {"agentId": self.agent.id}, {"Content-Type": "text/plain"})[0], 415)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class ProposerTests(unittest.TestCase):
    def test_custom_http_agent_and_model_proposer_complete_real_rerun_protocol(self):
        calls = []
        class Endpoint(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                if self.path == "/v1/chat/completions":
                    result = {"choices": [{"message": {"content": '{"prompt":"Return the input value."}'}}]}
                elif self.path == "/agent":
                    result = {"output": body["input"]["value"] if body["prompt"] == "Return the input value." else 0}
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                base = "http://127.0.0.1:" + str(server.server_port)
                config = {"id": "http-test", "name": "HTTP integration", "agentUrl": base + "/agent", "baselinePrompt": "Return zero.",
                          "examples": [{"id": split, "input": {"value": i}, "expected": i, "split": split}
                                       for i, split in enumerate(("train", "validation", "test"), start=1)]}
                path = Path(folder) / "agent.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                example = Path(__file__).resolve().parents[1] / "examples" / "optimize_http_agent.py"
                module_spec = importlib.util.spec_from_file_location("optimizer_http_example", example)
                module = importlib.util.module_from_spec(module_spec)
                module_spec.loader.exec_module(module)
                with patch.dict("os.environ", {"YT_OPTIMIZATION_CONFIG": str(path), "YT_OPTIMIZER_BASE_URL": base + "/v1", "YT_OPTIMIZER_MODEL": "fixture", "YT_OPTIMIZER_API_KEY": "", "YT_AGENT_TOKEN": ""}):
                    result = optimize(module.build_agent(), max_trials=1, max_calls=7)
                self.assertFalse(result["adoptable"], result)
                self.assertTrue(result["searchComplete"], result)
                self.assertEqual(result["usedCalls"], 5)
                proposals = [body for endpoint, body in calls if endpoint.endswith("chat/completions")]
                feedback = json.loads(proposals[0]["messages"][1]["content"])["trainingResults"]
                self.assertEqual([item["id"] for item in feedback], ["train"])
                agent_calls = [body for endpoint, body in calls if endpoint == "/agent"]
                self.assertEqual(len(agent_calls), 4)
                self.assertEqual({body["input"]["value"] for body in agent_calls}, {1, 2})
                self.assertIsNone(result["candidateTest"])
                self.assertIsNone(result["baselineTest"])
                self.assertIsNone(result["trials"][1]["validation"]["tokens"])
                self.assertIsNone(result["trials"][1]["validation"]["costUsd"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_chat_completion_wire_and_redacted_http_error(self):
        requests = []
        class Model(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body, self.headers.get("Authorization")))
                if len(requests) == 1:
                    response = {"choices": [{"message": {"content": json.dumps({"prompt": "Use actual schema columns."})}}]}
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(response).encode())
                else:
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b"do-not-expose-fixture-secret")
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Model)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ChatCompletionProposer(base_url="http://127.0.0.1:" + str(server.server_port) + "/v1", model="test-model", api_key="fixture-key")
            self.assertEqual(provider("original", [{"id": "train-1", "reason": "missing column"}], 1), "Use actual schema columns.")
            path, body, authorization = requests[0]
            self.assertEqual(path, "/v1/chat/completions")
            self.assertEqual(authorization, "Bearer fixture-key")
            self.assertEqual(json.loads(body["messages"][1]["content"])["trainingResults"][0]["id"], "train-1")
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
                provider("original", [], 2)
            self.assertNotIn("fixture-secret", str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)

    def test_remote_plaintext_and_url_credentials_rejected(self):
        for url in ["http://example.com/v1", "https://key@example.com/v1", "https://example.com/v1?key=secret"]:
            with self.assertRaises(ValueError):
                ChatCompletionProposer(base_url=url, model="fixture")


if __name__ == "__main__":
    unittest.main()
