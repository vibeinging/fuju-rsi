"""pi-agent × tau2-bench × fuju-tune 端到端冒烟测试。

前置：先运行 integrations/pi-agent-tau2/setup.sh（克隆 tau2-bench、建 venv、
安装 pi-ai）。缺前置时整个测试类跳过，而不是误报失败。

用系统 Python 运行：
    python3 -m unittest discover -s tests -v

覆盖：
1. 桥服务健康 + mock 域任务端到端拿到官方 reward（候选 1.0 / 默认 0.0）
2. casebook 导出通过 yitrace inspect（独立 group、无 warning）
3. fuju-tune 全流程演练：真实选出有收益的候选（selectedTrialId=trial-1）
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / ".vendor" / "tau2-bench"
TAU2_PY = VENDOR / ".venv" / "bin" / "python"
NODE = ROOT / "agent-node"

CANDIDATE = (
    "You are a customer service agent. Use the exact title the user provides when creating tasks.\n\n"
    "# Policy\nEach task must have a title."
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(TAU2_PY.exists() and (NODE / "node_modules").exists(), "先运行 setup.sh 准备 tau2 venv 与 pi-ai")
class PiTau2Smoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        env = dict(os.environ, PI_BRIDGE_PORT=str(cls.port))
        cls.server = subprocess.Popen(
            ["node", "src/server.mjs"], cwd=NODE, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.5):
                    break
            except OSError:
                if cls.server.poll() is not None:
                    raise AssertionError("bridge service exited early")
                time.sleep(0.2)
        else:
            cls.tearDownClass()
            raise AssertionError("bridge service did not start")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None) and cls.server.poll() is None:
            cls.server.send_signal(signal.SIGTERM)
            cls.server.wait(timeout=10)

    def _run_task(self, *args, prompt_file=None):
        cmd = [str(TAU2_PY), str(ROOT / "python" / "run_task.py"),
               "--endpoint", f"http://127.0.0.1:{self.port}", *args]
        if prompt_file:
            cmd += ["--prompt-file", str(prompt_file)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=True)
        return json.loads(result.stdout)

    def test_candidate_scores_full_reward_officially(self):
        path = ROOT / "candidates" / "exact-title-v1.txt"
        if not path.exists():
            path.write_text(CANDIDATE, encoding="utf-8")
        payload = self._run_task("--domain", "mock", "--task-ids", "create_task_1", prompt_file=path)
        row = payload["tasks"][0]
        self.assertEqual(row["reward"], 1.0, f"候选提示词应通过官方判分: {row}")

    def test_default_prompt_scores_zero(self):
        payload = self._run_task("--domain", "mock", "--task-ids", "create_task_1")
        row = payload["tasks"][0]
        self.assertEqual(row["reward"], 0.0, f"默认策略应因标题走样失败（提示词敏感性守卫）: {row}")

    def test_casebook_export_passes_inspect(self):
        out = ROOT / "evals" / "test-export.json"
        if out.exists():
            out.unlink()
        subprocess.run(
            [str(TAU2_PY), str(ROOT / "python" / "export_casebook.py"), "--output", str(out)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        book = json.loads(out.read_text())
        groups = {case["group"] for case in book["cases"]}
        self.assertEqual(len(groups), len(book["cases"]), "每个任务必须是独立 group")
        self.assertTrue(all(case["source"]["ref"] for case in book["cases"]))
        out.unlink()

    def test_full_tune_drill_selects_improving_candidate(self):
        workspace = ROOT / ".yitrace-optimization-test"
        casebook = ROOT / "benchmarks" / "mock-v1"
        candidate = ROOT / "candidates" / "exact-title-v1.txt"
        if not candidate.exists():
            candidate.write_text(CANDIDATE, encoding="utf-8")
        shutil.rmtree(workspace, ignore_errors=True)
        env = dict(os.environ, PI_TAU2_CASEBOOK=str(casebook),
                   PI_TAU2_ENDPOINT=f"http://127.0.0.1:{self.port}")
        skill = ROOT.parent.parent / "skills" / "fuju-tune" / "scripts" / "run_experiment.py"
        try:
            for extra in ([], ["--candidate-file", str(candidate)]):
                result = subprocess.run(
                    [str(TAU2_PY), str(skill), "--agent", "python.tune_agent:build_agent",
                     "--workspace", str(workspace), "--benchmark", str(casebook),
                     "--max-calls", "40", *extra],
                    cwd=ROOT, env=env, capture_output=True, text=True, timeout=600, check=True,
                )
                payload = json.loads(result.stdout.strip().splitlines()[-1])
                self.assertEqual(payload["status"], "completed", payload.get("error"))
                self.assertTrue(payload["searchComplete"])
            # 第二次（候选比较）必须真实选出 trial-1
            self.assertEqual(payload["selectedTrialId"], "trial-1", "有真实收益的候选应被选出")
            self.assertFalse(payload["adoptable"], "无独立验收时不得授予采用资格")
            trials = {t["trialId"]: t["cases"] for t in payload["trainingFeedback"]}
            self.assertEqual(
                {c["id"]: c["score"] for c in trials["baseline"]}["create_task_1"], 0.0)
            self.assertEqual(
                {c["id"]: c["score"] for c in trials["trial-1"]}["create_task_1"], 1.0)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
