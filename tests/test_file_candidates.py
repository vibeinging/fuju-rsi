"""配置候选只描述限定范围的改动，不改变原项目或授予采用资格。"""
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fuju_rsi import file_candidates
from fuju_rsi.file_candidates import FileBaseline, FileCandidate, FileCandidateError, FileTarget


class FileCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("dictionary.json", '{"aliases":{"收入":"sales"},"formula":"sum(amount)"}')
        self.write("rules.md", "只查询授权项目。\n")
        self.write("snapshot.txt", "业务数据版本 v1\n")

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def baseline(self, pointers=("/aliases",)):
        return FileBaseline.capture(
            self.root,
            [FileTarget("dictionary.json", "dictionary", pointers),
             FileTarget("rules.md", "project-rules")],
            context_files=["snapshot.txt"],
        )

    def candidate(self, baseline=None, **kwargs):
        return FileCandidate.propose(
            self.baseline() if baseline is None else baseline,
            {"dictionary.json": '{"aliases":{"收入":"sales","营收":"sales"},"formula":"sum(amount)"}'},
            rationale="补充同义词，不改变计算公式。", **kwargs
        )

    def test_baseline_and_candidate_have_stable_content_identity(self):
        first = self.baseline()
        second = FileBaseline.capture(
            self.root, list(reversed(first.targets)), context_files=first.context_files)
        self.assertEqual(first.digest, second.digest)
        candidate = self.candidate(first)
        self.assertEqual(candidate.digest, self.candidate(second).digest)
        self.assertEqual(candidate.baseline_digest, first.digest)
        self.assertEqual(len(candidate.digest), 64)
        self.assertEqual(candidate.files["snapshot.txt"], "业务数据版本 v1\n")
        self.assertEqual((self.root / "dictionary.json").read_text(encoding="utf-8"),
                         first.files["dictionary.json"])
        self.assertEqual(first.as_candidate().files, first.files)

    def test_identity_includes_scope_kind_rationale_and_format_version(self):
        first = self.baseline()
        scope = self.baseline(pointers=("/aliases/收入",))
        self.assertNotEqual(first.digest, scope.digest)
        different_kind = FileBaseline.capture(
            self.root, [FileTarget("dictionary.json", "metric-context", ("/aliases",)),
                        FileTarget("rules.md", "project-rules")], context_files=["snapshot.txt"])
        self.assertNotEqual(first.digest, different_kind.digest)
        candidate = self.candidate(first)
        other = FileCandidate.propose(first, {"dictionary.json": candidate.files["dictionary.json"]},
                                      rationale="经核对的同义词补充。")
        self.assertNotEqual(candidate.digest, other.digest)
        document = candidate.to_dict()
        document["schemaVersion"] = 2
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(json.dumps(document), first)

    def test_frozen_targets_and_read_only_file_mappings(self):
        target = FileTarget("rules.md", "project-rules")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            target.path = "other.md"
        baseline = self.baseline()
        candidate = self.candidate(baseline)
        for item in (baseline, candidate):
            with self.assertRaises(TypeError):
                item.files["snapshot.txt"] = "overwritten"
        document = candidate.to_dict()
        document["files"]["snapshot.txt"] = "overwritten"
        self.assertEqual(candidate.files["snapshot.txt"], "业务数据版本 v1\n")

    def test_rejects_noncanonical_or_escaping_paths(self):
        for path in ("", "/rules.md", "../rules.md", "a/../rules.md", "./rules.md",
                     "a//rules.md", "rules.md/", "C:/rules.md", "C:rules.md",
                     "a\\rules.md", "a/./rules.md", "a\x00b"):
            with self.subTest(path=path), self.assertRaises(FileCandidateError):
                FileTarget(path, "project-rules")

    def test_rejects_duplicate_and_case_colliding_paths(self):
        for targets, context in (
            ([FileTarget("rules.md", "project-rules")] * 2, []),
            ([FileTarget("rules.md", "project-rules"), FileTarget("RULES.md", "project-rules")], []),
            ([FileTarget("rules.md", "project-rules")], ["rules.md"]),
            ([FileTarget("Config/a.json", "dictionary"), FileTarget("config/b.json", "dictionary")], []),
            ([FileTarget("caf\u00e9.md", "project-rules"), FileTarget("cafe\u0301.md", "project-rules")], []),
            ([FileTarget("rules.md", "project-rules")], ["snapshot.txt", "SNAPSHOT.txt"]),
        ):
            with self.subTest(targets=targets, context=context), self.assertRaises(FileCandidateError):
                FileBaseline.capture(self.root, targets, context_files=context)

    def test_rejects_unlisted_files_and_read_only_context_replacements(self):
        baseline = self.baseline()
        for path in ("new.json", "snapshot.txt", "../rules.md"):
            with self.subTest(path=path), self.assertRaises(FileCandidateError):
                FileCandidate.propose(baseline, {path: "replacement"}, rationale="修改测试")

    def test_rejects_meaning_changing_and_unspecified_semantics(self):
        for semantics in ("meaning-changing", "", "unknown", None):
            with self.subTest(semantics=semantics), self.assertRaises(FileCandidateError):
                self.candidate(semantics=semantics)

    def test_requires_supported_kind_and_nonempty_rationale(self):
        for kind in ("code", None, []):
            with self.subTest(kind=kind), self.assertRaises(FileCandidateError):
                FileTarget("rules.md", kind)
        baseline = self.baseline()
        for rationale in ("", " \n", None, 1):
            with self.subTest(rationale=rationale), self.assertRaises(FileCandidateError):
                FileCandidate.propose(baseline, {}, rationale=rationale)

    def test_json_changes_outside_pointer_are_rejected(self):
        baseline = self.baseline()
        for text in (
            '{"aliases":{"收入":"sales"},"formula":"sum(profit)"}',
            '{"aliases":{"收入":"sales"}}',
            '{"aliases":{"收入":"sales"},"formula":"sum(amount)","unit":"万元"}',
        ):
            with self.subTest(text=text), self.assertRaises(FileCandidateError):
                FileCandidate.propose(baseline, {"dictionary.json": text}, rationale="修改测试")

    def test_json_pointer_escaping_and_subtree_changes(self):
        self.write("dictionary.json", '{"a/b":{"~key":{"original":1}},"x":1}')
        baseline = self.baseline(pointers=("/a~1b/~0key",))
        candidate = FileCandidate.propose(
            baseline, {"dictionary.json": '{"a/b":{"~key":{"replacement":[1,2]}},"x":1}'},
            rationale="仅修改许可子树。")
        self.assertIn("replacement", candidate.files["dictionary.json"])
        for pointer in ("aliases", "/a~2b", "/a~"):
            with self.subTest(pointer=pointer), self.assertRaises(FileCandidateError):
                FileTarget("dictionary.json", "dictionary", (pointer,))

    def test_json_arrays_and_scalar_type_changes_respect_exact_scope(self):
        self.write("dictionary.json", '{"values":[1,2,3],"fixed":true}')
        baseline = self.baseline(pointers=("/values/0",))
        FileCandidate.propose(baseline, {"dictionary.json": '{"values":[4,2,3],"fixed":true}'},
                              rationale="仅修改许可元素。")
        for text in ('{"values":[2,3],"fixed":true}',
                     '{"values":[4,2,3],"fixed":1}'):
            with self.subTest(text=text), self.assertRaises(FileCandidateError):
                FileCandidate.propose(baseline, {"dictionary.json": text}, rationale="修改测试")

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        baseline = self.baseline()
        for text in ('{"aliases":{},"aliases":{},"formula":"sum(amount)"}',
                     '{"aliases":{"x":NaN},"formula":"sum(amount)"}',
                     '{"aliases":{"x":Infinity},"formula":"sum(amount)"}',
                     '{"aliases":{"x":1e400},"formula":"sum(amount)"}'):
            with self.subTest(text=text), self.assertRaises(FileCandidateError):
                FileCandidate.propose(baseline, {"dictionary.json": text}, rationale="修改测试")
        self.write("dictionary.json", '{"aliases":{},"aliases":{}}')
        with self.assertRaises(FileCandidateError):
            self.baseline()

    def test_round_trip_revalidates_baseline_scope_and_digest(self):
        baseline = self.baseline()
        candidate = self.candidate(baseline)
        loaded = FileCandidate.from_json(candidate.to_json(), baseline)
        self.assertEqual((loaded.digest, loaded.files), (candidate.digest, candidate.files))
        for field, value in (("digest", "0" * 64), ("baselineDigest", "0" * 64),
                             ("semantics", "meaning-changing"), ("rationale", "被修改的理由")):
            document = candidate.to_dict()
            document[field] = value
            with self.subTest(field=field), self.assertRaises(FileCandidateError):
                FileCandidate.from_json(json.dumps(document, ensure_ascii=False), baseline)
        document = candidate.to_dict()
        document["files"]["snapshot.txt"] = "changed"
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(json.dumps(document), baseline)
        document = candidate.to_dict()
        document["targets"][0]["jsonPointers"] = [""]
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(json.dumps(document), baseline)

    def test_serialized_duplicate_keys_and_unknown_fields_are_rejected(self):
        baseline = self.baseline()
        candidate = self.candidate(baseline)
        document = candidate.to_json().rstrip()
        duplicate = document[:-1] + ',"digest":"' + candidate.digest + '"}'
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(duplicate, baseline)
        changed = candidate.to_dict()
        changed["adoptable"] = True
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(json.dumps(changed), baseline)

    def test_recomputed_digest_cannot_authorize_out_of_scope_content(self):
        baseline = self.baseline()
        candidate = self.candidate(baseline)
        for edit in ("formula", "new-file", "deleted-file", "read-only", "target-scope"):
            document = candidate.to_dict()
            if edit == "formula":
                document["files"]["dictionary.json"] = '{"aliases":{},"formula":"sum(profit)"}'
            elif edit == "new-file":
                document["files"]["new.txt"] = "unlisted"
            elif edit == "deleted-file":
                del document["files"]["rules.md"]
            elif edit == "read-only":
                document["files"]["snapshot.txt"] = "other version"
            else:
                document["targets"][0]["jsonPointers"] = [""]
            payload = {key: value for key, value in document.items() if key != "digest"}
            document["digest"] = hashlib.sha256(json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")).hexdigest()
            with self.subTest(edit=edit), self.assertRaises(FileCandidateError):
                FileCandidate.from_json(json.dumps(document, ensure_ascii=False), baseline)

    def test_loading_candidate_rechecks_live_original(self):
        baseline = self.baseline()
        serialized = self.candidate(baseline).to_json()
        self.write("rules.md", "已改变的业务规则\n")
        with self.assertRaises(FileCandidateError):
            FileCandidate.from_json(serialized, baseline)

    def test_baseline_drift_missing_files_and_context_drift_are_detected(self):
        for path in ("dictionary.json", "snapshot.txt"):
            baseline = self.baseline()
            original = baseline.files[path]
            self.write(path, original + "\n")
            with self.subTest(path=path), self.assertRaises(FileCandidateError):
                baseline.assert_current()
            with self.assertRaises(FileCandidateError):
                self.candidate(baseline)
            self.write(path, original)
        baseline = self.baseline()
        (self.root / "rules.md").unlink()
        with self.assertRaises(FileCandidateError):
            baseline.assert_current()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_leaf_parent_and_root_are_rejected(self):
        external = self.root / "external.md"
        external.write_text("outside", encoding="utf-8")
        (self.root / "leaf.md").symlink_to(external)
        (self.root / "parent").symlink_to(self.root, target_is_directory=True)
        for path in ("leaf.md", "parent/rules.md"):
            with self.subTest(path=path), self.assertRaises(FileCandidateError):
                FileBaseline.capture(self.root, [FileTarget(path, "project-rules")])
        link = self.root / "root-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(FileCandidateError):
            FileBaseline.capture(link, [FileTarget("rules.md", "project-rules")])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_baseline_rejects_file_replaced_by_symlink(self):
        baseline = self.baseline()
        original = self.root / "rules.md"
        destination = self.root / "copied.md"
        destination.write_text(baseline.files["rules.md"], encoding="utf-8")
        original.unlink()
        original.symlink_to(destination)
        with self.assertRaises(FileCandidateError):
            baseline.assert_current()

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_baseline_rejects_parent_directory_replaced_by_symlink(self):
        self.write("config/rules.md", "original")
        baseline = FileBaseline.capture(self.root, [FileTarget("config/rules.md", "project-rules")])
        (self.root / "config").rename(self.root / "moved")
        (self.root / "config").symlink_to(self.root / "moved", target_is_directory=True)
        with self.assertRaises(FileCandidateError):
            baseline.assert_current()

    def test_file_size_total_size_utf8_and_regular_file_limits(self):
        self.write("large.txt", "x" * (1024 * 1024 + 1))
        with self.assertRaises(FileCandidateError):
            FileBaseline.capture(self.root, [FileTarget("large.txt", "project-rules")])
        targets = []
        for number in range(5):
            name = "part%d.txt" % number
            self.write(name, "x" * (1024 * 1024))
            targets.append(FileTarget(name, "project-rules"))
        with self.assertRaises(FileCandidateError):
            FileBaseline.capture(self.root, targets)
        (self.root / "binary.txt").write_bytes(b"\xff")
        for name in ("binary.txt", "directory"):
            if name == "directory":
                (self.root / name).mkdir()
            with self.subTest(name=name), self.assertRaises(FileCandidateError):
                FileBaseline.capture(self.root, [FileTarget(name, "project-rules")])
        baseline = self.baseline()
        with self.assertRaises(FileCandidateError):
            FileCandidate.propose(baseline, {"rules.md": "x" * (1024 * 1024 + 1)}, rationale="大文件")
        with self.assertRaises(FileCandidateError):
            FileCandidate.propose(baseline, {"rules.md": "\ud800"}, rationale="无效 UTF-8")
        with self.assertRaises(FileCandidateError):
            FileCandidate.propose(baseline, {"rules.md": "中" * (512 * 1024)}, rationale="按字节限额")

    def test_capture_stops_reading_when_total_budget_is_exceeded(self):
        targets = [FileTarget("part%d.txt" % number, "project-rules") for number in range(20)]
        with mock.patch.object(file_candidates, "_read_file", return_value="x" * (1024 * 1024)) as read:
            with self.assertRaises(FileCandidateError):
                FileBaseline.capture(self.root, targets)
        self.assertEqual(read.call_count, 5)

    def test_empty_declaration_still_rechecks_root_existence(self):
        empty = self.root / "empty"
        empty.mkdir()
        baseline = FileBaseline.capture(empty, [])
        empty.rmdir()
        with self.assertRaises(FileCandidateError):
            baseline.assert_current()


if __name__ == "__main__":
    unittest.main()
