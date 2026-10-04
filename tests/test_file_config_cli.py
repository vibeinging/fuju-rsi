"""配置候选走真实子进程；合成 SQLite 数据只证明开发比较协议。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FileConfigCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fuju-file-config-cli-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copyfile(ROOT / "examples" / "file_config_fixture.py", self.root / "file_config_fixture.py")
        self.project = self.root / "file_config_project"
        shutil.copytree(ROOT / "examples" / "file_config_project", self.project)
        self.run_log = self.root / "run-paths.jsonl"
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                                PYTHONDONTWRITEBYTECODE="1", FUJU_FILE_FIXTURE_NOISY="1",
                                FUJU_FILE_FIXTURE_RUN_LOG=str(self.run_log))
        self.candidate = self.root / "candidate.json"

    def _run(self, arguments, *, expected_code=0):
        result = subprocess.run([sys.executable, "-m", "fuju_rsi", *arguments],
                                cwd=self.root, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected_code, result.stderr or result.stdout)
        self.assertEqual(result.stderr, "", result.stderr)
        self.assertNotIn("PRIVATE_INPUT_FILE_CONFIG_7f9641", result.stdout)
        self.assertNotIn("PRIVATE_EXPECTED_FILE_CONFIG_42a1dc", result.stdout)
        self.assertNotIn("private business debug", result.stdout)
        self.assertNotIn("private evaluation debug", result.stdout)
        self.assertNotIn("门店 A 的销售额是多少", result.stdout)
        self.assertNotIn("金额为 100 元", result.stdout)
        self.assertNotIn("金额为 80 元", result.stdout)
        return json.loads(result.stdout)

    def _prepare_candidate(self):
        result = subprocess.run(
            [sys.executable, "file_config_fixture.py", "--candidate-output", str(self.candidate)],
            cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.candidate.read_text(encoding="utf-8"))

    def _compare(self, workspace, *, max_calls=5, expected_code=0, extra=()):
        return self._run([
            "compare-files", "--agent", "file_config_fixture:build_agent",
            "--workspace", str(workspace), "--candidate-file", str(self.candidate),
            "--max-calls", str(max_calls), *extra,
        ], expected_code=expected_code)

    def _snapshot(self):
        return {path.name: path.read_bytes() for path in self.project.iterdir()}

    def _business(self, directory):
        # 隔离导入路径和 site-packages；此入口在没有 RSI 的条件下只读取普通文件。
        script = ("import importlib.util,json; "
                  "assert importlib.util.find_spec('fuju_rsi') is None; "
                  "assert importlib.util.find_spec('fuju_trace') is None; "
                  "spec=importlib.util.spec_from_file_location('product',%r); "
                  "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m); "
                  "print(json.dumps(m.run_business(%r,{'question':'门店 A 的销售额是多少？'})))" %
                  (str(self.root / "file_config_fixture.py"), str(directory)))
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script],
                                cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compare_files_command_is_available(self):
        result = subprocess.run(
            [sys.executable, "-m", "fuju_rsi", "compare-files", "--help"],
            cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--candidate-file", result.stdout)

    def test_candidate_reads_both_configs_and_reports_without_prompt_artifacts(self):
        original = self._snapshot()
        candidate = self._prepare_candidate()
        self.assertEqual(json.loads(candidate["files"]["rules.json"]),
                         json.loads(original["rules.json"]))
        summary = self._compare(self.root / "workspace", extra=("--name", "Known alias regression"))
        self.assertEqual(summary["status"], "completed")
        self.assertTrue(summary["searchComplete"])
        self.assertFalse(summary["adoptable"])
        self.assertEqual(summary["usedCalls"], 5)
        self.assertEqual(self._snapshot(), original)
        public = Path(summary["artifacts"]["result"]).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_INPUT_FILE_CONFIG_7f9641", public)
        self.assertNotIn("PRIVATE_EXPECTED_FILE_CONFIG_42a1dc", public)
        self.assertNotIn("private business debug", public)
        self.assertNotIn("private evaluation debug", public)
        self.assertNotIn("金额为 100 元", public)
        self.assertNotIn("金额为 80 元", public)
        report = Path(summary["artifacts"]["report"])
        self.assertFalse(list(report.parent.rglob("*prompt*")))
        self.assertFalse(list(report.parent.rglob("verified*")))
        self.assertTrue((report.parent / "changes.diff").is_file())
        self.assertTrue((report.parent / "candidate.json").is_file())
        logs = [json.loads(line) for line in self.run_log.read_text().splitlines()]
        self.assertEqual(len(logs), 4)
        self.assertTrue(all(set(row["loadedHashes"]) == {"dictionary.json", "rules.json"} for row in logs))
        self.assertEqual(logs[0]["loadedHashes"],
                         {name: hashlib.sha256(raw).hexdigest() for name, raw in original.items()})
        self.assertTrue(all(not Path(row["root"]).exists() for row in logs))

    def test_two_rounds_clean_up_and_delivered_files_work_without_rsi(self):
        original = self._snapshot()
        self._prepare_candidate()
        first = self._compare(self.root / "first")
        second = self._compare(self.root / "second")
        self.assertFalse(first["adoptable"])
        self.assertFalse(second["adoptable"])
        self.assertEqual(self._snapshot(), original)
        logs = [json.loads(line) for line in self.run_log.read_text().splitlines()]
        self.assertEqual(len(logs), 8)
        self.assertTrue(all(not Path(row["root"]).exists() for row in logs))
        artifacts = Path(first["artifacts"]["report"]).parent
        delivered = self.root / "ordinary-product-config"
        shutil.copytree(artifacts / "candidate", delivered)
        self.assertEqual(self._business(self.project)["result"]["rows"], [[2]])
        self.assertEqual(self._business(delivered)["result"]["rows"], [[100]])
        self.assertEqual((delivered / "rules.json").read_bytes(), original["rules.json"])
        shutil.rmtree(self.root / "first")
        shutil.rmtree(self.root / "second")
        self.candidate.unlink()
        # 两个实验工作区和候选包移除后，普通业务入口继续读取交付的配置。
        self.assertEqual(self._business(delivered)["result"]["rows"], [[100]])

    def test_exhausted_budget_never_claims_complete_or_adoptable(self):
        original = self._snapshot()
        self._prepare_candidate()
        summary = self._compare(self.root / "incomplete", max_calls=4, expected_code=2)
        self.assertFalse(summary["searchComplete"])
        self.assertFalse(summary["adoptable"])
        self.assertEqual(summary["usedCalls"], 4)
        self.assertEqual(self._snapshot(), original)
        self.assertTrue(all(not Path(json.loads(line)["root"]).exists()
                            for line in self.run_log.read_text().splitlines()))

    def test_exported_candidate_is_reusable_but_restore_package_cannot_be_replaced(self):
        self._prepare_candidate()
        first = self._compare(self.root / 'delivery-first')
        self.candidate = Path(first['artifacts']['candidate'])
        second = self._compare(self.root / 'delivery-second')
        self.assertTrue(second['searchComplete'])
        self.assertEqual(second['trials'][1]['validation']['score'], 1)
        package = json.loads(self.candidate.read_text(encoding='utf-8'))
        restore = package['baseline']
        # 即使重算摘要，恢复包也不能被另一份合法候选替换。
        restore['files']['dictionary.json'] = package['candidate']['files']['dictionary.json']
        payload = {key: value for key, value in restore.items() if key != 'digest'}
        restore['digest'] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                    separators=(',', ':')).encode('utf-8')).hexdigest()
        changed = self.root / 'tampered-delivery.json'
        changed.write_text(json.dumps(package, ensure_ascii=False), encoding='utf-8')
        self.candidate = changed
        before = len(self.run_log.read_text().splitlines())
        rejected = self._compare(self.root / 'delivery-tampered', expected_code=1)
        self.assertFalse(rejected['searchComplete'])
        self.assertEqual(len(self.run_log.read_text().splitlines()), before)

    def test_ask_data_bundle_keeps_shared_validation_exposure_guard(self):
        shutil.copyfile(ROOT / "examples" / "ask_data_catalog.json", self.root / "catalog.json")
        self._run(["ask-data", "prepare", "--catalog", "catalog.json", "--role", "development",
                   "--output", "casebook.json"])
        bundle, ledger = self.root / "development", self.root / "validation-history"
        self._run(["ask-data", "freeze", "--casebook", "casebook.json", "--role", "development",
                   "--output", str(bundle), "--version", "v1", "--scorer-file", "file_config_fixture.py"])
        self._run(["ask-data", "init-validation", "--directory", str(ledger), "--max-exposures", "2"])
        self._prepare_candidate()
        extra = ("--ask-data-bundle", str(bundle), "--validation-ledger", str(ledger))
        first = self._compare(self.root / "first-ask-data", extra=extra)
        self.assertTrue(first["searchComplete"])
        self.assertFalse(first["adoptable"])
        public = json.loads(Path(first["artifacts"]["result"]).read_text(encoding="utf-8"))
        self.assertEqual(public["askDataValidation"]["plannedExposures"], 2)
        count = len(self.run_log.read_text().splitlines())
        second = self._compare(self.root / "second-ask-data", extra=extra, expected_code=1)
        self.assertFalse(second["adoptable"])
        self.assertEqual(len(self.run_log.read_text().splitlines()), count)

    def test_copied_skill_runs_file_candidate_and_refuses_reused_validation(self):
        skill = self.root / "copied-skill"
        shutil.copytree(ROOT / "skills" / "fuju-tune", skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(ROOT / "examples" / "ask_data_catalog.json", self.root / "catalog.json")
        self._run(["ask-data", "prepare", "--catalog", "catalog.json", "--role", "development",
                   "--output", "casebook.json"])
        bundle, ledger = self.root / "development", self.root / "validation-history"
        self._run(["ask-data", "freeze", "--casebook", "casebook.json", "--role", "development",
                   "--output", str(bundle), "--version", "v1", "--scorer-file", "file_config_fixture.py"])
        self._run(["ask-data", "init-validation", "--directory", str(ledger), "--max-exposures", "2"])
        self._prepare_candidate()

        def run_skill(workspace):
            result = subprocess.run([
                sys.executable, str(skill / "scripts" / "run_experiment.py"),
                "--scenario", "ask-data", "--candidate-kind", "files",
                "--agent", "file_config_fixture:build_agent", "--workspace", str(workspace),
                "--benchmark", str(bundle), "--validation-ledger", str(ledger),
                "--candidate-file", str(self.candidate), "--max-calls", "5",
            ], cwd=self.root, env=self.environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.stderr, "", result.stderr)
            # Skill 的训练反馈允许看到训练输入；只允许训练题，不能返回验证输入/答案。
            self.assertNotIn("门店 B", result.stdout)
            return result.returncode, json.loads(result.stdout)

        code, first = run_skill(self.root / "first-skill")
        self.assertEqual(code, 0, first)
        self.assertTrue(first["searchComplete"])
        self.assertFalse(first["adoptable"])
        self.assertEqual(first["askDataValidation"]["plannedExposures"], 2)
        self.assertEqual(first["trials"][1]["validation"]["passed"], 1)
        count = len(self.run_log.read_text().splitlines())
        code, second = run_skill(self.root / "second-skill")
        self.assertEqual(code, 1, second)
        self.assertFalse(second["adoptable"])
        self.assertEqual(len(self.run_log.read_text().splitlines()), count)


if __name__ == "__main__":
    unittest.main()
