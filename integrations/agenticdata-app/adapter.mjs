#!/usr/bin/env node
// 默认检查路径只运行两组已有无模型测试；真实模型入口需要单独显式选择。
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const DIRECTORY = path.dirname(fileURLToPath(import.meta.url));
export const TEST_FILES = ['eval/tests/query-execution-evidence.test.mjs', 'eval/tests/ask-data-synthetic-multiturn.test.mjs'];
export const SOURCE_FILES = [...TEST_FILES, 'eval/tests/setup.mjs', 'eval/lib/driver.mjs',
  'eval/tasks/87-ask-data-synthetic-multiturn.task.mjs', 'server/src/engine/datasources/plugins/sqlite_plugin.js',
  'server/src/engine/datasources/query_worker_client.js', 'server/src/engine/datasources/query_worker.js',
  'server/src/engine/agents/workspace_agent.js', 'server/src/app/projects/index.js'];
const sha = value => createHash('sha256').update(value).digest('hex');

export function parseArgs(args) {
  const options = { command: args[0], allowModelCalls: false, port: 9433 };
  const flags = { '--project-root': 'projectRoot', '--output': 'output', '--scenario': 'scenario',
    '--prompt-file': 'promptFile', '--model-source-db': 'modelSourceDb', '--cdp-port': 'port' };
  for (let index = 1; index < args.length; index++) {
    const flag = args[index];
    if (flag === '--allow-model-calls') { options.allowModelCalls = true; continue; }
    if (!flags[flag] || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error(`未知参数或缺少值：${flag}`);
    const key = flags[flag];
    if (Object.hasOwn(options, key) && key !== 'port') throw new Error(`重复参数：${flag}`);
    options[key] = args[++index];
  }
  if (!['check', 'model'].includes(options.command) || !options.projectRoot || !options.output) {
    throw new Error('用法：adapter.mjs check|model --project-root 项目目录 --output 新结果.json');
  }
  if (options.command === 'check' && (options.allowModelCalls || options.modelSourceDb || options.promptFile || options.scenario)) {
    throw new Error('check 不接受模型、凭据或提示词参数');
  }
  if (options.command === 'model' && (!options.allowModelCalls || !options.modelSourceDb || !options.promptFile || !options.scenario)) {
    throw new Error('model 必须显式提供 --allow-model-calls、--model-source-db、--prompt-file 和 --scenario');
  }
  options.port = Number(options.port);
  if (!Number.isInteger(options.port) || options.port < 1024 || options.port > 65535) throw new Error('CDP 端口无效');
  options.projectRoot = path.resolve(options.projectRoot);
  options.output = path.resolve(options.output);
  if (!existsSync(path.join(options.projectRoot, 'scripts/run-with-project-node.mjs'))
      || !SOURCE_FILES.every(file => existsSync(path.join(options.projectRoot, file)))) throw new Error('项目目录缺少当前适配器需要的正式模块');
  if (existsSync(options.output) || existsSync(options.output + '.log')) throw new Error('结果已存在，必须使用新路径');
  return options;
}

export function sourceSnapshot(projectRoot) {
  return Object.fromEntries(SOURCE_FILES.map(file => [file, sha(readFileSync(path.join(projectRoot, file)))]));
}

export function isolatedEnv(root, inherited = process.env) {
  const env = { ...inherited };
  // 这些继承项能把“隔离”入口重新指向日常数据或已有窗口，必须固定。
  for (const key of ['AHA_EVAL_REUSE_DATA', 'AHA_EVAL_DB_SQLITE_PATH', 'AHA_EVAL_DATA_ROOT',
    'AHA_EVAL_PACKAGED_APP', 'AHA_DEV_URL', 'AHA_EVAL_KEEP_APP', 'AHA_EVAL_MODEL_SOURCE_DB']) delete env[key];
  return { ...env, AHA_TEST_ISOLATED: '1', AHA_EVAL_HOME: root,
    AHA_DATA_ROOT: path.join(root, 'data'), DB_SQLITE_PATH: path.join(root, 'data', 'local.db'),
    AHA_USER_DATA_DIR: path.join(root, 'electron'), ZHISHU_AGENT_RUNTIME_HOME: path.join(root, 'agent-runtime'),
    AHA_SKILLS_ROOT: path.join(root, 'skills') };
}

function spawnProject(options, args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [path.join(options.projectRoot, 'scripts/run-with-project-node.mjs'), 'node', ...args],
      { cwd: options.projectRoot, env, stdio: ['ignore', 'pipe', 'pipe'], shell: false });
    let output = '', truncated = false;
    const capture = chunk => {
      if (output.length < 8 * 1024 * 1024) output += chunk.toString();
      else truncated = true;
    };
    child.stdout.on('data', capture); child.stderr.on('data', capture);
    const interrupt = () => child.kill('SIGTERM');
    process.once('SIGINT', interrupt); process.once('SIGTERM', interrupt);
    child.once('error', reject);
    child.once('close', (code, signal) => {
      process.removeListener('SIGINT', interrupt); process.removeListener('SIGTERM', interrupt);
      resolve({ code, signal, output, truncated });
    });
  });
}

export async function main(args = process.argv.slice(2)) {
  const options = parseArgs(args);
  const root = mkdtempSync(path.join(tmpdir(), 'yitrace-agenticdata-adapter-'));
  const before = sourceSnapshot(options.projectRoot);
  mkdirSync(path.dirname(options.output), { recursive: true });
  const startedAt = new Date().toISOString();
  try {
    const argv = options.command === 'check'
      ? ['--import', './eval/tests/setup.mjs', '--test', '--test-concurrency=1', '--test-reporter=tap', ...TEST_FILES]
      : [path.join(DIRECTORY, 'model-run.mjs'), '--project-root', options.projectRoot, '--output', options.output,
        '--scenario', options.scenario, '--prompt-file', path.resolve(options.promptFile),
        '--model-source-db', path.resolve(options.modelSourceDb), '--isolation-root', root,
        '--cdp-port', String(options.port), '--allow-model-calls'];
    const result = await spawnProject(options, argv, isolatedEnv(root));
    writeFileSync(options.output + '.log', result.output, { flag: 'wx', mode: 0o600 });
    const after = sourceSnapshot(options.projectRoot);
    const changed = SOURCE_FILES.filter(file => before[file] !== after[file]);
    if (options.command === 'check') {
      const counts = Object.fromEntries(['tests', 'pass', 'fail', 'cancelled', 'skipped', 'todo'].map(key => {
        const match = new RegExp(`^# ${key} (\\d+)$`, 'm').exec(result.output);
        return [key, match ? Number(match[1]) : null];
      }));
      const success = result.code === 0 && !result.truncated && changed.length === 0
        && counts.tests > 0 && counts.fail === 0 && counts.cancelled === 0;
      const report = { schemaVersion: 1, mode: 'existing-tests', startedAt, finishedAt: new Date().toISOString(),
        status: success ? 'passed' : 'failed', projectRoot: options.projectRoot, command: argv, counts,
        childExitCode: result.code, childSignal: result.signal, logTruncated: result.truncated,
        modelCallsExecuted: false, credentialsReadByAdapter: false,
        sourceFilesBefore: before, sourceFilesAfter: after, changedDeclaredSourceFiles: changed,
        transport: 'direct production modules and unit fixtures; model path uses CDP/IPC',
        dataset: { kind: 'known-synthetic-regression', sourceGroups: 3, businessTurns: 7 },
        evidenceStatus: 'not_verified', adoptable: false,
        limitations: ['Existing tests include real SQLite/DuckDB workers and constructed protocol evidence.',
          'No real-model Agent turn or external business acceptance was run.',
          'Three known source groups are not an independent holdout and cannot satisfy the default 30-group gate.',
          'Source hashes cover only the declared files; they are not proof that every project file is unchanged.'] };
      writeFileSync(options.output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
      console.log(JSON.stringify({ status: report.status, counts, report: options.output, modelCallsExecuted: false }));
      return success ? 0 : 1;
    }
    if (changed.length) throw new Error('运行期间正式源文件改变，请保留原始结果并重新确认版本');
    console.log(JSON.stringify({ childExitCode: result.code, report: options.output, log: options.output + '.log' }));
    return result.code === 0 && !result.truncated ? 0 : 1;
  } finally {
    // 只清理本适配器刚创建的目录；真实模型进程负责先关闭自己启动的 App。
    rmSync(root, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().then(code => { process.exitCode = code; }).catch(error => {
    console.error(error.message); process.exitCode = 1;
  });
}
