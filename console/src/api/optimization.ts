// 优化实验始终读真实服务；独立前端的 mock 开关不适用于实验结果。
const BASE = ((import.meta.env?.VITE_API_BASE as string | undefined) ?? '/v1').replace(/\/$/, '')

export interface OptimizationAgent {
  id: string
  name: string
  description: string
  kind: string
  baselinePrompt: string
  counts: { train: number; validation: number; test: number }
}
export interface OptimizationCapabilities {
  available: boolean
  protocolVersion?: number
  verificationAvailable?: boolean
  traceAvailable?: boolean
  agents: OptimizationAgent[]
}
export interface EvaluationCase {
  id: string
  input: unknown
  expected: unknown
  output: unknown
  score: number
  passed: boolean
  reason: string
  error: string | null
  latencyMs: number
  tokens: number | null
  costUsd: number | null
  traceId: string | null
}
export interface EvaluationBatch {
  score: number
  passed: number
  total: number
  latencyMs: number
  tokens: number | null
  costUsd: number | null
  cases: EvaluationCase[]
}
export interface OptimizationTrial {
  id: string
  label: string
  prompt: string
  training: EvaluationBatch | null
  validation: EvaluationBatch | null
}
export interface Experiment {
  id: string
  agentId: string
  name: string
  kind: string
  createdAt: string
  updatedAt: string
  status: 'running' | 'completed' | 'cancelled' | 'failed' | 'interrupted'
  stage?: 'search' | 'verification'
  evidenceStatus?: 'not_verified' | 'insufficient' | 'regressed' | 'no_improvement' | 'improved' | 'invalid'
  acceptancePolicyVersion?: string | null
  verificationId?: string | null
  verificationUsedCalls?: number | null
  evidence?: {
    independentGroups?: number | null
    caseCount?: number | null
    runCount?: number | null
    baselineScore?: number | null
    candidateScore?: number | null
    delta?: number | null
    interval?: { lower?: number | null; upper?: number | null; confidence?: number | null; method?: string | null } | null
    reasons?: string[]
  } | null
  config: { maxTrials: number; maxCalls: number }
  baselinePrompt: string
  bestPrompt: string
  selectedTrialId: string | null
  adoptable: boolean
  adopted: boolean
  message: string
  usedCalls: number
  trials: OptimizationTrial[]
  baselineTest: EvaluationBatch | null
  candidateTest: EvaluationBatch | null
}
export interface ActivePrompt { agentId: string; prompt: string; experimentId: string | null }

function finiteInRange(value: unknown, minimum: number, maximum: number): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= minimum && value <= maximum
}

// JSON 的 null、布尔值或缺失字段不能经隐式数值转换变成零分与零收益。
export function formatEvidenceCount(value: unknown): string {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value.toLocaleString('zh-CN') : '未知'
}

export function formatEvidenceScore(value: unknown): string {
  return finiteInRange(value, 0, 1) ? `${(value * 100).toFixed(1).replace(/\.0$/, '')}%` : '未知'
}

export function formatEvidenceDelta(value: unknown): string {
  return finiteInRange(value, -1, 1) ? `${value > 0 ? '+' : ''}${(value * 100).toFixed(1)} 个百分点` : '未知'
}

export function formatEvidenceInterval(value: unknown): { range: string; confidence: string; method: string } | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const { lower, upper, confidence, method } = value as Record<string, unknown>
  if (!finiteInRange(lower, -1, 1) || !finiteInRange(upper, -1, 1) || lower > upper
    || !finiteInRange(confidence, 0, 1) || confidence === 0 || confidence === 1
    || typeof method !== 'string' || !method.trim()) return null
  return { range: `${formatEvidenceDelta(lower)} 至 ${formatEvidenceDelta(upper)}`,
    confidence: formatEvidenceScore(confidence), method }
}

// 旧记录中的 adoptable 只代表旧版规则；页面的采用与最终导出共用新的门槛。
export function isVerifiedExperiment(experiment: Experiment | undefined): boolean {
  return !!experiment && experiment.stage === 'verification' && experiment.status === 'completed'
    && experiment.adoptable === true && experiment.evidenceStatus === 'improved'
    && !!experiment.acceptancePolicyVersion && !!experiment.verificationId
}

export function isLegacyExperiment(experiment: Experiment): boolean {
  return !experiment.stage || !experiment.evidenceStatus
}

export function experimentOutcome(experiment: Experiment): string {
  if (experiment.status === 'running') return experiment.stage === 'verification' ? '独立验收进行中，尚未形成结论' : '正在搜索候选，尚未形成结论'
  if (experiment.status === 'interrupted') return '执行中断，已保留完成的记录'
  if (experiment.status === 'cancelled') return '实验已取消，未形成采用结论'
  if (experiment.status === 'failed') return '实验执行失败，未形成采用结论'
  if (isVerifiedExperiment(experiment)) return '在本次独立验收范围内满足采用规则'
  if (isLegacyExperiment(experiment)) return '旧版开发评测，尚无新版独立验收结论'
  if (experiment.stage === 'search') return '候选搜索已结束，尚未独立验收'
  const labels: Record<NonNullable<Experiment['evidenceStatus']>, string> = {
    not_verified: '尚未独立验收', insufficient: '证据不足，不能推荐采用', regressed: '发现业务退步，不能推荐采用',
    no_improvement: '尚未证明改善，不能推荐采用', improved: '观察到改善，但采用凭据不完整', invalid: '验收无效，不能推荐采用',
  }
  return labels[experiment.evidenceStatus!] ?? '尚无有效的独立验收结论'
}

async function request<T>(path: string, options: { signal?: AbortSignal; body?: unknown } = {}): Promise<T> {
  const headers: Record<string, string> = { accept: 'application/json' }
  const token = import.meta.env.VITE_API_TOKEN as string | undefined
  let tenant = import.meta.env.VITE_TENANT_ID as string | undefined
  if (!tenant) {
    try { tenant = window.localStorage.getItem('yitrace.tenantId') ?? undefined } catch { /* 浏览器禁用存储时仍可使用环境配置。 */ }
  }
  if (token) headers.authorization = `Bearer ${token}`
  if (tenant) headers['x-tenant-id'] = tenant
  if (options.body !== undefined) headers['content-type'] = 'application/json'
  const response = await fetch(`${BASE}/optimizations${path}`, {
    method: options.body === undefined ? 'GET' : 'POST', headers, signal: options.signal,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  })
  const data = await response.json().catch(() => null) as T & { error?: string } | null
  if (!response.ok) throw new Error(data?.error || `请求未完成（HTTP ${response.status}）`)
  if (!data) throw new Error('服务未返回有效的实验数据。请确认已启动优化服务。')
  return data
}

export const optimizationApi = {
  capabilities: (signal?: AbortSignal) => request<OptimizationCapabilities>('/capabilities', { signal }),
  list: (signal?: AbortSignal) => request<{ items: Experiment[] }>('', { signal }),
  get: (id: string, signal?: AbortSignal) => request<Experiment>(`/${encodeURIComponent(id)}`, { signal }),
  create: (body: { agentId: string; name: string; maxTrials: number; maxCalls: number }) => request<Experiment>('', { body }),
  action: (id: string, action: 'cancel' | 'adopt' | 'rollback') => request<Experiment>(`/${encodeURIComponent(id)}/${action}`, { body: {} }),
  prompt: (id: string, signal?: AbortSignal) => request<ActivePrompt>(`/agents/${encodeURIComponent(id)}/prompt`, { signal }),
}
