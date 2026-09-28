"""复制 Skill 后用离线问数案例检查冻结包和验证次数接缝。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillAskDataTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fuju-rsi-skill-ask-data-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "python-business"
        self.project.mkdir()
        self.skill = Path(temporary.name) / "installed-skill" / "fuju-tune"
        shutil.copytree(ROOT / "skills" / "fuju-tune", self.skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.helper = self.skill / "scripts" / "run_experiment.py"
        shutil.copy(ROOT / "examples" / "ask_data_catalog.json", self.project / "catalog.json")
        shutil.copy(ROOT / "examples" / "ask_data_fixture.py", self.project / "ask_data_fixture.py")
        self.bundle = self.project / "benchmarks" / "ask-data-v1"
        self.ledger = self.project / "validation-history"
        self.workspace = self.project / "experiments"
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                                PYTHONDONTWRITEBYTECODE="1")
        self._rsi("ask-data", "prepare", "--catalog", "catalog.json", "--role", "development",
                  "--output", "casebook.json")
        self._rsi("ask-data", "freeze", "--casebook", "casebook.json", "--role", "development",
                  "--output", str(self.bundle), "--version", "v1",
                  "--scorer-file", "ask_data_fixture.py")
        self._rsi("ask-data", "init-validation", "--directory", str(self.ledger),
                  "--max-exposures", "2")
        (self.project / "candidate.txt").write_text("sum-orders", encoding="utf-8")

    def _run(self, command):
        result = subprocess.run(command, cwd=self.project, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stderr, "", result.stderr)
        self.assertTrue(result.stdout.strip())
        return result.returncode, json.loads(result.stdout)

    def _rsi(self, *args):
        code, response = self._run([sys.executable, "-m", "fuju_rsi", *args])
        self.assertEqual(code, 0, response)
        return response

    def _skill(self, *, with_ledger=True):
        command = [sys.executable, str(self.helper), "--agent", "ask_data_fixture:build_agent",
                   "--workspace", str(self.workspace), "--benchmark", str(self.bundle),
                   "--candidate-file", "candidate.txt", "--max-calls", "5"]
        if with_ledger:
            command += ["--validation-ledger", str(self.ledger)]
        return self._run(command)

    def test_ask_data_requires_shared_ledger_before_runner(self):
        code, result = self._skill(with_ledger=False)
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertIn("--validation-ledger", result["error"])
        self.assertEqual(list((self.workspace / "experiments").glob("*.json")), [])

    def test_cli_uses_bundle_for_factory_and_report(self):
        code, result = self._run([
            sys.executable, "-m", "fuju_rsi", "optimize",
            "--agent", "ask_data_fixture:build_agent",
            "--workspace", str(self.workspace),
            "--ask-data-bundle", str(self.bundle),
            "--validation-ledger", str(self.ledger),
            "--max-trials", "1", "--max-calls", "5", "--telemetry", "log",
        ])
        self.assertEqual(code, 0, result)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 5)
        code, exported = self._run([
            sys.executable, "-m", "fuju_rsi", "report",
            "--agent", "ask_data_fixture:build_agent",
            "--workspace", str(self.workspace),
            "--ask-data-bundle", str(self.bundle),
            "--experiment", result["id"],
        ])
        self.assertEqual(code, 0, exported)
        self.assertTrue(Path(exported["artifacts"]["report"]).is_file())

    def test_skill_uses_frozen_bundle_and_spends_validation_budget_once(self):
        code, result = self._skill()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 5)
        self.assertEqual(result["askDataValidation"]["plannedExposures"], 2)
        self.assertEqual(result["askDataValidation"]["maxUsedExposures"], 2)
        self.assertEqual(result["trials"][1]["validation"]["passed"], 1)
        self.assertTrue(Path(result["artifacts"]["report"]).is_file())
        self.assertNotIn("门店 B", json.dumps(result, ensure_ascii=False))

        code, repeated = self._skill()
        self.assertEqual(code, 1, repeated)
        self.assertEqual(repeated["status"], "failed")
        self.assertFalse(repeated["adoptable"])
        self.assertEqual(repeated["usedCalls"], 0)
        self.assertIn("上限", repeated["message"])


if __name__ == "__main__":
    unittest.main()
