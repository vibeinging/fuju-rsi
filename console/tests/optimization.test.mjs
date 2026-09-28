import assert from 'node:assert/strict'
import test from 'node:test'
import { experimentOutcome, formatEvidenceCount, formatEvidenceDelta, formatEvidenceInterval, formatEvidenceScore, isLegacyExperiment, isVerifiedExperiment } from '../src/api/optimization.ts'

const verified = {
  stage: 'verification', status: 'completed', adoptable: true, evidenceStatus: 'improved',
  acceptancePolicyVersion: 'group-paired-v1', verificationId: 'receipt-1',
}

test('final export and adoption require a completed independent verification receipt', () => {
  assert.equal(isVerifiedExperiment(verified), true)
  const incomplete = [
    undefined,
    { ...verified, stage: 'search' },
    { ...verified, stage: undefined },
    { ...verified, adoptable: false },
    { ...verified, acceptancePolicyVersion: '' },
    { ...verified, verificationId: null },
    ...['running', 'cancelled', 'failed', 'interrupted'].map(status => ({ ...verified, status })),
    ...[undefined, 'not_verified', 'insufficient', 'regressed', 'no_improvement', 'invalid'].map(evidenceStatus => ({ ...verified, evidenceStatus })),
  ]
  for (const experiment of incomplete) assert.equal(isVerifiedExperiment(experiment), false, JSON.stringify(experiment))
})

test('old adopted experiments remain historical evidence and do not acquire a new success claim', () => {
  const legacy = { status: 'completed', adoptable: true, adopted: true, message: '已通过验证与保留测试' }
  assert.equal(isLegacyExperiment(legacy), true)
  assert.equal(isVerifiedExperiment(legacy), false)
  assert.equal(experimentOutcome(legacy), '旧版开发评测，尚无新版独立验收结论')
  assert.equal(legacy.adopted, true)
})

test('completed search and failed verification have distinct, non-success conclusions', () => {
  assert.equal(experimentOutcome({ ...verified, stage: 'search' }), '候选搜索已结束，尚未独立验收')
  assert.equal(experimentOutcome({ ...verified, evidenceStatus: 'insufficient' }), '证据不足，不能推荐采用')
  assert.equal(experimentOutcome({ ...verified, evidenceStatus: 'regressed' }), '发现业务退步，不能推荐采用')
  assert.equal(experimentOutcome({ ...verified, evidenceStatus: 'no_improvement' }), '尚未证明改善，不能推荐采用')
  assert.equal(experimentOutcome({ ...verified, verificationId: '' }), '观察到改善，但采用凭据不完整')
  assert.equal(experimentOutcome(verified), '在本次独立验收范围内满足采用规则')
})

test('missing or malformed verification values never become zero or NaN evidence', () => {
  for (const value of [undefined, null, false, true, '', '0', '1', NaN, Infinity, -Infinity, [], {}]) {
    assert.equal(formatEvidenceCount(value), '未知')
    assert.equal(formatEvidenceScore(value), '未知')
    assert.equal(formatEvidenceDelta(value), '未知')
  }
  assert.equal(formatEvidenceCount(-1), '未知')
  assert.equal(formatEvidenceCount(0.5), '未知')
  assert.equal(formatEvidenceCount(Number.MAX_SAFE_INTEGER + 1), '未知')
  assert.equal(formatEvidenceScore(-0.1), '未知')
  assert.equal(formatEvidenceScore(1.1), '未知')
  assert.equal(formatEvidenceDelta(-1.1), '未知')
  assert.equal(formatEvidenceDelta(1.1), '未知')
})

test('known zero values and negative gains remain distinguishable from missing evidence', () => {
  assert.equal(formatEvidenceCount(0), '0')
  assert.equal(formatEvidenceCount(180), '180')
  assert.equal(formatEvidenceScore(0), '0%')
  assert.equal(formatEvidenceScore(0.125), '12.5%')
  assert.equal(formatEvidenceScore(1), '100%')
  assert.equal(formatEvidenceDelta(0), '0.0 个百分点')
  assert.equal(formatEvidenceDelta(-0.125), '-12.5 个百分点')
  assert.equal(formatEvidenceDelta(0.125), '+12.5 个百分点')
})

test('partial or invalid confidence interval renders no usable interval', () => {
  const valid = { lower: -0.1, upper: 1, confidence: 0.95, method: 'paired-group-hoeffding-v1' }
  assert.deepEqual(formatEvidenceInterval(valid), {
    range: '-10.0 个百分点 至 +100.0 个百分点', confidence: '95%', method: valid.method,
  })
  const invalid = [undefined, null, false, {}, [],
    ...[undefined, null, false, '', '0', NaN, -Infinity, -1.1].map(lower => ({ ...valid, lower })),
    ...[undefined, null, false, 1.1, Infinity].map(upper => ({ ...valid, upper })),
    ...[undefined, null, false, '0.95', 0, 1, NaN].map(confidence => ({ ...valid, confidence })),
    ...[undefined, null, false, '', '   '].map(method => ({ ...valid, method })),
    { ...valid, lower: 0.8, upper: 0.1 },
  ]
  for (const interval of invalid) assert.equal(formatEvidenceInterval(interval), null, JSON.stringify(interval))
})
