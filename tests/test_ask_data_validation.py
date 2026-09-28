"""问数验证题跨实验使用次数，包含真实冻结包加载。"""
from pathlib import Path
import multiprocessing
import tempfile
import unittest

from fuju_rsi.ask_data import build_casebook, evaluate
from fuju_rsi.ask_data_validation import (
    ValidationLedger, ValidationUseError, optimize_ask_data,
)
from fuju_rsi.benchmark import examples_from, freeze_book
from fuju_rsi.core import AgentSpec, Example, Prediction
from fuju_rsi.workspace import ExperimentManager


SCORER = Path(__file__).resolve()


def run(prompt, question):
    value = 1 if prompt == "candidate" else 0
    return Prediction({"status": "succeeded", "behavior": "answer",
                       "result": {"columns": ["value"], "rows": [[value]],
                                  "complete": True}})


def propose(prompt, feedback, index):
    return "candidate"


def score(expected, prediction):
    return evaluate(expected, prediction)


def make_catalog():
    expected = {"behavior": "answer", "requiresDelivery": False,
                "result": {"columns": ["value"], "rows": [[1]], "ordered": True}}
    oracle = {"verified": True, "method": "independent-calculation",
              "evidence": "固定 fixture 的独立计算",
              "dataSnapshot": "fixture-1", "contextRevision": "rule-1"}
    return {
        "schemaVersion": 1, "role": "development", "id": "ask-data-demo",
        "name": "问数验证次数测试",
        "environment": {"dataSnapshot": "fixture-1", "contextRevision": "rule-1"},
        "sources": [
            {"id": "a", "kind": "user-case", "ref": "source-a",
             "familyId": "group-a", "split": "train"},
            {"id": "b", "kind": "user-case", "ref": "source-b",
             "familyId": "group-b", "split": "validation"},
        ],
        "cases": [
            {"id": "case-a", "sourceId": "a", "question": "训练问题", "tags": ["计算"],
             "status": "ready", "expected": expected, "oracle": oracle},
            {"id": "case-b", "sourceId": "b", "question": "验证问题", "tags": ["计算"],
             "status": "ready", "expected": expected, "oracle": oracle},
        ],
    }


def reserve_in_process(directory, examples, barrier, queue):
    barrier.wait(timeout=10)
    try:
        ValidationLedger(directory).reserve_run(examples, planned_exposures=1)
    except ValidationUseError:
        queue.put("blocked")
    else:
        queue.put("reserved")


class ValidationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="fuju-rsi-ask-data-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.ledger = ValidationLedger.initialize(root / "ledger", max_exposures=3)
        self.book = build_casebook(make_catalog())
        self.bundle = root / "bundle-v1"
        freeze_book(self.book, output=self.bundle, version="v1", scorer_file=SCORER)
        self.examples = [Example(**row) for row in examples_from(self.book)]
        self.spec = AgentSpec("ask-data", "问数", "baseline", self.examples,
                              run, propose, score, kind="ask-data")

    def test_staged_validation_reservation_precedes_any_runner_call(self):
        result = optimize_ask_data(self.spec, bundle_dir=self.bundle, ledger=self.ledger,
                                   max_trials=1, max_calls=5)
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["askDataValidation"]["plannedExposures"], 2)
        self.assertEqual(result["askDataValidation"]["maxUsedExposures"], 2)
        self.assertTrue(result["developmentHistoryTokens"])
        with self.assertRaisesRegex(ValidationUseError, "上限"):
            optimize_ask_data(self.spec, bundle_dir=self.bundle, ledger=self.ledger,
                              max_trials=1, max_calls=5)

    def test_repackaging_same_source_does_not_reset_validation_budget(self):
        self.ledger.reserve_run(self.examples, planned_exposures=2)
        changed = make_catalog()
        changed["cases"][1]["id"] = "renamed-validation-case"
        changed["cases"][1]["question"] = "验证问题的同义改写"
        second = build_casebook(changed)
        with self.assertRaisesRegex(ValidationUseError, "上限"):
            self.ledger.reserve_run([Example(**row) for row in examples_from(second)],
                                    planned_exposures=2)

    def test_move_training_source_to_validation_is_rejected(self):
        self.ledger.reserve_run(self.examples, planned_exposures=1)
        changed = make_catalog()
        changed["sources"][0]["split"] = "validation"
        second = build_casebook(changed)
        with self.assertRaisesRegex(ValidationUseError, "另一用途"):
            self.ledger.reserve_run([Example(**row) for row in examples_from(second)],
                                    planned_exposures=1)

    def test_unfrozen_or_mismatched_examples_never_consume_budget(self):
        changed = AgentSpec("ask-data", "问数", "baseline", self.examples[::-1],
                            run, propose, score, kind="ask-data")
        with self.assertRaisesRegex(ValidationUseError, "逐项一致"):
            optimize_ask_data(changed, bundle_dir=self.bundle, ledger=self.ledger,
                              max_trials=1, max_calls=5)
        self.assertEqual(self.ledger._read_uses()["tokens"], {})

    def test_cap_is_fixed_and_cannot_be_reset_in_same_directory(self):
        with self.assertRaises(FileExistsError):
            ValidationLedger.initialize(self.ledger.directory, max_exposures=100)
        with self.assertRaises(ValidationUseError):
            ValidationLedger.initialize(self.ledger.directory / "bad", max_exposures=0)

    def test_two_processes_cannot_spend_one_remaining_validation_slot(self):
        directory = Path(self.temporary.name) / "one-slot"
        ValidationLedger.initialize(directory, max_exposures=1)
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=reserve_in_process,
                                   args=(directory, self.examples, barrier, queue))
                   for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            outcomes = [queue.get(timeout=15) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(outcomes), ["blocked", "reserved"])
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            queue.close()

    def test_workspace_uses_guard_and_freezes_full_development_history(self):
        workspace = Path(self.temporary.name) / "workspace"
        manager = ExperimentManager(workspace, [self.spec], telemetry_mode="log",
                                    ask_data_bundle=self.bundle,
                                    validation_ledger=self.ledger.directory)
        try:
            started = manager.start(agent_id=self.spec.id, max_trials=1, max_calls=5)
            completed = manager.wait(started["id"])
            self.assertTrue(completed["searchComplete"])
            candidate = manager.freeze(started["id"], source_files=[str(SCORER)],
                                       environment={"dataSnapshot": "fixture-1"})
            self.assertEqual(candidate["developmentTokens"],
                             self.ledger.development_tokens())
            self.assertEqual(candidate["askDataValidation"]["ledgerId"],
                             self.ledger.metadata["ledgerId"])
            self.assertFalse(completed["adoptable"])
        finally:
            manager.close()

    def test_workspace_rejects_ask_data_without_frozen_bundle_and_ledger(self):
        manager = ExperimentManager(Path(self.temporary.name) / "unguarded",
                                    [self.spec], telemetry_mode="log")
        try:
            with self.assertRaisesRegex(ValueError, "冻结开发包"):
                manager.start(agent_id=self.spec.id)
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
