"""配置候选不能被旧提示词交付路径误认为已验收的提示词。"""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fuju_rsi.benchmark import digest
from fuju_rsi.core import optimize
from fuju_rsi.demo import build_demo_agent
from fuju_rsi.reporting import write_report
from fuju_rsi.runtime import initialize_prompt, prompt_status, publish_prompt
from fuju_rsi.verification import freeze_candidate, validate_candidate
from fuju_rsi.workspace import ExperimentManager


ROOT = Path(__file__).resolve().parents[1]


class FileCandidateBoundaryTests(unittest.TestCase):
    def result(self):
        spec = build_demo_agent()
        result = optimize(spec, max_trials=1)
        result['id'] = 'file-boundary-probe'
        return spec, result

    def test_default_prompt_and_unknown_kind(self):
        spec = build_demo_agent()
        self.assertEqual(spec.candidate_kind, 'prompt')
        with self.assertRaises(ValueError):
            replace(spec, candidate_kind='unknown')

    def test_prompt_manager_rejects_file_agent(self):
        spec = replace(build_demo_agent(), candidate_kind='files')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'compare-files'):
                ExperimentManager(directory, [spec], telemetry_mode='log')

    def test_file_record_cannot_export_prompt_artifacts(self):
        _, record = self.result()
        record['candidateKind'] = 'files'
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report'
            with self.assertRaisesRegex(ValueError, '配置|files'):
                write_report(record, output)
            self.assertFalse(output.exists())

    def test_file_record_cannot_freeze_prompt_candidate(self):
        spec, record = self.result()
        record['candidateKind'] = 'files'
        with self.assertRaisesRegex(ValueError, '配置|files'):
            freeze_candidate(spec, record, source_files=['src/fuju_rsi/demo.py'],
                             environment={'model': 'offline'}, root=ROOT)

    def test_file_package_cannot_validate_as_prompt(self):
        spec, record = self.result()
        candidate = freeze_candidate(spec, record, source_files=['src/fuju_rsi/demo.py'],
                                     environment={'model': 'offline'}, root=ROOT)
        candidate['candidateKind'] = 'files'
        candidate['digest'] = digest({k: v for k, v in candidate.items() if k != 'digest'})
        with self.assertRaisesRegex(ValueError, '配置|files'):
            validate_candidate(candidate, ROOT)

    def test_file_record_cannot_publish_prompt_even_if_adoptable_flag_is_true(self):
        class Manager:
            def get(self, experiment_id):
                return {'status': 'completed', 'adoptable': True, 'candidateKind': 'files',
                        'agentId': 'test', 'baselinePrompt': 'old', 'bestPrompt': '{"files":[]}'}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'prompt.json'
            original = initialize_prompt(path, agent_id='test', prompt='old')
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, '配置|files'):
                publish_prompt(Manager(), 'id', path, expected_version=original['version'])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(prompt_status(path)['prompt'], 'old')

    def test_factory_exit_cannot_report_process_success_or_leave_running_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'exit_factory.py').write_text('def build():\n    raise SystemExit(0)\n')
            process = subprocess.run([sys.executable, '-m', 'fuju_rsi', 'compare-files',
                                      '--agent', 'exit_factory:build', '--workspace', str(root / 'work')],
                                     cwd=root, env=dict(os.environ, PYTHONPATH=str(ROOT / 'src')),
                                     capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 1)
            summary = json.loads(process.stdout)
            self.assertEqual(summary['status'], 'failed')
            self.assertFalse(summary['searchComplete'])
            self.assertEqual(json.loads(Path(summary['record']).read_text())['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
