"""复制到独立业务项目后，经子进程使用 skill 调用真实 SQLite 测试入口。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = '''
from contextlib import closing
import sqlite3
from fuju_rsi import AgentSpec, Evaluation, Example, Prediction

def build_agent():
    def runner(prompt, item):
        print("runtime input:", item)
        if prompt == "NETWORK_ERROR":
            raise ConnectionError("private credential must not be reported")
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE numbers (value INTEGER)")
            db.executemany("INSERT INTO numbers VALUES (?)", [(v,) for v in item["values"]])
            operator = "SUM" if prompt == "SUM" else "COUNT"
            result = db.execute("SELECT " + operator + "(value) FROM numbers").fetchone()[0]
        if prompt == "OVERFIT":
            result = 0 if item["marker"] == "TEST_PRIVATE" else sum(item["values"])
        return Prediction({"value": result, "marker": item["marker"]}, tokens=0, cost_usd=0.0)

    def proposer(*args):
        raise AssertionError("the original model proposer must never run")

    def evaluate(expected, prediction):
        return Evaluation(float(expected == prediction.output), "fixed answer")

    examples = [Example(split, {"values": values, "marker": marker},
                        {"value": sum(values), "marker": marker}, split)
                for split, values, marker in [
                    ("train", [2, 4], "TRAIN_PUBLIC"),
                    ("validation", [1, 3], "VALIDATION_PRIVATE"),
                    ("test", [1, 4], "TEST_PRIVATE")]]
    return AgentSpec("sum-agent", "Sum agent", "COUNT", examples, runner, proposer, evaluate)

def broken_agent():
    print("private factory configuration")
    raise ValueError("private factory credential")

def network_agent():
    agent = build_agent()
    agent.baseline_prompt = "NETWORK_ERROR"
    return agent
'''


class SkillFlowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "business-app"
        self.project.mkdir()
        (self.project / "tune_agent.py").write_text(FIXTURE, encoding="utf-8")
        skill = self.root / "relocated-skills" / "fuju-tune"
        shutil.copytree(ROOT / "skills" / "fuju-tune", skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.helper = skill / "scripts" / "run_experiment.py"
        self.workspace = self.project / ".yitrace-optimization"
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))

    def run_helper(self, *prompts, max_calls=100, factory="build_agent"):
        command = [sys.executable, str(self.helper), "--agent", "tune_agent:" + factory,
                   "--workspace", str(self.workspace), "--max-calls", str(max_calls)]
        for index, prompt in enumerate(prompts):
            path = self.project / ("candidate-%s.txt" % index)
            path.write_text(prompt, encoding="utf-8")
            command.extend(["--candidate-file", str(path)])
        result = subprocess.run(command, cwd=self.project, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stderr, "")
        for marker in ("VALIDATION_PRIVATE", "TEST_PRIVATE", "private factory", "private credential"):
            self.assertNotIn(marker, result.stdout)
        return result.returncode, json.loads(result.stdout)

    def test_baseline_is_real_complete_but_not_accepted(self):
        code, result = self.run_helper()
        self.assertEqual(code, 0)
        self.assertTrue(result["baselineComplete"])
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 2)
        self.assertEqual(result["trainingFeedback"][0]["cases"][0]["output"]["value"], 2)
        self.assertEqual(result["trials"][0]["validation"]["passed"], 0)
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        self.assertNotIn("TEST_PRIVATE", Path(result["runtimeLog"]).read_text())

    def test_fixed_candidate_finishes_search_without_holdout_or_adoption(self):
        code, result = self.run_helper("SUM")
        self.assertEqual(code, 0)
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 5)
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        record = json.loads(Path(result["record"]).read_text())
        self.assertIsNone(record["candidateTest"])
        self.assertIsNone(record["baselineTest"])
        self.assertEqual(record["bestPrompt"], "SUM")
        self.assertEqual(record["stage"], "search")
        self.assertFalse(record.get("adopted", False))
        self.assertFalse((self.workspace / "active-prompts.json").exists())
        self.assertNotIn("TEST_PRIVATE", Path(result["runtimeLog"]).read_text())
        artifacts = result["artifacts"]
        self.assertTrue(Path(artifacts["report"]).is_file())
        self.assertEqual(Path(artifacts["candidatePrompt"]).read_text(), "SUM")
        self.assertEqual(Path(artifacts["baselinePrompt"]).read_text(), "COUNT")
        self.assertIsNone(artifacts["verifiedPrompt"])
        exported = json.loads(Path(artifacts["result"]).read_text())
        self.assertEqual(exported["id"], result["id"])
        self.assertFalse(exported["adoptable"])
        for key in ("report", "result"):
            content = Path(artifacts[key]).read_text()
            for private in ("VALIDATION_PRIVATE", "TEST_PRIVATE", "TRAIN_PUBLIC", "private credential"):
                self.assertNotIn(private, content)

    def test_report_directory_conflict_does_not_run_or_replace_previous_output(self):
        output = self.root / "existing-report"
        output.mkdir()
        (output / "report.md").write_text("keep this report")
        result = subprocess.run([sys.executable, str(self.helper), "--agent", "tune_agent:broken_agent",
                                 "--workspace", str(self.workspace), "--report-dir", str(output)],
                                cwd=self.project, env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.workspace.exists())
        self.assertEqual((output / "report.md").read_text(), "keep this report")

    def test_multiple_candidates_select_by_validation_without_running_any_holdout(self):
        code, result = self.run_helper("SUM", "WRONG")
        self.assertEqual(code, 0)
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertEqual(result["usedCalls"], 8)
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        log = Path(result["runtimeLog"]).read_text()
        self.assertNotIn("TEST_PRIVATE", log)
        self.assertEqual(result["trials"][2]["validation"]["passed"], 0)

    def test_duplicate_and_no_improvement_do_not_claim_test_success(self):
        for prompt in ("COUNT", "WRONG"):
            with self.subTest(prompt=prompt):
                code, result = self.run_helper(prompt)
                self.assertEqual(code, 0)
                self.assertTrue(result["searchComplete"])
                self.assertFalse(result["adoptable"])
                self.assertIsNone(result["candidateTest"])

    def test_development_gain_alone_cannot_accept_an_overfitted_candidate(self):
        # 候选只会通过开发题；搜索不能读取旧 test，也不能擅自宣称通过独立验收。
        source = (self.project / "tune_agent.py").read_text()
        source = source.replace('"value": sum(values), "marker": marker',
                                '"value": (len(values) if split == "test" else sum(values)), "marker": marker')
        (self.project / "tune_agent.py").write_text(source)
        code, result = self.run_helper("OVERFIT")
        self.assertEqual(code, 0)
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        self.assertFalse(result["adoptable"])
        self.assertNotIn("TEST_PRIVATE", Path(result["runtimeLog"]).read_text())

    def test_budget_zero_and_partial_baseline_are_incomplete(self):
        for calls in (0, 1):
            with self.subTest(calls=calls):
                code, result = self.run_helper(max_calls=calls)
                self.assertEqual(code, 2)
                self.assertFalse(result["baselineComplete"])
                self.assertFalse(result["searchComplete"])
                self.assertEqual(result["usedCalls"], calls)
        code, result = self.run_helper("SUM", max_calls=3)
        self.assertEqual(code, 2)
        self.assertFalse(result["adoptable"])
        self.assertFalse(result["searchComplete"])
        self.assertLessEqual(result["usedCalls"], 3)

    def test_invalid_candidate_fails_before_running(self):
        for prompt in ("  ", "x" * 32769):
            with self.subTest(length=len(prompt)):
                code, result = self.run_helper(prompt)
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(self.workspace.exists())

    def test_factory_failure_is_private_and_has_local_diagnostics(self):
        code, result = self.run_helper(factory="broken_agent")
        self.assertEqual(code, 1)
        self.assertFalse(result["adoptable"])
        self.assertIn("private factory credential", Path(result["runtimeLog"]).read_text())

    def test_runtime_error_is_not_successful_baseline(self):
        code, result = self.run_helper(factory="network_agent")
        self.assertEqual(code, 2)
        self.assertFalse(result["baselineComplete"])
        self.assertFalse(result["searchComplete"])
        self.assertEqual(result["trials"][0]["validation"]["errors"], 1)


if __name__ == "__main__":
    unittest.main()
