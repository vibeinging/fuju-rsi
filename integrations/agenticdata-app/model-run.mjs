#!/usr/bin/env node
// 由 adapter.mjs 使用项目自己的 Node 启动。没有显式开关时不加载模型配置。
import { createHash, randomUUID } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { assertIsolatedSession, createPromptDriver } from './driver-wrapper.mjs';
import { sourceSnapshot } from './adapter.mjs';

export function modelArgs(args) {
  const values = {};
  const allowed = new Set(['--project-root', '--output', '--scenario', '--prompt-file', '--model-source-db', '--isolation-root', '--cdp-port']);
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (flag === '--allow-model-calls') { values.allow = true; continue; }
    if (!allowed.has(flag) || !args[index + 1] || args[index + 1].startsWith('--') || Object.hasOwn(values, flag)) {
      throw new Error('真实模型参数无效');
    }
    values[flag] = args[++index];
  }
  if (!values.allow || [...allowed].some(key => !values[key])) throw new Error('真实模型必须显式授权并提供全部隔离参数');
  if (!/^(?:ask-data-synthetic-revenue-multiturn|ask-data-synthetic-precision-empty|ask-data-synthetic-coverage-multiturn)$/.test(values['--scenario'])) {
    throw new Error('只支持三个已审查的非敏感合成业务场景');
  }
  const port = Number(values['--cdp-port']);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('CDP 端口无效');
  return values;
}

export async function runModel(args) {
  const options = modelArgs(args);
  const root = path.resolve(options['--project-root']);
  const output = path.resolve(options['--output']);
  const isolationRoot = path.resolve(options['--isolation-root']);
  if (existsSync(output)) throw new Error('结果已存在，不能覆盖旧实验');
  // adapter 父进程负责创建新的专用目录并固定环境变量。
  if (path.resolve(process.env.AHA_EVAL_HOME || '') !== isolationRoot
      || path.resolve(process.env.AHA_DATA_ROOT || '') !== path.join(isolationRoot, 'data')
      || path.resolve(process.env.DB_SQLITE_PATH || '') !== path.join(isolationRoot, 'data', 'local.db')) {
    throw new Error('运行环境不属于此次新建隔离目录');
  }
  const instructions = readFileSync(path.resolve(options['--prompt-file']), 'utf8');
  if (!instructions.trim() || instructions.length > 8000) throw new Error('项目指令必须为 1–8000 字符');
  const importApp = file => import(pathToFileURL(path.join(root, file)).href);
  const [{ openSession }, { makeDriver }, { runTasks }, { seedConfiguredModels }, { tasks }, { redactEvidence }] = await Promise.all([
    importApp('eval/lib/cdp.mjs'), importApp('eval/lib/driver.mjs'), importApp('eval/lib/runner.mjs'),
    importApp('eval/lib/configured-models.mjs'), importApp('eval/tasks/87-ask-data-synthetic-multiturn.task.mjs'),
    importApp('eval/benchmarks/zhishubench/evidence.mjs'),
  ]);
  const task = tasks.find(item => item.id === options['--scenario']);
  if (!task || task.eval?.model !== 'real' || task.eval?.data !== 'synthetic' || task.eval?.repeats !== 1) {
    throw new Error('当前任务定义发生变化，请重新审查适配器');
  }
  const startedAt = new Date().toISOString();
  const sourceFilesBefore = sourceSnapshot(root);
  const controller = new AbortController();
  let session, wrapped, results = [], errorType = null, interrupted = false;
  const interrupt = () => {
    interrupted = true;
    controller.abort(new Error('用户停止验收'));
    void session?.close?.({ preserveData: false, forceStop: true }).catch(() => {});
  };
  process.once('SIGINT', interrupt); process.once('SIGTERM', interrupt);
  try {
    session = await openSession({ port: Number(options['--cdp-port']), isolate: true,
      reuseExisting: false, keepData: false, signal: controller.signal });
    assertIsolatedSession(session.info, isolationRoot);
    const driver = makeDriver(session);
    await driver.login();
    // 只有此显式模型分支会读取用户指定配置库；项目模块用 readonly 打开。
    await seedConfiguredModels(driver.raw.api, { sourceDbPath: path.resolve(options['--model-source-db']) });
    wrapped = createPromptDriver(driver, { sessionInfo: session.info, isolationRoot, instructions, runId: randomUUID() });
    results = await runTasks(wrapped, [task], {
      artifactsDir: output + '.artifacts', maxInfrastructureRecoveriesPerTask: 0,
      environment: { integration: 'yitrace-agenticdata', dataset: 'known-synthetic-regression' },
    });
  } catch (error) {
    errorType = String(error?.code || error?.name || 'Error');
    if (Array.isArray(error?.partial_results)) results = error.partial_results;
  } finally {
    await session?.close?.({ preserveData: false, forceStop: true }).catch(() => { errorType = 'APP_CLOSE_FAILED'; });
    process.removeListener('SIGINT', interrupt); process.removeListener('SIGTERM', interrupt);
  }
  const sourceFilesAfter = sourceSnapshot(root);
  const unchanged = JSON.stringify(sourceFilesBefore) === JSON.stringify(sourceFilesAfter);
  const passed = !interrupted && !errorType && unchanged && results.length === 1 && results[0].status === 'passed';
  const report = { schemaVersion: 1, mode: 'real-agent-known-regression', startedAt, finishedAt: new Date().toISOString(),
    status: passed ? 'passed' : interrupted ? 'cancelled' : 'failed', errorType,
    scenario: task.id, transport: 'CDP -> Renderer electronAPI -> IPC -> application process',
    projectRoot: root, instructionsSha256: createHash('sha256').update(instructions).digest('hex'),
    instructionReceipts: wrapped?.instructionReceipts() || [], sourceFilesBefore, sourceFilesAfter,
    results: redactEvidence(results), modelCallsAuthorized: true, modelSpendUsd: null,
    dataset: { kind: 'known-synthetic-regression', sourceGroupsInThisRun: 1, sourceGroupsInCatalog: 3 },
    evidenceStatus: 'not_verified', adoptable: false,
    limitations: ['This uses one known synthetic scenario, not independent final business acceptance.',
      'A task may make several internal model calls; timeout and maxQueryTurns are not a money limit.',
      'Current driver does not forward settings.maxQueryTurns; model usage must be reviewed independently.',
      'Model configuration was explicitly copied read-only into a newly isolated App; no production project was selected.',
      'This command runs one prompt only; paired baseline/candidate scheduling and unseen business cases are separate work.'] };
  mkdirSync(path.dirname(output), { recursive: true });
  writeFileSync(output, JSON.stringify(report, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  console.log(JSON.stringify({ status: report.status, report: output, adoptable: false }));
  return passed ? 0 : interrupted ? 130 : 1;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  runModel(process.argv.slice(2)).then(code => { process.exitCode = code; }).catch(error => {
    console.error(error.message); process.exitCode = 1;
  });
}
