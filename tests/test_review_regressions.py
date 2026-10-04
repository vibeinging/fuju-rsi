"""审查反例：使用合成值验证评分、来源隔离和执行终态，不调用模型。"""
from copy import deepcopy
from dataclasses import replace
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.ask_data import AskDataError, build_casebook, evaluate
from fuju_rsi.ask_data_validation import ValidationLedger
from fuju_rsi.benchmark import BenchmarkError, freeze_book
from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction, optimize
from fuju_rsi.holdout_registry import HoldoutRegistry
from fuju_rsi.runtime import export_pack, initialize_runtime, run_once, runtime_status
from fuju_rsi.verification import VerificationSpec, freeze_candidate, verify_candidate
from fuju_rsi.workspace import ExperimentManager


def reference(value, ordered=False, tolerance=0):
    return {"behavior": "answer", "requiresDelivery": False,
            "result": {"columns": ["x"], "rows": [[value]], "ordered": ordered,
                       "absoluteTolerance": tolerance}}


def observed(value):
    return {"status": "succeeded", "behavior": "answer",
            "result": {"columns": ["x"], "rows": [[value]], "complete": True}}


class ScoringRegressions(unittest.TestCase):
    def test_large_integers_are_exact_in_unordered_rows(self):
        value = 123456789012345678901234567890
        self.assertEqual(evaluate(reference(value), Prediction(observed(value + 1))).score, 0)

    def test_zero_sign_does_not_change_equality(self):
        self.assertEqual(evaluate(reference(0), Prediction(observed(-0.0))).score, 1)

    def test_tolerance_does_not_round_away_excess(self):
        limit = 100000000000000000000000000000
        self.assertEqual(evaluate(reference(limit + 1, ordered=True, tolerance=limit),
                                  Prediction(observed(0))).score, 0)

    def test_json_objects_cannot_impersonate_numbers(self):
        for left, right in ((1, {"number": "1"}), ({"number": "1"}, 1)):
            with self.subTest(left=left):
                self.assertEqual(evaluate(reference(left), Prediction(observed(right))).score, 0)

    def test_later_infrastructure_failure_is_not_hidden_by_wrong_answer(self):
        expected = {"turns": [reference(1), reference(2)]}
        actual = {"turns": [observed(0), {"status": "failed", "behavior": "answer"}]}
        with self.assertRaises(AskDataError):
            evaluate(expected, Prediction(actual))

    def test_freeze_rejects_known_parent_crossing_splits(self):
        book = {"schemaVersion": 2, "role": "development", "id": "review-lineage",
                "name": "Synthetic lineage", "scoring": "ask-data-structured-v1",
                "environment": {"dataSnapshot": "v1", "contextRevision": "v1"},
                "cases": []}
        for identifier, split, parents in (("parent", "train", []),
                                           ("variant", "validation", ["parent"])):
            book["cases"].append({"id": identifier, "input": identifier + " question",
                "expected": reference(1), "expectedStatus": "verified", "evidence": "synthetic oracle",
                "group": identifier, "split": split, "tags": ["arithmetic"],
                "source": {"kind": "synthetic", "ref": identifier},
                "origin": "variant" if parents else "original", "parentCaseIds": parents})
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(BenchmarkError):
            freeze_book(book, output=Path(folder) / "bundle", version="v1",
                        scorer_file=__file__)


class Harness:
    def __init__(self, name):
        self.name, self.exit, self.verifying, self.first = name, False, False, True
        self.entered = self.release = self.other_entered = self.other_done = None

    def run(self, prompt, value):
        if self.exit:
            raise SystemExit(7)
        if self.verifying and self.first and self.entered:
            self.first = False
            print(self.name + "_ENTER")
            self.entered.set()
            if self.release is not None:
                if not self.release.wait(5):
                    raise TimeoutError("test schedule")
            if self.other_done is not None:
                if not self.other_done.wait(5):
                    raise TimeoutError("test schedule")
            print(self.name + "_PRIVATE_AFTER_WAIT")
        return Prediction(value * (2 if prompt == "double" else 1))

    def score(self, expected, prediction):
        return Evaluation(float(expected == prediction.output))

    def propose(self, _prompt, _feedback, _index):
        return "double"

    def reset(self):
        pass

    def agent(self):
        return AgentSpec(self.name, self.name, "identity", [
            Example("train", 1, 2, "train", group_id="train", source_id="train", exposure="development"),
            Example("validation", 2, 4, "validation", group_id="validation", source_id="validation", exposure="development")
        ], self.run, self.propose, self.score)

    def candidate(self):
        agent = self.agent()
        result = optimize(agent, max_trials=1, max_calls=5)
        result["id"] = uuid.uuid4().hex
        return freeze_candidate(agent, result, source_files=[__file__], environment={"fixture": "review"},
                                policy=AcceptancePolicy(repeats=1), root=ROOT)

    def verifier(self, offset):
        return VerificationSpec([
            Example(str(offset + n), offset + n, 2 * (offset + n), "test",
                    group_id=str(offset + n), source_id=str(offset + n), exposure="unseen")
            for n in range(30)
        ], self.run, self.score, self.reset, AcceptancePolicy(repeats=1), "independent",
            "Synthetic protocol cases only", {"fixture": "review"})


class ExecutionRegressions(unittest.TestCase):
    def test_exited_worker_has_terminal_state_and_can_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            harness = Harness("exit-worker")
            harness.exit = True
            manager = ExperimentManager(folder, [harness.agent()], project_root=ROOT, telemetry_mode="log")
            try:
                record = manager.start(agent_id=harness.name, max_trials=1, max_calls=5)
                result = manager.wait(record["id"], timeout=5)
                self.assertIn(result["status"], ("failed", "cancelled", "interrupted"))
                self.assertFalse(result["adoptable"])
                second = manager.start(agent_id=harness.name, max_trials=1, max_calls=5)
                self.assertNotEqual(second["id"], record["id"])
                manager.wait(second["id"], timeout=5)
            finally:
                manager.close()

    def test_exited_verifier_consumes_batch_and_ends_running(self):
        with tempfile.TemporaryDirectory() as folder:
            HoldoutRegistry.initialize(folder, "review")
            harness = Harness("exit-verifier")
            candidate = harness.candidate()
            harness.exit = True
            receipt = verify_candidate(candidate, harness.verifier(3000), registry=folder,
                signing_key=b"synthetic-review-key-01234567890123456789", root=ROOT)
            self.assertFalse(receipt["body"]["adoptable"])
            self.assertEqual(receipt["body"]["status"], "failed")
            self.assertNotEqual(HoldoutRegistry(folder).get(candidate["id"])["status"], "running")

    def test_concurrent_verifiers_keep_private_output_and_restore_streams(self):
        with tempfile.TemporaryDirectory() as folder:
            HoldoutRegistry.initialize(folder, "review")
            a, b = Harness("A"), Harness("B")
            ca, cb = a.candidate(), b.candidate()
            a.verifying = b.verifying = True
            a.entered, a.release, b.entered = threading.Event(), threading.Event(), threading.Event()
            a_done = threading.Event()
            b.other_done = a_done
            receipts, errors = [], []
            original_out, original_err = sys.stdout, sys.stderr
            public = io.StringIO()
            sys.stdout = public
            def worker(harness, candidate, offset):
                try:
                    receipts.append(verify_candidate(candidate, harness.verifier(offset), registry=folder,
                        signing_key=b"synthetic-review-key-01234567890123456789", root=ROOT))
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    if harness is a:
                        a_done.set()
            ta = threading.Thread(target=worker, args=(a, ca, 1000))
            tb = threading.Thread(target=worker, args=(b, cb, 2000))
            restored, closed = False, False
            try:
                ta.start()
                self.assertTrue(a.entered.wait(5))
                tb.start()
                b.entered.wait(.1)
                a.release.set()
                ta.join(5)
                tb.join(5)
                restored, closed = sys.stdout is public, sys.stdout.closed
            finally:
                a.release.set()
                sys.stdout, sys.stderr = original_out, original_err
            self.assertFalse(ta.is_alive() or tb.is_alive())
            self.assertFalse(errors)
            self.assertEqual(len(receipts), 2)
            self.assertTrue(restored)
            self.assertFalse(closed)
            self.assertNotIn("PRIVATE", public.getvalue())
            for harness, candidate in ((a, ca), (b, cb)):
                log = (Path(folder) / "private-logs" / (candidate["id"] + ".log")).read_text()
                self.assertIn(harness.name + "_PRIVATE_AFTER_WAIT", log)
                self.assertNotIn(("B" if harness is a else "A") + "_", log)


class AskDataRuntimeRegression(unittest.TestCase):
    def test_shared_validation_ledger_survives_runtime_runs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            previous = Path.cwd()
            os.chdir(root)
            sys.path.insert(0, str(root))
            try:
                shutil.copyfile(ROOT / "examples/ask_data_fixture.py", root / "review_ask_data_fixture.py")
                importlib.invalidate_caches()
                catalog = json.loads((ROOT / "examples/ask_data_catalog.json").read_text())
                book = build_casebook(catalog)
                bundle = root / "development"
                freeze_book(book, output=bundle, version="v1", scorer_file="review_ask_data_fixture.py")
                ledger = ValidationLedger.initialize(root / "validation", max_exposures=2)
                wrong_bundle = str(root / "unrelated-bundle")
                with patch.dict(os.environ, {"FUJU_RSI_BENCHMARK": wrong_bundle}):
                    pack = export_pack("review_ask_data_fixture:build_agent", bundle, root / "pack",
                                       source_files=[], max_trials=1, max_calls=5)
                    self.assertEqual(os.environ["FUJU_RSI_BENCHMARK"], wrong_bundle)
                workspace = root / "worker"
                with self.assertRaises(ValueError):
                    initialize_runtime(root / "pack", workspace)
                self.assertFalse(workspace.exists())
                initialize_runtime(root / "pack", workspace, validation_ledger=ledger.directory)
                first = run_once(root / "pack", workspace, "first")
                self.assertTrue(first["searchComplete"])
                self.assertFalse(first["adoptable"])
                self.assertEqual(first["usedCalls"], 5)
                self.assertEqual(run_once(root / "pack", workspace, "first"), first)
                second = run_once(root / "pack", workspace, "second")
                self.assertEqual(second["status"], "failed")
                self.assertFalse(second["searchComplete"])
                self.assertEqual(runtime_status(root / "pack", workspace)["reservedCalls"], 10)
                # 绑定后的账本身份不能被换成一个新账本，以此清空验证次数。
                replacement = ValidationLedger.initialize(root / "replacement", max_exposures=2)
                state_file = workspace / "runtime.json"
                state = json.loads(state_file.read_text())
                state["validationLedger"]["directory"] = str(replacement.directory)
                state_file.write_text(json.dumps(state))
                with self.assertRaises(ValueError):
                    run_once(root / "pack", workspace, "third")
                self.assertEqual(runtime_status(root / "pack", workspace)["reservedCalls"], 10)
            finally:
                os.chdir(previous)
                sys.path.remove(str(root))
                sys.modules.pop("review_ask_data_fixture", None)


if __name__ == "__main__":
    unittest.main()
