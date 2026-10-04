"""在新环境验证 wheel；--with-trace 会从 PyPI 安装可选 Trace 插件。"""
import argparse
import json
import os
from pathlib import Path
import shutil
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

CONNECT_APP = '''
import importlib.util, json, sys
assert importlib.util.find_spec("fuju_rsi") is None
request = json.load(sys.stdin)
print(json.dumps({"amount": sum(request["amounts"])}))
'''

CONNECT_ADAPTER = '''
import json, subprocess, sys
from pathlib import Path
from fuju_rsi import AgentSpec, Evaluation, Example, Prediction

def runner(prompt, request):
    with Path("calls.txt").open("a") as stream:
        stream.write(prompt + "\\n")
    result = subprocess.run([sys.executable, "-I", "-S", "business.py"],
                            input=json.dumps(request), capture_output=True, text=True,
                            check=True, timeout=5)
    return Prediction(json.loads(result.stdout))

def evaluate(expected, observed):
    return Evaluation(float(expected == observed.output), "Known arithmetic regression")

def proposer(*args):
    raise AssertionError("baseline has no proposer calls")

def build_agent():
    return AgentSpec("clean-connect", "Consumer connection", "original product config", [
        Example("train", {"amounts": [2, 4]}, {"amount": 6}, "train"),
        Example("validation", {"amounts": [3, 5]}, {"amount": 8}, "validation"),
    ], runner, proposer, evaluate)
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
        # 在外部项目用已安装包运行新配置入口，再复制 Skill 走同一条链路。
        # 不设置源码 PYTHONPATH，普通业务子进程 -I -S 下不能依赖 RSI。
        business = root / 'file-business'
        business.mkdir()
        repository = Path(__file__).resolve().parents[1]
        shutil.copyfile(repository / 'examples/file_config_fixture.py', business / 'file_config_fixture.py')
        shutil.copytree(repository / 'examples/file_config_project', business / 'file_config_project')
        copied_skill = root / 'copied-skill'
        shutil.copytree(repository / 'skills/fuju-tune', copied_skill,
                        ignore=shutil.ignore_patterns('__pycache__'))
        # 陌生项目先生成连接骨架，再模拟编码 Agent 绑定真实业务命令。
        # 安装 wheel、复制 Skill 和业务进程均不使用源码 PYTHONPATH。
        connection_project = root / 'connect-business'
        connection_project.mkdir()
        (connection_project / 'business.py').write_text(CONNECT_APP, encoding='utf-8')
        subprocess.run([python, '-I', '-m', 'fuju_rsi', 'connect', 'init', '--kind', 'custom',
                        '--project', str(connection_project)], cwd=root, env=environment,
                       capture_output=True, text=True, check=True, timeout=30)
        adapter = connection_project / '.fuju-rsi/connection/fuju_connection_adapter.py'
        adapter.write_text(CONNECT_ADAPTER, encoding='utf-8')
        connect_helper = copied_skill / 'scripts/connect.py'
        check = subprocess.run([python, '-I', str(connect_helper), 'check', '--project', str(connection_project)],
                               cwd=root, env=environment, capture_output=True, text=True, check=True, timeout=30)
        assert json.loads(check.stdout)['status'] == 'ready', check.stdout
        assert not (connection_project / 'calls.txt').exists()
        baseline_records = []
        for command in ([python, '-I', '-m', 'fuju_rsi', 'connect'], [python, '-I', str(connect_helper)]):
            baseline = subprocess.run(command + ['baseline', '--project', str(connection_project), '--max-calls', '2'],
                                      cwd=root, env=environment, capture_output=True, text=True, check=True, timeout=30)
            response = json.loads(baseline.stdout)
            assert response['baselineComplete'] and not response['adoptable'], response
            assert response['mode'] == 'baseline' and response['usedCalls'] == 2, response
            assert Path(response['artifacts']['report']).is_file(), response
            baseline_records.append(response['record'])
        assert baseline_records[0] != baseline_records[1]
        assert len((connection_project / 'calls.txt').read_text().splitlines()) == 4
        print('installed connect CLI, copied Skill and isolated business baseline: OK')
        subprocess.run([python, '-I', '-c',
                        'import sys;sys.path.insert(0,".");import file_config_fixture as f;f.build_candidate("candidate.json")'],
                       cwd=business, env=environment, check=True, timeout=30)
        config_command = [python, '-I', '-m', 'fuju_rsi', 'compare-files', '--agent',
                          'file_config_fixture:build_agent', '--workspace', str(root / 'file-cli'),
                          '--candidate-file', 'candidate.json', '--max-calls', '5']
        result = subprocess.run(config_command, cwd=business, env=environment, capture_output=True,
                                text=True, check=True, timeout=30)
        config = json.loads(result.stdout)
        assert config['searchComplete'] and not config['adoptable'], config
        assert config['candidateKind'] == 'files' and config['trials'][1]['validation']['score'] == 1, config
        delivery = Path(config['artifacts']['candidate']).parent
        clean = subprocess.run([python, '-I', '-S', '-c',
                                'import importlib.util,sys;assert importlib.util.find_spec("fuju_rsi") is None;'
                                'sys.path.insert(0,".");import file_config_fixture as f;'
                                'assert f.run_business(sys.argv[1],{"question":"门店 A 的销售额是多少？"})'
                                '["result"]["rows"] == [[100]]', str(delivery / 'candidate')],
                               cwd=business, env=environment, capture_output=True, text=True, timeout=30)
        assert clean.returncode == 0, clean.stderr
        skill = subprocess.run([python, '-I', str(copied_skill / 'scripts/run_experiment.py'),
                                '--candidate-kind', 'files', '--agent', 'file_config_fixture:build_agent',
                                '--workspace', str(root / 'file-skill'), '--candidate-file', 'candidate.json',
                                '--max-calls', '5'], cwd=business, env=environment,
                               capture_output=True, text=True, timeout=30)
        assert skill.returncode == 0, (skill.stdout, skill.stderr)
        assert json.loads(skill.stdout)['searchComplete']
        print('installed file CLI, copied Skill and detached config: OK')
        if args.with_trace:
            # 已装 SDK 但没装存储插件是独立的常见状态，真实卸掉 adapter 验证回退。
            subprocess.run([python, '-m', 'pip', 'uninstall', '-y', 'fuju-trace-sql'],
                           check=True, timeout=30, env=environment)
            subprocess.run([python, '-I', '-c', SMOKE, str(root / 'sdk-only'), 'sdk-only'],
                           cwd=root, env=environment, check=True, timeout=60)


if __name__ == '__main__':
    main()
