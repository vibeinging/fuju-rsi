"""真实子进程验证两个宿主的安装和复制边界，不调用模型或 SDK。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "fuju-tune"


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "business"
        self.project.mkdir()
        (self.project / "app.py").write_text("keep application", encoding="utf-8")
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home))
        self.environment.pop("PYTHONPATH", None)

    def run_installer(self, *arguments, source=SKILL):
        result = subprocess.run([sys.executable, str(source / "scripts" / "install.py"), *arguments],
                                cwd=self.project, env=self.environment, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(result.stderr, "")
        return result.returncode, json.loads(result.stdout)

    def source_copy(self, name="copy"):
        source = self.root / name / "fuju-tune"
        shutil.copytree(SKILL, source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
        return source

    def test_both_project_hosts_install_complete_resources_and_reinstall(self):
        code, result = self.run_installer("--host", "both", "--project", str(self.project))
        self.assertEqual(code, 0)
        self.assertFalse(result["sdkInstalled"])
        self.assertEqual([item["host"] for item in result["installations"]], ["codex", "claude"])
        for summary, host_directory in zip(result["installations"], (".agents", ".claude")):
            destination = self.project / host_directory / "skills" / "fuju-tune"
            self.assertEqual(summary["directory"], str(destination))
            self.assertEqual(summary["status"], "installed")
            for item in SKILL.rglob("*"):
                if item.is_file() and "__pycache__" not in item.parts and item.suffix not in (".pyc", ".pyo"):
                    self.assertEqual((destination / item.relative_to(SKILL)).read_bytes(), item.read_bytes())
        code, repeated = self.run_installer("--host", "both", "--project", str(self.project))
        self.assertEqual(code, 0)
        self.assertEqual([item["status"] for item in repeated["installations"]], ["unchanged", "unchanged"])
        self.assertEqual((self.project / "app.py").read_text(), "keep application")

    def test_user_hosts_and_custom_destination_use_explicit_locations(self):
        code, result = self.run_installer("--host", "both")
        self.assertEqual(code, 0)
        self.assertTrue((self.home / ".agents" / "skills" / "fuju-tune" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "fuju-tune" / "SKILL.md").is_file())
        custom = self.root / "custom"
        code, result = self.run_installer("--host", "codex", "--destination", str(custom))
        self.assertEqual(code, 0)
        self.assertEqual(result["installations"][0]["directory"], str(custom / "fuju-tune"))
        custom_both = self.root / "custom-both"
        code, result = self.run_installer("--host", "both", "--destination", str(custom_both))
        self.assertEqual(code, 0)
        for host in ("codex", "claude"):
            self.assertTrue((custom_both / host / "fuju-tune" / "SKILL.md").is_file())

    def test_different_installation_and_extra_resources_are_preserved(self):
        for modification in ("edit", "extra"):
            with self.subTest(modification=modification):
                custom = self.root / modification
                self.assertEqual(self.run_installer("--host", "claude", "--destination", str(custom))[0], 0)
                destination = custom / "fuju-tune"
                changed = destination / ("SKILL.md" if modification == "edit" else "private-note.md")
                changed.write_text("USER_PRIVATE_CONTENT", encoding="utf-8")
                code, result = self.run_installer("--host", "claude", "--destination", str(custom))
                self.assertEqual(code, 1)
                self.assertNotIn("USER_PRIVATE_CONTENT", json.dumps(result))
                self.assertEqual(changed.read_text(), "USER_PRIVATE_CONTENT")

    def test_both_prechecks_conflict_before_creating_other_host(self):
        self.assertEqual(self.run_installer("--host", "claude", "--project", str(self.project))[0], 0)
        changed = self.project / ".claude" / "skills" / "fuju-tune" / "SKILL.md"
        changed.write_text("keep user version")
        self.assertEqual(self.run_installer("--host", "both", "--project", str(self.project))[0], 1)
        self.assertFalse((self.project / ".agents").exists())
        self.assertEqual(changed.read_text(), "keep user version")

    def test_copied_installer_and_copied_helper_work_without_checkout(self):
        copied = self.source_copy()
        custom = self.root / "installed"
        code, result = self.run_installer("--host", "claude", "--destination", str(custom), source=copied)
        self.assertEqual(code, 0)
        helper = custom / "fuju-tune" / "scripts" / "scenarios.py"
        execution = subprocess.run([sys.executable, str(helper), "list"], cwd=self.project,
                                   env=self.environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(execution.returncode, 0, execution.stderr)
        self.assertEqual(json.loads(execution.stdout)["scenarios"][0]["id"], "ask-data")
        second = self.root / "installed-again"
        code, result = self.run_installer("--host", "codex", "--destination", str(second),
                                          source=custom / "fuju-tune")
        self.assertEqual(code, 0)

    def test_source_and_destination_overlap_are_rejected(self):
        source = self.source_copy()
        for arguments in (("--project", str(source)), ("--destination", str(source.parent))):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.run_installer("--host", "codex", *arguments, source=source)[0], 1)
        self.assertFalse((source / ".agents").exists())
        nested = self.source_copy("nested-root/fuju-tune/nested")
        code, result = self.run_installer("--host", "claude", "--destination", str(self.root / "nested-root"),
                                          source=nested)
        self.assertEqual(code, 1)

    @unittest.skipIf(os.name == "nt", "创建符号链接可能要求 Windows 额外权限")
    def test_source_resource_and_path_symlinks_are_rejected(self):
        source = self.source_copy()
        secret = self.root / "private.txt"
        secret.write_text("PRIVATE_VALUE")
        (source / "references" / "linked.md").symlink_to(secret)
        code, result = self.run_installer("--host", "codex", "--project", str(self.project), source=source)
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE_VALUE", json.dumps(result))
        self.assertFalse((self.project / ".agents").exists())
        (source / "references" / "linked.md").unlink()
        linked = self.root / "linked-skill"
        linked.symlink_to(source, target_is_directory=True)
        self.assertEqual(self.run_installer("--host", "codex", "--project", str(self.project), source=linked)[0], 1)

    @unittest.skipIf(os.name == "nt", "创建符号链接可能要求 Windows 额外权限")
    def test_destination_symlink_and_parent_symlink_are_rejected(self):
        real = self.root / "real"
        real.mkdir()
        parent = self.root / "linked-parent"
        parent.symlink_to(real, target_is_directory=True)
        self.assertEqual(self.run_installer("--host", "codex", "--destination", str(parent))[0], 1)
        destination = self.root / "target"
        destination.mkdir()
        (destination / "fuju-tune").symlink_to(real, target_is_directory=True)
        self.assertEqual(self.run_installer("--host", "codex", "--destination", str(destination))[0], 1)
        self.assertEqual(list(real.iterdir()), [])

    def test_runtime_cache_does_not_make_reinstallation_fail(self):
        custom = self.root / "cached"
        self.assertEqual(self.run_installer("--host", "codex", "--destination", str(custom))[0], 0)
        cache = custom / "fuju-tune" / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "helper.cpython.pyc").write_bytes(b"runtime cache")
        code, result = self.run_installer("--host", "codex", "--destination", str(custom))
        self.assertEqual(code, 0)
        self.assertEqual(result["installations"][0]["status"], "unchanged")

    def test_missing_project_is_rejected_without_creating_it(self):
        missing = self.root / "missing"
        code, result = self.run_installer("--host", "codex", "--project", str(missing))
        self.assertEqual(code, 1)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
