"""比较明确的配置文件候选；业务输出留在本地私有记录中。"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import sys
import traceback
import uuid

from .output import private_output
from .workspace import atomic_json


def add_parser(subparsers):
    parser = subparsers.add_parser('compare-files', help='compare bounded config candidates in isolated directories')
    parser.add_argument('--agent', required=True, help='local module:factory returning FileExperimentSpec')
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--candidate-file', action='append', default=[], help='versioned UTF-8 file candidate JSON')
    parser.add_argument('--max-calls', type=int, default=100, help='runner + proposer callbacks, not a money limit')
    parser.add_argument('--ask-data-bundle', help='frozen Ask Data development bundle')
    parser.add_argument('--validation-ledger', help='shared Ask Data validation-use ledger')
    parser.add_argument('--report-dir', help='new output directory')
    parser.add_argument('--name', help='experiment label')
    return parser


def _load(reference):
    from .file_runs import FileExperimentSpec
    module, separator, factory = reference.partition(':')
    if not separator or not module or not factory.isidentifier():
        raise ValueError('factory 必须使用 module:factory')
    current = str(Path.cwd())
    if current not in sys.path:
        sys.path.insert(0, current)
    spec = getattr(importlib.import_module(module), factory)()
    if not isinstance(spec, FileExperimentSpec):
        raise ValueError('配置 factory 必须返回 FileExperimentSpec')
    return spec


def _aggregate(batch):
    if not isinstance(batch, dict):
        return None
    return {**{key: batch.get(key) for key in ('score', 'passed', 'total', 'latencyMs', 'tokens', 'costUsd')},
            'evaluated': len(batch.get('cases', [])),
            'errors': sum(case.get('error') is not None for case in batch.get('cases', []))}


def run(args):
    from .file_candidates import FileCandidate
    from .file_runs import compare_file_candidates, export_file_report
    if type(args.max_calls) is not int or not 0 <= args.max_calls <= 10000 or len(args.candidate_file) > 20:
        raise ValueError('max-calls 必须为 0..10000，最多 20 个文件候选')
    if args.name is not None and len(args.name) > 120:
        raise ValueError('name 最多 120 字符')
    output = Path(args.report_dir) if args.report_dir else None
    if output and (output.exists() or output.is_symlink()):
        raise ValueError('报告目录必须是新目录')
    bundle = getattr(args, 'ask_data_bundle', None) or getattr(args, 'benchmark', None)
    workspace = Path(args.workspace).resolve()
    run_id = uuid.uuid4().hex
    directory = workspace / 'file-runs' / run_id
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    record_path = directory / 'record.json'
    log_path = directory / 'runtime.log'
    try:
        # factory、runner 和判分都可能打印业务内容；终端只发布状态与汇总。
        with log_path.open('x', encoding='utf-8') as log, private_output(log):
            previous_bundle = os.environ.get('FUJU_RSI_BENCHMARK')
            try:
                if bundle:
                    from .benchmark import load_bundle
                    # 在导入业务 factory、读取案例正文之前拒绝 holdout。
                    load_bundle(bundle, role='development')
                    os.environ['FUJU_RSI_BENCHMARK'] = str(Path(bundle).resolve())
                spec = _load(args.agent)
            finally:
                if previous_bundle is None:
                    os.environ.pop('FUJU_RSI_BENCHMARK', None)
                else:
                    os.environ['FUJU_RSI_BENCHMARK'] = previous_bundle
            scenario = getattr(args, 'scenario', None)
            if scenario:
                # Skill 脚本已经核对场景清单；领域实现仍只支持 ask-data。
                if scenario != 'ask-data' or spec.kind != scenario:
                    raise ValueError('场景与配置实验不一致')
            candidates = []
            spec.baseline.assert_current()
            for filename in args.candidate_file:
                path = Path(filename)
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                    raise ValueError('候选必须是受限大小的普通 JSON 文件')
                from .benchmark import read_json
                document = read_json(path)
                if isinstance(document, dict) and document.get('type') == 'file-comparison-delivery':
                    if set(document) != {'schemaVersion', 'type', 'baseline', 'candidate'} or document['schemaVersion'] != 1:
                        raise ValueError('配置交付包格式无效')
                    restored = FileCandidate.from_json(json.dumps(document['baseline'], ensure_ascii=False), spec.baseline)
                    if restored.digest != spec.baseline.as_candidate().digest:
                        raise ValueError('交付包原版与当前项目不一致')
                    document = document['candidate']
                candidates.append(FileCandidate.from_json(json.dumps(document, ensure_ascii=False), spec.baseline))

            def save(state):
                atomic_json(record_path, {**state, 'id': run_id, 'candidateKind': 'files'})

            result = compare_file_candidates(spec, candidates, max_calls=args.max_calls,
                                             ask_data_bundle=bundle,
                                             validation_ledger=args.validation_ledger,
                                             on_update=save)
            result['id'] = run_id
            result['name'] = args.name or spec.name
            save(result)
            artifacts = export_file_report(result, output or directory / 'report')
    except BaseException as error:
        # 异常正文也可能携带配置、题目或答案，只写入上述私有诊断文件。
        with log_path.open('a', encoding='utf-8') as log:
            traceback.print_exc(file=log)
        status = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed'
        message = '配置比较未完成，请检查本地诊断日志。'
        try:
            from .benchmark import read_json
            previous = read_json(record_path) if record_path.exists() else {}
            atomic_json(record_path, {**previous, 'id': run_id, 'stage': 'search', 'status': status,
                                      'candidateKind': 'files', 'adoptable': False,
                                      'searchComplete': False, 'message': message})
        except (OSError, ValueError):
            pass
        print(json.dumps({'status': status, 'candidateKind': 'files', 'adoptable': False,
                          'searchComplete': False, 'message': message,
                          'runtimeLog': str(log_path),
                          'record': str(record_path) if record_path.exists() else None}, ensure_ascii=False))
        return 130 if status == 'cancelled' else 1
    summary = {key: result.get(key) for key in
               ('id', 'status', 'message', 'stage', 'searchComplete', 'usedCalls', 'selectedTrialId',
                'candidateKind', 'selectedFileCandidateDigest')}
    summary.update(adoptable=False, artifacts=artifacts, record=str(record_path), runtimeLog=str(log_path),
                   trials=[{'id': trial['id'], 'training': _aggregate(trial.get('training')),
                            'validation': _aggregate(trial.get('validation'))}
                           for trial in result.get('trials', [])])
    if getattr(args, 'skill_feedback', False):
        summary['trainingFeedback'] = [{'trialId': trial['id'], 'cases': trial['training']['cases']}
                                       for trial in result.get('trials', []) if trial.get('training')]
        summary['askDataValidation'] = result.get('askDataValidation')
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if result.get('searchComplete') and result.get('status') == 'completed' else 2
