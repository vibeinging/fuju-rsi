"""文件候选调用真实读文件程序；临时配置不会进入业务源目录。"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fuju_rsi.core import Evaluation, Example, Prediction
from fuju_rsi.file_candidates import FileBaseline, FileCandidate, FileTarget
from fuju_rsi.file_runs import (
    FileExperimentSpec, FileRunContext, FileRunResult,
    compare_file_candidates, export_file_report,
)


def score(expected, prediction):
    return Evaluation(float(expected == prediction.output))


def ask_data_score(expected, prediction):
    from fuju_rsi.ask_data import evaluate
    return evaluate(expected, prediction)


def read_program(context, question):
    """代表业务现有配置入口；回执来自实际读取的字节。"""
    contents = {name: (context.root / name).read_bytes() for name in context.expected_hashes}
    dictionary = json.loads(contents["dictionary.json"])
    metrics = json.loads(contents["metrics.json"])
    field = metrics.get(dictionary.get(question))
    value = sum(row.get(field, 0) for row in ({"amount": 2}, {"amount": 3}))
    return FileRunResult(Prediction(value), {
        name: hashlib.sha256(content).hexdigest() for name, content in contents.items()})


class FileRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="file-run-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "business"
        self.root.mkdir()
        (self.root / "dictionary.json").write_text(
            '{"收入":"revenue", "营业收入":"revenue"}', encoding="utf-8")
        (self.root / "metrics.json").write_text('{"revenue":"wrong"}', encoding="utf-8")
        (self.root / "context.txt").write_text("只读业务说明", encoding="utf-8")
        self.baseline = FileBaseline.capture(self.root, [
            FileTarget("dictionary.json", "dictionary"),
            FileTarget("metrics.json", "metric-context"),
        ], context_files=["context.txt"])
        self.candidate = FileCandidate.propose(self.baseline, {
            "dictionary.json": '{"收入":"sales", "营业收入":"sales"}',
            "metrics.json": '{"sales":"amount"}',
        }, rationale="按原有 amount 口径修正词典映射，不改变公式")
        self.examples = [Example("train", "收入", 5, "train"),
                         Example("validation", "营业收入", 5, "validation")]

    def spec(self, runner=read_program, kind="custom"):
        return FileExperimentSpec("files", "两文件词典修正", self.baseline,
                                  self.examples, runner, score, kind=kind)

    def test_local_program_reads_both_candidate_files_without_source_changes(self):
        original = dict(self.baseline.files)
        result = compare_file_candidates(self.spec(), [self.candidate], max_calls=5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["candidateKind"], "files")
        self.assertEqual(result["selectedFileCandidateDigest"], self.candidate.digest)
        self.assertEqual(result["trials"][0]["validation"]["score"], 0)
        self.assertEqual(result["trials"][1]["validation"]["score"], 1)
        self.assertEqual(result["usedCalls"], 5)
        self.assertEqual({name: (self.root / name).read_text(encoding="utf-8")
                          for name in original}, original)

    def test_each_case_has_a_unique_cleaned_root_and_native_callback_identity(self):
        roots = []
        def runner(context, question):
            roots.append(context.root)
            with self.assertRaises(TypeError):
                context.expected_hashes["context.txt"] = "0" * 64
            return read_program(context, question)
        result = compare_file_candidates(self.spec(runner), [self.candidate])
        self.assertEqual(len(set(roots)), 4)
        self.assertTrue(all(not root.exists() for root in roots))
        self.assertEqual(result["callbackIdentity"][0]["name"], runner.__qualname__)
        self.assertEqual(result["callbackIdentity"][1]["name"], score.__qualname__)

    def test_wrong_or_missing_loaded_hashes_are_runtime_errors(self):
        for mode in ("wrong", "missing"):
            def runner(context, question):
                result = read_program(context, question)
                if mode == "wrong":
                    result.loaded_hashes["metrics.json"] = "0" * 64
                else:
                    del result.loaded_hashes["metrics.json"]
                return result
            result = compare_file_candidates(self.spec(runner), [self.candidate])
            self.assertFalse(result["searchComplete"])
            self.assertFalse(result["adoptable"])
            self.assertIsNotNone(result["trials"][0]["training"]["cases"][0]["error"])

    def test_scratch_changes_and_extra_entries_reject_success(self):
        for mode in ("content", "extra", "symlink", "directory"):
            roots = []
            def runner(context, question):
                roots.append(context.root)
                result = read_program(context, question)
                context.root.chmod(0o700)
                if mode == "content":
                    path = context.root / "metrics.json"
                    path.chmod(0o600)
                    path.write_text("{}", encoding="utf-8")
                elif mode == "extra":
                    (context.root / "cache.txt").write_text("unexpected")
                elif mode == "symlink":
                    (context.root / "alias").symlink_to(self.root)
                else:
                    (context.root / "new-dir").mkdir()
                return result
            result = compare_file_candidates(self.spec(runner), [self.candidate])
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["searchComplete"])
            self.assertTrue(all(not root.exists() for root in roots))

    def test_source_drift_before_or_during_callback_is_rejected(self):
        (self.root / "metrics.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError):
            compare_file_candidates(self.spec(), [self.candidate])
        (self.root / "metrics.json").write_text(self.baseline.files["metrics.json"], encoding="utf-8")
        def runner(context, question):
            (self.root / "metrics.json").write_text("{}", encoding="utf-8")
            return read_program(context, question)
        result = compare_file_candidates(self.spec(runner), [self.candidate])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["searchComplete"])

    def test_callback_exceptions_and_baseexception_clean_scratch(self):
        roots = []
        def runner(context, question):
            roots.append(context.root)
            raise RuntimeError("external business failure")
        result = compare_file_candidates(self.spec(runner), [self.candidate])
        self.assertFalse(result["searchComplete"])
        self.assertTrue(all(not root.exists() for root in roots))
        def interrupted(context, question):
            roots.append(context.root)
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            compare_file_candidates(self.spec(interrupted), [self.candidate])
        self.assertTrue(all(not root.exists() for root in roots))

    def test_candidate_from_another_baseline_is_rejected_before_running(self):
        other_root = Path(self.temp.name) / "other"
        other_root.mkdir()
        (other_root / "x.txt").write_text("old")
        other = FileBaseline.capture(other_root, [FileTarget("x.txt", "project-rules")])
        wrong = FileCandidate.propose(other, {"x.txt": "new"}, rationale="fix")
        with self.assertRaises(ValueError):
            compare_file_candidates(self.spec(), [wrong])

    def test_budget_and_cancellation_do_not_claim_complete_success(self):
        result = compare_file_candidates(self.spec(), [self.candidate], max_calls=4)
        self.assertFalse(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertLessEqual(result["usedCalls"], 4)
        cancelled = compare_file_candidates(self.spec(), [self.candidate], is_cancelled=lambda: True)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(cancelled["searchComplete"])

    def test_ask_data_requires_bundle_and_ledger_before_any_run(self):
        calls = []
        def runner(context, question):
            calls.append(question)
            return read_program(context, question)
        for options in ({}, {"ask_data_bundle": "missing"}, {"validation_ledger": "missing"}):
            with self.assertRaisesRegex(ValueError, "冻结开发包|账本"):
                compare_file_candidates(self.spec(runner, "ask-data"), [self.candidate], **options)
        self.assertEqual(calls, [])

    def ask_data_setup(self):
        from fuju_rsi.ask_data import build_casebook
        from fuju_rsi.ask_data_validation import ValidationLedger
        from fuju_rsi.benchmark import examples_from, freeze_book
        from test_ask_data_validation import make_catalog
        book = build_casebook(make_catalog())
        bundle = Path(self.temp.name) / "bundle"
        freeze_book(book, output=bundle, version="v1", scorer_file=Path(__file__))
        ledger = ValidationLedger.initialize(Path(self.temp.name) / "ledger", max_exposures=2)
        examples = [Example(**row) for row in examples_from(book)]
        def runner(context, question):
            loaded = read_program(context, "收入")
            loaded.prediction = Prediction({"status": "succeeded", "behavior": "answer",
                "result": {"columns": ["value"], "rows": [[int(loaded.prediction.output == 5)]],
                           "complete": True}})
            return loaded
        spec = FileExperimentSpec("ask", "问数词典", self.baseline, examples,
                                  runner, ask_data_score, kind="ask-data")
        return spec, bundle, ledger

    def test_ask_data_uses_actual_frozen_examples_and_shared_ledger(self):
        spec, bundle, ledger = self.ask_data_setup()
        result = compare_file_candidates(spec, [self.candidate], max_calls=5,
                                         ask_data_bundle=bundle, validation_ledger=ledger)
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["askDataValidation"]["plannedExposures"], 2)
        self.assertEqual(result["askDataValidation"]["maxUsedExposures"], 2)
        self.assertEqual(result["callbackIdentity"][1]["name"], ask_data_score.__qualname__)
        with self.assertRaisesRegex(ValueError, "上限"):
            compare_file_candidates(spec, [self.candidate],
                                    ask_data_bundle=bundle, validation_ledger=ledger)

    def test_ask_data_wrong_actual_scorer_or_examples_is_rejected_before_reservation(self):
        from fuju_rsi.ask_data import evaluate
        spec, bundle, ledger = self.ask_data_setup()
        for examples, evaluator in ((spec.examples[::-1], ask_data_score), (spec.examples, evaluate)):
            changed = FileExperimentSpec(spec.id, spec.name, spec.baseline, examples,
                                         spec.runner, evaluator, kind="ask-data")
            with self.assertRaises(ValueError):
                compare_file_candidates(changed, [self.candidate],
                                        ask_data_bundle=bundle, validation_ledger=ledger)
            self.assertEqual(ledger._read_uses()["tokens"], {})

    def test_evaluator_body_drift_is_rejected_even_if_source_file_unchanged(self):
        changed = []
        def evaluator(expected, prediction):
            evaluator.__code__ = replacement.__code__
            changed.append(True)
            return score(expected, prediction)
        def replacement(expected, prediction):
            if evaluator is replacement and changed:
                return Evaluation(0)
            return Evaluation(1)
        spec = FileExperimentSpec("drift", "评分漂移", self.baseline, self.examples,
                                  read_program, evaluator)
        result = compare_file_candidates(spec, [self.candidate])
        self.assertTrue(changed)
        self.assertIs(evaluator.__code__, replacement.__code__)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["searchComplete"])

    def test_parallel_experiments_do_not_share_scratch(self):
        roots = []
        lock = threading.Lock()
        def runner(context, question):
            with lock:
                roots.append(context.root)
            return read_program(context, question)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: compare_file_candidates(
                self.spec(runner), [self.candidate]), range(2)))
        self.assertTrue(all(result["searchComplete"] for result in results))
        self.assertEqual(len(set(roots)), 8)
        self.assertTrue(all(not root.exists() for root in roots))

    def test_exported_files_run_without_rsi_and_hide_private_cases(self):
        result = compare_file_candidates(self.spec(), [self.candidate])
        output = Path(self.temp.name) / "delivery"
        export_file_report(result, output)
        files = {name: (output / "candidate" / name).read_text(encoding="utf-8")
                 for name in self.baseline.files}
        dictionary, metrics = json.loads(files["dictionary.json"]), json.loads(files["metrics.json"])
        self.assertEqual(metrics[dictionary["收入"]], "amount")
        detached = """import importlib.util,json,sys
from pathlib import Path
assert importlib.util.find_spec('fuju_rsi') is None
root=Path(sys.argv[1])
dictionary=json.loads((root/'dictionary.json').read_text(encoding='utf-8'))
metrics=json.loads((root/'metrics.json').read_text(encoding='utf-8'))
field=metrics[dictionary['收入']]
print(sum(row[field] for row in ({'amount':2},{'amount':3})))
"""
        self.assertEqual(subprocess.check_output(
            [sys.executable, "-I", "-S", "-c", detached, str(output / "candidate")],
            cwd=self.temp.name, text=True).strip(), "5")
        self.assertEqual((output / "baseline" / "metrics.json").read_text(),
                         self.baseline.files["metrics.json"])
        summary = json.loads((output / "result.json").read_text())
        self.assertNotIn("cases", json.dumps(summary))
        self.assertFalse(summary["adoptable"])
        self.assertTrue((output / "changes.diff").is_file())
        delivery = json.loads((output / "candidate.json").read_text())
        self.assertEqual(delivery["baseline"]["files"], dict(self.baseline.files))
        self.assertEqual(delivery["candidate"]["digest"], self.candidate.digest)
        self.assertFalse((output / "verified-prompt.txt").exists())
        with self.assertRaises(FileExistsError):
            export_file_report(result, output)

    def test_export_refuses_symlink_destination(self):
        result = compare_file_candidates(self.spec(), [self.candidate])
        output = Path(self.temp.name) / "linked"
        output.symlink_to(Path(self.temp.name) / "nonexistent")
        with self.assertRaises(ValueError):
            export_file_report(result, output)
        parent = Path(self.temp.name) / "linked-parent"
        parent.symlink_to(self.root)
        with self.assertRaises(ValueError):
            export_file_report(result, parent / "report")

    def test_export_refuses_candidate_identity_mismatch_and_path_traversal(self):
        result = compare_file_candidates(self.spec(), [self.candidate])
        result["selectedFileCandidateDigest"] = result["baselineFileCandidateDigest"]
        with self.assertRaises(ValueError):
            export_file_report(result, Path(self.temp.name) / "wrong-selection")
        result = compare_file_candidates(self.spec(), [self.candidate])
        baseline = result["fileCandidates"][result["baselineFileCandidateDigest"]]
        baseline["files"]["../outside"] = "must not write"
        with self.assertRaises(ValueError):
            export_file_report(result, Path(self.temp.name) / "wrong-path")
        self.assertFalse((Path(self.temp.name) / "outside").exists())

    def test_failed_export_removes_staging_and_reserved_output(self):
        result = compare_file_candidates(self.spec(), [self.candidate])
        output = Path(self.temp.name) / "failed-output"
        with patch("fuju_rsi.file_runs.os.replace", side_effect=OSError("atomic replace failed")):
            with self.assertRaises(OSError):
                export_file_report(result, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(Path(self.temp.name).glob(".fuju-rsi-report-*")), [])


if __name__ == "__main__":
    unittest.main()
