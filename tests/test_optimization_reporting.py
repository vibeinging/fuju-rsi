"""报告是普通交付文件；不得泄漏案例，或把开发结果变成验收资格。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fuju_rsi.reporting import write_report


def batch(score, *, complete=True):
    cases = [{"id": "PRIVATE_CASE_ID", "input": "PRIVATE_INPUT", "expected": "PRIVATE_EXPECTED",
              "output": "PRIVATE_OUTPUT", "reason": "PRIVATE_REASON", "error": None}]
    return {"score": score, "passed": 1, "total": 1 if complete else 2, "latencyMs": 12.5,
            "tokens": None, "costUsd": None, "cases": cases}


def record():
    return {"id": "experiment-1", "agentId": "agent-1", "name": "业务验收", "status": "completed",
            "stage": "search", "searchComplete": True, "evidenceStatus": "not_verified", "adoptable": False,
            "baselinePrompt": "原版\r\n保留空格  ", "bestPrompt": "选中候选\r\n精确内容  ",
            "selectedTrialId": "trial-1", "usedCalls": 7, "config": {"maxCalls": 10},
            "trials": [{"id": "baseline", "prompt": "原版\r\n保留空格  ", "training": batch(.2), "validation": batch(.3)},
                       {"id": "trial-1", "label": "PRIVATE_LABEL", "prompt": "选中候选\r\n精确内容  ", "training": batch(.9), "validation": batch(.8)},
                       {"id": "trial-2", "prompt": "最后一个失败候选", "training": batch(.4), "validation": batch(.2)}],
            "message": "PRIVATE_MESSAGE", "previousActive": "PRIVATE_ACTIVE", "callbackSnapshot": "PRIVATE_CALLBACK",
            "verificationReceipt": {"body": {"raw": "PRIVATE_RECEIPT"}}, "frozenCandidate": "PRIVATE_FROZEN"}


def qualified():
    value = record()
    value.update(stage="verification", adoptable=True, evidenceStatus="improved",
                 acceptancePolicyVersion="paired-group-hoeffding-v1", verificationId="verification-1",
                 verificationUsedCalls=180, verificationPlannedCalls=180, verificationTokens=1800,
                 verificationCostUsd=.018,
                 evidence={"independentGroups": 30, "caseCount": 30, "runCount": 180,
                           "baselineScore": .1, "candidateScore": .9, "delta": .8,
                           "interval": {"lower": .3, "upper": 1, "confidence": .95, "method": "paired-group-hoeffding-v1"},
                           "reasons": ["PRIVATE_VERIFICATION_REASON"], "cases": ["PRIVATE_HOLDOUT"]})
    return value


class ReportingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output = Path(self.tmp.name) / "nested" / "report"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self, value):
        paths = write_report(value, self.output)
        return paths, json.loads(Path(paths["result"]).read_text()), Path(paths["report"]).read_text()

    def test_search_exports_selected_candidate_exactly_without_verification(self):
        value = record()
        paths, result, markdown = self.read(value)
        self.assertEqual(Path(paths["baselinePrompt"]).read_bytes(), value["baselinePrompt"].encode())
        self.assertEqual(Path(paths["candidatePrompt"]).read_bytes(), value["trials"][1]["prompt"].encode())
        self.assertIn("+选中候选", Path(paths["promptDiff"]).read_text())
        self.assertIn("No newline at end of file", Path(paths["promptDiff"]).read_text())
        self.assertEqual(result["development"]["candidate"]["validation"]["score"], .8)
        self.assertEqual(result["schemaVersion"], 1)
        self.assertTrue(result["snapshotOnly"])
        self.assertFalse(result["adoptable"])
        self.assertIsNone(paths["verifiedPrompt"])
        self.assertIn("尚未通过独立验收", markdown)
        self.assertTrue(all(Path(value).is_absolute() for value in paths.values() if value is not None))

    def test_private_case_and_receipt_details_are_never_copied(self):
        paths, result, markdown = self.read(qualified())
        for path in self.output.iterdir():
            self.assertNotIn("PRIVATE_", path.read_text(), path.name)
        self.assertEqual(len(result["trials"]), 3)
        self.assertEqual(set(result["trials"][1]), {"id", "selected", "training", "validation"})
        self.assertNotIn("reason", json.dumps(result))
        self.assertNotIn("bestPrompt", result)
        self.assertNotIn("baselinePrompt", result)

    def test_complete_verified_export_contains_plain_prompt_and_counts(self):
        value = qualified()
        before = deepcopy(value)
        paths, result, markdown = self.read(value)
        self.assertTrue(result["adoptable"])
        self.assertEqual(Path(paths["verifiedPrompt"]).read_bytes(), value["bestPrompt"].encode())
        self.assertEqual(result["verification"]["independentGroups"], 30)
        self.assertEqual(result["verification"]["runCount"], 180)
        self.assertEqual(result["usage"]["verification"]["usedCalls"], 180)
        self.assertEqual(result["usage"]["verification"]["costUsd"], .018)
        self.assertIn("重复运行次数不是独立样本数", markdown)
        self.assertIn("不是长期有效的验收凭据", markdown)
        self.assertIn("不写入产品配置", markdown)
        self.assertEqual(value, before)

    def test_each_qualification_gate_is_required(self):
        gates = {"stage": "search", "status": "failed", "adoptable": False, "evidenceStatus": "insufficient",
                 "acceptancePolicyVersion": None, "verificationId": "", "bestPrompt": "不匹配",
                 "selectedTrialId": "missing", "searchComplete": False}
        for key, change in gates.items():
            with self.subTest(key=key):
                value = qualified()
                value[key] = change
                paths = write_report(value, Path(self.tmp.name) / key)
                self.assertIsNone(paths["verifiedPrompt"])
                self.assertFalse(json.loads(Path(paths["result"]).read_text())["adoptable"])

    def test_no_improvement_produces_only_baseline_and_report(self):
        value = record()
        value.update(selectedTrialId="baseline", bestPrompt=value["baselinePrompt"])
        paths, result, markdown = self.read(value)
        self.assertIsNone(paths["candidatePrompt"])
        self.assertIsNone(paths["promptDiff"])
        self.assertIsNone(paths["verifiedPrompt"])
        self.assertIsNone(result["development"]["candidate"])
        self.assertIn("没有选出更好的候选", markdown)
        self.assertEqual(len(list(self.output.iterdir())), 3)

    def test_partial_and_failed_results_remain_diagnostic(self):
        for status in ("completed", "failed", "cancelled", "interrupted", "running"):
            with self.subTest(status=status):
                value = record()
                value.update(status=status, searchComplete=False)
                value["trials"][1]["validation"] = batch(.4, complete=False)
                paths = write_report(value, Path(self.tmp.name) / status)
                result = json.loads(Path(paths["result"]).read_text())
                markdown = Path(paths["report"]).read_text()
                self.assertFalse(result["development"]["candidate"]["validation"]["complete"])
                self.assertIsNone(paths["verifiedPrompt"])
                self.assertIn("未完整完成" if status == "completed" else "未完成或执行异常", markdown)

    def test_legacy_adoptable_does_not_get_verified_artifact(self):
        value = qualified()
        value.pop("stage")
        paths, result, markdown = self.read(value)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["stage"], "legacy")
        self.assertEqual(result["evidenceStatus"], "not_verified")
        self.assertIsNone(result["verification"])
        self.assertIsNone(paths["verifiedPrompt"])
        self.assertIn("旧版实验记录", markdown)

    def test_unknown_and_invalid_metrics_are_null_instead_of_zero(self):
        value = qualified()
        value["evidence"].update(baselineScore=None, candidateScore=float("nan"), delta=float("inf"), independentGroups=True)
        value["evidence"]["interval"].update(lower=None, confidence=".95")
        value["verificationTokens"] = -1
        value["verificationCostUsd"] = float("nan")
        value["trials"][0]["training"]["score"] = False
        value["usedCalls"] = None
        paths, result, markdown = self.read(value)
        for key in ("baselineScore", "candidateScore", "delta", "independentGroups"):
            self.assertIsNone(result["verification"][key])
        self.assertIsNone(result["usage"]["search"]["usedCalls"])
        self.assertIsNone(result["usage"]["verification"]["costUsd"])
        self.assertIn("保守收益下界：未知", markdown)
        self.assertIn("未知不等于零", markdown)
        self.assertNotIn("NaN", Path(paths["result"]).read_text())
        self.assertIsNone(result["development"]["baseline"]["training"]["score"])

    def test_markdown_metadata_cannot_create_html_or_active_links(self):
        value = record()
        value["name"] = "<script>alert(1)</script>\n# injected | [link](https://example.test)"
        _, _, markdown = self.read(value)
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("\n# injected", markdown)
        self.assertNotIn("[link](https://example.test)", markdown)
        self.assertIn("&lt;script", markdown)

    def test_existing_directory_is_never_overwritten(self):
        self.output.mkdir(parents=True)
        sentinel = self.output / "report.md"
        sentinel.write_text("原有内容")
        with self.assertRaises(FileExistsError):
            write_report(record(), self.output)
        self.assertEqual(sentinel.read_text(), "原有内容")
        self.assertEqual(list(self.output.iterdir()), [sentinel])

    def test_write_failure_removes_only_new_output_directory(self):
        sentinel = Path(self.tmp.name) / "existing.txt"
        sentinel.write_text("保留")
        original = Path.open

        def fail_second(path, *args, **kwargs):
            if path.name == "result.json":
                raise OSError("模拟磁盘写入失败")
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", fail_second):
            with self.assertRaises(OSError):
                write_report(record(), self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(sentinel.read_text(), "保留")

    def test_known_runner_usage_is_separate_from_callback_count(self):
        value = record()
        for trial in value["trials"]:
            for split in ("training", "validation"):
                trial[split].update(tokens=10, costUsd=.01)
        _, result, markdown = self.read(value)
        self.assertEqual(result["usage"]["search"]["tokens"], 60)
        self.assertAlmostEqual(result["usage"]["search"]["costUsd"], .06)
        self.assertEqual(result["usage"]["search"]["usedCalls"], 7)
        self.assertIn("不含 proposer、评分器", markdown)

    def test_verification_outcomes_are_reported_without_success_artifact(self):
        for status, expected in (("no_improvement", "未证明收益"), ("regressed", "发现退步"),
                                 ("insufficient", "证据不足"), ("invalid", "验收无效")):
            with self.subTest(status=status):
                value = qualified()
                value.update(evidenceStatus=status, adoptable=False)
                paths = write_report(value, Path(self.tmp.name) / status)
                self.assertIsNone(paths["verifiedPrompt"])
                self.assertIn(expected, Path(paths["report"]).read_text())

    def test_demo_report_does_not_claim_real_model_improvement(self):
        value = record()
        value["kind"] = "demo"
        _, result, markdown = self.read(value)
        self.assertEqual(result["kind"], "demo")
        self.assertIn("离线 SQLite 与规则候选示例", markdown)
        self.assertIn("不代表云模型或真实产品的收益", markdown)

    def test_identical_prompt_has_no_candidate_artifacts(self):
        value = qualified()
        value["trials"][1]["prompt"] = value["bestPrompt"] = value["baselinePrompt"]
        paths, result, _ = self.read(value)
        self.assertFalse(result["adoptable"])
        self.assertIsNone(paths["candidatePrompt"])
        self.assertIsNone(paths["promptDiff"])
        self.assertIsNone(paths["verifiedPrompt"])

    def test_unselected_failed_and_regressed_candidates_remain_visible(self):
        value = record()
        value.update(selectedTrialId="baseline", bestPrompt=value["baselinePrompt"])
        value["trials"][1]["validation"].update(score=0, passed=0)
        value["trials"][1]["validation"]["cases"][0]["error"] = "PRIVATE_ERROR"
        value["trials"][2]["validation"].update(score=.1, passed=0, latencyMs=45.25)
        _, result, markdown = self.read(value)
        self.assertEqual([trial["id"] for trial in result["trials"]], ["baseline", "trial-1", "trial-2"])
        self.assertEqual([trial["selected"] for trial in result["trials"]], [True, False, False])
        self.assertEqual(result["trials"][1]["validation"]["errors"], 1)
        self.assertEqual(result["trials"][2]["validation"]["score"], .1)
        self.assertEqual(result["trials"][2]["validation"]["passed"], 0)
        self.assertEqual(result["trials"][2]["validation"]["latencyMs"], 45.25)
        self.assertIn("trial\\-1", markdown)
        self.assertIn("trial\\-2 | 否 | 验证 | 0.1 | 0 / 1 | 1 / 1 | 45.25", markdown)
        self.assertIn("不是单次平均延迟", markdown)
        self.assertNotIn("PRIVATE_ERROR", markdown)

    def test_baseline_only_report_does_not_claim_candidates_were_compared(self):
        value = record()
        value.update(selectedTrialId="baseline", bestPrompt=value["baselinePrompt"])
        value["config"]["maxTrials"] = 0
        value["trials"] = value["trials"][:1]
        _, result, markdown = self.read(value)
        self.assertEqual(len(result["trials"]), 1)
        self.assertIn("尚未比较候选", markdown)
        self.assertNotIn("没有选出更好的候选", markdown)

    def test_unknown_interval_method_has_no_claimed_lower_bound(self):
        value = qualified()
        value["evidence"]["interval"]["method"] = "PRIVATE_INTERVAL_METHOD"
        _, result, markdown = self.read(value)
        self.assertIsNone(result["verification"]["interval"]["lower"])
        self.assertIsNone(result["verification"]["interval"]["method"])
        self.assertIn("保守收益下界：未知", markdown)
        self.assertNotIn("PRIVATE_INTERVAL_METHOD", markdown)

    def test_dangling_output_symlink_is_not_followed_or_overwritten(self):
        self.output.parent.mkdir(parents=True)
        target = Path(self.tmp.name) / "other-location"
        try:
            self.output.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("此环境不支持符号链接")
        with self.assertRaises(FileExistsError):
            write_report(record(), self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
