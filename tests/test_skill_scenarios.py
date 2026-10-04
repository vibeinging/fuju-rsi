"""复制后的 Skill 能发现社区场景，同时拒绝越界指南和坏清单。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "skills" / "fuju-tune"


class SkillScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.skill = Path(temporary.name) / "installed" / "fuju-tune"
        shutil.copytree(SOURCE, self.skill, ignore=shutil.ignore_patterns("__pycache__"))
        self.script = self.skill / "scripts" / "scenarios.py"

    def run_cli(self, *arguments):
        process = subprocess.run([sys.executable, str(self.script), *arguments],
                                 cwd=self.skill.parent, capture_output=True, text=True,
                                 timeout=10)
        return process.returncode, json.loads(process.stdout)

    def test_copied_skill_discovers_builtin_and_new_draft_without_editing_entrypoint(self):
        community = self.skill / "scenarios" / "tool-agent"
        community.mkdir()
        (community / "guide.md").write_text("# 工具调用场景\n", encoding="utf-8")
        (community / "manifest.json").write_text(json.dumps({
            "schemaVersion": 1, "id": "tool-agent", "title": "工具调用",
            "description": "检查工具调用结果。", "status": "draft", "guide": "guide.md",
            "targets": ["tool-config", "prompt"], "automatedTargets": [],
        }), encoding="utf-8")
        # 场景目录中即使出现 Python 文件，发现脚本也只读 JSON 与 Markdown。
        marker = community / "executed"
        (community / "__init__.py").write_text("from pathlib import Path\nPath(%r).touch()\n" % str(marker), encoding="utf-8")
        code, catalog = self.run_cli("list")
        self.assertEqual(code, 0, catalog)
        self.assertEqual([item["id"] for item in catalog["scenarios"]], ["ask-data", "tool-agent"])
        self.assertEqual(catalog["scenarios"][1]["status"], "draft")
        self.assertEqual(catalog["scenarios"][1]["targets"], ["tool-config", "prompt"])
        self.assertFalse(marker.exists())
        code, item = self.run_cli("show", "tool-agent")
        self.assertEqual(code, 0, item)
        self.assertEqual(Path(item["guidePath"]).read_text(), "# 工具调用场景\n")
        self.assertEqual(self.run_cli("check")[0], 0)

    def test_manifest_cannot_reference_guide_outside_its_scene(self):
        manifest = self.skill / "scenarios" / "ask-data" / "manifest.json"
        value = json.loads(manifest.read_text())
        value["guide"] = "../verification.md"
        manifest.write_text(json.dumps(value), encoding="utf-8")
        code, outcome = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["status"], "failed")
        self.assertIn("指南", outcome["error"])

    def test_duplicate_fields_and_symlink_are_rejected(self):
        manifest = self.skill / "scenarios" / "ask-data" / "manifest.json"
        manifest.write_text('{"schemaVersion":1,"schemaVersion":1}', encoding="utf-8")
        self.assertEqual(self.run_cli("check")[0], 1)
        manifest.write_text(json.dumps({
            "schemaVersion": 1, "id": "ask-data", "title": "智能问数",
            "description": "问数场景", "status": "integrated", "guide": "linked.md",
            "targets": ["prompt"], "automatedTargets": ["prompt"],
        }), encoding="utf-8")
        (manifest.parent / "linked.md").symlink_to(manifest.parent / "guide.md")
        code, outcome = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertEqual(outcome["status"], "failed")

    def test_boolean_schema_version_is_rejected(self):
        manifest = self.skill / "scenarios" / "ask-data" / "manifest.json"
        value = json.loads(manifest.read_text())
        value["schemaVersion"] = True
        manifest.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.run_cli("check")[0], 1)

    def test_draft_cannot_claim_automated_config_changes(self):
        manifest = self.skill / "scenarios" / "ask-data" / "manifest.json"
        value = json.loads(manifest.read_text())
        value["status"] = "draft"
        value["automatedTargets"] = ["metric-config"]
        manifest.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(self.run_cli("check")[0], 1)


if __name__ == "__main__":
    unittest.main()
