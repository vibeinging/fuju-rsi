// 外部测试适配器：只给本次新建隔离项目设置普通项目指令。
// 这层检查用于防止误操作，不是对同进程恶意代码的权限沙箱。
import { randomUUID } from 'node:crypto';
import path from 'node:path';

function inside(root, value) {
  if (typeof value !== 'string' || !value) return false;
  const relative = path.relative(path.resolve(root), path.resolve(value));
  return relative === '' || (!relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative));
}

export function assertIsolatedSession(info, isolationRoot) {
  if (!isolationRoot || info?.mode !== 'isolated' || info.reused_existing_app !== false
      || !['eval_home', 'data_root', 'database_path', 'user_data_dir'].every(key => inside(isolationRoot, info[key]))) {
    throw new Error('必须使用本次新建目录内的隔离 App，禁止连接已有 App 或用户数据库');
  }
}

export function createPromptDriver(driver, { sessionInfo, isolationRoot, instructions, runId = randomUUID() }) {
  assertIsolatedSession(sessionInfo, isolationRoot);
  if (typeof instructions !== 'string' || !instructions.trim() || instructions.length > 8000) {
    throw new Error('项目指令必须是非空文本，最多 8000 字符');
  }
  if (!driver?.raw?.api || !/^[a-zA-Z0-9_-]+$/.test(runId)) throw new Error('无效的 driver 或 runId');
  const prompt = instructions.trim().replace(/\r\n?/g, '\n');
  const baseApi = driver.raw.api;
  const owned = new Set();
  const receipts = [];
  let sequence = 0;
  const owns = id => {
    if (typeof id !== 'string' || !owned.has(id)) throw new Error('仅允许操作本次创建的隔离项目');
  };
  const ok = response => response?.status >= 200 && response?.status < 300;
  async function checkPrompt(id) {
    owns(id);
    const response = await baseApi('GET', `/api/projects/${encodeURIComponent(id)}`);
    if (!ok(response) || response.json?.data?.id !== id || response.json.data.instructions !== prompt) {
      throw new Error('隔离项目指令未生效或已改变，拒绝继续模型调用');
    }
  }
  async function scopedApi(method, url, body) {
    // IPC/URL 路由可能规范化路径；先拒绝父目录和编码后的路径变体。
    if (typeof url !== 'string' || url.includes('#') || url.includes('\\')) throw new Error('无效的接口路径');
    const decodedPath = decodeURIComponent(url.split('?')[0]);
    if (decodedPath.includes('\\') || decodedPath.split('/').some(part => part === '.' || part === '..')) {
      throw new Error('接口路径不能包含目录跳转');
    }
    const match = /^\/api\/(?:agent\/)?projects\/([^/?]+)(?:[/?]|$)/.exec(url);
    if (match) {
      const id = decodeURIComponent(match[1]);
      owns(id);
      if (method !== 'GET' && (url === `/api/projects/${id}` || url === `/api/projects/${encodeURIComponent(id)}`)) {
        throw new Error('任务不得改写或删除固定指令项目');
      }
      if (method !== 'GET' && !((method === 'POST' && url === `/api/projects/${id}/sessions`)
          || (method === 'PUT' && url === `/api/projects/${id}/plugins/ask-data` && body?.enabled === true))) {
        throw new Error('该写入不在当前合成问数适配范围内');
      }
    } else if (method !== 'GET' || !/^\/api\/agents\/(?:runs|evidence-bundles)\//.test(url)) {
      throw new Error('该接口不在当前合成问数适配范围内');
    }
    return baseApi(method, url, body);
  }
  return {
    raw: {
      api: scopedApi,
      ...(driver.raw.infrastructureCheckpoint ? { infrastructureCheckpoint: driver.raw.infrastructureCheckpoint } : {}),
      ...(driver.raw.infrastructurePollutionSince ? { infrastructurePollutionSince: driver.raw.infrastructurePollutionSince } : {}),
    },
    ui: driver.ui?.screenshot ? { screenshot: () => driver.ui.screenshot() } : {},
    login: () => driver.login(),
    async ensureProjectRecord() {
      // 不调用原 ensureProjectRecord；它会按名字复用已有项目。
      const before = await baseApi('GET', '/api/projects');
      const items = before?.json?.data?.items;
      if (!ok(before) || !Array.isArray(items) || before.json.data.total !== items.length) {
        throw new Error('无法完整核对隔离 App 中已有项目');
      }
      const prior = new Set(items.map(item => item.id));
      const name = `yiTrace-${runId.slice(0, 32)}-${++sequence}`;
      const created = await baseApi('POST', '/api/projects', { name, description: 'yiTrace known synthetic regression', instructions: prompt });
      const row = created?.json?.data;
      if (!ok(created) || !row?.id || prior.has(row.id) || owned.has(row.id) || row.name !== name) {
        throw new Error('项目创建未返回新的唯一项目，禁止设置指令');
      }
      owned.add(row.id);
      // 使用正式项目设置接口；只允许本次 POST 返回的新项目。
      const updated = await baseApi('PUT', `/api/projects/${encodeURIComponent(row.id)}`, { instructions: prompt });
      if (!ok(updated)) throw new Error('无法设置新建隔离项目指令');
      await checkPrompt(row.id);
      receipts.push({ projectId: row.id, createdNew: true, instructionsVerified: true });
      return row.id;
    },
    async importDatabase(id, file, options) {
      owns(id);
      return driver.importDatabase(id, file, options);
    },
    async continueAgent(id, sessionId, message, options) {
      await checkPrompt(id);
      return driver.continueAgent(id, sessionId, message, options);
    },
    instructionReceipts: () => structuredClone(receipts),
  };
}
