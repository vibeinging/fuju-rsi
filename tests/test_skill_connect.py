"""从未接入的外部项目开始；真实运行普通业务进程，不调用模型。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = '''
import json, sqlite3, sys
assert "fuju_rsi" not in sys.modules
request = json.load(sys.stdin)
with sqlite3.connect(":memory:") as db:
    db.execute("CREATE TABLE values_table (value INTEGER)")
    db.executemany("INSERT INTO values_table VALUES (?)", [(v,) for v in request["values"]])
    operator = "SUM" if sys.argv[1] == "SUM" else "COUNT"
    value = db.execute("SELECT " + operator + "(value) FROM values_table").fetchone()[0]
print(json.dumps({"value": value}))
'''
ADAPTER = '''
import json, subprocess, sys
from pathlib import Path
from fuju_rsi import AgentSpec, Example, Prediction, Evaluation

def runner(prompt, request):
    with Path("calls.txt").open("a") as stream:
        stream.write(prompt + "\\n")
    result = subprocess.run([sys.executable, "-I", "-S", "business.py", prompt],
                            input=json.dumps(request), capture_output=True, text=True,
                            check=True, timeout=5)
    return Prediction(json.loads(result.stdout))

def evaluate(expected, observed):
    return Evaluation(float(expected == observed.output), "固定算术结果")

def propose(*args):
    raise AssertionError("first connection must only run baseline")

def build_agent():
    print("FACTORY_PRIVATE_TOKEN")
    return AgentSpec("business", "Business Agent", Path("prompt.txt").read_text(), [
        Example("train", {"values": [2, 4]}, {"value": 6}, "train"),
        Example("validation", {"values": [3, 5], "private": "VALIDATION_PRIVATE"}, {"value": 8}, "validation"),
        Example("test", {"values": [4, 9], "private": "HOLDOUT_PRIVATE"}, {"value": 13}, "test"),
    ], runner, propose, evaluate)
'''


class ProjectConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fuju-connect-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "external-app"
        self.project.mkdir()
        (self.project / "business.py").write_text(APP, encoding="utf-8")
        (self.project / "prompt.txt").write_text("COUNT", encoding="utf-8")
        self.skill = self.root / "copied-skill"
        shutil.copytree(ROOT / "skills/fuju-tune", self.skill, ignore=shutil.ignore_patterns("__pycache__"))
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"), PYTHONDONTWRITEBYTECODE="1")
        self.profile = self.project / ".fuju-rsi/connection/connection.json"
        self.adapter = self.project / ".fuju-rsi/connection/fuju_connection_adapter.py"

    def execute(self, *args, helper=False):
        command = [sys.executable, str(self.skill / "scripts/connect.py")] if helper else [sys.executable, "-m", "fuju_rsi", "connect"]
        result = subprocess.run(command + list(args) + ["--project", str(self.project)],
                                cwd=self.root, env=self.environment, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.stderr, "", result.stderr)
        for private in ("FACTORY_PRIVATE_TOKEN", "VALIDATION_PRIVATE", "HOLDOUT_PRIVATE", "PASSWORD_PRIVATE"):
            self.assertNotIn(private, result.stdout)
        return result.returncode, json.loads(result.stdout)

    def prepare(self):
        code, response = self.execute("init", "--kind", "custom")
        self.assertEqual(code, 0, response)
        self.adapter.write_text(ADAPTER, encoding="utf-8")

    def test_skeleton_is_blocked_without_running_business_or_making_answers(self):
        code, result = self.execute("init", "--kind", "custom", helper=True)
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "prepared")
        code, result = self.execute("check", helper=True)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["executionChecked"])
        self.assertFalse((self.project / "calls.txt").exists())
        code, result = self.execute("baseline", helper=True)
        self.assertEqual(code, 1)
        self.assertFalse(result["adoptable"])
        self.assertFalse((self.project / "calls.txt").exists())

    def test_check_has_no_runner_calls_then_baseline_runs_original_in_fresh_workspace(self):
        self.prepare()
        code, check = self.execute("check")
        self.assertEqual(code, 0, check)
        self.assertEqual(check["status"], "ready")
        self.assertEqual(check["plannedRunnerCalls"], 2)
        self.assertFalse(check["executionChecked"])
        self.assertFalse((self.project / "calls.txt").exists())
        # 旧实验的active提示词不能污染业务原版。
        (self.project / ".fuju-rsi/active-prompts.json").write_text('{"business":"SUM"}')
        results = []
        for helper in (False, True):
            code, result = self.execute("baseline", "--max-calls", "2", helper=helper)
            self.assertEqual(code, 0, result)
            self.assertTrue(result["baselineComplete"])
            self.assertEqual(result["mode"], "baseline")
            self.assertEqual(result["usedCalls"], 2)
            self.assertFalse(result["adoptable"])
            self.assertIsNone(result["baselineTest"])
            self.assertEqual(len(result["trials"]), 1)
            self.assertEqual(result["trials"][0]["validation"]["score"], 0)
            self.assertEqual(Path(result["artifacts"]["baselinePrompt"]).read_text(), "COUNT")
            self.assertTrue(Path(result["artifacts"]["report"]).is_file())
            results.append(result)
        self.assertNotEqual(Path(results[0]["record"]).parents[1], Path(results[1]["record"]).parents[1])
        self.assertEqual((self.project / "calls.txt").read_text().splitlines(), ["COUNT"] * 4)

    def test_repeated_init_preserves_adapter_and_profile(self):
        self.prepare()
        original = self.adapter.read_bytes(), self.profile.read_bytes()
        code, result = self.execute("init", "--kind", "custom")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(original, (self.adapter.read_bytes(), self.profile.read_bytes()))
        code, result = self.execute("init", "--kind", "ask-data")
        self.assertEqual(code, 1)
        self.assertEqual(original, (self.adapter.read_bytes(), self.profile.read_bytes()))

    def test_existing_adapter_can_be_reused_without_generated_stub(self):
        (self.project / "existing_adapter.py").write_text(ADAPTER)
        code, result = self.execute("init", "--kind", "custom", "--agent", "existing_adapter:build_agent")
        self.assertEqual(code, 0, result)
        self.assertIsNone(result["adapter"])
        self.assertFalse(self.adapter.exists())
        code, result = self.execute("baseline", "--max-calls", "2")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["baselineComplete"])

    def test_missing_existing_adapter_directory_does_not_leave_bad_profile(self):
        code, result = self.execute("init", "--kind", "custom", "--agent", "existing:build_agent",
                                    "--adapter-dir", "missing-adapter")
        self.assertEqual(code, 1, result)
        self.assertFalse(self.profile.exists())
        (self.project / "existing.py").write_text(ADAPTER)
        code, result = self.execute("init", "--kind", "custom", "--agent", "existing:build_agent", "--adapter-dir", ".")
        self.assertEqual(code, 0, result)

    def test_unknown_fields_do_not_print_credentials(self):
        self.prepare()
        value = json.loads(self.profile.read_text())
        value["password"] = "PASSWORD_PRIVATE"
        self.profile.write_text(json.dumps(value))
        code, result = self.execute("baseline")
        self.assertEqual(code, 1)
        self.assertFalse((self.project / "calls.txt").exists())

    def test_file_candidate_spec_does_not_enter_prompt_baseline(self):
        self.prepare()
        self.adapter.write_text(ADAPTER.replace('return AgentSpec(', 'spec = AgentSpec(') +
                                '\n    spec.candidate_kind = "files"\n    return spec\n')
        code, result = self.execute("baseline", "--max-calls", "2")
        self.assertEqual(code, 1, result)
        self.assertFalse((self.project / "calls.txt").exists())

    def test_inspect_only_lists_cues_and_missing_sdk_returns_actionable_result(self):
        (self.project / "app.py").write_text('raise RuntimeError("PASSWORD_PRIVATE")')
        (self.project / ".env").write_text("PASSWORD_PRIVATE")
        (self.project / "test_existing.py").write_text('raise RuntimeError("do not execute")')
        (self.project / "known-tests.json").write_text('{"private":"PASSWORD_PRIVATE"}')
        code, result = self.execute("inspect", helper=True)
        self.assertEqual(code, 0)
        self.assertIn("app.py", result["possibleEntries"])
        self.assertIn("test_existing.py", result["existingTests"])
        self.assertIn("known-tests.json", result["existingTests"])
        self.assertFalse(result["executedProjectCode"])
        missing = subprocess.run([sys.executable, "-I", "-S", str(self.skill / "scripts/connect.py"), "inspect"],
                                 env=self.environment, capture_output=True, text=True, timeout=10)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["status"], "blocked")

    def test_supported_runner_with_unimplemented_other_branch_is_not_a_stub(self):
        self.prepare()
        self.adapter.write_text(ADAPTER.replace('def runner(prompt, request):',
                                               'def runner(prompt, request):\n    if prompt == "unsupported":\n        raise NotImplementedError("unsupported backend")'))
        code, check = self.execute("check")
        self.assertEqual(code, 0, check)
        code, result = self.execute("baseline", "--max-calls", "2")
        self.assertEqual(code, 0, result)
        self.assertTrue(result["baselineComplete"])

    def test_symlink_workspace_does_not_write_outside_project(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.project / ".fuju-rsi").symlink_to(outside, target_is_directory=True)
        code, result = self.execute("init", "--kind", "custom")
        self.assertEqual(code, 1, result)
        self.assertEqual(list(outside.iterdir()), [])

    def test_ask_data_missing_bundle_blocks_before_factory_import(self):
        (self.project / "existing_adapter.py").write_text('from pathlib import Path;Path("imported").touch()\n')
        self.execute("init", "--kind", "ask-data", "--agent", "existing_adapter:build_agent")
        code, result = self.execute("baseline")
        self.assertEqual(code, 1, result)
        self.assertFalse((self.project / "imported").exists())

    def test_ask_data_reuses_ledger_and_exhaustion_never_calls_runner(self):
        shutil.copyfile(ROOT / "examples/ask_data_catalog.json", self.project / "catalog.json")
        fixture = (ROOT / "examples/ask_data_fixture.py").read_text()
        fixture = fixture.replace('def run(prompt, question):', 'def run(prompt, question):\n    from pathlib import Path\n    with Path("calls.txt").open("a") as stream:\n        stream.write("called\\n")')
        (self.project / "ask_data_fixture.py").write_text(fixture)
        def rsi(*args):
            result = subprocess.run([sys.executable, "-m", "fuju_rsi", *args], cwd=self.project,
                                    env=self.environment, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        rsi("ask-data", "prepare", "--catalog", "catalog.json", "--role", "development", "--output", "casebook.json")
        rsi("ask-data", "freeze", "--casebook", "casebook.json", "--role", "development", "--output", "development-v1", "--version", "v1", "--scorer-file", "ask_data_fixture.py")
        rsi("ask-data", "init-validation", "--directory", "validation-history", "--max-exposures", "1")
        code, result = self.execute("init", "--kind", "ask-data", "--agent", "ask_data_fixture:build_agent",
                                    "--benchmark", "development-v1", "--validation-ledger", "validation-history")
        self.assertEqual(code, 0, result)
        uses = self.project / "validation-history/uses.json"
        original = uses.read_bytes()
        code, check = self.execute("check")
        self.assertEqual(code, 0, check)
        self.assertEqual(uses.read_bytes(), original)
        code, first = self.execute("baseline", "--max-calls", "2", helper=True)
        self.assertEqual(code, 0, first)
        self.assertTrue(first["baselineComplete"])
        self.assertEqual(first["askDataValidation"]["plannedExposures"], 1)
        consumed = uses.read_bytes()
        self.assertNotEqual(consumed, original)
        self.execute("init", "--kind", "ask-data")
        code, second = self.execute("baseline", "--max-calls", "2")
        self.assertNotEqual(code, 0)
        self.assertEqual(second.get("usedCalls", 0), 0)
        self.assertEqual(uses.read_bytes(), consumed)
        self.assertEqual((self.project / "calls.txt").read_text().splitlines(), ["called"] * 2)


if __name__ == "__main__":
    unittest.main()
