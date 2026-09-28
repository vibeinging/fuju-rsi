"""真实文件/子进程验证可插拔发布；算术样本只检验协议，不代表业务收益。"""
import importlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fuju_rsi import Example, VerificationSpec
from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.runtime import (export_pack, initialize_runtime, load_pack, load_runtime_agent, run_once, runtime_status,
                                         initialize_prompt, prompt_status, publish_prompt, rollback_prompt)
from fuju_rsi.verification import initialize_authority, verify_candidate
from fuju_rsi.workspace import ExperimentManager, atomic_json


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="recur-runtime-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        sys.path.insert(0, str(self.root))
        self.addCleanup(sys.path.remove, str(self.root))
        for name in ("runtime_demo", "runtime_product"):
            sys.modules.pop(name, None)
            shutil.copyfile(EXAMPLES / (name + ".py"), self.root / (name + ".py"))
            self.addCleanup(sys.modules.pop, name, None)
        importlib.invalidate_caches()
        self.demo = importlib.import_module("runtime_demo")
        self.demo.prepare()
        self.pack_dir, self.workspace = self.root / "pack", self.root / "worker"
        self.pack = export_pack("runtime_demo:build_agent", "development-v1", self.pack_dir,
                                source_files=["runtime_product.py"], max_trials=1, max_calls=5,
                                max_runs=2, total_calls=10)
        initialize_runtime(self.pack_dir, self.workspace)

    def manager(self, authorities=None):
        return ExperimentManager(self.workspace, [load_runtime_agent(self.pack_dir)], verification_authorities=authorities)

    def qualify(self, job):
        registry, key = self.root / "authority", self.root / "authority.key"
        initialize_authority(registry, "fixture", key)
        manager = self.manager({"fixture": str(key)})
        self.addCleanup(manager.close)
        candidate = manager.freeze(job["experimentId"], source_files=["runtime_demo.py", "runtime_product.py"],
                                   environment={"fixture": "arithmetic-protocol", "model": "none"},
                                   policy=AcceptancePolicy(resamples=100))
        cases = [Example("holdout-" + str(n), {"values": [100 + n, 200 + n]}, 300 + 2*n, "test",
                         group_id="group-" + str(n), source_id="source-" + str(n), exposure="unseen") for n in range(30)]
        verifier = VerificationSpec(examples=cases, runner=self.demo.run, evaluator=self.demo.evaluate,
                                    reset=lambda: None, isolation="independent", policy=AcceptancePolicy(resamples=100),
                                    environment={"fixture": "arithmetic-protocol", "model": "none"},
                                    provenance="Synthetic protocol groups; not a real independent business environment")
        receipt = verify_candidate(candidate, verifier, registry=registry, signing_key=key.read_bytes(), max_calls=180)
        self.assertTrue(manager.import_verification(receipt)["adoptable"])
        return manager

    def product(self, target, values):
        # -I 隔离 PYTHONPATH/site 注入；业务程序没有任何 Recur import。
        result = subprocess.run([sys.executable, "-I", "-S", str(self.root / "runtime_product.py"), str(target), json.dumps(values)],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_background_search_is_idempotent_and_budgeted(self):
        job = run_once(self.pack_dir, self.workspace, "first")
        self.assertTrue(job["searchComplete"])
        self.assertEqual(job["usedCalls"], 5)
        self.assertFalse(job["adoptable"])
        self.assertIsNone(job["artifacts"]["verifiedPrompt"])
        self.assertEqual(Path(job["artifacts"]["candidatePrompt"]).read_text(), "sum_all")
        with patch.object(ExperimentManager, "start", side_effect=AssertionError("should not run")):
            self.assertEqual(run_once(self.pack_dir, self.workspace, "first"), job)
        self.assertEqual(len(list((self.workspace / "experiments").glob("*.json"))), 1)
        run_once(self.pack_dir, self.workspace, "second")
        with self.assertRaisesRegex(ValueError, "limit reached"):
            run_once(self.pack_dir, self.workspace, "third")
        self.assertEqual(runtime_status(self.pack_dir, self.workspace)["reservedCalls"], 10)

    def test_holdout_rejected_before_cases_are_read(self):
        directory = self.root / "private"
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({"schemaVersion": 2, "role": "holdout"}))
        with patch("fuju_rsi.runtime.load_bundle", side_effect=AssertionError("must not read cases")):
            with self.assertRaisesRegex(ValueError, "development-only"):
                export_pack("runtime_demo:build_agent", directory, self.root / "bad-pack", source_files=[])
        self.assertFalse((self.root / "bad-pack").exists())

    def test_changed_code_blocks_before_factory_import(self):
        (self.root / "runtime_product.py").write_text("raise RuntimeError('changed')")
        with patch("fuju_rsi.cli.load_agent", side_effect=AssertionError("must not import")):
            with self.assertRaisesRegex(ValueError, "files changed"):
                run_once(self.pack_dir, self.workspace, "changed")

    def test_pack_is_portable_and_does_not_copy_private_files(self):
        self.assertNotIn(str(self.root), json.dumps(self.pack))
        (self.root / "development-v1" / "private-key").write_text("not an input")
        exported = self.root / "only-development"
        export_pack("runtime_demo:build_agent", "development-v1", exported, source_files=["runtime_product.py"])
        self.assertFalse((exported / "development" / "private-key").exists())
        destination = self.root / "moved"
        shutil.copytree(self.pack_dir, destination)
        self.assertEqual(load_pack(destination), self.pack)
        self.assertEqual(sorted(p.name for p in (self.pack_dir / "development").glob("*.json")),
                         ["casebook.json", "examples.json", "manifest.json"])

    def test_missing_ledger_cannot_reset_budget(self):
        (self.workspace / "runtime.json").unlink()
        with self.assertRaises(ValueError):
            run_once(self.pack_dir, self.workspace, "missing")
        with self.assertRaises(FileExistsError):
            initialize_runtime(self.pack_dir, self.workspace)

    def test_interrupted_reservation_is_not_replayed_or_refunded(self):
        state = json.loads((self.workspace / "runtime.json").read_text())
        state["jobs"]["crashed"] = {"status": "running", "reservedCalls": 5}
        atomic_json(self.workspace / "runtime.json", state)
        job = run_once(self.pack_dir, self.workspace, "crashed")
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(runtime_status(self.pack_dir, self.workspace)["reservedCalls"], 5)
        self.assertEqual(list((self.workspace / "experiments").glob("*.json")), [])

    def test_concurrent_worker_refused_without_reserving(self):
        manager = self.manager()
        try:
            with self.assertRaises(RuntimeError):
                run_once(self.pack_dir, self.workspace, "other")
        finally:
            manager.close()
        self.assertEqual(runtime_status(self.pack_dir, self.workspace)["reservedCalls"], 0)

    def test_development_candidate_cannot_publish(self):
        job = run_once(self.pack_dir, self.workspace, "first")
        target = self.root / "product.json"
        original = initialize_prompt(target, agent_id="runtime-sum", prompt="preview")
        manager = self.manager()
        try:
            with self.assertRaisesRegex(ValueError, "independent verification"):
                publish_prompt(manager, job["experimentId"], target, expected_version=original["version"])
        finally:
            manager.close()
        self.assertEqual(prompt_status(target), original)

    def test_verified_publish_product_readback_detachment_and_rollback(self):
        job = run_once(self.pack_dir, self.workspace, "first")
        manager = self.qualify(job)
        target = self.root / "product.json"
        original = initialize_prompt(target, agent_id="runtime-sum", prompt="preview")
        self.assertEqual(self.product(target, [2, 3])["amount"], 2)
        target.chmod(0o640)
        published = publish_prompt(manager, job["experimentId"], target, expected_version=original["version"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(self.product(target, [2, 3]), {"version": published["version"], "amount": 5})
        self.assertEqual(manager.active_prompt("runtime-sum")["prompt"], "preview")
        manager.close()
        shutil.rmtree(self.workspace)
        shutil.rmtree(self.pack_dir)
        (self.root / "runtime_demo.py").unlink()
        self.assertEqual(self.product(target, [11, 13])["amount"], 24)
        with self.assertRaisesRegex(ValueError, "version changed"):
            rollback_prompt(target, expected_version=original["version"])
        rolled = rollback_prompt(target, expected_version=published["version"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(self.product(target, [11, 13]), {"version": rolled["version"], "amount": 11})
        self.assertNotEqual(rolled["version"], original["version"])

    def test_wrong_product_baseline_and_stale_code_block_publish(self):
        job = run_once(self.pack_dir, self.workspace, "first")
        manager = self.qualify(job)
        target = self.root / "wrong.json"
        original = initialize_prompt(target, agent_id="runtime-sum", prompt="different")
        with self.assertRaisesRegex(ValueError, "baseline/version"):
            publish_prompt(manager, job["experimentId"], target, expected_version=original["version"])
        proper = self.root / "proper.json"
        original = initialize_prompt(proper, agent_id="runtime-sum", prompt="preview")
        (self.root / "runtime_product.py").write_text("# changed")
        with self.assertRaisesRegex(ValueError, "independent verification"):
            publish_prompt(manager, job["experimentId"], proper, expected_version=original["version"])
        self.assertEqual(prompt_status(proper), original)

    def test_failed_file_write_keeps_product_snapshot(self):
        job = run_once(self.pack_dir, self.workspace, "first")
        manager = self.qualify(job)
        target = self.root / "product.json"
        original = initialize_prompt(target, agent_id="runtime-sum", prompt="preview")
        with patch("fuju_rsi.workspace.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                publish_prompt(manager, job["experimentId"], target, expected_version=original["version"])
        self.assertEqual(prompt_status(target), original)

    def test_invalid_file_uses_product_fallback(self):
        target = self.root / "broken.json"
        target.write_text("{")
        self.assertEqual(self.product(target, [5, 7]), {"version": "built-in", "amount": 5})

    def test_killed_worker_keeps_reservation_and_cli_logs_private_output(self):
        source = self.root / "runtime_demo.py"
        source.write_text(source.read_text().replace(
            "def run(prompt, value):\n", "def run(prompt, value):\n"
            "    import os, time\n"
            "    print('PRIVATE_RUN_OUTPUT')\n"
            "    if os.environ.get('RUNTIME_TEST_BLOCK'):\n"
            "        Path('entered-runner').write_text('started')\n"
            "        time.sleep(30)\n"))
        # 新文件版本建立新开发包；新解释器避免 Python 的模块缓存复用旧函数。
        env = dict(os.environ, PYTHONPATH=str(EXAMPLES.parent / "src"))
        def cli(*arguments, extra=None):
            return subprocess.run([sys.executable, "-m", "fuju_rsi.__main__", *arguments], cwd=self.root,
                                  env=dict(env, **(extra or {})), text=True, capture_output=True, timeout=10)
        shutil.rmtree(self.root / "development-v1")
        subprocess.run([sys.executable, "runtime_demo.py"], env=env, check=True, capture_output=True)
        exported = cli("runtime", "export", "--agent", "runtime_demo:build_agent", "--benchmark", "development-v1",
                       "--source-file", "runtime_product.py", "--output", "process-pack", "--max-trials", "1",
                       "--max-calls", "5", "--max-runs", "2", "--total-calls", "10")
        self.assertEqual(exported.returncode, 0, exported.stdout)
        initialized = cli("runtime", "init", "--pack", "process-pack", "--workspace", "process-worker")
        self.assertEqual(initialized.returncode, 0, initialized.stdout)
        args = ["runtime", "run", "--pack", "process-pack", "--workspace", "process-worker", "--run-id", "killed"]
        process = subprocess.Popen([sys.executable, "-m", "fuju_rsi.__main__", *args], cwd=self.root,
                                   env=dict(env, RUNTIME_TEST_BLOCK="1"), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "entered-runner").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.root / "entered-runner").exists())
            busy = cli(*args[:-1], "other")
            self.assertEqual(busy.returncode, 1, busy.stdout)
        finally:
            process.kill()
            process.communicate(timeout=5)
        replay = cli(*args)
        self.assertEqual(replay.returncode, 2, replay.stdout)
        self.assertEqual(json.loads(replay.stdout)["status"], "interrupted")
        completed = cli(*args[:-1], "next")
        self.assertEqual(completed.returncode, 0, completed.stdout)
        self.assertNotIn("PRIVATE_RUN_OUTPUT", completed.stdout + completed.stderr)
        job = json.loads(completed.stdout)
        self.assertIn("PRIVATE_RUN_OUTPUT", Path(job["diagnosticLog"]).read_text())
        state = json.loads((self.root / "process-worker" / "runtime.json").read_text())
        self.assertEqual(sum(j["reservedCalls"] for j in state["jobs"].values()), 10)


if __name__ == "__main__":
    unittest.main()
