"""优化器的预算、数据隔离、真实重跑和采用门槛，不依赖网络或模型。"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fuju_rsi.core import AgentSpec, Evaluation, Example, Prediction, optimize


def examples():
    return [Example(split, {"question": split}, {"answer": 1}, split)
            for split in ("train", "validation", "test")]


def specification(**overrides):
    values = dict(id="sql", name="问数助手", baseline_prompt="original", examples=examples(),
                  runner=lambda prompt, value: Prediction({"answer": int(prompt != "original")}),
                  proposer=lambda prompt, feedback, index: "candidate-%s" % index,
                  evaluator=lambda expected, prediction: Evaluation(float(expected == prediction.output)))
    values.update(overrides)
    return AgentSpec(**values)


class OptimizationCoreTests(unittest.TestCase):
    def test_replays_development_cases_without_running_legacy_holdout(self):
        calls = []
        proposal_feedback = []

        def run(prompt, value):
            calls.append((prompt, value["question"]))
            return Prediction({"answer": int(prompt != "original")}, tokens=11, cost_usd=0.002, trace_id="trace-" + str(len(calls)))

        def propose(prompt, feedback, index):
            proposal_feedback.append(copy.deepcopy(feedback))
            return "candidate"

        result = optimize(specification(runner=run, proposer=propose), max_trials=1, max_calls=7)
        self.assertFalse(result["adoptable"])
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertEqual(result["usedCalls"], 5)
        self.assertEqual(calls, [("original", "train"), ("original", "validation"),
                                 ("candidate", "train"), ("candidate", "validation")])
        self.assertEqual([case["id"] for case in proposal_feedback[0]], ["train"])
        self.assertNotIn("validation", json.dumps(proposal_feedback))
        self.assertNotIn("test", json.dumps(proposal_feedback))
        self.assertEqual(result["stage"], "search")
        self.assertEqual(result["evidenceStatus"], "not_verified")
        self.assertIsNone(result["acceptancePolicyVersion"])
        self.assertIsNone(result["verificationId"])
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])
        validation = result["trials"][1]["validation"]
        self.assertEqual(validation["tokens"], 11)
        self.assertEqual(validation["costUsd"], 0.002)
        self.assertEqual(validation["cases"][0]["traceId"], "trace-4")

    def test_unknown_usage_stays_unknown_and_all_case_fields_are_present(self):
        result = optimize(specification(), max_trials=1)
        case = result["trials"][1]["validation"]["cases"][0]
        self.assertEqual(set(case), {"id", "input", "expected", "output", "score", "passed", "reason",
                                     "error", "latencyMs", "tokens", "costUsd", "traceId"})
        self.assertIsNone(result["trials"][1]["validation"]["tokens"])
        self.assertIsNone(result["trials"][1]["validation"]["costUsd"])
        self.assertGreaterEqual(case["latencyMs"], 0)

    def test_later_proposals_only_receive_training_feedback_from_selected_prompt(self):
        feedback_calls = []

        def propose(prompt, feedback, index):
            feedback_calls.append((prompt, copy.deepcopy(feedback)))
            return "candidate-%s" % index

        result = optimize(specification(proposer=propose), max_trials=2)
        self.assertFalse(result["adoptable"])
        self.assertTrue(result["searchComplete"])
        self.assertEqual([item[0] for item in feedback_calls], ["original", "candidate-1"])
        self.assertEqual([item[1][0]["score"] for item in feedback_calls], [0, 1])
        for _, feedback in feedback_calls:
            self.assertEqual([case["id"] for case in feedback], ["train"])

    def test_budget_every_boundary_never_exceeds_limit_or_adopts_untested(self):
        for budget in range(10):
            with self.subTest(budget=budget):
                external_calls = []

                def run(prompt, value):
                    external_calls.append("run")
                    return Prediction({"answer": int(prompt != "original")})

                def propose(*args):
                    external_calls.append("propose")
                    return "candidate"

                result = optimize(specification(runner=run, proposer=propose), max_trials=1, max_calls=budget)
                self.assertEqual(result["usedCalls"], len(external_calls))
                self.assertLessEqual(result["usedCalls"], budget)
                self.assertEqual(result["status"], "completed")
                self.assertFalse(result["adoptable"])
                self.assertEqual(result["searchComplete"], budget >= 5)
                self.assertIsNone(result["candidateTest"])
                self.assertIsNone(result["baselineTest"])
                self.assertEqual(result["stage"], "search")
                self.assertEqual(result["evidenceStatus"], "not_verified")

    def test_budget_preserves_partial_candidate_without_reserving_final_test_calls(self):
        # 原版 2，第一与第二候选各 3，第三候选提案 1；全部预算用于开发比较。
        result = optimize(specification(), max_trials=3, max_calls=9)
        self.assertFalse(result["adoptable"])
        self.assertFalse(result["searchComplete"])
        self.assertEqual(result["usedCalls"], 9)
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertEqual(result["trials"][2]["validation"]["passed"], 1)
        unfinished = result["trials"][3]
        self.assertEqual(unfinished["training"]["cases"], [])
        self.assertIsNone(unfinished["validation"])
        self.assertIsNone(result["candidateTest"])
        self.assertIsNone(result["baselineTest"])

    def test_zero_trials_records_baseline_without_reserving_unneeded_test_budget(self):
        result = optimize(specification(), max_trials=0, max_calls=2)
        self.assertEqual(result["usedCalls"], 2)
        self.assertEqual(len(result["trials"]), 1)
        self.assertIsNotNone(result["trials"][0]["validation"])
        self.assertIsNone(result["baselineTest"])
        self.assertFalse(result["adoptable"])

    def test_identical_and_repeated_proposals_do_not_replay_duplicate_prompts(self):
        proposals = [" original ", "candidate", "candidate"]
        result = optimize(specification(proposer=lambda prompt, feedback, index: proposals[index - 1]),
                          max_trials=3, max_calls=9)
        self.assertEqual(result["usedCalls"], 7)
        self.assertFalse(result["adoptable"])
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["selectedTrialId"], "trial-2")
        self.assertIsNone(result["trials"][1]["training"])
        self.assertIsNone(result["trials"][3]["validation"])

    def test_no_improvement_does_not_open_test_set(self):
        observed = []

        def run(prompt, value):
            observed.append(value["question"])
            return Prediction({"answer": 0})

        result = optimize(specification(runner=run), max_trials=1)
        self.assertFalse(result["adoptable"])
        self.assertNotIn("test", observed)
        self.assertIsNone(result["baselineTest"])
        self.assertEqual(result["selectedTrialId"], "baseline")

    def test_validation_average_improvement_cannot_hide_individual_regression(self):
        rows = examples() + [Example("validation-2", {"question": "validation-2"}, {}, "validation")]

        def run(prompt, value):
            original = {"train": 0, "validation": 0.3, "validation-2": 0, "test": 1}
            candidate = {"train": 1, "validation": 0.2, "validation-2": 1, "test": 1}
            return Prediction((original if prompt == "original" else candidate)[value["question"]])

        result = optimize(specification(examples=rows, runner=run, evaluator=lambda expected, pred: Evaluation(pred.output)), max_trials=1)
        self.assertGreater(result["trials"][1]["validation"]["score"], result["trials"][0]["validation"]["score"])
        self.assertFalse(result["adoptable"])
        self.assertIsNone(result["candidateTest"])

    def test_legacy_holdout_is_not_passed_to_runner_evaluator_or_feedback(self):
        rows = examples()[:2] + [Example("heldout-private", {"secret-input": "not-for-search"},
                                       {"secret-answer": "not-for-search"}, "test")]
        observed = {"runner": [], "evaluator": [], "proposer": []}

        def run(prompt, value):
            observed["runner"].append(copy.deepcopy(value))
            self.assertNotIn("secret-input", value)
            return Prediction({"answer": int(prompt != "original")})

        def evaluate(expected, prediction):
            observed["evaluator"].append(copy.deepcopy(expected))
            self.assertNotIn("secret-answer", expected)
            return Evaluation(float(expected == prediction.output))

        def propose(prompt, feedback, index):
            observed["proposer"].append(copy.deepcopy(feedback))
            return "candidate"

        result = optimize(specification(examples=rows, runner=run, evaluator=evaluate, proposer=propose), max_trials=1)
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertEqual(len(observed["runner"]), 4)
        self.assertEqual(len(observed["evaluator"]), 4)
        self.assertNotIn("not-for-search", json.dumps(observed))
        self.assertNotIn("not-for-search", json.dumps(result))
        self.assertFalse(result["adoptable"])

    def test_holdout_content_never_enters_the_persisted_development_digest(self):
        """摘要只由开发题派生；否则改一道验收题就能从摘要确认它的内容。"""
        def run(prompt, value):
            return Prediction({"answer": int(prompt != "original")})

        def rows(secret):
            return examples()[:2] + [Example("heldout", {"secret": secret}, {"secret": secret}, "test")]

        first = optimize(specification(examples=rows("first-secret"), runner=run), max_trials=1)
        second = optimize(specification(examples=rows("second-secret"), runner=run), max_trials=1)
        self.assertEqual(first["developmentDigest"], second["developmentDigest"])
        self.assertNotIn("secret", json.dumps(first))

        # 反证：开发题变化必须仍然改变摘要，否则上面的相等是空断言。
        changed = optimize(specification(examples=examples()[:2] + [Example("train-2", {"question": "train-2"}, {"answer": 1}, "train")],
                                         runner=run), max_trials=1)
        self.assertNotEqual(first["developmentDigest"], changed["developmentDigest"])

    def test_development_only_registration_and_search_need_no_holdout_rows(self):
        result = optimize(specification(examples=examples()[:2]), max_trials=1, max_calls=5)
        self.assertTrue(result["searchComplete"])
        self.assertEqual(result["selectedTrialId"], "trial-1")
        self.assertFalse(result["adoptable"])
        self.assertIsNone(result["baselineTest"])
        self.assertIsNone(result["candidateTest"])

    def test_runner_exception_is_failed_case_and_cannot_fake_an_improvement(self):
        def run(prompt, value):
            if prompt == "original":
                raise RuntimeError("key=must-not-be-saved")
            return Prediction({"answer": 1})

        result = optimize(specification(runner=run), max_trials=1)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["status"], "completed")
        case = result["trials"][0]["training"]["cases"][0]
        self.assertEqual(case["score"], 0)
        self.assertIn("RuntimeError", case["error"])
        self.assertNotIn("must-not-be-saved", json.dumps(result))

    def test_baseline_validation_timeout_never_proves_prompt_improved(self):
        def run(prompt, value):
            if prompt == "original" and value["question"] == "validation":
                raise TimeoutError("temporary upstream failure")
            return Prediction({"answer": int(prompt != "original")})

        result = optimize(specification(runner=run), max_trials=1)
        self.assertFalse(result["adoptable"])
        self.assertFalse(result["searchComplete"])
        self.assertEqual(result["status"], "completed")
        self.assertIn("运行错误", result["message"])
        self.assertEqual(result["selectedTrialId"], "baseline")

    def test_candidate_runtime_error_cannot_equal_a_baseline_business_failure(self):
        rows = examples() + [Example("validation-2", {"question": "validation-2"}, {"answer": 1}, "validation")]

        def run(prompt, value):
            if prompt != "original" and value["question"] == "validation":
                raise ConnectionError("temporary failure")
            return Prediction({"answer": int(prompt != "original")})

        result = optimize(specification(examples=rows, runner=run), max_trials=1)
        self.assertFalse(result["adoptable"])
        self.assertFalse(result["searchComplete"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["trials"][1]["validation"]["score"], 0.5)
        self.assertIn("运行错误", result["message"])
        self.assertEqual(result["selectedTrialId"], "baseline")

    def test_evaluator_failure_on_baseline_cannot_create_artificial_improvement(self):
        def evaluate(expected, prediction):
            if prediction.output["answer"] == 0:
                raise RuntimeError("hidden credential")
            return Evaluation(1)

        result = optimize(specification(evaluator=evaluate), max_trials=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usedCalls"], 1)
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["trials"][0]["training"]["cases"][0]["output"], {"answer": 0})
        self.assertNotIn("hidden credential", json.dumps(result))

    def test_evaluator_failure_on_candidate_training_or_validation_stops_search(self):
        for fail_at in (3, 4):
            with self.subTest(fail_at=fail_at):
                scored = []

                def evaluate(expected, prediction):
                    scored.append(1)
                    if len(scored) == fail_at:
                        raise ValueError("bad evaluator")
                    return Evaluation(float(expected == prediction.output))

                result = optimize(specification(evaluator=evaluate), max_trials=1)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["adoptable"])
                self.assertEqual(len(scored), fail_at)

    def test_proposer_exception_preserves_completed_baseline(self):
        def propose(*args):
            raise OSError("authorization=secret")

        result = optimize(specification(proposer=propose))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["usedCalls"], 3)
        self.assertEqual(result["trials"][0]["validation"]["total"], 1)
        self.assertFalse(result["adoptable"])
        self.assertNotIn("secret", json.dumps(result))

    def test_cancellation_before_any_callback_consumes_no_budget(self):
        result = optimize(specification(), is_cancelled=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["usedCalls"], 0)
        self.assertFalse(result["adoptable"])

    def test_cancellation_after_runner_preserves_output_but_does_not_evaluate(self):
        stop = []
        evaluations = []

        def run(*args):
            stop.append(True)
            return Prediction({"answer": 1})

        result = optimize(specification(runner=run, evaluator=lambda *args: evaluations.append(1)), is_cancelled=lambda: bool(stop))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["usedCalls"], 1)
        self.assertEqual(evaluations, [])
        self.assertEqual(result["trials"][0]["training"]["cases"][0]["output"], {"answer": 1})

    def test_cancellation_after_proposer_stops_before_replaying_candidate(self):
        stop = []

        def propose(*args):
            stop.append(True)
            return "candidate"

        result = optimize(specification(proposer=propose), is_cancelled=lambda: bool(stop))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["usedCalls"], 3)
        self.assertEqual(len(result["trials"]), 1)
        self.assertFalse(result["adoptable"])

    def test_cancel_status_callback_failure_returns_failed_and_keeps_completed_case(self):
        reads = []

        def cancelled():
            reads.append(1)
            if len(reads) >= 3:
                raise RuntimeError("private error")
            return False

        result = optimize(specification(), is_cancelled=cancelled)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 1)
        self.assertEqual(len(result["trials"][0]["training"]["cases"]), 1)
        self.assertNotIn("private error", json.dumps(result))

    def test_cancellation_after_last_development_evaluator_or_terminal_update_blocks_completion(self):
        for phase in ("evaluator", "terminal-update"):
            with self.subTest(phase=phase):
                counter = []
                stop = []

                def evaluate(expected, prediction):
                    counter.append(1)
                    if phase == "evaluator" and len(counter) == 4:
                        stop.append(True)
                    return Evaluation(float(expected == prediction.output))

                def update(state):
                    if phase == "terminal-update" and state["status"] == "completed":
                        stop.append(True)

                result = optimize(specification(evaluator=evaluate), max_trials=1, on_update=update, is_cancelled=lambda: bool(stop))
                self.assertEqual(result["status"], "cancelled")
                self.assertFalse(result["adoptable"])
                self.assertFalse(result["searchComplete"])
                self.assertEqual(len(result["trials"][1]["validation"]["cases"]), 1)
                self.assertIsNone(result["candidateTest"])

    def test_mutating_callback_arguments_never_changes_dataset_or_previous_snapshots(self):
        rows = examples()
        original_rows = copy.deepcopy(rows)
        snapshots = []
        shared_prediction = Prediction({"answer": 0})
        holder = []

        def run(prompt, value):
            value["question"] = "changed"
            holder[0].examples[0].expected["answer"] = -5
            holder[0].runner = lambda *args: None
            shared_prediction.output["answer"] = int(prompt != "original")
            return shared_prediction

        def evaluate(expected, prediction):
            score = float(expected == prediction.output)
            expected["answer"] = 42
            prediction.output["answer"] = 42
            return Evaluation(score)

        def propose(prompt, feedback, index):
            self.assertEqual(feedback[0]["expected"], {"answer": 1})
            feedback[0]["expected"]["answer"] = 99
            feedback.append({"poison": True})
            return "candidate"

        def update(state):
            snapshots.append(copy.deepcopy(state))
            state["trials"].clear()
            state["adoptable"] = True

        spec = specification(examples=rows, runner=run, evaluator=evaluate, proposer=propose)
        holder.append(spec)
        result = optimize(spec, max_trials=1, on_update=update)
        self.assertFalse(result["adoptable"])
        self.assertTrue(result["searchComplete"])
        for item in result["trials"]:
            for batch_name in ("training", "validation"):
                for case in item[batch_name]["cases"]:
                    self.assertEqual(case["expected"], {"answer": 1})
                    self.assertNotEqual(case["input"]["question"], "changed")
        self.assertEqual(result["trials"][0]["training"]["cases"][0]["output"], {"answer": 0})
        self.assertEqual(original_rows[0].expected, {"answer": 1})
        completed_snapshot = snapshots[-1]
        result["trials"].clear()
        self.assertEqual(len(completed_snapshot["trials"]), 2)

    def test_update_failure_is_visible_and_never_returns_adoptable(self):
        def update(state):
            if state["usedCalls"] >= 1:
                raise OSError("disk full credential=hidden")

        result = optimize(specification(), on_update=update)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 1)
        self.assertEqual(len(result["trials"][0]["training"]["cases"]), 1)
        self.assertIn("保存", result["message"])
        self.assertNotIn("hidden", json.dumps(result))

    def test_terminal_persistence_failure_clears_search_completion(self):
        def update(state):
            if state["status"] == "completed":
                raise OSError("terminal write failed")

        result = optimize(specification(), max_trials=1, on_update=update)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertFalse(result["searchComplete"])
        self.assertEqual(result["trials"][1]["validation"]["score"], 1)
        self.assertIn("保存", result["message"])

    def test_nonfinite_or_invalid_scores_fail_experiment(self):
        for score in (float("nan"), float("inf"), float("-inf"), -0.1, 1.1, True, "1"):
            with self.subTest(score=score):
                result = optimize(specification(evaluator=lambda expected, prediction: Evaluation(score)))
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["adoptable"])
                json.dumps(result, allow_nan=False)

    def test_invalid_prediction_usage_and_output_fail_experiment(self):
        values = [Prediction(float("nan")), Prediction(1, tokens=-1), Prediction(1, tokens=True),
                  Prediction(1, cost_usd=float("inf")), Prediction(1, cost_usd=-1), Prediction(1, trace_id=2)]
        for prediction in values:
            with self.subTest(prediction=prediction):
                result = optimize(specification(runner=lambda *args: prediction), max_trials=0)
                self.assertEqual(result["status"], "failed")
                self.assertFalse(result["adoptable"])
                self.assertEqual(result["trials"][0]["training"]["cases"][0]["score"], 0)
                self.assertIsNotNone(result["trials"][0]["training"]["cases"][0]["error"])
                json.dumps(result, allow_nan=False)

    def test_invalid_prediction_type_is_an_infrastructure_failure(self):
        result = optimize(specification(runner=lambda *args: {"output": "wrong return type"}))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        self.assertEqual(result["usedCalls"], 1)

    def test_business_failure_predictions_remain_valid_comparisons(self):
        result = optimize(specification(), max_trials=1)
        self.assertEqual(result["trials"][0]["validation"]["score"], 0)
        self.assertIsNone(result["trials"][0]["validation"]["cases"][0]["error"])
        self.assertFalse(result["adoptable"])
        self.assertTrue(result["searchComplete"])

    def test_overflowing_cost_aggregation_fails_without_serializing_infinity(self):
        rows = examples() + [Example("train-2", {"question": "train-2"}, {}, "train")]
        result = optimize(specification(examples=rows, runner=lambda *args: Prediction(0, cost_usd=1e308)), max_trials=0)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["adoptable"])
        json.dumps(result, allow_nan=False)

    def test_registration_rejects_leaked_ids_inputs_and_invalid_data(self):
        bad_rows = [
            examples()[:1],
            [Example("same", row.input, row.expected, row.split) for row in examples()],
            [Example(row.id, {"identical": [1, 2]}, row.expected, row.split) for row in examples()],
            [Example("train", {"a": 1, "b": 2}, 1, "train"),
             Example("validation", {"b": 2, "a": 1}, 1, "validation"), examples()[2]],
            [Example("train", float("nan"), 1, "train")] + examples()[1:],
            [Example("train", {1: "invalid key"}, 1, "train")] + examples()[1:],
            [Example("train", {}, 1, [])] + examples()[1:],
        ]
        for rows in bad_rows:
            with self.subTest(rows=rows):
                with self.assertRaises(ValueError):
                    specification(examples=rows)
        for overrides in ({"id": ""}, {"name": None}, {"baseline_prompt": " "}, {"runner": None}, {"examples": ()}):
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    specification(**overrides)

    def test_invalid_budgets_are_rejected_before_callbacks(self):
        for kwargs in ({"max_calls": -1}, {"max_calls": True}, {"max_trials": 1.5}, {"max_trials": -1}, {"baseline_prompt": ""}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    optimize(specification(), **kwargs)

    def test_baseline_override_is_replayed_and_used_for_proposal(self):
        received = []

        def propose(prompt, feedback, index):
            received.append(prompt)
            return "candidate"

        result = optimize(specification(proposer=propose), max_trials=1, baseline_prompt="active-version")
        self.assertEqual(received, ["active-version"])
        self.assertEqual(result["baselinePrompt"], "active-version")
        self.assertFalse(result["adoptable"])


if __name__ == "__main__":
    unittest.main()
