// pi 栈 Agent 桥服务。
//
// 两个角色（同进程、同端口）：
// 1. /v1/agent/* —— 被 tau2-bench 自定义 Agent 调用的会话服务。
//    每个会话持有一段对话上下文，complete 时经 pi-ai 做一次模型调用，
//    返回文本或 toolCall 块；工具执行留在 tau2 官方 orchestrator，
//    因此 reward 的 action 检查能看到完整轨迹。
// 2. /v1/chat/completions —— OpenAI 兼容端点。
//    mock 模式下同时服务两条路径：
//    - tau2 官方 litellm（用户模拟器 / 判分器，按 model 名路由）
//    - pi-ai 自身（provider baseUrl 指回本进程，被测 Agent 的模型调用）
//    所有 mock 行为都是确定性的：不随机、不依赖时间。
//
// 真实模型路径：PI_AI_BASE_URL / PI_AI_API_KEY / PI_AI_EXTRA_MODELS
// 指向任何 OpenAI 兼容服务（或用 PI_AI_PROVIDER_JSON 完整覆盖 provider 定义）。

import { createServer } from 'node:http';
import { createModels, createProvider } from '@earendil-works/pi-ai';
import { openAICompletionsApi } from '@earendil-works/pi-ai/api/openai-completions.lazy';

const PORT = Number.parseInt(process.env.PI_BRIDGE_PORT || '7891', 10);
const HOST = '127.0.0.1';
const SELF_BASE = `http://${HOST}:${PORT}`;

// ---------------------------------------------------------------------------
// 确定性 mock 模型
// ---------------------------------------------------------------------------

// 被测 Agent 的桩：证明「提示词呈现方式影响行为」的最小实现。
// 系统提示词含 "exact"（不分大小写）时，严格使用用户原话里的标题；
// 否则把标题转小写——模仿「策略文档没写清 → 执行走样」的真实失败模式。
function mockAgentDecision({ systemPrompt = '', messages = [] }) {
  const last = messages[messages.length - 1];
  if (last && last.role === 'tool') {
    return { content: 'Your request has been completed successfully.' };
  }
  const userText = [...messages].reverse().find((m) => m.role === 'user')?.content || '';
  const exact = /exact/i.test(systemPrompt);
  const quoted = userText.match(/['"]([^'"]+)['"]/) || userText.match(/called\s+([A-Za-z0-9][\w\s]*)/i);
  let title = quoted ? quoted[1].trim() : 'New Task';
  if (!exact) title = title.toLowerCase();
  const user = (userText.match(/user_\d+/) || ['user_1'])[0];
  if (/delete|remove/i.test(userText)) {
    return { toolCall: ['transfer_to_human_agents', { summary: 'policy forbids deletion' }] };
  }
  const taskId = userText.match(/task_\d+/);
  if (taskId && /update|mark|status|complet/i.test(userText)) {
    const status = /complet/i.test(userText) ? 'completed' : 'pending';
    return { toolCall: ['update_task_status', { task_id: taskId[0], status }] };
  }
  return { toolCall: ['create_task', { user_id: user, title }] };
}

// 用户模拟器的桩：tau2 的系统提示词以 <scenario>…</scenario> 包住任务指令，
// 首轮复述其中最后一句（即用户的具体诉求）；见到完成确认即停止。
// 注意：tau2 会翻转角色——从用户视角，客服（被测 Agent）的话以 role=user 到达。
function mockUserDecision({ systemPrompt = '', messages = [] }) {
  const lastFromAgent = [...messages].reverse().find((m) => m.role === 'user');
  const said = typeof lastFromAgent?.content === 'string' ? lastFromAgent.content : '';
  if (lastFromAgent && /success|created|updated|completed|done/i.test(said)) {
    return { content: '###STOP###' };
  }
  const scenarioMatch = systemPrompt.match(/<scenario>([\s\S]*?)<\/scenario>/);
  const scenario = (scenarioMatch ? scenarioMatch[1] : systemPrompt).trim();
  const sentences = scenario.split(/(?<=[.!?])\s+/).filter((s) => s.trim());
  return { content: (sentences[sentences.length - 1] || scenario || 'Hello.').trim() };
}

function mockJudgeDecision() {
  return { content: 'yes' };
}

const MOCK_MODELS = new Map([
  ['mock-agent', mockAgentDecision],
  ['mock-user', mockUserDecision],
  ['mock-judge', mockJudgeDecision],
]);

function openAiCompletionBody(model, decision, callCounter) {
  const message = { role: 'assistant', content: decision.content ?? null };
  let finish_reason = 'stop';
  if (decision.toolCall) {
    const [name, args] = decision.toolCall;
    message.tool_calls = [
      { id: `call_${callCounter}`, type: 'function', function: { name, arguments: JSON.stringify(args) } },
    ];
    finish_reason = 'tool_calls';
  }
  return {
    id: `chatcmpl_mock_${callCounter}`,
    object: 'chat.completion',
    created: 0,
    model,
    choices: [{ index: 0, message, finish_reason }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

// pi-ai 经 OpenAI SDK 以 stream:true 调用，mock 端点需要回 SSE 分块。
function sendOpenAiStream(res, model, decision, callCounter) {
  res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache', connection: 'keep-alive' });
  const base = {
    id: `chatcmpl_mock_${callCounter}`,
    object: 'chat.completion.chunk',
    created: 0,
    model,
  };
  const chunks = [];
  const firstDelta = { role: 'assistant' };
  if (decision.content) firstDelta.content = decision.content;
  if (decision.toolCall) {
    const [name, args] = decision.toolCall;
    firstDelta.tool_calls = [
      { index: 0, id: `call_${callCounter}`, type: 'function', function: { name, arguments: JSON.stringify(args) } },
    ];
  }
  chunks.push({ ...base, choices: [{ index: 0, delta: firstDelta, finish_reason: null }] });
  const finish = decision.toolCall ? 'tool_calls' : 'stop';
  chunks.push({ ...base, choices: [{ index: 0, delta: {}, finish_reason: finish }] });
  for (const chunk of chunks) res.write(`data: ${JSON.stringify(chunk)}\n\n`);
  res.write('data: [DONE]\n\n');
  res.end();
}

// ---------------------------------------------------------------------------
// pi-ai provider：默认指回本进程的 mock 端点；真实模式用环境变量覆盖
// ---------------------------------------------------------------------------

function buildProviderModels() {
  const defs = [...MOCK_MODELS.keys()].map((id) => ({
    id,
    name: `Mock ${id} (deterministic)`,
    api: 'openai-completions',
    provider: 'yitrace',
    baseUrl: `${SELF_BASE}/v1`,
    reasoning: false,
    input: ['text'],
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    contextWindow: 128000,
    maxTokens: 8192,
  }));
  if (process.env.PI_AI_EXTRA_MODELS) {
    for (const def of JSON.parse(process.env.PI_AI_EXTRA_MODELS)) defs.push(def);
  }
  return defs;
}

function buildProvider() {
  const override = process.env.PI_AI_PROVIDER_JSON;
  if (override) return createProvider(JSON.parse(override));
  const baseUrl = process.env.PI_AI_BASE_URL || `${SELF_BASE}/v1`;
  const apiKey = process.env.PI_AI_API_KEY || 'mock';
  return createProvider({
    id: 'yitrace',
    name: 'yiTrace bridge provider',
    baseUrl,
    auth: { apiKey: { name: 'yiTrace bridge', resolve: async () => ({ auth: { apiKey } }) } },
    models: buildProviderModels(),
    api: openAICompletionsApi(),
  });
}

const models = createModels();
models.setProvider(buildProvider());

// ---------------------------------------------------------------------------
// 会话：tau2 自定义 Agent 与本服务之间的全部状态
// ---------------------------------------------------------------------------

const sessions = new Map();
let sessionSeq = 0;

function requireSession(id) {
  const session = sessions.get(id);
  if (!session) throw new HttpError(404, `unknown session ${id}`);
  return session;
}

// pi-ai 的 assistant 回复（含 toolCall 块）由本服务自己追加，
// Python 侧只上报 user 文本与工具执行结果，避免两份消息格式互相转换。
async function completeSession(session) {
  const model = models.getModel('yitrace', session.model);
  if (!model) throw new HttpError(400, `unknown model ${session.model}`);
  const response = await models.complete(model, session.context);
  // pi-ai 不抛模型错误：失败时返回 stopReason=error + 空 content。
  // 必须显式上抛，否则空决策会以「无内容消息」的形式掩盖真实故障。
  if (response.stopReason === 'error' || response.errorMessage) {
    throw new HttpError(502, `model ${session.model} failed: ${response.errorMessage || response.stopReason}`);
  }
  session.context.messages.push(response);
  session.callSeq += 1;
  const blocks = response.content.map((block) => {
    if (block.type === 'text') return { type: 'text', text: block.text };
    if (block.type === 'toolCall') {
      return {
        type: 'toolCall',
        id: block.id || `call_${session.id}_${session.callSeq}`,
        name: block.name,
        arguments: block.arguments,
      };
    }
    return { type: block.type };
  });
  for (const block of blocks) {
    if (block.type === 'toolCall') session.toolCalls.set(block.id, block.name);
  }
  return { content: blocks };
}

// ---------------------------------------------------------------------------
// HTTP 基础设施
// ---------------------------------------------------------------------------

class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function readJson(req) {
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  if (!chunks.length) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new HttpError(400, 'invalid JSON body');
  }
}

function sendJson(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(body) });
  res.end(body);
}

let mockCallSeq = 0;

const server = createServer(async (req, res) => {
  try {
    const url = new URL(req.url, SELF_BASE);

    if (req.method === 'GET' && url.pathname === '/healthz') {
      return sendJson(res, 200, { ok: true, sessions: sessions.size });
    }

    if (req.method === 'POST' && url.pathname === '/v1/chat/completions') {
      const body = await readJson(req);
      const decide = MOCK_MODELS.get(body.model);
      if (!decide) throw new HttpError(404, `mock endpoint only serves ${[...MOCK_MODELS.keys()].join(', ')}`);
      const messages = (body.messages || []).map((m) => {
        if (m.role === 'tool') return { role: 'tool', content: m.content, toolCallId: m.tool_call_id };
        if (m.role === 'assistant') return { role: 'assistant', content: m.content };
        return { role: m.role, content: m.content, ...(m.role === 'system' ? { system: true } : {}) };
      });
      const system = messages.find((m) => m.role === 'system')?.content || '';
      mockCallSeq += 1;
      if (process.env.PI_BRIDGE_DEBUG) {
        const roles = messages.map((m) => `${m.role}:${(m.content || '').slice(0, 40)}`).join(' | ');
        process.stderr.write(`[mock] model=${body.model} stream=${Boolean(body.stream)} :: ${roles}\n`);
      }
      const decision = decide({ systemPrompt: system, messages: messages.filter((m) => m.role !== 'system') });
      if (body.stream) return sendOpenAiStream(res, body.model, decision, mockCallSeq);
      return sendJson(res, 200, openAiCompletionBody(body.model, decision, mockCallSeq));
    }

    if (req.method === 'POST' && url.pathname === '/v1/agent/sessions') {
      const body = await readJson(req);
      if (!body.model || typeof body.systemPrompt !== 'string' || !Array.isArray(body.tools)) {
        throw new HttpError(400, 'require model, systemPrompt and tools[]');
      }
      const id = `s_${++sessionSeq}`;
      sessions.set(id, {
        id,
        model: body.model,
        callSeq: 0,
        toolCalls: new Map(),
        context: {
          systemPrompt: body.systemPrompt,
          messages: [],
          tools: body.tools.map((t) => ({ name: t.name, description: t.description || '', parameters: t.parameters })),
        },
      });
      return sendJson(res, 201, { sessionId: id });
    }

    const sessionMatch = url.pathname.match(/^\/v1\/agent\/sessions\/([^/]+)(\/.*)?$/);
    if (sessionMatch) {
      const session = requireSession(sessionMatch[1]);
      const action = sessionMatch[2] || '';
      if (req.method === 'POST' && action === '/messages') {
        const body = await readJson(req);
        for (const event of body.events || []) {
          if (event.type === 'user') {
            session.context.messages.push({
              role: 'user',
              content: String(event.text ?? ''),
              timestamp: Date.now(),
            });
          } else if (event.type === 'toolResults') {
            for (const result of event.results || []) {
              session.context.messages.push({
                role: 'toolResult',
                toolCallId: String(result.toolCallId),
                toolName: session.toolCalls.get(String(result.toolCallId)) || 'unknown',
                content: [{ type: 'text', text: String(result.text ?? '') }],
                isError: Boolean(result.error),
                timestamp: Date.now(),
              });
            }
          } else {
            throw new HttpError(400, `unknown event type ${event.type}`);
          }
        }
        return sendJson(res, 204, {});
      }
      if (req.method === 'POST' && action === '/complete') {
        return sendJson(res, 200, await completeSession(session));
      }
      if (req.method === 'DELETE' && action === '') {
        sessions.delete(sessionMatch[1]);
        return sendJson(res, 204, {});
      }
    }

    throw new HttpError(404, `no route ${req.method} ${url.pathname}`);
  } catch (error) {
    const status = error instanceof HttpError ? error.status : 500;
    sendJson(res, status, { error: String(error.message || error) });
  }
});

server.listen(PORT, HOST, () => {
  process.stdout.write(`pi-agent bridge listening on ${SELF_BASE} (mock models: ${[...MOCK_MODELS.keys()].join(', ')})\n`);
});
