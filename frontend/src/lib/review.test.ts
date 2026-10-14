import { describe, expect, it } from 'vitest'
import { buildApproval, filterOps, initialDecisions, sortApplied, sortForReview } from './review'
import type { Op } from './types'

function op(position: number, flags: Op['risk_flags'] = [], reason = 'technical_dead_air'): Op {
  return {
    id: `op${position}`, position, kind: 'remove', asset_id: 'a', src_start_us: 0, src_end_us: 1,
    requested_start_us: null, requested_end_us: null, adjust_note: null, reason, why: '', kept_alternative: null,
    segment_ids: [], risk_flags: flags, decision: 'pending', override_reason: null, candidate_joint_frame: null,
    current_joint_frame: null, review: 'pending',
  }
}

describe('сборка утверждения', () => {
  const ops = [op(0), op(1, ['high_impact']), op(2, ['protected'])]

  it('рискованные без отметки и защищённое без причины — не отправляем', () => {
    const { missing } = buildApproval(ops, initialDecisions(ops))
    expect(missing).toHaveLength(3)
  })

  it('отмечено и объяснено — отправляем то же, что ждёт сервер', () => {
    const d = initialDecisions(ops)
    d.op1.acknowledged = true
    d.op2.acknowledged = true
    d.op2.override = '  оговорка повторяется дальше  '
    const { body, missing } = buildApproval(ops, d)
    expect(missing).toEqual([])
    expect(body).toEqual({
      exclude_op_ids: [], acknowledge_op_ids: ['op1', 'op2'], overrides: { op2: 'оговорка повторяется дальше' },
    })
  })

  it('исключённые не требуют отметок, но все исключить — это отклонение', () => {
    const d = initialDecisions(ops)
    d.op1.include = false
    d.op2.include = false
    expect(buildApproval(ops, d)).toEqual({
      body: { exclude_op_ids: ['op1', 'op2'], acknowledge_op_ids: [], overrides: {} }, missing: [],
    })
    d.op0.include = false
    expect(buildApproval(ops, d).missing).toHaveLength(1)
  })
})

describe('фильтры очереди', () => {
  const ops = [op(0), op(1, ['cut_in_speech'], 'false_start'), op(2)]
  it('по причине и риску', () => {
    expect(filterOps(ops, 'all', 'risky').map((o) => o.id)).toEqual(['op1'])
    expect(filterOps(ops, 'technical_dead_air', 'all').map((o) => o.id)).toEqual(['op0', 'op2'])
  })
  it('рискованные сначала', () => {
    expect(sortForReview(ops).map((o) => o.id)).toEqual(['op1', 'op0', 'op2'])
  })
})

describe('проверка после применения', () => {
  it('отложенные, потом рискованные, потом остальные, проверенные в конце', () => {
    const ops = [op(0), op(1, ['cut_in_speech']), op(2), op(3)]
    ops[2].review = 'held'
    ops[3].review = 'kept'
    expect(sortApplied(ops).map((o) => o.id)).toEqual(['op2', 'op1', 'op0', 'op3'])
  })
})
