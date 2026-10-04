"""验收规则的数学对账、分组与重复运行边界，全部使用标准库。"""
import copy
import json
import math
import os
import random
import sys
import unittest
from dataclasses import FrozenInstanceError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fuju_rsi.acceptance import AcceptancePolicy, POLICY_VERSION, compare


def policy(**values):
    defaults = {"resamples": 100}
    defaults.update(values)
    return AcceptancePolicy(**defaults)


def case(index, baseline=0.0, candidate=1.0, group=None, critical=False, repeats=3):
    return {"id": "case-%s" % index, "groupId": "group-%s" % (index if group is None else group),
            "baselineScores": [baseline] * repeats, "candidateScores": [candidate] * repeats,
            "critical": critical}


class AcceptancePolicyTests(unittest.TestCase):
    def test_defaults_roundtrip_and_frozen(self):
        value = AcceptancePolicy()
        expected = {"repeats": 3, "minGroups": 30, "minGain": 0.0, "confidence": 0.95,
                    "maxGroupRegression": 0.0, "resamples": 2000, "seed": 1729}
        self.assertEqual(value.to_dict(), expected)
        self.assertEqual(AcceptancePolicy.from_dict(expected), value)
        self.assertEqual(AcceptancePolicy.from_dict({}), value)
        with self.assertRaises(FrozenInstanceError):
            value.repeats = 5
        detached = value.to_dict()
        detached["repeats"] = 9
        self.assertEqual(value.repeats, 3)

    def test_camel_case_partial_policy_is_supported(self):
        value = AcceptancePolicy.from_dict({"minGroups": 2, "minGain": 0.1, "seed": 0})
        self.assertEqual(value.min_groups, 2)
        self.assertEqual(value.min_gain, 0.1)
        self.assertEqual(value.seed, 0)
        self.assertEqual(value.repeats, 3)

    def test_unknown_policy_fields_and_non_objects_are_rejected(self):
        for value in (None, [], "{}", {"min_groups": 4}, {"confidnce": 0.9}, {1: 0.95}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AcceptancePolicy.from_dict(value)

    def test_integer_rules_reject_bool_floats_nonfinite_and_below_range(self):
        for field, minimum in (("repeats", 1), ("minGroups", 2), ("resamples", 1), ("seed", 0)):
            for value in (True, False, "3", 3.0, None, float("nan"), float("inf"), minimum - 1):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    AcceptancePolicy.from_dict({field: value})
            self.assertEqual(AcceptancePolicy.from_dict({field: minimum}).to_dict()[field], minimum)

    def test_score_rules_reject_bool_nonfinite_and_outside_range(self):
        for field in ("minGain", "maxGroupRegression", "confidence"):
            for value in (True, False, "0.9", None, float("nan"), float("inf"), -float("inf"),
                          -0.01, 1.01, 10 ** 1000):
                with self.subTest(field=field, value=repr(value)[:30]), self.assertRaises(ValueError):
                    AcceptancePolicy.from_dict({field: value})
        for value in (0, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AcceptancePolicy(confidence=value)
        self.assertEqual(AcceptancePolicy(min_gain=1, max_group_regression=1).min_gain, 1.0)


class AcceptanceComparisonTests(unittest.TestCase):
    def test_hoeffding_matches_analytical_reference(self):
        settings = policy(confidence=0.99)
        result = compare([case(i, baseline=0.1, candidate=0.9) for i in range(50)], settings)
        expected_lower = 0.8 - math.sqrt(2.0 * math.log(100.0) / 50)
        self.assertEqual(result["evidenceStatus"], "improved")
        self.assertAlmostEqual(result["interval"]["lower"], expected_lower, places=14)
        self.assertEqual(result["interval"]["upper"], 1.0)
        self.assertEqual(result["interval"]["confidence"], 0.99)
        self.assertEqual(result["interval"]["method"], "paired-group-hoeffding-v1")
        self.assertEqual(result["acceptancePolicyVersion"], POLICY_VERSION)
        self.assertEqual(result["independentGroups"], 50)
        self.assertEqual(result["caseCount"], 50)
        self.assertEqual(result["runCount"], 300)
        self.assertAlmostEqual(result["baselineScore"], 0.1)
        self.assertAlmostEqual(result["candidateScore"], 0.9)
        self.assertAlmostEqual(result["delta"], 0.8)

    def test_independent_groups_are_equally_weighted(self):
        # 一组被改写 100 次，不能使它比另一组拥有 100 倍权重。
        cases = [case(i, group="rewrite", baseline=0.0, candidate=1.0) for i in range(100)]
        cases.append(case("other", baseline=1.0, candidate=0.0))
        result = compare(cases, policy(min_groups=2, max_group_regression=1))
        self.assertEqual(result["independentGroups"], 2)
        self.assertEqual(result["baselineScore"], 0.5)
        self.assertEqual(result["candidateScore"], 0.5)
        self.assertEqual(result["delta"], 0.0)
        self.assertEqual(result["evidenceStatus"], "no_improvement")

    def test_group_duplication_does_not_inflate_sample_count_or_evidence(self):
        cases = [case(i, candidate=(i % 4 + 1) / 4) for i in range(40)]
        settings = policy()
        original = compare(cases, settings)
        duplicate = copy.deepcopy(cases)
        # 复制一个完整来源组。新增案例 id，但来源组及运行结果不变。
        for index in range(100):
            clone = copy.deepcopy(cases[0])
            clone["id"] = "rewrite-%s" % index
            duplicate.append(clone)
        repeated = compare(duplicate, settings)
        for field in ("evidenceStatus", "independentGroups", "baselineScore", "candidateScore",
                      "delta", "interval", "bootstrapInterval"):
            self.assertEqual(original[field], repeated[field], field)
        self.assertEqual(repeated["caseCount"], 140)
        self.assertEqual(repeated["runCount"], 840)

    def test_repeat_count_does_not_inflate_independent_groups(self):
        cases = [case(i) for i in range(30)]
        original = compare(cases, policy())
        more = [case(i, repeats=12) for i in range(30)]
        repeated = compare(more, policy(repeats=12))
        self.assertEqual(original["interval"], repeated["interval"])
        self.assertEqual(original["bootstrapInterval"], repeated["bootstrapInterval"])
        self.assertEqual(repeated["independentGroups"], 30)
        self.assertEqual(repeated["runCount"], 720)

    def test_same_group_case_means_and_repeated_scores_are_averaged(self):
        cases = [case("a", group="same"), case("b", group="same"), case("c")]
        cases[0]["baselineScores"] = [0.0, 0.5, 1.0]
        cases[0]["candidateScores"] = [0.5, 1.0, 0.0]
        cases[1]["candidateScores"] = [0.5, 0.5, 0.5]
        result = compare(cases, policy(min_groups=2))
        self.assertEqual(result["independentGroups"], 2)
        self.assertEqual(result["baselineScore"], 0.125)
        self.assertEqual(result["candidateScore"], 0.75)
        self.assertEqual(result["delta"], 0.625)

    def test_all_zero_changes_do_not_pass(self):
        result = compare([case(i, baseline=1.0, candidate=1.0) for i in range(40)], policy())
        self.assertEqual(result["evidenceStatus"], "no_improvement")
        self.assertEqual(result["delta"], 0.0)
        self.assertEqual(result["bootstrapInterval"]["lower"], 0.0)
        self.assertEqual(result["bootstrapInterval"]["upper"], 0.0)
        self.assertLess(result["interval"]["lower"], 0.0)

    def test_tiny_positive_gain_with_many_repeats_remains_insufficient(self):
        cases = [case(i, baseline=0.5, candidate=0.51, repeats=50) for i in range(30)]
        result = compare(cases, policy(repeats=50))
        self.assertEqual(result["evidenceStatus"], "insufficient")
        self.assertGreater(result["delta"], 0.0)
        self.assertLess(result["interval"]["lower"], 0.0)

    def test_minimum_groups_blocks_even_a_strong_gain(self):
        result = compare([case(i) for i in range(20)], policy())
        self.assertGreater(result["interval"]["lower"], 0.0)
        self.assertEqual(result["evidenceStatus"], "insufficient")
        self.assertIn("最低组数", " ".join(result["reasons"]))

    def test_degenerate_bootstrap_cannot_override_primary_bound(self):
        result = compare([case(i) for i in range(2)], policy(min_groups=2))
        self.assertEqual(result["bootstrapInterval"]["lower"], 1.0)
        self.assertEqual(result["bootstrapInterval"]["upper"], 1.0)
        self.assertEqual(result["bootstrapInterval"]["descriptiveOnly"], True)
        self.assertLess(result["interval"]["lower"], 0.0)
        self.assertEqual(result["evidenceStatus"], "insufficient")

    def test_negative_group_or_critical_case_blocks_aggregate_gain(self):
        cases = [case(i) for i in range(40)]
        cases.append(case("regression", baseline=0.75, candidate=0.25))
        result = compare(cases, policy())
        self.assertGreater(result["interval"]["lower"], 0.0)
        self.assertEqual(result["evidenceStatus"], "regressed")
        self.assertIn("来源组", " ".join(result["reasons"]))
        self.assertNotIn("regression", str(result))
        # 即使组级平均值变好，关键案例退步也不能被同组其他案例抵消。
        cases[-1]["groupId"] = cases[0]["groupId"]
        cases[-1]["critical"] = True
        result = compare(cases, policy(max_group_regression=1))
        self.assertGreater(result["delta"], 0.9)
        self.assertEqual(result["evidenceStatus"], "regressed")
        self.assertIn("关键案例", " ".join(result["reasons"]))
        self.assertNotIn("regression", str(result))

    def test_allowed_group_regression_is_respected_without_overriding_critical_cases(self):
        cases = [case(i) for i in range(40)] + [case("r", baseline=0.75, candidate=0.25)]
        self.assertEqual(compare(cases, policy(max_group_regression=0.5))["evidenceStatus"], "improved")
        self.assertEqual(compare(cases, policy(max_group_regression=0.49))["evidenceStatus"], "regressed")
        cases[-1]["critical"] = True
        self.assertEqual(compare(cases, policy(max_group_regression=1))["evidenceStatus"], "regressed")

    def test_minimum_gain_is_strict_for_point_estimate_and_lower_bound(self):
        cases = [case(i, candidate=0.5) for i in range(100)]
        self.assertEqual(compare(cases, policy(min_gain=0.5))["evidenceStatus"], "no_improvement")
        self.assertEqual(compare(cases, policy(min_gain=0.4))["evidenceStatus"], "insufficient")
        self.assertEqual(compare(cases, policy(min_gain=0.1))["evidenceStatus"], "improved")
        lower = compare(cases, policy())["interval"]["lower"]
        self.assertEqual(compare(cases, policy(min_gain=lower))["evidenceStatus"], "insufficient")

    def test_invalid_or_empty_input_cannot_produce_partial_scores(self):
        for cases in (None, {}, "cases", [], [None], [case(1), {}]):
            with self.subTest(cases=cases):
                result = compare(cases, policy())
                self.assertEqual(result["evidenceStatus"], "invalid")
                self.assertTrue(result["reasons"])
                self.assertIsNone(result["baselineScore"])
                self.assertIsNone(result["candidateScore"])
                self.assertIsNone(result["delta"])
                self.assertIsNone(result["interval"]["lower"])
                self.assertIsNone(result["bootstrapInterval"])
                json.dumps(result, allow_nan=False)

    def test_case_fields_are_required_and_duplicate_ids_rejected(self):
        for field in ("id", "groupId", "baselineScores", "candidateScores", "critical"):
            value = case(1)
            del value[field]
            with self.subTest(field=field):
                self.assertEqual(compare([value], policy())["evidenceStatus"], "invalid")
        for field, values in (("id", [None, 1, "", "  "]), ("groupId", [None, 1, "", "\n"]),
                              ("critical", [None, 0, 1, "false"])):
            for value in values:
                example = case(1)
                example[field] = value
                with self.subTest(field=field, value=value):
                    self.assertEqual(compare([example], policy())["evidenceStatus"], "invalid")
        self.assertEqual(compare([case(1), case(1)], policy())["evidenceStatus"], "invalid")

    def test_missing_unequal_extra_and_non_list_repeats_are_invalid(self):
        for field in ("baselineScores", "candidateScores"):
            for scores in ([], [1], [1, 1], [1, 1, 1, 1], (1, 1, 1), "111", None):
                value = case(1)
                value[field] = scores
                with self.subTest(field=field, scores=scores):
                    self.assertEqual(compare([value], policy())["evidenceStatus"], "invalid")

    def test_scores_reject_nonfinite_bool_outside_range_and_errors(self):
        for score in (True, False, None, "error", {"error": "timeout"}, float("nan"), float("inf"),
                      -float("inf"), -0.01, 1.01, 10 ** 1000):
            for field in ("baselineScores", "candidateScores"):
                value = case(1)
                value[field][1] = score
                with self.subTest(field=field, score=repr(score)[:30]):
                    result = compare([value], policy())
                    self.assertEqual(result["evidenceStatus"], "invalid")
                    json.dumps(result, allow_nan=False)

    def test_bound_is_clipped_and_finite_for_extreme_valid_confidence(self):
        # 使用相邻浮点数的精确字面值，保留边界覆盖并兼容 Python 3.8。
        for confidence in (float.fromhex("0x0.0000000000001p-1022"),
                           float.fromhex("0x1.fffffffffffffp-1")):
            result = compare([case(i) for i in range(2)], policy(min_groups=2, confidence=confidence))
            self.assertGreaterEqual(result["interval"]["lower"], -1.0)
            self.assertLessEqual(result["interval"]["lower"], 1.0)
            json.dumps(result, allow_nan=False)

    def test_bootstrap_is_reproducible_order_invariant_and_leaves_global_rng_alone(self):
        settings = policy(seed=81, resamples=701)
        cases = [case(i, candidate=(i % 7) / 7) for i in range(35)]
        untouched = copy.deepcopy(cases)
        random.seed(101)
        state = random.getstate()
        first = compare(cases, settings)
        self.assertEqual(state, random.getstate())
        self.assertEqual(first, compare(list(reversed(cases)), settings))
        self.assertEqual(cases, untouched)
        self.assertEqual(first["bootstrapInterval"]["method"], "paired-group-bootstrap-basic-v1")
        self.assertEqual(first["bootstrapInterval"]["resamples"], 701)
        self.assertEqual(first["bootstrapInterval"]["seed"], 81)

    def test_bootstrap_seed_and_resampling_do_not_change_acceptance(self):
        cases = [case(i, candidate=(i % 7 + 1) / 7) for i in range(42)]
        first = compare(cases, policy(resamples=1, seed=1))
        second = compare(cases, policy(resamples=1000, seed=91))
        self.assertEqual(first["evidenceStatus"], second["evidenceStatus"])
        self.assertEqual(first["interval"], second["interval"])
        self.assertNotEqual(first["bootstrapInterval"], second["bootstrapInterval"])

    def test_bootstrap_basic_interval_matches_asymmetric_exact_distribution(self):
        # 组差值为 [0, 0, 0, 1]，每次抽 4 组，和服从 Binomial(4, 1/4)。
        # 精确 2.5% 与 97.5% 分位点分别为 0 和 3/4。
        # basic 区间是 2*(1/4) - [3/4, 0] = [-1/4, 1/2]，
        # 这能区分“基本区间”和直接报告重采样分位点的错误实现。
        cases = [case(i, candidate=float(i == 3)) for i in range(4)]
        result = compare(cases, policy(min_groups=2, resamples=20000))
        self.assertEqual(result["bootstrapInterval"]["lower"], -0.25)
        self.assertEqual(result["bootstrapInterval"]["upper"], 0.5)

    def test_policy_object_is_required(self):
        with self.assertRaises(ValueError):
            compare([case(1)], {})

    def test_seeded_independent_null_experiments_match_honest_false_positive_bound(self):
        # 每次实验使用新独立来源组，候选提前固定。原版与候选在总体上相同：
        # 每组差值等概率为 -1 或 +1。允许个别组退步以单独检验统计门槛。
        # 这不是反复查看同一保留集，也不模拟语义相关性或错误评分。
        groups, experiments, confidence = 16, 2000, 0.95
        settings = policy(repeats=1, min_groups=2, max_group_regression=1,
                          resamples=1, confidence=confidence)
        radius = math.sqrt(2 * math.log(1 / (1 - confidence)) / groups)
        exact_probability = sum(math.comb(groups, wins) for wins in range(groups + 1)
                                if (2 * wins / groups - 1) > radius) / (2 ** groups)
        rng = random.Random(717)
        accepted = 0
        for experiment in range(experiments):
            cases = []
            for group in range(groups):
                winner = rng.randrange(2)
                cases.append(case("%s-%s" % (experiment, group), baseline=1 - winner,
                                  candidate=winner, repeats=1))
            accepted += compare(cases, settings)["evidenceStatus"] == "improved"
        observed_probability = accepted / experiments
        # 对本次 Monte Carlo 自身给出 99% 双侧 Hoeffding 误差界。
        calibration_error = math.sqrt(math.log(2 / 0.01) / (2 * experiments))
        self.assertLessEqual(exact_probability, 1 - confidence)
        self.assertLessEqual(abs(observed_probability - exact_probability), calibration_error)
        self.assertLessEqual(observed_probability, (1 - confidence) + calibration_error)
        # 预设种子下确实出现误报，明确拒绝“验收通过保证永不误报”的说法。
        self.assertGreater(accepted, 0)
        self.assertEqual(accepted, 23)


if __name__ == "__main__":
    unittest.main()
