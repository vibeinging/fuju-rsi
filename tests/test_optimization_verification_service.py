"""工作区通过真实验收回执采用与回退；使用本地算术协议样本。

30 个来源组只是独立验收协议的测试输入，不是实际业务样本，也不证明
本测试进程拥有外部账号隔离。成功结论由真实 runner/evaluator 和比较器产生。
"""
from __future__ import annotations

import copy
import http.client
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.core import Example
from fuju_rsi.server import create_server
from fuju_rsi.verification import VerificationSpec, initialize_authority, verify_candidate
from fuju_rsi.workspace import ExperimentError, ExperimentManager, atomic_json, read_active_prompt


APPLICATION = '''from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction

calls = []
resets = 0

def run(prompt, value):
    calls.append((prompt, value["number"]))
    return Prediction(value["number"] * (2 if prompt == "double" else 1))

def evaluate(expected, prediction):
    return Evaluation(float(prediction.output == expected), "arithmetic equality")

def propose(prompt, feedback, index):
    return "double"

def reset():
    global resets
    resets += 1

def build_agent():
    return AgentSpec(id="arithmetic", name="Arithmetic protocol fixture", baseline_prompt="identity",
        examples=[Example("train", {"number": 1}, 2, "train", group_id="train-group", source_id="train-source", exposure="development"),
                  Example("validation", {"number": 2}, 4, "validation", group_id="validation-group", source_id="validation-source", exposure="development")],
        runner=run, evaluator=evaluate, proposer=propose)
'''


class VerificationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="yitrace-verified-workspace-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "application.py"
        self.source.write_text(APPLICATION, encoding="utf-8")
        module_spec = importlib.util.spec_from_file_location("arithmetic_fixture_" + uuid.uuid4().hex, self.source)
        self.application = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(self.application)
        self.agent = self.application.build_agent()
        self.workspace = self.root / "workspace"
        self.registry = self.root / "authority"
        self.key_file = self.root / "authority.key"
        self.authority = "arithmetic-fixture-authority"
        initialize_authority(self.registry, self.authority, self.key_file)
        self.authorities = {self.authority: str(self.key_file)}
        self.environment = {"runtime": "python-stdlib", "model": "none", "scope": "synthetic arithmetic protocol"}
        self.policy = AcceptancePolicy(resamples=100)
        self.manager = self.make_manager()
        self.addCleanup(lambda: self.manager.close())

    def make_manager(self, *, authorities=None, workspace=None):
        return ExperimentManager(workspace or self.workspace, [self.agent],
                                 project_root=self.root,
                                 verification_authorities=self.authorities if authorities is None else authorities)

    def search_and_freeze(self, manager=None):
        manager = manager or self.manager
        started = manager.start(agent_id=self.agent.id, max_trials=1, max_calls=5)
        result = manager.wait(started["id"], timeout=5)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["searchComplete"])
        self.assertFalse(result["adoptable"])
        candidate = manager.freeze(result["id"], source_files=[str(self.source)],
                                   environment=self.environment, policy=self.policy)
        return result, candidate

    def verify(self, candidate, *, offset=1000, registry=None, key_file=None):
        examples = [Example("acceptance-%s" % i, {"number": i}, i * 2, "test",
                            group_id="acceptance-group-%s" % i, source_id="acceptance-source-%s" % i,
                            exposure="unseen") for i in range(offset, offset + 30)]
        spec = VerificationSpec(examples=examples, runner=self.application.run, evaluator=self.application.evaluate,
                                reset=self.application.reset, policy=self.policy, isolation="independent",
                                provenance="synthetic independent-protocol fixture; no external isolation or business claim",
                                environment=self.environment)
        return verify_candidate(candidate, spec, registry=registry or self.registry,
                                signing_key=Path(key_file or self.key_file).read_bytes(), root=self.root)

    def assert_rejected(self, receipt):
        with self.assertRaises(ExperimentError) as error:
            self.manager.import_verification(receipt)
        self.assertEqual(error.exception.status, 409)

    def test_actual_verification_adoption_restart_and_rollback(self):
        search, candidate = self.search_and_freeze()
        start_calls, start_resets = len(self.application.calls), self.application.resets
        receipt = self.verify(candidate)
        self.assertEqual(len(self.application.calls) - start_calls, 180)
        self.assertEqual(self.application.resets - start_resets, 180)
        self.assertEqual(receipt["body"]["evidence"]["independentGroups"], 30)
        self.assertEqual(receipt["body"]["evidence"]["runCount"], 180)
        self.assertEqual(receipt["body"]["evidence"]["baselineScore"], 0)
        self.assertEqual(receipt["body"]["evidence"]["candidateScore"], 1)
        imported = self.manager.import_verification(receipt)
        self.assertTrue(imported["adoptable"])
        self.assertFalse(imported["adopted"])
        self.assertEqual(imported["stage"], "verification")
        self.assertEqual(imported["verificationId"], candidate["id"])
        self.assertEqual(imported["evidenceStatus"], "improved")
        self.assertEqual(imported["trials"], search["trials"])
        self.assertEqual(imported["usedCalls"], 5)
        self.assertEqual(imported["verificationUsedCalls"], 180)
        self.assertNotIn("verificationReceipt", imported)
        self.assertNotIn("frozenCandidate", imported)
        self.assertNotIn("acceptance-source-1000", json.dumps(imported))
        self.assertIsNone(imported["candidateTest"])
        self.assertTrue(self.manager.adopt(search["id"])["adopted"])
        self.assertEqual(read_active_prompt(self.workspace, self.agent.id, "missing"), "double")
        self.manager.close()
        self.manager = self.make_manager()
        reopened = self.manager.get(search["id"])
        self.assertTrue(reopened["adoptable"])
        self.assertTrue(reopened["adopted"])
        self.assertEqual(reopened["evidence"], imported["evidence"])
        self.assertFalse(self.manager.rollback(search["id"])["adopted"])
        self.assertEqual(read_active_prompt(self.workspace, self.agent.id, "missing"), "identity")

    def test_unknown_authority_and_signature_mutation_cannot_change_search(self):
        search, candidate = self.search_and_freeze()
        unknown_registry, unknown_key = self.root / "unknown-authority", self.root / "unknown.key"
        initialize_authority(unknown_registry, "not-registered", unknown_key)
        unknown = self.verify(candidate, registry=unknown_registry, key_file=unknown_key)
        self.assertTrue(unknown["body"]["adoptable"])
        self.assert_rejected(unknown)
        receipt = self.verify(candidate)
        poisoned = copy.deepcopy(receipt)
        poisoned["signature"] = ("1" if poisoned["signature"][0] == "0" else "0") + poisoned["signature"][1:]
        self.assert_rejected(poisoned)
        self.assert_rejected({"body": receipt["body"]})
        self.assertFalse(self.manager.get(search["id"])["adoptable"])
        self.assertEqual(self.manager.get(search["id"])["stage"], "search")

    def test_headless_report_rechecks_trust_without_executing_or_adopting(self):
        search, candidate = self.search_and_freeze()
        self.manager.import_verification(self.verify(candidate))
        before = len(self.application.calls)
        from unittest.mock import patch
        # 导出只读取已保存结果；若它试图启动服务或重跑业务，此测试会失败。
        with patch("socket.socket.bind", side_effect=AssertionError("report must not start a server")):
            first = self.manager.export_report(search["id"], self.root / "trusted-report")
        self.assertEqual(Path(first["verifiedPrompt"]).read_text(), "double")
        self.assertEqual(len(self.application.calls), before)
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], "identity")
        self.manager.verification_authorities = {}
        second = self.manager.export_report(search["id"], self.root / "untrusted-report")
        self.assertIsNone(second["verifiedPrompt"])
        self.assertFalse(json.loads(Path(second["result"]).read_text())["adoptable"])
        self.assertEqual(len(self.application.calls), before)

    def test_valid_receipt_for_another_workspace_experiment_is_rejected(self):
        search, _ = self.search_and_freeze()
        foreign = self.make_manager(workspace=self.root / "foreign-workspace")
        self.addCleanup(foreign.close)
        _, foreign_candidate = self.search_and_freeze(foreign)
        receipt = self.verify(foreign_candidate)
        self.assertTrue(receipt["body"]["adoptable"])
        self.assert_rejected(receipt)
        self.assertFalse(self.manager.get(search["id"])["adoptable"])
        self.assertEqual(len(self.manager.list()["items"]), 1)

    def test_source_change_after_verification_blocks_import(self):
        search, candidate = self.search_and_freeze()
        receipt = self.verify(candidate)
        self.source.write_text(APPLICATION + "\n# changed after verification\n", encoding="utf-8")
        self.assert_rejected(receipt)
        self.assertFalse(self.manager.get(search["id"])["adoptable"])

    def test_source_change_after_import_removes_adoption_eligibility(self):
        search, candidate = self.search_and_freeze()
        self.assertTrue(self.manager.import_verification(self.verify(candidate))["adoptable"])
        self.source.write_text(APPLICATION + "\n# changed before adoption\n", encoding="utf-8")
        self.assertFalse(self.manager.get(search["id"])["adoptable"])
        with self.assertRaises(ExperimentError):
            self.manager.adopt(search["id"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], "identity")

    def test_restart_without_authority_preserves_active_but_cannot_regrant_adoption(self):
        search, candidate = self.search_and_freeze()
        self.manager.import_verification(self.verify(candidate))
        self.manager.adopt(search["id"])
        self.manager.close()
        self.manager = self.make_manager(authorities={})
        reopened = self.manager.get(search["id"])
        self.assertFalse(reopened["adoptable"])
        self.assertTrue(reopened["adopted"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], "double")
        self.assertFalse(self.manager.rollback(search["id"])["adopted"])
        with self.assertRaises(ExperimentError):
            self.manager.adopt(search["id"])
        self.assertEqual(self.manager.active_prompt(self.agent.id)["prompt"], "identity")

    def test_changed_active_baseline_rejects_stale_adoption_and_receipt_import(self):
        first, first_candidate = self.search_and_freeze()
        second, second_candidate = self.search_and_freeze()
        first_receipt = self.verify(first_candidate, offset=1000)
        second_receipt = self.verify(second_candidate, offset=2000)
        self.manager.import_verification(first_receipt)
        self.manager.import_verification(second_receipt)
        self.manager.adopt(first["id"])
        self.assertFalse(self.manager.get(second["id"])["adoptable"])
        with self.assertRaises(ExperimentError):
            self.manager.adopt(second["id"])
        self.assert_rejected(second_receipt)
        self.assertEqual(self.manager.active_prompt(self.agent.id)["experimentId"], first["id"])

    def test_old_boolean_without_receipt_cannot_grant_new_adoption(self):
        search, _ = self.search_and_freeze()
        legacy = copy.deepcopy(search)
        for key in ("stage", "evidenceStatus", "acceptancePolicyVersion", "verificationId", "searchComplete"):
            legacy.pop(key, None)
        legacy["adoptable"] = True
        self.manager.close()
        atomic_json(self.workspace / "experiments" / (search["id"] + ".json"), legacy)
        self.manager = self.make_manager()
        record = self.manager.get(search["id"])
        self.assertTrue(record["legacyAdoptable"])
        self.assertFalse(record["adoptable"])
        with self.assertRaises(ExperimentError):
            self.manager.adopt(search["id"])

    def test_http_accepts_only_signed_receipt_bound_to_requested_experiment(self):
        search, candidate = self.search_and_freeze()
        another, _ = self.search_and_freeze()
        receipt = self.verify(candidate)
        server = create_server(self.manager, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(identifier, body):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("POST", "/v1/optimizations/" + identifier + "/verification",
                                   json.dumps(body), {"Content-Type": "application/json"})
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        try:
            self.assertEqual(request(another["id"], {"receipt": receipt})[0], 400)
            self.assertEqual(request(search["id"], {"adoptable": True})[0], 400)
            self.assertEqual(request(search["id"], {"receipt": {"body": receipt["body"]}})[0], 409)
            self.assertEqual(request(search["id"], {"receipt": receipt, "key": "client supplied"})[0], 400)
            self.assertFalse(self.manager.get(search["id"])["adoptable"])
            status, imported = request(search["id"], {"receipt": receipt})
            self.assertEqual(status, 200, imported)
            self.assertTrue(imported["adoptable"])
            self.assertEqual(imported["verificationId"], candidate["id"])
            self.assertFalse(self.manager.get(another["id"])["adoptable"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
