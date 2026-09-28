"""真实 SQLite 重跑验收与保留集使用边界。

这里的案例是本地生成的 synthetic independent-protocol fixture，用来检验
代码协议；不表示真实业务样本独立，也不表示当前测试进程具有外部账号隔离。
所有业务评分来自执行 SQL 后与独立算术结果比较，不伪造验收成功。
"""
import copy
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.benchmark import digest
from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction, optimize
from fuju_rsi.holdout_registry import HoldoutRegistry, RegistryError
from fuju_rsi.verification import (VerificationSpec, freeze_candidate,
                                              validate_receipt, verify_candidate)


BASELINE = "SELECT gross FROM sales"
CANDIDATE = "SELECT gross - refund FROM sales"
MEMORIZED = "SELECT CASE WHEN gross < 100 THEN gross - refund ELSE gross END FROM sales"
PROVENANCE = ("synthetic independent-protocol fixture; generated locally for tests; "
              "not actual business data or proof of externally isolated execution")


def example(index, *, split="test", group=None, source=None, gross=None, refund=7,
            operation="net", critical=False):
    amount = (1000 + index * 13) if gross is None else gross
    group_id = "%s-group-%s" % (split, index if group is None else group)
    source_id = "%s-source-%s" % (split, index if source is None else source)
    value = {"gross": amount, "refund": refund, "operation": operation}
    expected = amount - refund if operation == "net" else amount
    return Example("%s-case-%s" % (split, index), value, expected, split,
                   group_id=group_id, source_id=source_id,
                   exposure="unseen" if split == "test" else "development", critical=critical)


class SqlHarness:
    """被测函数每次独立打开内存 SQLite；reset 记录成对运行边界。"""

    def __init__(self, proposal=CANDIDATE):
        self.proposal = proposal
        self.calls = []
        self.resets = 0
        self.scores = 0
        self.fail_runner = False
        self.fail_evaluator = False
        self.fail_reset = False
        self.on_run = None

    def runner(self, prompt, value):
        self.calls.append((prompt, copy.deepcopy(value)))
        if self.fail_runner:
            raise RuntimeError("synthetic SQL connection failure")
        if self.on_run:
            self.on_run()
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE sales (gross INTEGER NOT NULL, refund INTEGER NOT NULL)")
            db.execute("INSERT INTO sales VALUES (?, ?)", (value["gross"], value["refund"]))
            answer = db.execute(prompt).fetchone()[0]
        return Prediction(answer, tokens=None, cost_usd=None)

    def evaluator(self, expected, prediction):
        self.scores += 1
        if self.fail_evaluator:
            raise RuntimeError("synthetic grader unavailable")
        return Evaluation(float(prediction.output == expected), "independent arithmetic equality")

    def proposer(self, prompt, feedback, index):
        return self.proposal

    def reset(self):
        self.resets += 1
        if self.fail_reset:
            raise RuntimeError("synthetic reset failed")


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".verification-test-", dir=str(ROOT / "tests"))
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dependency = self.directory / "business-rules.txt"
        self.dependency.write_text("net = gross - refund\n", encoding="utf-8")
        self.registry_path = self.directory / "authority"
        HoldoutRegistry.initialize(self.registry_path, "synthetic-test-authority")
        self.registry = HoldoutRegistry(self.registry_path)
        self.key = b"synthetic-test-signing-key-only!!" + b"!"
        self.environment = {"fixture": "synthetic-sqlite-net-sales", "runtime": "stdlib-sqlite", "model": "none"}
        # 最低组数与成对重跑次数保留公开默认值；减少辅助描述的重采样次数以缩短测试。
        self.policy = AcceptancePolicy(resamples=100)
        self.harness = SqlHarness()
        self.dev = [example(1, split="train", gross=20, refund=3),
                    example(2, split="validation", gross=40, refund=5)]
        self.agent = self.make_agent()
        self.search = self.search_agent()
        self.candidate = self.freeze()
        self.spec = self.verification_spec()
        self.harness.calls.clear()
        self.harness.resets = 0
        self.harness.scores = 0

    def make_agent(self):
        return AgentSpec("sql-net", "Synthetic SQL test", BASELINE, self.dev,
                         self.harness.runner, self.harness.proposer, self.harness.evaluator)

    def search_agent(self):
        result = optimize(self.agent, max_trials=1, max_calls=5)
        result["id"] = uuid.uuid4().hex
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertFalse(result["adoptable"])
        return result

    def freeze(self, **overrides):
        options = {"source_files": [str(Path(__file__).resolve()), str(self.dependency)],
                   "environment": self.environment, "policy": self.policy, "root": ROOT}
        options.update(overrides)
        return freeze_candidate(self.agent, self.search, **options)

    def verification_spec(self, examples=None, **overrides):
        values = {"examples": [example(i) for i in range(30)] if examples is None else examples,
                  "runner": self.harness.runner, "evaluator": self.harness.evaluator,
                  "reset": self.harness.reset, "policy": self.policy, "isolation": "independent",
                  "provenance": PROVENANCE, "environment": copy.deepcopy(self.environment)}
        values.update(overrides)
        return VerificationSpec(**values)

    def verify(self, candidate=None, spec=None, **overrides):
        options = {"registry": self.registry, "signing_key": self.key, "root": ROOT}
        options.update(overrides)
        return verify_candidate(self.candidate if candidate is None else candidate,
                                self.spec if spec is None else spec, **options)

    def test_real_sqlite_reruns_both_arms_reset_and_alternate_order(self):
        receipt = self.verify()
        body = validate_receipt(receipt, self.key)
        self.assertTrue(body["adoptable"])
        self.assertEqual(body["evidenceStatus"], "improved")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["plannedCalls"], 180)
        self.assertEqual(body["usedCalls"], 180)
        self.assertEqual(len(self.harness.calls), 180)
        self.assertEqual(self.harness.resets, 180)
        self.assertEqual(self.harness.scores, 180)
        self.assertEqual(body["evidence"]["independentGroups"], 30)
        self.assertEqual(body["evidence"]["baselineScore"], 0.0)
        self.assertEqual(body["evidence"]["candidateScore"], 1.0)
        for index, ex in enumerate(self.spec.examples):
            for repeat in range(3):
                start = index * 6 + repeat * 2
                expected = [BASELINE, CANDIDATE] if not (index + repeat) % 2 else [CANDIDATE, BASELINE]
                self.assertEqual([prompt for prompt, _ in self.harness.calls[start:start + 2]], expected)
                self.assertEqual([value for _, value in self.harness.calls[start:start + 2]], [ex.input, ex.input])
        self.assertEqual(self.registry.get(self.candidate["id"])["status"], "consumed")
        self.assertEqual(body["candidateDigest"], self.candidate["digest"])

    def test_same_source_rewrites_do_not_inflate_independent_groups(self):
        examples = [example(i, group="one-origin", source="one-origin") for i in range(30)]
        body = validate_receipt(self.verify(spec=self.verification_spec(examples)), self.key)
        self.assertEqual(body["evidence"]["caseCount"], 30)
        self.assertEqual(body["evidence"]["independentGroups"], 1)
        self.assertEqual(body["usedCalls"], 180)
        self.assertEqual(body["evidenceStatus"], "insufficient")
        self.assertFalse(body["adoptable"])

    def test_one_source_cannot_be_relabeled_as_many_independent_groups(self):
        examples = [example(i, source="shared-origin") for i in range(30)]
        with self.assertRaises(ValueError):
            self.verify(spec=self.verification_spec(examples))
        self.assertEqual(self.harness.calls, [])

    def test_workflow_only_cannot_be_adopted_even_with_large_gain(self):
        self.spec.isolation = "workflow_only"
        body = validate_receipt(self.verify(), self.key)
        self.assertEqual(body["evidence"]["delta"], 1.0)
        self.assertGreater(body["evidence"]["interval"]["lower"], 0.0)
        self.assertEqual(body["evidenceStatus"], "insufficient")
        self.assertFalse(body["adoptable"])

    def test_identical_heldout_business_results_are_no_improvement(self):
        examples = [example(i, refund=0) for i in range(30)]
        body = validate_receipt(self.verify(spec=self.verification_spec(examples)), self.key)
        self.assertEqual(body["evidence"]["baselineScore"], 1.0)
        self.assertEqual(body["evidence"]["candidateScore"], 1.0)
        self.assertEqual(body["evidenceStatus"], "no_improvement")
        self.assertFalse(body["adoptable"])

    def test_critical_business_regression_blocks_aggregate_gain(self):
        examples = [example(i) for i in range(30)]
        # 有的业务问的是销售总额；把所有问题都减退款是过度套用开发规则。
        examples[-1] = example(29, operation="gross", critical=True)
        body = validate_receipt(self.verify(spec=self.verification_spec(examples)), self.key)
        self.assertGreater(body["evidence"]["delta"], 0.9)
        self.assertEqual(body["evidenceStatus"], "regressed")
        self.assertFalse(body["adoptable"])

    def test_candidate_memorizing_development_range_fails_unseen_cases(self):
        self.harness.proposal = MEMORIZED
        self.search = self.search_agent()
        candidate = self.freeze()
        self.harness.calls.clear()
        body = validate_receipt(self.verify(candidate=candidate), self.key)
        self.assertEqual(self.search["bestPrompt"], MEMORIZED)
        self.assertEqual(body["evidence"]["baselineScore"], 0.0)
        self.assertEqual(body["evidence"]["candidateScore"], 0.0)
        self.assertEqual(body["evidenceStatus"], "no_improvement")
        self.assertFalse(body["adoptable"])

    def test_development_input_source_and_group_overlap_rejected_before_runner(self):
        original = self.spec.examples[0]
        variants = [replace(original, input=copy.deepcopy(self.dev[0].input)),
                    replace(original, source_id=self.dev[0].source_id),
                    replace(original, group_id=self.dev[0].group_id)]
        for contaminated in variants:
            with self.subTest(contaminated=contaminated):
                values = [contaminated] + self.spec.examples[1:]
                with self.assertRaisesRegex(ValueError, "重叠"):
                    self.verify(spec=self.verification_spec(values))
        self.assertEqual(self.harness.calls, [])
        with self.assertRaises(RegistryError):
            self.registry.get(self.candidate["id"])

    def test_consumed_holdout_rejects_renamed_partial_input_overlap(self):
        self.verify()
        old_calls = len(self.harness.calls)
        new_candidate = self.freeze()
        rows = [example(i + 1000) for i in range(30)]
        rows[0] = replace(rows[0], input=copy.deepcopy(self.spec.examples[0].input), expected=-999)
        with self.assertRaises(RegistryError):
            self.verify(candidate=new_candidate, spec=self.verification_spec(rows))
        self.assertEqual(len(self.harness.calls), old_calls)

    def test_consumed_holdout_rejects_partial_source_or_group_overlap(self):
        self.verify()
        old_calls = len(self.harness.calls)
        for field in ("source_id", "group_id"):
            rows = [example(i + 1000) for i in range(30)]
            rows[0] = replace(rows[0], **{field: getattr(self.spec.examples[0], field)})
            with self.subTest(field=field), self.assertRaises(RegistryError):
                self.verify(candidate=self.freeze(), spec=self.verification_spec(rows))
        self.assertEqual(len(self.harness.calls), old_calls)

    def test_unknown_exposure_duplicate_input_and_invalid_expected_are_rejected(self):
        variants = [replace(self.spec.examples[0], exposure="unknown"),
                    replace(self.spec.examples[0], split="validation"),
                    replace(self.spec.examples[0], expected=float("nan")),
                    replace(self.spec.examples[0], critical=1)]
        for invalid in variants:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.verify(spec=self.verification_spec([invalid] + self.spec.examples[1:]))
        duplicate = replace(self.spec.examples[0], id="renamed-case")
        with self.assertRaises(ValueError):
            self.verify(spec=self.verification_spec(self.spec.examples + [duplicate]))
        self.assertEqual(self.harness.calls, [])

    def test_changed_candidate_payload_is_rejected(self):
        for field, value in (("candidatePrompt", "SELECT 123"), ("baselinePrompt", "SELECT 456"),
                              ("selectedTrialId", "trial-99"), ("experimentId", "changed")):
            candidate = copy.deepcopy(self.candidate)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.verify(candidate=candidate)
        self.assertEqual(self.harness.calls, [])

    def test_changed_declared_file_invalidates_candidate_before_runner(self):
        self.dependency.write_text("net = gross\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "文件已改变"):
            self.verify()
        self.assertEqual(self.harness.calls, [])

    def test_changed_policy_and_environment_are_rejected_before_runner(self):
        changed = self.verification_spec(policy=AcceptancePolicy(min_groups=2, resamples=100))
        with self.assertRaisesRegex(ValueError, "规则"):
            self.verify(spec=changed)
        changed = self.verification_spec(environment={**self.environment, "model": "changed"})
        with self.assertRaisesRegex(ValueError, "运行条件"):
            self.verify(spec=changed)
        self.assertEqual(self.harness.calls, [])

    def test_insufficient_budget_rejected_before_runner_or_reservation(self):
        with self.assertRaisesRegex(ValueError, "预算不足"):
            self.verify(max_calls=179)
        self.assertEqual(self.harness.calls, [])
        with self.assertRaises(RegistryError):
            self.registry.get(self.candidate["id"])
        for budget in (True, -1, 180.0, 1_000_001):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                self.verify(max_calls=budget)

    def test_runner_evaluator_and_reset_failure_burn_batch_without_adoption(self):
        for failure, expected_calls in (("fail_runner", 1), ("fail_evaluator", 1), ("fail_reset", 0)):
            with self.subTest(failure=failure):
                candidate = self.freeze()
                # 每个故障实验使用新内容和新来源，不能借测试代码复用已消耗题。
                offset = {"fail_runner": 100, "fail_evaluator": 200, "fail_reset": 300}[failure]
                spec = self.verification_spec([example(i + offset) for i in range(30)])
                setattr(self.harness, failure, True)
                before = len(self.harness.calls)
                receipt = self.verify(candidate=candidate, spec=spec)
                setattr(self.harness, failure, False)
                body = validate_receipt(receipt, self.key)
                self.assertEqual(body["status"], "failed")
                self.assertFalse(body["adoptable"])
                self.assertEqual(body["usedCalls"], expected_calls)
                self.assertEqual(len(self.harness.calls) - before, expected_calls)
                self.assertEqual(self.registry.get(candidate["id"])["status"], "interrupted")
                with self.assertRaises(RegistryError):
                    self.verify(candidate=candidate, spec=spec)
                with self.assertRaises(RegistryError):
                    self.verify(candidate=self.freeze(), spec=spec)

    def test_cancellation_after_first_runner_burns_batch_and_cannot_resume(self):
        receipt = self.verify(is_cancelled=lambda: len(self.harness.calls) >= 1)
        body = validate_receipt(receipt, self.key)
        self.assertEqual(body["status"], "cancelled")
        self.assertEqual(body["usedCalls"], 1)
        self.assertFalse(body["adoptable"])
        self.assertEqual(self.registry.get(self.candidate["id"])["status"], "interrupted")
        with self.assertRaises(RegistryError):
            self.verify()
        self.assertEqual(len(self.harness.calls), 1)

    def test_file_change_during_runner_invalidates_evidence_and_consumes_batch(self):
        self.harness.on_run = lambda: self.dependency.write_text("modified during execution", encoding="utf-8")
        body = validate_receipt(self.verify(), self.key)
        self.assertEqual(body["status"], "failed")
        self.assertFalse(body["adoptable"])
        self.assertEqual(self.registry.get(self.candidate["id"])["status"], "interrupted")

    def test_exact_replay_returns_same_signed_cached_receipt_without_calls(self):
        original = self.verify()
        calls, resets, scores = len(self.harness.calls), self.harness.resets, self.harness.scores
        # 即便回调现在失败，也不能重新调用；同一个验收任务返回原签名结果。
        self.harness.fail_runner = True
        replay = self.verify()
        self.assertEqual(original, replay)
        self.assertEqual(len(self.harness.calls), calls)
        self.assertEqual(self.harness.resets, resets)
        self.assertEqual(self.harness.scores, scores)
        self.assertEqual(validate_receipt(replay, self.key)["candidateDigest"], self.candidate["digest"])

    def test_receipt_tampering_wrong_key_and_malformed_signature_are_rejected(self):
        original = self.verify()
        poisoned = copy.deepcopy(original)
        poisoned["body"]["adoptable"] = False
        with self.assertRaises(ValueError):
            validate_receipt(poisoned, self.key)
        with self.assertRaises(ValueError):
            validate_receipt(original, b"wrong-key-is-at-least-32-bytes-long")
        for signature in (None, 1, "", "0" * 64, "é" * 64):
            poisoned = copy.deepcopy(original)
            poisoned["signature"] = signature
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                validate_receipt(poisoned, self.key)

    def test_freeze_rejects_search_result_with_untested_selected_prompt(self):
        self.search["bestPrompt"] = "SELECT 12345"
        with self.assertRaises(ValueError):
            self.freeze()

    def test_freeze_requires_runner_and_evaluator_source_in_declared_files(self):
        with self.assertRaises(ValueError):
            self.freeze(source_files=[str(self.dependency)])

    def test_freeze_ignores_legacy_test_rows_when_deriving_the_development_digest(self):
        """v1 旧包把 test 行和开发题放在同一列表：它不参与摘要，也不阻止固定候选。"""
        rows = copy.deepcopy(self.dev) + [example(9)]
        self.assertEqual(rows[-1].split, "test")
        agent = AgentSpec("sql-net", "Synthetic SQL test", BASELINE, rows,
                          self.harness.runner, self.harness.proposer, self.harness.evaluator)
        search = optimize(agent, max_trials=1, max_calls=5)
        search["id"] = uuid.uuid4().hex
        self.assertTrue(search["searchComplete"])
        self.assertEqual(search["developmentDigest"],
                         digest([vars(e) for e in rows if e.split != "test"]))
        candidate = freeze_candidate(agent, search,
                                     source_files=[str(Path(__file__).resolve()), str(self.dependency)],
                                     environment=self.environment, policy=self.policy, root=ROOT)
        self.assertEqual(candidate["developmentDigest"], search["developmentDigest"])

    def test_mutating_public_spec_during_runner_cannot_relax_fixed_policy_or_isolation(self):
        self.spec = self.verification_spec([example(i) for i in range(2)], isolation="workflow_only")

        def mutate():
            self.spec.policy = AcceptancePolicy(min_groups=2, confidence=0.01, resamples=1)
            self.spec.isolation = "independent"
            self.spec.provenance = "changed after observing results"
            self.spec.environment["model"] = "changed"

        self.harness.on_run = mutate
        body = validate_receipt(self.verify(), self.key)
        self.assertFalse(body["adoptable"])
        self.assertEqual(body["isolation"], "workflow_only")
        self.assertEqual(body["provenance"], PROVENANCE)
        self.assertEqual(body["environment"], self.environment)
        self.assertEqual(body["evidenceStatus"], "insufficient")

    def test_mutating_public_candidate_during_runner_cannot_change_prompts_or_receipt_identity(self):
        original = copy.deepcopy(self.candidate)

        def mutate():
            self.candidate["candidatePrompt"] = "SELECT 12345"
            self.candidate["experimentId"] = "changed-after-start"
            self.candidate["digest"] = digest({k: v for k, v in self.candidate.items() if k != "digest"})

        self.harness.on_run = mutate
        receipt = self.verify()
        body = validate_receipt(receipt, self.key)
        self.assertEqual(body["candidateDigest"], original["digest"])
        self.assertEqual(body["experimentId"], original["experimentId"])
        self.assertEqual({prompt for prompt, _ in self.harness.calls}, {BASELINE, CANDIDATE})
        self.assertEqual(body["evidenceStatus"], "improved")


if __name__ == "__main__":
    unittest.main()
