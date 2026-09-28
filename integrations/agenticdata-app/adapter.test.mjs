import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { createPromptDriver, assertIsolatedSession } from './driver-wrapper.mjs';
import { isolatedEnv } from './adapter.mjs';
import { modelArgs } from './model-run.mjs';

const root = path.join(tmpdir(), 'adapter-protocol-test');
const info = () => ({ mode: 'isolated', reused_existing_app: false, eval_home: root,
  data_root: path.join(root, 'data'), database_path: path.join(root, 'data/local.db'), user_data_dir: path.join(root, 'electron') });

function harness({ returnExisting = false, instructionWriteFails = false } = {}) {
  const rows = new Map([['existing', { id: 'existing', name: 'original', instructions: 'KEEP' }]]);
  const calls = [], turns = [];
  const driver = { raw: { async api(method, url, body) {
    calls.push({ method, url, body });
    if (method === 'GET' && url === '/api/projects') return { status: 200, json: { data: { items: [...rows.values()], total: rows.size } } };
    if (method === 'POST' && url === '/api/projects') {
      const id = returnExisting ? 'existing' : `new-${rows.size}`;
      const row = { id, ...body };
      if (!returnExisting) rows.set(id, row);
      return { status: 200, json: { data: row } };
    }
    const id = url.split('/')[3];
    if (method === 'PUT') {
      if (instructionWriteFails) return { status: 500 };
      rows.set(id, { ...rows.get(id), ...body });
    }
    return { status: 200, json: { data: rows.get(id) } };
  } }, async login() {}, async ensureProjectRecord() { throw new Error('name reuse must never be called'); },
  async importDatabase(id, file) { return { connId: `${id}:${file}` }; },
  async continueAgent(...args) { turns.push(args); return { events: [] }; } };
  return { rows, calls, turns, driver, wrapped: createPromptDriver(driver, {
    sessionInfo: info(), isolationRoot: root, instructions: 'candidate rule', runId: 'fixture',
  }) };
}

test('adapter writes instructions only on a newly created project before its first turn', async () => {
  const h = harness();
  const id = await h.wrapped.ensureProjectRecord('existing-name');
  await h.wrapped.continueAgent(id, 'session', 'business question', { searchMode: 'off' });
  assert.equal(h.rows.get('existing').instructions, 'KEEP');
  assert.equal(h.turns.length, 1);
  assert.deepEqual(h.calls.filter(call => call.method === 'PUT').map(call => call.url), [`/api/projects/${id}`]);
  assert.equal(h.rows.get(id).instructions, 'candidate rule');
  assert.deepEqual(h.wrapped.instructionReceipts(), [{ projectId: id, createdNew: true, instructionsVerified: true }]);
  const second = await h.wrapped.ensureProjectRecord('existing-name');
  assert.notEqual(second, id);
});

test('existing id returned by creation never receives a prompt update or a turn', async () => {
  const h = harness({ returnExisting: true });
  await assert.rejects(h.wrapped.ensureProjectRecord('x'), /唯一项目/);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 0);
  assert.equal(h.rows.get('existing').instructions, 'KEEP');
  assert.equal(h.turns.length, 0);
});

test('unowned projects and global writes are refused by wrapper methods', async () => {
  const h = harness();
  await assert.rejects(h.wrapped.continueAgent('existing', 'session', 'x'), /本次创建/);
  await assert.rejects(h.wrapped.importDatabase('existing', 'file'), /本次创建/);
  await assert.rejects(h.wrapped.raw.api('PUT', '/api/projects/existing', { instructions: 'bad' }), /本次创建/);
  await assert.rejects(h.wrapped.raw.api('PUT', '/api/agent/settings/instructions', { instructions: 'bad' }), /适配范围/);
  assert.equal(h.calls.length, 0);
});

test('instruction failure or change blocks model turn', async () => {
  const failed = harness({ instructionWriteFails: true });
  await assert.rejects(failed.wrapped.ensureProjectRecord(), /无法设置/);
  const h = harness();
  const id = await h.wrapped.ensureProjectRecord();
  h.rows.get(id).instructions = 'modified';
  await assert.rejects(h.wrapped.continueAgent(id, 'session', 'x'), /已改变/);
  await assert.rejects(h.wrapped.raw.api('PUT', `/api/projects/${id}`, { instructions: 'bad' }), /不得改写/);
  assert.equal(h.turns.length, 0);
});

test('path normalization and unrelated own-project writes cannot escape the scoped routes', async () => {
  const h = harness();
  const id = await h.wrapped.ensureProjectRecord();
  for (const url of [`/api/projects/${id}/../existing`, `/api/projects/${id}/%2e%2e/existing`,
    `/api/projects/${id}/runtime-location`, `/api/projects/${id}/source-folders`]) {
    await assert.rejects(h.wrapped.raw.api('PUT', url, { instructions: 'bad' }));
  }
  assert.equal(h.rows.get('existing').instructions, 'KEEP');
  assert.deepEqual(h.calls.filter(call => call.method === 'PUT').map(call => call.url), [`/api/projects/${id}`]);
});

test('reused App, external data root and sibling-prefix paths fail isolation', () => {
  for (const mutated of [{ ...info(), mode: 'normal' }, { ...info(), reused_existing_app: true },
    { ...info(), database_path: path.join(tmpdir(), 'production.db') },
    { ...info(), data_root: root + '-other' }]) assert.throws(() => assertIsolatedSession(mutated, root), /隔离 App/);
  assert.doesNotThrow(() => assertIsolatedSession(info(), root));
});

test('inherited path overrides cannot redirect the isolated child to user data', () => {
  const env = isolatedEnv(root, { DB_SQLITE_PATH: '/user/local.db', AHA_DATA_ROOT: '/user/data',
    AHA_EVAL_REUSE_DATA: '1', AHA_EVAL_DB_SQLITE_PATH: '/user/local.db', AHA_EVAL_PACKAGED_APP: '/user/app',
    AHA_EVAL_KEEP_APP: '1', AHA_DEV_URL: 'http://old-window' });
  assert.equal(env.DB_SQLITE_PATH, path.join(root, 'data/local.db'));
  assert.equal(env.AHA_DATA_ROOT, path.join(root, 'data'));
  for (const key of ['AHA_EVAL_REUSE_DATA', 'AHA_EVAL_DB_SQLITE_PATH', 'AHA_EVAL_PACKAGED_APP', 'AHA_EVAL_KEEP_APP', 'AHA_DEV_URL']) {
    assert.equal(env[key], undefined);
  }
});

test('real-model entry requires explicit permission and cannot select KDD or arbitrary tasks', () => {
  const args = ['--project-root', root, '--output', '/tmp/report.json', '--scenario', 'ask-data-synthetic-revenue-multiturn',
    '--prompt-file', '/tmp/prompt.txt', '--model-source-db', '/tmp/models.db', '--isolation-root', root, '--cdp-port', '9433'];
  assert.throws(() => modelArgs(args), /显式授权/);
  assert.equal(modelArgs([...args, '--allow-model-calls']).allow, true);
  const bad = [...args, '--allow-model-calls'];
  bad[bad.indexOf('--scenario') + 1] = 'kdd-task_180';
  assert.throws(() => modelArgs(bad), /非敏感合成/);
});
