"""验收历史的真实跨进程互斥、崩溃保留和损坏拒绝检查。"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from fuju_rsi.holdout_registry import HoldoutRegistry, RegistryError
from fuju_rsi import holdout_registry as registry_module


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


TOKENS = ["input:" + digest("question"), "source:" + digest("source"), "group:" + digest("family")]
FINGERPRINT = digest("candidate-and-evaluation-plan")


def reserve_worker(directory, job_id, barrier, queue):
    try:
        registry = HoldoutRegistry(directory)
        barrier.wait(timeout=10)
        result = registry.reserve(job_id, FINGERPRINT, TOKENS)
        registry.start(job_id)
        queue.put(("started", result["jobId"]))
    except RegistryError:
        queue.put(("blocked", job_id))
    except BaseException as exc:
        queue.put(("unexpected", type(exc).__name__))


def crash_worker(directory):
    registry = HoldoutRegistry(directory)
    registry.reserve("crashed", FINGERPRINT, TOKENS)
    registry.start("crashed")
    os._exit(17)


class HoldoutRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "verification-history"
        self.metadata = HoldoutRegistry.initialize(self.directory, "project:test")
        self.registry = HoldoutRegistry(self.directory)

    def tearDown(self):
        self.temporary.cleanup()

    def reserve(self, job="job-1", tokens=None):
        return self.registry.reserve(job, FINGERPRINT, TOKENS if tokens is None else tokens)

    def rewrite(self, filename, change, *, resign=False):
        path = self.directory / filename
        value = json.loads(path.read_text())
        change(value["payload"])
        if resign:
            value = registry_module._envelope(value["payload"])
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_persisted_lifecycle_and_defensive_copies(self):
        self.assertEqual(self.registry.metadata, self.metadata)
        self.assertEqual(self.registry.authority_id, "project:test")
        self.registry.metadata["authorityId"] = "changed"
        reserved = self.reserve(tokens=TOKENS + [TOKENS[0]])
        self.assertEqual(reserved["tokens"], sorted(TOKENS))
        reserved["tokens"].clear()
        self.assertEqual(self.reserve()["tokens"], sorted(TOKENS))
        self.registry.start("job-1")
        result = {"adoptable": False, "evidence": {"reason": "inconclusive"}}
        finished = self.registry.finish("job-1", result)
        self.assertEqual(finished["status"], "consumed")
        result["evidence"]["reason"] = "mutated"
        reopened = HoldoutRegistry(self.directory)
        receipt = reopened.reserve("job-1", FINGERPRINT, list(reversed(TOKENS)))
        self.assertEqual(receipt, finished)
        self.assertEqual(receipt["result"]["evidence"]["reason"], "inconclusive")
        self.assertEqual(reopened.finish("job-1", receipt["result"]), receipt)
        receipt["result"]["evidence"]["reason"] = "another mutation"
        with self.assertRaises(RegistryError):
            reopened.finish("job-1", receipt["result"])

    def test_snapshot_and_token_identity_cannot_change(self):
        self.reserve()
        with self.assertRaises(RegistryError):
            self.registry.reserve("job-1", digest("different candidate"), TOKENS)
        with self.assertRaises(RegistryError):
            self.reserve(tokens=TOKENS + ["input:" + digest("another question")])
        self.registry.start("job-1")
        self.registry.finish("job-1", {})
        with self.assertRaises(RegistryError):
            self.reserve(tokens=[TOKENS[0]])

    def test_renamed_and_partial_overlap_is_rejected_in_every_occupied_state(self):
        for status in ("reserved", "running", "consumed", "interrupted"):
            with self.subTest(status=status):
                directory = self.directory.parent / status
                HoldoutRegistry.initialize(directory, "project:" + status)
                registry = HoldoutRegistry(directory)
                registry.reserve("original-name", FINGERPRINT, TOKENS)
                if status in ("running", "consumed", "interrupted"):
                    registry.start("original-name")
                if status == "consumed":
                    registry.finish("original-name", {"adoptable": False})
                if status == "interrupted":
                    registry.interrupt("original-name")
                for token in TOKENS:
                    with self.assertRaises(RegistryError):
                        registry.reserve("renamed-benchmark", digest("new name"),
                                         [token, "input:" + digest("genuinely new question")])

    def test_real_processes_only_one_can_start_shared_content(self):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=reserve_worker,
                                   args=(str(self.directory), "parallel-" + str(index), barrier, queue))
                   for index in range(2)]
        try:
            for worker in workers:
                worker.start()
            results = [queue.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(row[0] for row in results), ["blocked", "started"], results)
            winner = next(job for status, job in results if status == "started")
            self.assertEqual(HoldoutRegistry(self.directory).get(winner)["status"], "running")
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            queue.close()

    def test_same_job_concurrent_start_also_executes_only_once(self):
        context = multiprocessing.get_context("spawn")
        barrier, queue = context.Barrier(2), context.Queue()
        workers = [context.Process(target=reserve_worker,
                                   args=(str(self.directory), "same-job", barrier, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            results = [queue.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(sorted(row[0] for row in results), ["blocked", "started"], results)
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
            queue.close()

    def test_process_death_does_not_make_content_fresh(self):
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=crash_worker, args=(str(self.directory),))
        worker.start()
        worker.join(timeout=15)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=5)
            self.fail("crash worker did not exit")
        self.assertEqual(worker.exitcode, 17)
        reopened = HoldoutRegistry(self.directory)
        self.assertEqual(reopened.get("crashed")["status"], "running")
        for job in ("crashed", "retry-new-id"):
            with self.assertRaises(RegistryError):
                reopened.reserve(job, FINGERPRINT, TOKENS)
        reopened.interrupt("crashed")
        self.assertEqual(HoldoutRegistry(self.directory).get("crashed")["status"], "interrupted")
        with self.assertRaises(RegistryError):
            reopened.release("crashed")

    def test_release_requires_never_started_reservation_and_keeps_tombstone(self):
        self.reserve()
        self.assertEqual(self.registry.release("job-1")["status"], "released")
        with self.assertRaises(RegistryError):
            self.reserve()
        self.reserve("new-job")
        self.registry.start("new-job")
        with self.assertRaises(RegistryError):
            self.registry.release("new-job")
        self.registry.interrupt("new-job")
        with self.assertRaises(RegistryError):
            self.reserve("third-job")

    def test_reserved_job_is_not_automatically_released_on_reopen(self):
        self.reserve()
        reopened = HoldoutRegistry(self.directory)
        with self.assertRaises(RegistryError):
            reopened.reserve("new-job", FINGERPRINT, TOKENS)
        self.assertEqual(reopened.reserve("job-1", FINGERPRINT, TOKENS)["status"], "reserved")

    def test_illegal_transitions_fail_closed(self):
        self.reserve()
        with self.assertRaises(RegistryError):
            self.registry.finish("job-1", {})
        self.registry.start("job-1")
        for action in (lambda: self.registry.start("job-1"), lambda: self.reserve()):
            with self.assertRaises(RegistryError):
                action()
        self.registry.interrupt("job-1")
        self.assertEqual(self.registry.interrupt("job-1")["status"], "interrupted")
        with self.assertRaises(RegistryError):
            self.registry.finish("job-1", {})
        with self.assertRaises(RegistryError):
            self.registry.get("unknown")

    def test_failed_result_or_interrupt_write_never_frees_running_tokens(self):
        self.reserve()
        self.registry.start("job-1")
        for action in (lambda: self.registry.finish("job-1", {"adoptable": True}),
                       lambda: self.registry.interrupt("job-1")):
            with patch.object(registry_module, "_atomic_write_json", side_effect=OSError("disk full")):
                with self.assertRaises(RegistryError):
                    action()
            reopened = HoldoutRegistry(self.directory)
            self.assertEqual(reopened.get("job-1")["status"], "running")
            with self.assertRaises(RegistryError):
                reopened.reserve("other-job", FINGERPRINT, TOKENS)

    def test_failed_start_write_prevents_running_state(self):
        self.reserve()
        with patch.object(registry_module, "_atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(RegistryError):
                self.registry.start("job-1")
        self.assertEqual(self.registry.get("job-1")["status"], "reserved")

    def test_atomic_replace_failure_preserves_old_history_and_cleans_temporary_file(self):
        self.reserve()
        self.registry.start("job-1")
        path = self.directory / "ledger.json"
        original = path.read_bytes()
        with patch.object(registry_module.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaises(RegistryError):
                self.registry.finish("job-1", {"adoptable": True})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".writing-*")), [])
        reopened = HoldoutRegistry(self.directory)
        self.assertEqual(reopened.get("job-1")["status"], "running")
        with self.assertRaises(RegistryError):
            reopened.reserve("new-job", FINGERPRINT, TOKENS)

    def test_initialization_is_explicit_and_partial_history_is_never_recreated(self):
        absent = self.directory.parent / "absent"
        with self.assertRaises(RegistryError):
            HoldoutRegistry(absent)
        self.assertFalse(absent.exists())
        with self.assertRaises(RegistryError):
            HoldoutRegistry.initialize(self.directory, "new-authority")
        for filename in ("metadata.json", "ledger.json", ".holdout.lock"):
            with self.subTest(filename=filename):
                path = self.directory / filename
                original = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaises(RegistryError):
                        HoldoutRegistry(self.directory)
                    with self.assertRaises(RegistryError):
                        HoldoutRegistry.initialize(self.directory, "project:test")
                    self.assertFalse(path.exists())
                finally:
                    path.write_bytes(original)

    def test_initialization_failure_cannot_be_retried_as_empty_history(self):
        directory = self.directory.parent / "interrupted-initialization"
        with patch.object(registry_module, "_atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(RegistryError):
                HoldoutRegistry.initialize(directory, "project:test")
        with self.assertRaises(RegistryError):
            HoldoutRegistry(directory)
        with self.assertRaises(RegistryError):
            HoldoutRegistry.initialize(directory, "project:test")

    def test_content_mutation_checksum_and_duplicate_keys_fail_closed(self):
        self.reserve()
        original = (self.directory / "ledger.json").read_bytes()
        self.rewrite("ledger.json", lambda value: value["jobs"].clear())
        with self.assertRaises(RegistryError):
            self.registry.get("job-1")
        with self.assertRaises(RegistryError):
            HoldoutRegistry(self.directory)
        path = self.directory / "ledger.json"
        path.write_bytes(original)
        path.write_text('{"payload":{},"payload":{},"sha256":"' + "0" * 64 + '"}')
        with self.assertRaises(RegistryError):
            HoldoutRegistry(self.directory)

    def test_structural_corruption_rejected_even_with_recomputed_checksum(self):
        self.reserve()
        path = self.directory / "ledger.json"
        original = path.read_bytes()
        mutations = [
            lambda value: value.update(authorityId="other-project"),
            lambda value: value.update(schemaVersion=True),
            lambda value: value["jobs"]["job-1"].update(status="consumed"),
            lambda value: value["jobs"]["job-1"].update(tokens=TOKENS + [TOKENS[0]]),
            lambda value: value["jobs"]["job-1"].update(jobId="other-job"),
            lambda value: value["jobs"]["job-1"].update(reservedAt="not a date"),
        ]
        for mutate in mutations:
            path.write_bytes(original)
            self.rewrite("ledger.json", mutate, resign=True)
            with self.assertRaises(RegistryError):
                HoldoutRegistry(self.directory)

    def test_metadata_corruption_and_replaced_history_are_rejected(self):
        original = (self.directory / "metadata.json").read_bytes()
        self.rewrite("metadata.json", lambda value: value.update(authorityId="changed"))
        with self.assertRaises(RegistryError):
            HoldoutRegistry(self.directory)
        (self.directory / "metadata.json").write_bytes(original)
        self.reserve()
        # 新进程也会拒绝元信息与历史不匹配，已打开句柄额外拒绝身份变更。
        self.rewrite("metadata.json", lambda value: value.update(authorityId="changed"), resign=True)
        self.rewrite("ledger.json", lambda value: value.update(authorityId="changed"), resign=True)
        with self.assertRaises(RegistryError):
            self.registry.get("job-1")

    def test_open_instance_detects_observed_history_rollback(self):
        path = self.directory / "ledger.json"
        original = path.read_bytes()
        self.reserve()
        path.write_bytes(original)
        with self.assertRaises(RegistryError):
            self.registry.get("job-1")

    def test_canonical_identifiers_tokens_and_json_results(self):
        for job in ("../escape", "", "任务", "job\n", "a" * 161, 1):
            with self.assertRaises(RegistryError):
                self.reserve(job)
        for tokens in ([], (), ["input:" + "A" * 64], ["unknown:" + digest("x")], [False]):
            with self.assertRaises(RegistryError):
                self.reserve(tokens=tokens)
        with self.assertRaises(RegistryError):
            self.registry.reserve("valid", "0" * 63, TOKENS)
        self.reserve()
        self.registry.start("job-1")
        circular = {}
        circular["self"] = circular
        for result in ([], {"score": float("nan")}, {"score": float("inf")}, {1: "converted key"}, circular):
            with self.assertRaises(RegistryError):
                self.registry.finish("job-1", result)
        self.assertEqual(self.registry.get("job-1")["status"], "running")


if __name__ == "__main__":
    unittest.main()
