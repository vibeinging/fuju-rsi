"""在新环境验证 wheel；--with-trace 会从 PyPI 安装可选 Trace 插件。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


SMOKE = r'''
import importlib.util
import json
from pathlib import Path
import re
import sys
import threading
import urllib.request
import fuju_rsi
from fuju_rsi.demo import build_demo_agent
from fuju_rsi.server import create_server
from fuju_rsi.workspace import ExperimentManager

workspace, mode = Path(sys.argv[1]), sys.argv[2]
with_trace = mode == 'trace'
assert (importlib.util.find_spec('fuju_trace') is not None) == (mode != 'core')
agent = build_demo_agent()
manager = ExperimentManager(workspace, [agent])
server = worker = None
try:
    record = manager.start(agent_id=agent.id, max_trials=1)
    result = manager.wait(record['id'], timeout=20)
    assert result['searchComplete'] and not result['adoptable'], result
    health = manager.telemetry.health()
    assert health['backend'] == ('fuju-trace' if with_trace else 'log'), health
    assert health['droppedEvents'] == 0, health
    assert health['sentEvents' if with_trace else 'loggedEvents'] == 2, health
    if not with_trace:
        events = [json.loads(line) for line in (workspace / 'telemetry/events.jsonl').read_text().splitlines()]
        assert len(events) == 2
        assert agent.baseline_prompt not in json.dumps(events)
    # 真实安装的控制台首页和资产必须可读，不能只验证源码目录。
    server = create_server(manager, port=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    html = urllib.request.urlopen(base + '/', timeout=5).read().decode()
    assets = re.findall(r'(?:src|href)="(\./assets/[^\"]+)"', html)
    assert assets, html
    for asset in assets:
        assert urllib.request.urlopen(base + '/' + asset[2:], timeout=5).read()
finally:
    if server:
        server.shutdown()
        server.server_close()
        worker.join(5)
    manager.close()
assert manager.telemetry.health()['closed']
if with_trace:
    from fuju_trace import connect
    from fuju_rsi.plugins.fuju_trace import FujuTracePlugin
    with connect(sqlite_path=workspace / 'telemetry/trace.sqlite', tenant_id=1) as db:
        spans = db.list_spans()['items']
        assert len(spans) == 2, spans
        assert {row['attrs']['status'] for row in spans} == {'running', 'completed'}, spans
        assert agent.baseline_prompt not in json.dumps(spans)
        # 调用方可注入任一 Trace store，插件不能关闭借来的连接。
        plugin = FujuTracePlugin(db=db)
        plugin.emit({'type':'consumer.check','experimentId':'consumer','agentId':'demo','status':'completed'})
        plugin.close()
        assert db.ping()
        from fuju_rsi.telemetry import Telemetry
        class FailingOnce:
            def __init__(self):
                self.calls = 0
            def ingest(self, events, **kwargs):
                self.calls += 1
                assert len(events) == 2, events
                if self.calls == 1:
                    raise RuntimeError('private connection details')
                db.ingest(events, **kwargs)
        plugin = FujuTracePlugin(db=FailingOnce())
        telemetry = Telemetry(workspace / 'fallback', plugin=plugin)
        telemetry.emit('experiment.started', experiment_id='failed-write', agent_id='demo', status='running')
        telemetry.emit('experiment.finished', experiment_id='recovered-write', agent_id='demo', status='completed')
        assert telemetry.health()['loggedEvents'] == 1 and telemetry.health()['sentEvents'] == 1
        assert not telemetry.health()['degraded']
        assert db.list_spans()['total'] == 4
        assert not db.list_spans(filters={'attrs': {'experimentId':'failed-write'}})['items']
        assert 'private connection' not in telemetry.path.read_text()
        telemetry.close()
        plugin.close()
print('installed consumer:', mode, 'OK')
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel', type=Path)
    parser.add_argument('--with-trace', action='store_true')
    parser.add_argument('--trace-version', default='0.1.10', help='published Trace version used for verification')
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    if wheel.suffix != '.whl':
        parser.error('provide a built wheel')
    environment = dict(os.environ)
    environment.pop('PYTHONPATH', None)
    with tempfile.TemporaryDirectory(prefix='fuju-rsi-consumer-') as folder:
        root = Path(folder)
        venv.EnvBuilder(with_pip=True).create(root / 'venv')
        binaries = root / 'venv' / ('Scripts' if os.name == 'nt' else 'bin')
        python = str(binaries / ('python.exe' if os.name == 'nt' else 'python'))
        command = [python, '-m', 'pip', 'install', '--no-deps', str(wheel)]
        subprocess.run(command, check=True, timeout=120, env=environment)
        if args.with_trace:
            subprocess.run([python, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                            str(wheel) + '[trace]', 'fuju-trace==' + args.trace_version],
                           check=True, timeout=120, env=environment)
        subprocess.run([python, '-m', 'pip', 'check'], check=True, timeout=30, env=environment)
        subprocess.run([python, '-I', '-c', SMOKE, str(root / 'workspace'),
                        'trace' if args.with_trace else 'core'],
                       cwd=root, env=environment, check=True, timeout=60)
        result = subprocess.run([python, '-I', '-m', 'fuju_rsi', 'optimize', '--demo',
                                 '--workspace', str(root / 'cli'), '--telemetry', 'log', '--max-trials', '1'],
                                cwd=root, env=environment, capture_output=True, text=True, timeout=30, check=True)
        record = json.loads(result.stdout)
        assert record['searchComplete'] and not record['adoptable'], record
        print('installed CLI and report: OK')
        if args.with_trace:
            # 已装 SDK 但没装存储插件是独立的常见状态，真实卸掉 adapter 验证回退。
            subprocess.run([python, '-m', 'pip', 'uninstall', '-y', 'fuju-trace-sql'],
                           check=True, timeout=30, env=environment)
            subprocess.run([python, '-I', '-c', SMOKE, str(root / 'sdk-only'), 'sdk-only'],
                           cwd=root, env=environment, check=True, timeout=60)


if __name__ == '__main__':
    main()
