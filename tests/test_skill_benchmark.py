"""复制 skill 到临时业务项目，经真实子进程验证评测集整理、冻结和核验。"""
from copy import deepcopy
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
SCORER = '''def score(expected, actual):
    return float(expected == actual)
'''
LOAD_BUNDLE = '''
import json
import sys
sys.path.insert(0, sys.argv[1])
from benchmark import BenchmarkError, load_bundle
role = sys.argv[3] if len(sys.argv) > 3 else None
try:
    manifest, examples = load_bundle(sys.argv[2], role=role)
except BenchmarkError as exc:
    print(json.dumps({"status": "failed", "error": str(exc)}))
    sys.exit(1)
print(json.dumps({"status": "ok", "manifest": manifest, "examples": examples}))
'''
EVALUATOR = '''
from fuju_rsi import Evaluation

def score(expected, prediction):
    print("evaluator comparison:", expected, prediction.output)
    return Evaluation(float(expected == prediction.output), "Verified SQLite result")
'''
BUSINESS_AGENT = '''
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from fuju_rsi import AgentSpec, Example, Prediction
from score import score

PROJECT = Path(__file__).resolve().parent

def build_agent():
    cases = json.loads((PROJECT / "benchmarks/business-v1/examples.json").read_text())
    print("factory frozen examples:", cases)
    examples = [Example(**case) for case in cases]

    def runner(prompt, item):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE quantities (value INTEGER)")
            db.executemany("INSERT INTO quantities VALUES (?)", [(value,) for value in item["values"]])
            if prompt == "SUM":
                value = db.execute("SELECT SUM(value) FROM quantities").fetchone()[0]
            else:
                value = db.execute("SELECT COUNT(value) FROM quantities").fetchone()[0]
        output = {"value": value, "marker": item["marker"]}
        with (PROJECT / "runner-calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"prompt": prompt, "output": output}) + "\\n")
        print("runner private output:", output)
        return Prediction(output, tokens=0, cost_usd=0.0)

    def original_proposer(*args):
        (PROJECT / "original-proposer-called").write_text("Unexpected callback")
        raise AssertionError("Original proposer must not be used for fixed candidates")

    return AgentSpec("sqlite-business", "SQLite business agent", "COUNT", examples,
                     runner, original_proposer, score)

def mismatched_agent():
    agent = build_agent()
    agent.examples[0] = replace(agent.examples[0], expected={"value": 999, "marker": "unfrozen"})
    return agent
'''


def verified_case(identifier, split, *, expected=None):
    return {
        "id": identifier,
        "group": "business-request-" + identifier,
        "input": {"request": identifier, "options": {"limit": 3, "active": True}},
        "expected": expected,
        "expectedStatus": "verified",
        "evidence": "Independent fixture review: " + identifier,
        "split": split,
        "tags": ["boundary", split],
        "source": {"kind": "reviewed-fixture", "ref": "fixtures/" + identifier},
    }


class SkillBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.project = self.root / "business-app"
        self.project.mkdir()
        self.skill = self.root / "relocated-skills" / "fuju-tune"
        shutil.copytree(ROOT / "skills" / "fuju-tune", self.skill,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.helper = self.skill / "scripts" / "benchmark.py"
        self.scorer = self.project / "score.py"
        self.scorer.write_text(SCORER, encoding="utf-8")
        self.source = self.project / "business-eval.json"
        self.bundle = self.project / "benchmarks" / "business-v1"
        self.book = {
            "schemaVersion": 1,
            "id": "business-eval",
            "name": "业务流程评测",
            "environment": {"runtime": "python-stdlib", "fixtures": "reviewed-v1"},
            "scoring": "Exact output match, including explicit null results",
            "cases": [verified_case("empty-result", "train"),
                      verified_case("filtered-result", "validation", expected={"count": 2}),
                      verified_case("limit-result", "test", expected=[1, 2, 3])],
        }
        self.write_json(self.source, self.book)
        # skill 已成为 SDK 公共模块的入口，SDK 单独提供；复制后的 skill 不查找开发仓库。
        # 此处显式加载本次源码，真正干净安装由 verify_python_sdk_consumer.py 验证。
        self.environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        self.environment["PYTHONDONTWRITEBYTECODE"] = "1"

    def write_json(self, path, value):
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def run_process(self, command):
        result = subprocess.run(command, cwd=self.project, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.stderr, "", result.stderr)
        self.assertTrue(result.stdout.strip(), "Helper must return a JSON result")
        return result.returncode, json.loads(result.stdout)

    def run_helper(self, command, *args):
        return self.run_process([sys.executable, str(self.helper), command,
                                 "--input", str(self.source), *map(str, args)])

    def freeze(self, *, output=None, version="v1", scorer=None):
        return self.run_helper("freeze", "--output", output or self.bundle,
                               "--version", version, "--scorer-file", scorer or self.scorer)

    def assert_frozen(self):
        code, result = self.freeze()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "ok")
        return result

    def load_bundle(self, role=None):
        command = [sys.executable, "-c", LOAD_BUNDLE,
                   str(self.helper.parent), str(self.bundle)]
        if role is not None:
            command.append(role)
        return self.run_process(command)

    def assert_failure(self, outcome, message):
        code, result = outcome
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "failed")
        self.assertIn(message, result["error"])

    def prepare_experiment(self):
        self.scorer.write_text(EVALUATOR, encoding="utf-8")
        (self.project / "business_agent.py").write_text(BUSINESS_AGENT, encoding="utf-8")
        inputs = [("train", [2, 4], "TRAIN_BENCHMARK_PUBLIC"),
                  ("validation", [3, 5], "VALIDATION_BENCHMARK_PRIVATE"),
                  ("test", [5, 7], "TEST_BENCHMARK_PRIVATE")]
        self.book["cases"] = []
        for split, values, marker in inputs:
            case = verified_case("sqlite-" + split, split, expected={"value": sum(values), "marker": marker})
            case["input"] = {"values": values, "marker": marker}
            self.book["cases"].append(case)
        self.write_json(self.source, self.book)
        self.workspace = self.project / ".yitrace-optimization"
        self.environment["PYTHONPATH"] = str(ROOT / "src")

    def run_experiment(self, *, factory="build_agent"):
        candidate = self.project / "candidate.txt"
        candidate.write_text("SUM", encoding="utf-8")
        outcome = self.run_process([
            sys.executable, str(self.helper.parent / "run_experiment.py"),
            "--agent", "business_agent:" + factory,
            "--benchmark", str(self.bundle), "--workspace", str(self.workspace),
            "--candidate-file", str(candidate), "--max-calls", "20", "--name", "SQLite acceptance",
        ])
        summary = json.dumps(outcome[1], ensure_ascii=False)
        for marker in ("VALIDATION_BENCHMARK_PRIVATE", "TEST_BENCHMARK_PRIVATE"):
            self.assertNotIn(marker, summary)
        self.assertFalse((self.project / "original-proposer-called").exists())
        return outcome

    def assert_experiment_rejected_before_runner(self, outcome, log_message):
        code, result = outcome
        self.assertEqual(code, 1, result)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["errorType"], "BenchmarkError")
        self.assertIn(log_message, Path(result["runtimeLog"]).read_text())
        self.assertFalse((self.project / "runner-calls.jsonl").exists())
        self.assertFalse((self.workspace / "experiments").exists())
        self.assertFalse((self.workspace / "active-prompts.json").exists())
        self.assertEqual(list((self.workspace / "skill-runs").glob("*.benchmark.json")), [])

    def test_drafts_can_be_inspected_without_expected_answers_or_splits(self):
        draft = deepcopy(self.book)
        for case in draft["cases"]:
            for key in ("expected", "expectedStatus", "evidence", "split"):
                case.pop(key)
        self.write_json(self.source, draft)
        code, result = self.run_helper("inspect")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["draftCount"], 3)
        self.assertEqual(result["verifiedCount"], 0)
        self.assertEqual(result["splits"], {})
        self.assertEqual(result["coverage"]["boundary"], 3)
        self.assertNotIn("cases", result)
        self.assertNotIn("Independent fixture", json.dumps(result))
        self.assert_failure(self.freeze(), "verified expectations")
        self.assertFalse(self.bundle.exists())

    def test_verified_requires_both_expected_and_nonempty_evidence(self):
        invalid_changes = [("expected", None), ("evidence", None), ("evidence", "   ")]
        for key, value in invalid_changes:
            with self.subTest(field=key, value=value):
                book = deepcopy(self.book)
                if value is None:
                    book["cases"][0].pop(key)
                else:
                    book["cases"][0][key] = value
                self.write_json(self.source, book)
                self.assert_failure(self.run_helper("inspect"),
                                    "expected" if key == "expected" else "evidence")
                self.assert_failure(self.freeze(),
                                    "expected" if key == "expected" else "evidence")
                self.assertFalse(self.bundle.exists())

    def test_explicit_null_expected_survives_freeze_and_load(self):
        self.assert_frozen()
        code, result = self.load_bundle()
        self.assertEqual(code, 0, result)
        empty_case = next(case for case in result["examples"] if case["id"] == "empty-result")
        self.assertIn("expected", empty_case)
        self.assertIsNone(empty_case["expected"])
        self.assertEqual(empty_case["split"], "train")

    def test_merge_appends_to_new_file_and_preserves_source_and_existing_output(self):
        additions = self.project / "additional-cases.json"
        added_case = verified_case("retry-result", "train", expected={"retried": True})
        self.write_json(additions, [added_case])
        output = self.project / "business-eval-expanded.json"
        original_bytes = self.source.read_bytes()
        code, result = self.run_helper("merge", "--add", additions, "--output", output)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["caseCount"], 4)
        self.assertEqual(self.source.read_bytes(), original_bytes)
        self.assertEqual(json.loads(output.read_text())["cases"], self.book["cases"] + [added_case])
        output_bytes = output.read_bytes()
        self.assert_failure(self.run_helper("merge", "--add", additions, "--output", output),
                            "new destination")
        self.assertEqual(output.read_bytes(), output_bytes)
        self.assert_failure(self.run_helper("merge", "--add", additions, "--output", self.source),
                            "new destination")
        self.assertEqual(self.source.read_bytes(), original_bytes)

    def test_merge_rejects_duplicate_ids_and_normalized_inputs_without_losing_cases(self):
        duplicate_id = verified_case("empty-result", "train", expected="different answer")
        duplicate_id["input"] = {"request": "different request"}
        duplicate_input = verified_case("different-case", "train")
        # JSON 对象的键顺序不同不代表新的业务输入。
        duplicate_input["input"] = {"options": {"active": True, "limit": 3}, "request": "empty-result"}
        additions = self.project / "additional-cases.json"
        output = self.project / "expanded.json"
        source_bytes = self.source.read_bytes()
        for case, message in ((duplicate_id, "Duplicate case id"),
                              (duplicate_input, "Duplicate normalized input")):
            with self.subTest(reason=message):
                self.write_json(additions, [case])
                self.assert_failure(self.run_helper("merge", "--add", additions, "--output", output), message)
                self.assertFalse(output.exists())
                self.assertEqual(self.source.read_bytes(), source_bytes)

    def test_merge_rejects_duplicates_within_the_additions(self):
        additions = self.project / "additional-cases.json"
        new_case = verified_case("new-result", "train")
        self.write_json(additions, [new_case, new_case])
        output = self.project / "expanded.json"
        self.assert_failure(self.run_helper("merge", "--add", additions, "--output", output),
                            "Duplicate case id")
        self.assertFalse(output.exists())

    def test_related_group_cannot_cross_splits_during_inspect_merge_or_freeze(self):
        related = verified_case("same-request-retry", "test")
        related["group"] = self.book["cases"][0]["group"]
        additions = self.project / "additional-cases.json"
        self.write_json(additions, [related])
        output = self.project / "expanded.json"
        self.assert_failure(self.run_helper("merge", "--add", additions, "--output", output),
                            "cannot cross splits")
        self.assertFalse(output.exists())
        self.book["cases"].append(related)
        self.write_json(self.source, self.book)
        self.assert_failure(self.run_helper("inspect"), "cannot cross splits")
        self.assert_failure(self.freeze(), "cannot cross splits")
        self.assertFalse(self.bundle.exists())

    def test_related_cases_in_the_same_split_are_allowed(self):
        related = verified_case("same-request-retry", "train")
        related["group"] = self.book["cases"][0]["group"]
        self.book["cases"].append(related)
        self.write_json(self.source, self.book)
        result = self.assert_frozen()
        self.assertEqual(result["splits"], {"train": 2, "validation": 1, "test": 1})

    def test_freeze_requires_each_of_the_three_splits(self):
        for absent_split in ("train", "validation", "test"):
            with self.subTest(absent_split=absent_split):
                book = deepcopy(self.book)
                book["cases"] = [case for case in book["cases"] if case["split"] != absent_split]
                self.write_json(self.source, book)
                self.assert_failure(self.freeze(), "nonempty splits")
                self.assertFalse(self.bundle.exists())

    def test_freeze_rejects_an_unassigned_or_explicitly_draft_case(self):
        for field, value in (("split", None), ("expectedStatus", "draft")):
            with self.subTest(field=field):
                book = deepcopy(self.book)
                book["cases"][0][field] = value
                self.write_json(self.source, book)
                self.assert_failure(self.freeze(), "verified expectations")
                self.assertFalse(self.bundle.exists())

    def test_freeze_records_version_conditions_and_scorer_fingerprint(self):
        result = self.assert_frozen()
        self.assertEqual(result["version"], "v1")
        self.assertEqual(result["environment"], self.book["environment"])
        self.assertEqual(result["scoring"], self.book["scoring"])
        self.assertEqual(result["scorer"], {
            "path": "score.py", "sha256": hashlib.sha256(self.scorer.read_bytes()).hexdigest()})
        self.assertRegex(result["digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(set(result["files"]), {"casebook.json", "examples.json"})
        self.assertEqual({p.name for p in self.bundle.iterdir()},
                         {"manifest.json", "casebook.json", "examples.json"})
        self.assertEqual(json.loads((self.bundle / "casebook.json").read_text()), self.book)
        examples = json.loads((self.bundle / "examples.json").read_text())
        self.assertEqual(len(examples), 3)
        for example in examples:
            self.assertEqual(set(example), {"id", "input", "expected", "split"})

    def test_freeze_never_overwrites_a_version_directory(self):
        self.assert_frozen()
        before = {path.name: path.read_bytes() for path in self.bundle.iterdir()}
        self.book["cases"][0]["expected"] = "a later answer"
        self.write_json(self.source, self.book)
        self.assert_failure(self.freeze(version="v2"), "new destination")
        self.assertEqual({path.name: path.read_bytes() for path in self.bundle.iterdir()}, before)
        empty = self.project / "reserved-version"
        empty.mkdir()
        self.assert_failure(self.freeze(output=empty), "new destination")
        self.assertEqual(list(empty.iterdir()), [])

    def test_freeze_requires_conditions_and_a_scorer_inside_the_project(self):
        for field, value in (("environment", {}), ("scoring", " ")):
            with self.subTest(field=field):
                book = deepcopy(self.book)
                book[field] = value
                self.write_json(self.source, book)
                self.assert_failure(self.freeze(), field)
                self.assertFalse(self.bundle.exists())
        self.write_json(self.source, self.book)
        outside = self.root / "outside-score.py"
        outside.write_text(SCORER, encoding="utf-8")
        self.assert_failure(self.freeze(scorer=outside), "inside the current project")
        self.assertFalse(self.bundle.exists())

    def test_load_rejects_modified_examples_casebook_and_manifest(self):
        self.assert_frozen()
        mutations = [
            ("examples.json", lambda data: data[0].update(expected="unreviewed answer"), "cases changed"),
            ("casebook.json", lambda data: data["cases"][0].update(evidence="new evidence"), "cases changed"),
            ("manifest.json", lambda data: data.update(version="v2"), "manifest digest mismatch"),
        ]
        for name, mutate, message in mutations:
            with self.subTest(file=name):
                path = self.bundle / name
                original = path.read_bytes()
                data = json.loads(original)
                mutate(data)
                self.write_json(path, data)
                self.assert_failure(self.load_bundle(), message)
                path.write_bytes(original)
        code, result = self.load_bundle()
        self.assertEqual(code, 0, result)

    def test_load_rejects_changed_or_missing_scorer(self):
        self.assert_frozen()
        self.scorer.write_text("def score(expected, actual):\n    return 1.0\n", encoding="utf-8")
        self.assert_failure(self.load_bundle(), "Scorer file changed")
        self.scorer.unlink()
        self.assert_failure(self.load_bundle(), "Scorer file must be readable")

    def test_load_rejects_incomplete_bundle(self):
        self.assert_frozen()
        (self.bundle / "manifest.json").unlink()
        self.assert_failure(self.load_bundle(), "Cannot read a valid finite JSON document")

    def test_duplicate_json_keys_and_nonfinite_answers_are_rejected(self):
        valid = self.source.read_text()
        self.source.write_text(valid.replace('"schemaVersion": 1',
                                             '"schemaVersion": 1, "schemaVersion": 1', 1))
        self.assert_failure(self.run_helper("inspect"), "valid finite JSON document")
        self.book["cases"][0]["expected"] = float("nan")
        self.write_json(self.source, self.book)
        self.assert_failure(self.freeze(), "valid finite JSON document")
        self.assertFalse(self.bundle.exists())

    def test_experiment_runs_frozen_sqlite_cases_and_links_benchmark_receipt_to_record(self):
        self.prepare_experiment()
        frozen = self.assert_frozen()
        code, result = self.run_experiment()
        self.assertEqual(code, 0, result)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["baselineComplete"])
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 5)
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        self.assertEqual(result["trials"][0]["validation"]["passed"], 0)
        self.assertEqual(result["trials"][1]["validation"]["passed"], 1)
        benchmark = result["benchmark"]
        self.assertEqual(set(benchmark), {"id", "version", "digest", "receipt", "manifest"})
        self.assertEqual({key: benchmark[key] for key in ("id", "version", "digest")},
                         {key: frozen[key] for key in ("id", "version", "digest")})
        receipt = json.loads(Path(benchmark["receipt"]).read_text())
        self.assertEqual(receipt, {"experimentId": result["id"],
                                  **{key: value for key, value in benchmark.items() if key != "receipt"}})
        manifest = json.loads(Path(benchmark["manifest"]).read_text())
        self.assertEqual(manifest, json.loads((self.bundle / "manifest.json").read_text()))
        record = json.loads(Path(result["record"]).read_text())
        self.assertEqual(record["id"], receipt["experimentId"])
        self.assertEqual(record["agentId"], "sqlite-business")
        self.assertIn("business-eval@v1", record["name"])
        self.assertIsNone(record["candidateTest"])
        self.assertIsNone(record["baselineTest"])
        self.assertEqual(record["stage"], "search")
        self.assertFalse(record.get("adopted", False))
        self.assertFalse((self.workspace / "active-prompts.json").exists())
        calls = [json.loads(line) for line in (self.project / "runner-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 4)
        self.assertEqual([item["output"]["value"] for item in calls if item["prompt"] == "SUM"], [6, 8])
        self.assertEqual([item["output"]["value"] for item in calls if item["prompt"] == "COUNT"], [2, 2])
        self.assertFalse(any(item["output"]["marker"] == "TEST_BENCHMARK_PRIVATE" for item in calls))
        # v1 工厂本身读取过旧 test，因此不能把它升级成独立盲测。搜索未执行这些题。
        self.assertIn("TEST_BENCHMARK_PRIVATE", Path(result["runtimeLog"]).read_text())

    def test_v2_development_bundle_has_no_holdout_and_preserves_source_metadata(self):
        self.prepare_experiment()
        self.book.update(schemaVersion=2, role="development")
        self.book["cases"] = [case for case in self.book["cases"] if case["split"] != "test"]
        self.book["cases"][0]["tags"].append("critical")
        self.write_json(self.source, self.book)
        frozen = self.assert_frozen()
        self.assertEqual(frozen["role"], "development")
        self.assertEqual(frozen["splits"], {"train": 1, "validation": 1})
        code, loaded = self.load_bundle()
        self.assertEqual(code, 0, loaded)
        for example, source in zip(loaded["examples"], self.book["cases"]):
            self.assertEqual(example["group_id"], source["group"])
            self.assertEqual(example["source_id"], source["source"]["ref"])
            self.assertEqual(example["exposure"], "development")
            self.assertEqual(example["critical"], "critical" in source["tags"])
        # 验收入口不能把开发包当验收包读走。
        self.assert_failure(self.load_bundle("holdout"), "Expected a holdout bundle")
        code, result = self.run_experiment()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 5)
        self.assertIsNone(result["candidateTest"])

    def test_v2_holdout_bundle_is_unseen_and_cannot_be_used_for_search(self):
        self.prepare_experiment()
        self.book.update(schemaVersion=2, role="holdout")
        self.book["cases"] = [case for case in self.book["cases"] if case["split"] == "test"]
        self.write_json(self.source, self.book)
        frozen = self.assert_frozen()
        self.assertEqual(frozen["role"], "holdout")
        # 默认读取必须在解析案例正文之前拒绝：搜索侧不能靠 load_bundle 拿到验收题。
        # 这是有意收紧的语义；只有验收方显式声明 role="holdout" 才读得出。
        self.assert_failure(self.load_bundle(), "explicit role='holdout'")
        code, loaded = self.load_bundle("holdout")
        self.assertEqual(code, 0, loaded)
        self.assertEqual(loaded["examples"][0]["exposure"], "unseen")
        # 反向：开发入口拿不到验收包，验收入口也拿不到开发包。
        self.assert_failure(self.load_bundle("development"), "explicit role='holdout'")
        code, result = self.run_experiment()
        self.assertEqual(code, 1, result)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["errorType"], "BenchmarkError")
        log = Path(result["runtimeLog"]).read_text()
        self.assertIn("holdout", log.lower())
        self.assertNotIn("factory frozen examples:", log)
        self.assertFalse((self.project / "runner-calls.jsonl").exists())

    def test_v2_role_and_source_rules_block_cross_split_leakage(self):
        self.book.update(schemaVersion=2, role="development")
        self.write_json(self.source, self.book)
        self.assert_failure(self.freeze(), "separate bundles")
        self.book["cases"] = [case for case in self.book["cases"] if case["split"] != "test"]
        self.book["cases"][1]["source"] = deepcopy(self.book["cases"][0]["source"])
        self.write_json(self.source, self.book)
        self.assert_failure(self.run_helper("inspect"), "one source cannot cross splits")
        self.assert_failure(self.freeze(), "one source cannot cross splits")
        self.assertFalse(self.bundle.exists())

    def test_v2_near_identical_text_requires_provenance_review_before_freeze(self):
        self.book.update(schemaVersion=2, role="development")
        self.book["cases"] = [case for case in self.book["cases"] if case["split"] != "test"]
        self.book["cases"][0]["input"] = "What is the NET revenue?"
        self.book["cases"][1]["input"] = "ｗｈａｔ  is the net revenue!"
        self.write_json(self.source, self.book)
        code, inspected = self.run_helper("inspect")
        self.assertEqual(code, 0, inspected)
        self.assertTrue(inspected["warnings"])
        self.assert_failure(self.freeze(), "resolve provenance")
        self.assertFalse(self.bundle.exists())

    def test_experiment_rejects_factory_cases_different_from_the_frozen_benchmark(self):
        self.prepare_experiment()
        self.assert_frozen()
        self.assert_experiment_rejected_before_runner(self.run_experiment(factory="mismatched_agent"),
                                                     "Factory examples must match the frozen benchmark")

    def test_experiment_rejects_scorer_changes_before_loading_factory_or_running(self):
        self.prepare_experiment()
        self.assert_frozen()
        self.scorer.write_text(EVALUATOR.replace("expected == prediction.output", "True"), encoding="utf-8")
        outcome = self.run_experiment()
        self.assert_experiment_rejected_before_runner(outcome, "Scorer file changed")
        self.assertNotIn("factory frozen examples:", Path(outcome[1]["runtimeLog"]).read_text())

    def test_experiment_rejects_fingerprinting_an_unrelated_scorer_file(self):
        self.prepare_experiment()
        unrelated_scorer = self.project / "unrelated_scorer.py"
        # 即使两个文件内容相同，冻结时也必须登记实际 evaluator 所在的文件。
        unrelated_scorer.write_text(EVALUATOR, encoding="utf-8")
        code, frozen = self.freeze(scorer=unrelated_scorer)
        self.assertEqual(code, 0, frozen)
        self.assert_experiment_rejected_before_runner(self.run_experiment(),
                                                     "Scorer file must define the registered evaluator function")


if __name__ == "__main__":
    unittest.main()
