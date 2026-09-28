"""防止直接 SDK 绕过去重、回调身份和并发执行归属检查。

使用合成算术样本验证协议，不代表真实业务样本独立或实际权限隔离。
验收结果来自真实 runner、evaluator、比较器和文件账本，不伪造签名结论。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import re
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

SDK_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SDK_ROOT / "src"))

from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.benchmark import near_key
from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction, optimize
from fuju_rsi.holdout_registry import HoldoutRegistry, RegistryError
from fuju_rsi.verification import VerificationSpec, freeze_candidate, validate_receipt, verify_candidate


class ArithmeticHarness:
    def __init__(self):
        self.calls = []
        self.scores = 0
        self.resets = 0

    def runner(self, prompt, value):
        self.calls.append((prompt, value))
        number = value["number"] if isinstance(value, dict) else int(re.search(r"\d+", value).group())
        return Prediction(number * (2 if prompt == "double" else 1))

    def other_runner(self, prompt, value):
        # 故意在同一源文件中放另一个入口；文件摘要一致不能代替函数身份一致。
        return self.runner(prompt, value)

    def evaluator(self, expected, prediction):
        self.scores += 1
        return Evaluation(float(expected == prediction.output), "arithmetic equality")

    def other_evaluator(self, expected, prediction):
        return self.evaluator(expected, prediction)

    def proposer(self, prompt, feedback, index):
        return "double"

    def reset(self):
        self.resets += 1


class VerificationBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yitrace-boundary-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.registry_path = Path(self.temporary.name) / "registry"
        HoldoutRegistry.initialize(self.registry_path, "synthetic-boundary-authority")
        self.registry = HoldoutRegistry(self.registry_path)
        self.key = b"synthetic-boundary-test-key-only-32"
        self.policy = AcceptancePolicy(resamples=100)
        self.environment = {"fixture": "synthetic-arithmetic-boundaries", "runtime": "stdlib", "model": "none"}
        self.harness = ArithmeticHarness()
        self.agent = AgentSpec("arithmetic-boundaries", "Arithmetic boundary fixture", "identity", [
            Example("train", {"number": 1}, 2, "train", group_id="dev-train", source_id="dev-source-train", exposure="development"),
            Example("validation", {"number": 2}, 4, "validation", group_id="dev-validation", source_id="dev-source-validation", exposure="development"),
        ], self.harness.runner, self.harness.proposer, self.harness.evaluator)
        self.search = optimize(self.agent, max_trials=1, max_calls=5)
        self.search["id"] = uuid.uuid4().hex
        self.assertTrue(self.search["searchComplete"])
        self.assertEqual(self.search["selectedTrialId"], "trial-1")
        self.candidate = self.freeze(self.agent)
        self.harness.calls.clear()
        self.harness.scores = 0

    def freeze(self, agent):
        return freeze_candidate(agent, self.search, source_files=[str(Path(__file__).resolve())],
                                environment=self.environment, policy=self.policy, root=SDK_ROOT)

    def specification(self, **overrides):
        values = dict(
            examples=[Example("case-%s" % i, {"number": 1000 + i}, 2 * (1000 + i), "test",
                              group_id="group-%s" % i, source_id="source-%s" % i, exposure="unseen")
                      for i in range(30)],
            runner=self.harness.runner, evaluator=self.harness.evaluator, reset=self.harness.reset,
            policy=self.policy, isolation="independent", environment=self.environment,
            provenance="Synthetic independent-protocol fixture; not business or OS isolation evidence",
        )
        values.update(overrides)
        return VerificationSpec(**values)

    def verify(self, spec):
        return verify_candidate(self.candidate, spec, registry=self.registry,
                                signing_key=self.key, root=SDK_ROOT, max_calls=180)

    def assert_not_started(self):
        self.assertEqual(self.harness.calls, [])
        self.assertEqual(self.harness.scores, 0)
        self.assertEqual(self.harness.resets, 0)
        with self.assertRaises(RegistryError):
            self.registry.get(self.candidate["id"])

    def test_near_identical_inputs_cannot_inflate_independent_groups_via_sdk(self):
        rows = [Example("near-%s" % i, "number 1000" + "!" * i, 2000, "test",
                        group_id="near-group-%s" % i, source_id="near-source-%s" % i, exposure="unseen")
                for i in range(30)]
        self.assertEqual(len({near_key(row.input) for row in rows}), 1)
        with self.assertRaises(ValueError):
            self.verify(self.specification(examples=rows))
        self.assert_not_started()

    def test_verifier_cannot_substitute_another_callback_from_same_source_file(self):
        for field, callback in (("runner", self.harness.other_runner), ("evaluator", self.harness.other_evaluator)):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.verify(self.specification(**{field: callback}))
                self.assert_not_started()

    def test_verifier_callback_roles_cannot_be_swapped(self):
        # 集合相等不足够；两个回调的文件与身份都相同，但 runner/evaluator 的角色颠倒。
        with self.assertRaises(ValueError):
            self.verify(self.specification(runner=self.harness.evaluator, evaluator=self.harness.runner))
        self.assert_not_started()

    def test_freeze_cannot_substitute_callbacks_after_search_without_changing_file(self):
        for field, callback in (("runner", self.harness.other_runner), ("evaluator", self.harness.other_evaluator)):
            with self.subTest(field=field):
                changed = replace(self.agent, **{field: callback})
                with self.assertRaises(ValueError):
                    self.freeze(changed)
                self.assert_not_started()

    def test_same_job_start_loser_does_not_interrupt_the_winning_verifier(self):
        # 两个调用均已完成真实 reserve，再竞争真实 start。只约束时序，不替换状态写入。
        both_reserved = threading.Barrier(2)
        losing_call_finished = threading.Event()
        original_start = HoldoutRegistry.start
        receipts, failures = [], []

        def coordinated_start(registry, job_id):
            both_reserved.wait(timeout=5)
            result = original_start(registry, job_id)
            # 赢方尚未调用 runner，等另一调用完成拒绝路径，捕获其错误地 interrupt 的行为。
            if not losing_call_finished.wait(timeout=5):
                raise AssertionError("Concurrent loser did not finish")
            return result

        def worker():
            try:
                receipts.append(self.verify(self.specification()))
            except BaseException as error:
                failures.append(error)
            finally:
                losing_call_finished.set()

        workers = [threading.Thread(target=worker, daemon=True) for _ in range(2)]
        with patch.object(HoldoutRegistry, "start", coordinated_start):
            for thread in workers:
                thread.start()
            for thread in workers:
                thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in workers))
        self.assertEqual(len(failures), 1, [type(error).__name__ for error in failures])
        self.assertIsInstance(failures[0], RegistryError)
        self.assertEqual(len(receipts), 1)
        body = validate_receipt(receipts[0], self.key)
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["adoptable"])
        self.assertEqual(body["usedCalls"], 180)
        self.assertEqual(body["evidence"]["independentGroups"], 30)
        self.assertEqual(len(self.harness.calls), 180)
        self.assertEqual(self.harness.resets, 180)
        self.assertEqual(self.harness.scores, 180)
        record = self.registry.get(self.candidate["id"])
        self.assertEqual(record["status"], "consumed")
        self.assertEqual(record["result"], receipts[0])


if __name__ == "__main__":
    unittest.main()
