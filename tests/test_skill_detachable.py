"""业务程序不依赖 yiTrace；外部调优后移除工具，普通提示词配置仍独立生效。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
APP = '''
import argparse
import json
from pathlib import Path
import sqlite3
import sys

parser = argparse.ArgumentParser()
parser.add_argument("--prompt-file", required=True)
args = parser.parse_args()
prompt = Path(args.prompt_file).read_text(encoding="utf-8")
request = json.load(sys.stdin)
db = sqlite3.connect(":memory:")
try:
    db.execute("CREATE TABLE items (amount INTEGER)")
    db.executemany("INSERT INTO items VALUES (?)", [(v,) for v in request["values"]])
    if "SUM" in prompt:
        amount = db.execute("SELECT SUM(amount) FROM items").fetchone()[0]
    else:
        amount = db.execute("SELECT COUNT(amount) FROM items").fetchone()[0]
    print(json.dumps({"amount": amount}))
finally:
    db.close()
'''

ADAPTER = '''
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from fuju_rsi import AgentSpec, Evaluation, Example, Prediction

def build_agent():
    app = Path(os.environ["TEST_BUSINESS_APP"])
    def runner(prompt, request):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "prompt.txt"
            config.write_text(prompt, encoding="utf-8")
            result = subprocess.run([sys.executable, "-I", "-S", str(app), "--prompt-file", str(config)],
                                    input=json.dumps(request), capture_output=True, text=True,
                                    timeout=5, check=True)
            return Prediction(json.loads(result.stdout))
    def evaluate(expected, prediction):
        return Evaluation(float(expected == prediction.output), "fixed arithmetic result")
    def proposer(*args):
        raise AssertionError("candidate comes from a plain text file")
    return AgentSpec("detachable", "External evaluation", "COUNT", [
        Example("train", {"values": [2, 4]}, {"amount": 6}, "train"),
        Example("validation", {"values": [3, 5]}, {"amount": 8}, "validation"),
        Example("test", {"values": [5, 7]}, {"amount": 12}, "test"),
    ], runner, proposer, evaluate)

def reset():
    # 每次 runner 都启动全新进程和内存 SQLite，业务状态不会跨案例共享。
    pass
'''

FREEZE = '''
import json
import os
from pathlib import Path
import sys
from eval_adapter import build_agent
from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.verification import freeze_candidate

root = Path(os.environ["TEST_PROJECT_ROOT"])
record = json.loads(Path(sys.argv[1]).read_text())
candidate = freeze_candidate(
    build_agent(), record, root=root,
    source_files=[root / "evaluation-tools/eval_adapter.py", root / "product/app.py"],
    environment={"runtime": "python-stdlib", "fixtures": "detachable-protocol-v2"},
    policy=AcceptancePolicy(repeats=2, min_groups=30, resamples=200),
)
Path(sys.argv[2]).write_text(json.dumps(candidate))
print(json.dumps({"candidateDigest": candidate["digest"], "candidateId": candidate["id"]}))
'''

VERIFY = '''
import json
import os
from pathlib import Path
from eval_adapter import build_agent, reset
from fuju_rsi import Example
from fuju_rsi.acceptance import AcceptancePolicy
from fuju_rsi.verification import (
    VerificationSpec, initialize_authority, validate_receipt, verify_candidate,
)

root = Path(os.environ["TEST_PROJECT_ROOT"])
tooling = root / "evaluation-tools"
candidate = json.loads((tooling / "frozen-candidate.json").read_text())
cases = json.loads((tooling / "private-verifier/cases.json").read_text())
agent = build_agent()
spec = VerificationSpec(
    examples=[Example(**case) for case in cases], runner=agent.runner,
    evaluator=agent.evaluator, reset=reset,
    policy=AcceptancePolicy.from_dict(candidate["policy"]),
    isolation="independent",
    provenance="Protocol fixture: a separate verifier process receives 30 new source groups after candidate freeze; synthetic data, not a real customer blind evaluation.",
    environment=candidate["environment"],
)
registry = tooling / "private-verifier/history"
key_file = tooling / "private-verifier/authority.key"
initialize_authority(registry, "detachable-fixture-ci", key_file)
receipt = verify_candidate(candidate, spec, registry=registry, signing_key=key_file.read_bytes(),
                           root=root, max_calls=120)
body = validate_receipt(receipt, key_file.read_bytes())
receipt_file = tooling / "signed-receipt.json"
receipt_file.write_text(json.dumps(receipt))
print(json.dumps({"body": body, "receipt": str(receipt_file)}))
'''


class DetachableSkillTests(unittest.TestCase):
    def test_app_keeps_improvement_after_tools_and_workspace_are_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            product = root / "product"
            product.mkdir()
            app = product / "app.py"
            app.write_text(textwrap.dedent(APP), encoding="utf-8")
            original_app = app.read_bytes()
            config = product / "prompt.txt"
            config.write_text("COUNT", encoding="utf-8")

            def run_product():
                # -I -S 忽略工具 PYTHONPATH 和 site-packages；显式证明 Fuju Trace 不可导入。
                probe = subprocess.run(
                    [sys.executable, "-I", "-S", "-c",
                     "import importlib.util; assert importlib.util.find_spec('fuju_trace') is None"],
                    cwd=product, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(probe.returncode, 0, probe.stderr)
                result = subprocess.run(
                    [sys.executable, "-I", "-S", str(app), "--prompt-file", str(config)],
                    cwd=product, input=json.dumps({"values": [5, 7]}),
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)

            self.assertEqual(run_product(), {"amount": 2})
            tooling = root / "evaluation-tools"
            tooling.mkdir()
            skill = tooling / "skill"
            shutil.copytree(ROOT / "skills" / "fuju-tune", skill,
                            ignore=shutil.ignore_patterns("__pycache__"))
            (tooling / "eval_adapter.py").write_text(textwrap.dedent(ADAPTER), encoding="utf-8")
            candidate = tooling / "candidate.txt"
            candidate.write_text("Use SUM to add item amounts.", encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(ROOT / "src"),
                               TEST_BUSINESS_APP=str(app), TEST_PROJECT_ROOT=str(root))
            experiment = subprocess.run(
                [sys.executable, str(skill / "scripts" / "run_experiment.py"),
                 "--agent", "eval_adapter:build_agent", "--workspace", str(tooling / "experiments"),
                 "--candidate-file", str(candidate), "--max-calls", "5"],
                cwd=tooling, env=environment, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(experiment.returncode, 0, experiment.stderr)
            summary = json.loads(experiment.stdout)
            self.assertTrue(summary["searchComplete"])
            self.assertFalse(summary["adoptable"])
            self.assertEqual(summary["usedCalls"], 5)
            self.assertIsNone(summary["baselineTest"])
            self.assertIsNone(summary["candidateTest"])
            record = json.loads(Path(summary["record"]).read_text())
            self.assertEqual(record["bestPrompt"], candidate.read_text())
            self.assertEqual(app.read_bytes(), original_app)
            self.assertEqual(config.read_text(), "COUNT")  # 实验不修改产品配置
            self.assertEqual(run_product(), {"amount": 2})

            freeze_script = tooling / "freeze_candidate.py"
            freeze_script.write_text(textwrap.dedent(FREEZE), encoding="utf-8")
            frozen_file = tooling / "frozen-candidate.json"
            frozen = subprocess.run(
                [sys.executable, str(freeze_script), summary["record"], str(frozen_file)],
                cwd=tooling, env=environment, capture_output=True, text=True, timeout=15,
            )
            self.assertEqual(frozen.returncode, 0, frozen.stderr)
            candidate_identity = json.loads(frozen.stdout)

            # 候选固定后才交给独立验收进程这些新输入。这里检查协议与移除边界；
            # 同一测试账号的合成数据不构成真实客户的盲测或统计独立性证明。
            verifier_folder = tooling / "private-verifier"
            verifier_folder.mkdir()
            holdout_cases = [
                {"id": "holdout-" + str(index), "input": {"values": [11 + index, 101 + index]},
                 "expected": {"amount": 112 + 2 * index}, "split": "test",
                 "group_id": "unseen-group-" + str(index), "source_id": "new-fixture-" + str(index),
                 "exposure": "unseen", "critical": True}
                for index in range(30)
            ]
            (verifier_folder / "cases.json").write_text(json.dumps(holdout_cases), encoding="utf-8")
            verification_script = tooling / "verify_candidate.py"
            verification_script.write_text(textwrap.dedent(VERIFY), encoding="utf-8")
            verification = subprocess.run(
                [sys.executable, str(verification_script)], cwd=tooling, env=environment,
                capture_output=True, text=True, timeout=90,
            )
            self.assertEqual(verification.returncode, 0, verification.stderr)
            verification_result = json.loads(verification.stdout)
            accepted = verification_result["body"]
            self.assertTrue(accepted["adoptable"], accepted)
            self.assertEqual(accepted["stage"], "verification")
            self.assertEqual(accepted["evidenceStatus"], "improved")
            self.assertEqual(accepted["candidateDigest"], candidate_identity["candidateDigest"])
            self.assertEqual(accepted["verificationId"], candidate_identity["candidateId"])
            self.assertEqual(accepted["usedCalls"], 120)
            self.assertEqual(accepted["evidence"]["independentGroups"], 30)
            signed = json.loads(Path(verification_result["receipt"]).read_text())
            self.assertRegex(signed["signature"], r"^[0-9a-f]{64}$")
            self.assertEqual(signed["body"], accepted)
            self.assertEqual(config.read_text(), "COUNT")  # 验收同样不会部署到产品
            self.assertEqual(app.read_bytes(), original_app)

            # 只有实际验收、签名核验与候选身份对应都成功后才交付普通提示词文件。
            config.write_text(record["bestPrompt"], encoding="utf-8")
            shutil.rmtree(tooling)
            self.assertFalse(tooling.exists())
            self.assertEqual(run_product(), {"amount": 12})
            self.assertEqual(app.read_bytes(), original_app)
            self.assertEqual({path.name for path in product.iterdir()}, {"app.py", "prompt.txt"})
            config.write_text("COUNT", encoding="utf-8")
            self.assertEqual(run_product(), {"amount": 2})  # 原有配置回滚同样无需工具


if __name__ == "__main__":
    unittest.main()
