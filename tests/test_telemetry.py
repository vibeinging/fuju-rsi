"""观测插件可以被替换；Trace 失败后保留最少量本地状态。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import threading

from fuju_rsi.demo import build_demo_agent
from fuju_rsi.telemetry import Telemetry
from fuju_rsi.workspace import ExperimentManager


class Plugin:
    def __init__(self, result=True):
        self.result = result
        self.events = []
        self.closed = 0

    def emit(self, event):
        self.events.append(event)
        return self.result

    def close(self):
        self.closed += 1


class TelemetryTests(unittest.TestCase):
    def test_default_plugin_uses_workspace_sqlite_and_closes_once(self):
        plugin = Plugin()
        with tempfile.TemporaryDirectory() as directory, patch(
                'fuju_rsi.plugins.fuju_trace.FujuTracePlugin', return_value=plugin) as factory:
            telemetry = Telemetry(directory)
            factory.assert_called_once_with(Path(directory) / 'telemetry' / 'trace.sqlite')
            telemetry.close()
            telemetry.close()
            self.assertEqual(plugin.closed, 1)
            self.assertTrue(telemetry.health()['closed'])

    def test_missing_sdk_falls_back_without_affecting_experiment(self):
        with tempfile.TemporaryDirectory() as directory, patch(
                'fuju_rsi.plugins.fuju_trace.FujuTracePlugin', side_effect=ImportError):
            telemetry = Telemetry(directory)
            telemetry.emit('experiment.started', experiment_id='abc', agent_id='agent', status='running')
            self.assertEqual(json.loads(telemetry.path.read_text())['experimentId'], 'abc')
            self.assertEqual(telemetry.health()['lastErrorType'], 'trace_sdk_unavailable')

    def test_trace_plugin_gets_only_public_status_and_log_is_not_written(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Plugin()
            agent = build_demo_agent()
            manager = ExperimentManager(directory, [agent], telemetry_plugin=plugin)
            try:
                started = manager.start(agent_id=agent.id, max_trials=1)
                result = manager.wait(started["id"], timeout=10)
                self.assertEqual(result["status"], "completed")
                self.assertEqual([item["type"] for item in plugin.events],
                                 ["experiment.started", "experiment.finished"])
                self.assertEqual(manager.capabilities()["telemetry"]["backend"], "fuju-trace")
                self.assertEqual(manager.telemetry.health()['sentEvents'], 2)
                self.assertFalse((Path(directory) / "telemetry" / "events.jsonl").exists())
                for event in plugin.events:
                    self.assertEqual(set(event) - {"type", "timestamp", "experimentId", "agentId",
                                                   "status", "usedCalls"}, set())
                    self.assertNotIn(agent.baseline_prompt, json.dumps(event))
            finally:
                manager.close()

    def test_unavailable_plugin_falls_back_to_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Plugin(result=False)
            telemetry = Telemetry(directory, plugin=plugin)
            telemetry.emit("experiment.started", experiment_id="abc", agent_id="agent", status="running")
            event = json.loads(telemetry.path.read_text().strip())
            self.assertEqual(event["experimentId"], "abc")
            self.assertEqual(telemetry.health()["backend"], "log")
            self.assertTrue(telemetry.health()["degraded"])
            self.assertEqual(telemetry.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(telemetry.health()['loggedEvents'], 1)

    def test_plugin_cannot_add_private_fields_to_fallback_log(self):
        class MutatingPlugin:
            def emit(self, event):
                event['prompt'] = 'private prompt'
                event['experimentId'] = 'changed'
                raise RuntimeError('private credentials')
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Telemetry(directory, plugin=MutatingPlugin())
            telemetry.emit('experiment.started', experiment_id='abc', agent_id='agent', status='running')
            record = json.loads(telemetry.path.read_text())
            self.assertEqual(record['experimentId'], 'abc')
            self.assertNotIn('prompt', record)
            self.assertEqual(telemetry.health()['lastErrorType'], 'RuntimeError')
            self.assertNotIn('private', json.dumps(telemetry.health()))

    def test_log_write_failure_is_visible_even_in_log_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Telemetry(directory, mode='log')
            with patch('fuju_rsi.telemetry.os.open', side_effect=PermissionError):
                telemetry.emit('experiment.finished', experiment_id='abc', agent_id='agent', status='failed')
            self.assertTrue(telemetry.health()['degraded'])
            self.assertEqual(telemetry.health()['droppedEvents'], 1)
            self.assertEqual(telemetry.health()['loggedEvents'], 0)
            telemetry.emit('experiment.finished', experiment_id='abc', agent_id='agent', status='failed')
            self.assertFalse(telemetry.health()['degraded'])
            self.assertEqual(telemetry.health()['droppedEvents'], 1)
            self.assertEqual(telemetry.health()['loggedEvents'], 1)

    def test_injected_plugin_is_borrowed_and_no_events_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Plugin()
            telemetry = Telemetry(directory, plugin=plugin)
            telemetry.close()
            telemetry.emit('experiment.finished', experiment_id='abc', agent_id='agent', status='failed')
            self.assertEqual(plugin.closed, 0)
            self.assertEqual(plugin.events, [])
            self.assertEqual(telemetry.health()['droppedEvents'], 1)

    def test_manager_defers_plugin_close_until_worker_finishes(self):
        plugin, entered, release = Plugin(), threading.Event(), threading.Event()
        agent = build_demo_agent()
        runner = agent.runner
        def blocked_runner(prompt, request):
            entered.set()
            self.assertTrue(release.wait(5))
            return runner(prompt, request)
        agent.runner = blocked_runner
        with tempfile.TemporaryDirectory() as directory, patch(
                'fuju_rsi.plugins.fuju_trace.FujuTracePlugin', return_value=plugin):
            manager = ExperimentManager(directory, [agent])
            try:
                record = manager.start(agent_id=agent.id)
                self.assertTrue(entered.wait(5))
                manager.close(timeout=0)
                self.assertEqual(plugin.closed, 0)
                release.set()
                manager.wait(record['id'], timeout=5)
                self.assertEqual(plugin.closed, 1)
                self.assertEqual([e['type'] for e in plugin.events],
                                 ['experiment.started', 'experiment.finished'])
                self.assertEqual(plugin.events[-1]['status'], 'cancelled')
            finally:
                release.set()
                manager.close()

    def test_log_mode_does_not_call_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            plugin = Plugin()
            telemetry = Telemetry(directory, mode="log", plugin=plugin)
            telemetry.emit("experiment.finished", experiment_id="abc", agent_id="agent", status="failed")
            self.assertEqual(plugin.events, [])
            self.assertEqual(json.loads(telemetry.path.read_text())["status"], "failed")


if __name__ == "__main__":
    unittest.main()
