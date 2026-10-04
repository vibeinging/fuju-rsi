"""SDK 执行入口在安装形态和外部工作目录下保持 Skill 的守卫。"""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import venv


ROOT = Path(__file__).resolve().parents[1]
FACTORY = '''
import json
from pathlib import Path
from fuju_rsi import AgentSpec, Evaluation, Example, Prediction

def runner(prompt, value):
    print("private runtime", value)
    with Path("runner-calls.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\\n")
    return Prediction({"answer": value["number"]})

def evaluate(expected, prediction):
    return Evaluation(float(expected == prediction.output), "existing answer")

def propose(*args):
    raise AssertionError("baseline must not generate candidates")

def build_agent():
    print("private factory configuration")
    return AgentSpec("external", "Existing business", "product original", [
        Example("train", {"number": 2, "marker": "TRAIN_PUBLIC"}, {"answer": 2}, "train"),
        Example("validation", {"number": 3, "marker": "VALIDATION_PRIVATE"}, {"answer": 3}, "validation"),
        Example("test", {"number": 4, "marker": "HOLDOUT_PRIVATE"}, {"answer": 4}, "test"),
    ], runner, propose, evaluate)

def broken_agent():
    print("private factory configuration")
    raise ValueError("private factory credential")
'''


class InstalledSkillRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installation = tempfile.TemporaryDirectory(prefix="fuju-skill-runner-installed-")
        cls.addClassCleanup(cls.installation.cleanup)
        environment = Path(cls.installation.name) / "venv"
        venv.EnvBuilder(with_pip=False).create(environment)
        binaries = environment / ("Scripts" if os.name == "nt" else "bin")
        cls.python = str(binaries / ("python.exe" if os.name == "nt" else "python"))
        location = subprocess.run(
            [cls.python, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            capture_output=True, text=True, check=True, timeout=15,
        )
        # 模拟包安装后的目录布局，不通过 PYTHONPATH 或 Skill 开发仓库查找 SDK。
        # 完整 wheel 安装另外由 verify_python_consumer.py 检查。
        shutil.copytree(ROOT / "src/fuju_rsi", Path(location.stdout.strip()) / "fuju_rsi",
                        ignore=shutil.ignore_patterns("__pycache__"))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fuju-skill-runner-business-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        (self.project / "eval_adapter.py").write_text(FACTORY, encoding="utf-8")
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)

    def execute(self, args, *, baseline_only=True, expected_kind=None):
        command = [self.python, "-I", "-c",
                   "from fuju_rsi.skill_runner import main; import sys; "
                   "sys.exit(main(sys.argv[1:], baseline_only=%r, expected_kind=%r))" %
                   (baseline_only, expected_kind), *args]
        result = subprocess.run(command, cwd=self.project, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr

    def baseline_args(self, factory="build_agent"):
        return ["--agent", "eval_adapter:" + factory, "--workspace", "workspace",
                "--max-calls", "2"]

    def copy_skill(self):
        skill = self.project / "copied-skill/fuju-tune"
        shutil.copytree(ROOT / "skills/fuju-tune", skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        return skill

    def execute_skill(self, skill, args):
        result = subprocess.run([self.python, "-I", str(skill / "scripts/run_experiment.py"), *args],
                                cwd=self.project, env=self.environment, capture_output=True,
                                text=True, timeout=30)
        return result.returncode, result.stdout, result.stderr

    def test_copied_skill_isolated_baseline_does_not_require_scenario_discovery(self):
        skill = self.copy_skill()
        (skill / "scripts/scenarios.py").unlink()
        shutil.rmtree(skill / "scenarios")
        code, output, error = self.execute_skill(skill, self.baseline_args())
        self.assertEqual(code, 0, (output, error))
        summary = json.loads(output)
        self.assertTrue(summary["baselineComplete"])
        self.assertEqual(summary["usedCalls"], 2)
        self.assertFalse(summary["adoptable"])

    def test_installed_baseline_reuses_privacy_and_never_runs_test_or_proposer(self):
        code, output, error = self.execute(self.baseline_args())
        self.assertEqual(code, 0, (output, error))
        self.assertEqual(error, "")
        summary = json.loads(output)
        self.assertEqual(summary["mode"], "baseline")
        self.assertTrue(summary["baselineComplete"])
        self.assertEqual(summary["usedCalls"], 2)
        self.assertEqual(len(summary["trials"]), 1)
        self.assertFalse(summary["adoptable"])
        self.assertIsNone(summary["baselineTest"])
        self.assertIsNone(summary["candidateTest"])
        for private in ("VALIDATION_PRIVATE", "HOLDOUT_PRIVATE", "private factory"):
            self.assertNotIn(private, output)
        calls = (self.project / "runner-calls.jsonl").read_text()
        self.assertEqual(len(calls.splitlines()), 2)
        self.assertNotIn("HOLDOUT_PRIVATE", calls)
        self.assertTrue(Path(summary["artifacts"]["report"]).is_file())

    def test_baseline_rejects_candidates_before_factory_or_workspace(self):
        for extra in (["--candidate-file", "missing.txt"], ["--candidate-kind", "files"]):
            with self.subTest(extra=extra):
                code, output, error = self.execute(self.baseline_args("broken_agent") + extra)
                self.assertEqual(code, 1)
                self.assertIn("baseline only", error)
                self.assertNotIn("private factory", output + error)
                self.assertFalse((self.project / "workspace").exists())

    def test_installed_sdk_does_not_discover_a_local_scenario_module(self):
        (self.project / "scenarios.py").write_text("raise AssertionError('must not import me')")
        code, output, error = self.execute(self.baseline_args() + ["--scenario", "tool-agent"])
        self.assertEqual(code, 1, error)
        self.assertEqual(json.loads(output)["error"], "Unknown Skill scenario.")
        self.assertFalse((self.project / "runner-calls.jsonl").exists())

    def test_factory_exception_is_private_and_keeps_local_diagnostics(self):
        code, output, error = self.execute(self.baseline_args("broken_agent"))
        self.assertEqual(code, 1)
        self.assertEqual(error, "")
        self.assertNotIn("private factory", output)
        summary = json.loads(output)
        self.assertFalse(summary["adoptable"])
        self.assertIn("private factory credential", Path(summary["runtimeLog"]).read_text())

    @unittest.skipUnless(os.name == "posix", "POSIX file mode check")
    def test_runtime_logs_are_created_with_private_file_permissions(self):
        for factory in ("build_agent", "broken_agent"):
            with self.subTest(factory=factory):
                _code, output, _error = self.execute(self.baseline_args(factory))
                path = Path(json.loads(output)["runtimeLog"])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertIn("private factory configuration", path.read_text())

    def test_saved_connection_kind_cannot_change_to_another_execution_contract(self):
        code, output, error = self.execute(self.baseline_args(), expected_kind="ask-data")
        self.assertEqual(code, 1, error)
        self.assertIn("saved connection kind", json.loads(output)["error"])
        self.assertFalse((self.project / "runner-calls.jsonl").exists())

    def test_baseline_rejects_a_workspace_prompt_that_differs_from_the_product(self):
        workspace = self.project / "workspace"
        workspace.mkdir()
        (workspace / "active-prompts.json").write_text(json.dumps({
            "external": {"agentId": "external", "prompt": "old experiment prompt", "experimentId": None}
        }), encoding="utf-8")
        code, output, error = self.execute(self.baseline_args())
        self.assertEqual(code, 1, error)
        self.assertIn("differs from the product prompt", json.loads(output)["error"])
        self.assertFalse((self.project / "runner-calls.jsonl").exists())
        self.assertEqual(list((workspace / "experiments").glob("*.json")), [])

    def prepare_ask_data(self, *, stub_callback=None):
        shutil.copyfile(ROOT / "examples/ask_data_catalog.json", self.project / "catalog.json")
        shutil.copyfile(ROOT / "examples/ask_data_fixture.py", self.project / "ask_data_fixture.py")
        if stub_callback:
            with (self.project / "ask_data_fixture.py").open("a", encoding="utf-8") as stream:
                stream.write("\n\ndef %s(*args):\n" % stub_callback)
                stream.write("    from pathlib import Path\n")
                stream.write("    Path('stub-was-called').write_text('unexpected')\n")
                stream.write("    raise NotImplementedError('private missing adapter')\n")
        commands = [
            ["prepare", "--catalog", "catalog.json", "--role", "development", "--output", "casebook.json"],
            ["freeze", "--casebook", "casebook.json", "--role", "development", "--output", "bundle",
             "--version", "v1", "--scorer-file", "ask_data_fixture.py"],
            ["init-validation", "--directory", "ledger", "--max-exposures", "1"],
        ]
        for arguments in commands:
            result = subprocess.run([self.python, "-I", "-m", "fuju_rsi", "ask-data", *arguments],
                                    cwd=self.project, env=self.environment, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        return ["--agent", "ask_data_fixture:build_agent", "--scenario", "ask-data",
                "--workspace", "workspace", "--benchmark", "bundle",
                "--validation-ledger", "ledger", "--max-calls", "2"]

    def test_partial_custom_adapters_are_rejected_without_calling_business(self):
        for name, exception in (("runner", "NotImplementedError"),
                                ("evaluate", "NotImplementedError('private placeholder')")):
            with self.subTest(callback=name):
                (self.project / "eval_adapter.py").write_text(
                    FACTORY + "\n\ndef %s(*args):\n" % name +
                    "    Path('stub-was-called').write_text('unexpected')\n" +
                    "    raise %s\n" % exception, encoding="utf-8")
                code, output, error = self.execute(self.baseline_args())
                self.assertEqual(code, 1, error)
                self.assertIn("still a placeholder", json.loads(output)["error"])
                self.assertFalse((self.project / "runner-calls.jsonl").exists())
                self.assertFalse((self.project / "stub-was-called").exists())
                self.assertEqual(list((self.project / "workspace/experiments").glob("*.json")), [])

    def test_partial_ask_data_runner_preserves_validation_budget(self):
        self.assert_partial_ask_data_adapter("run")

    def test_partial_ask_data_evaluator_preserves_validation_budget(self):
        self.assert_partial_ask_data_adapter("score")

    def assert_partial_ask_data_adapter(self, callback):
        args = self.prepare_ask_data(stub_callback=callback)
        code, output, error = self.execute(args)
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        self.assertIn("still a placeholder", summary["error"])
        self.assertNotIn("private missing adapter", output)
        self.assertFalse((self.project / "stub-was-called").exists())
        self.assertEqual(list((self.project / "workspace/experiments").glob("*.json")), [])
        uses = json.loads((self.project / "ledger/uses.json").read_text())
        self.assertEqual(uses["revision"], 0)
        self.assertEqual(uses["tokens"], {})

    def test_installed_ask_data_baseline_reserves_one_use_and_rejects_repeat(self):
        args = self.prepare_ask_data()
        code, output, error = self.execute(args)
        self.assertEqual(code, 0, (output, error))
        summary = json.loads(output)
        self.assertTrue(summary["baselineComplete"])
        self.assertEqual(summary["usedCalls"], 2)
        self.assertEqual(summary["askDataValidation"]["plannedExposures"], 1)
        self.assertFalse(summary["adoptable"])
        self.assertNotIn("门店 B", output)
        code, output, error = self.execute(args)
        self.assertEqual(code, 1, (output, error))
        repeated = json.loads(output)
        self.assertEqual(repeated["usedCalls"], 0)
        self.assertIn("上限", repeated["message"])

    def test_copied_skill_isolated_ask_data_reads_its_own_scenario_manifest(self):
        skill = self.copy_skill()
        args = self.prepare_ask_data()
        code, output, error = self.execute_skill(skill, args)
        self.assertEqual(code, 0, (output, error))
        self.assertTrue(json.loads(output)["baselineComplete"])
        manifest_path = skill / "scenarios/ask-data/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest.update(status="draft", automatedTargets=[])
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        code, output, error = self.execute_skill(skill, args)
        self.assertEqual(code, 1, (output, error))
        summary = json.loads(output)
        self.assertIn("no executable integration", summary["error"])
        uses = json.loads((self.project / "ledger/uses.json").read_text())
        self.assertEqual(uses["revision"], 1)

    def test_holdout_manifest_is_rejected_before_casebook_or_factory(self):
        args = self.prepare_ask_data()
        script = ("from fuju_rsi.benchmark import read_json,digest,write_new; "
                  "from pathlib import Path; import json; "
                  "p=Path('bundle/manifest.json'); m=read_json(p); "
                  "m['role']='holdout'; m['digest']=digest({k:v for k,v in m.items() if k!='digest'}); "
                  "p.write_text(json.dumps(m)); Path('bundle/casebook.json').unlink()")
        subprocess.run([self.python, "-I", "-c", script], cwd=self.project,
                       env=self.environment, check=True, timeout=15)
        args[1] = "eval_adapter:broken_agent"
        code, output, error = self.execute(args)
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        log = Path(summary["runtimeLog"]).read_text()
        self.assertIn("Holdout bundle requires", log)
        self.assertNotIn("private factory", log)

    def test_frozen_scorer_identity_is_checked_before_runner(self):
        args = self.prepare_ask_data()
        (self.project / "wrong_scorer.py").write_text(
            "from fuju_rsi import Evaluation\n"
            "def score(expected,prediction): return Evaluation(1.0,'wrong scorer')\n", encoding="utf-8")
        (self.project / "wrong_factory.py").write_text(
            "from ask_data_fixture import build_agent as original\n"
            "from wrong_scorer import score\n"
            "def build_agent():\n"
            "    agent=original(); agent.evaluator=score; return agent\n", encoding="utf-8")
        args[1] = "wrong_factory:build_agent"
        code, output, error = self.execute(args)
        self.assertEqual(code, 1, error)
        summary = json.loads(output)
        self.assertIn("Scorer file must define", Path(summary["runtimeLog"]).read_text())
        uses = json.loads((self.project / "ledger/uses.json").read_text())
        self.assertEqual(uses["revision"], 0)


if __name__ == "__main__":
    unittest.main()
