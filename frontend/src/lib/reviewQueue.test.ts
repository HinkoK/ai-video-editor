import { describe, expect, it } from 'vitest'
import {
  buildQueue, jointWindow, nearestJoint, nextAfter, opPlace, opRange, opTitle, plural, queueCounts, riskTag,
} from './reviewQueue'
import type { Asset, Joint, Op, Proposal } from './types'

function op(id: string, position: number, review: Op['review'], flags: Op['risk_flags'] = []): Op {
  return {
    id, position, kind: 'remove', asset_id: 'a2', src_start_us: 724_300_000, src_end_us: 727_900_000,
    requested_start_us: null, requested_end_us: null, adjust_note: null, reason: 'false_start', why: '',
    kept_alternative: null, segment_ids: [], risk_flags: flags, decision: 'accepted', override_reason: null,
    candidate_joint_frame: null, current_joint_frame: null, review,
  }
}

function proposal(id: string, number: number, ops: Op[]): Proposal {
  return {
    id, number, status: 'approved', base_revision_id: 'r0', candidate_revision_id: 'r1', result_revision_id: 'r1',
    rationale: '', created_by: 'agent', created_at: '', auto_applied: 1, unreviewed: 0, held: 0, ops_count: ops.length,
    pass: 'cleanup', policy_version: 'v', notes: [], ops,
  }
}

const assets = [{ id: 'a1' }, { id: 'a2' }] as Asset[]

describe('очередь проверки', () => {
  it('правки нескольких предложений — одна очередь со сквозными номерами и порядком sortApplied', () => {
    const q = buildQueue([
      proposal('p2', 2, [op('b0', 0, 'pending'), op('b1', 1, 'held', ['protected'])]),
      proposal('p1', 1, [op('a0', 0, 'kept'), op('a1', 1, 'pending', ['long_cut']), op('a2', 2, 'pending')]),
    ])
    expect(q.map((i) => [i.op.id, i.n, i.group, i.proposalId])).toEqual([
      ['b1', 5, 'held', 'p2'],
      ['a1', 2, 'risky', 'p1'],
      ['a2', 3, 'pending', 'p1'],
      ['b0', 4, 'pending', 'p2'],
      ['a0', 1, 'done', 'p1'],
    ])
    // Правка не меняется: позиция остаётся своей, для сообщений сервера
    expect(q[0].op.position).toBe(1)
  })

  it('счёт по группам: оставлено и возвращено отдельно', () => {
    const q = buildQueue([proposal('p', 1, [
      op('x', 0, 'kept'), op('y', 1, 'reverted'), op('z', 2, 'pending'), op('w', 3, 'held'), op('v', 4, 'pending', ['high_impact']),
    ])])
    expect(queueCounts(q)).toEqual({ held: 1, risky: 1, pending: 1, kept: 1, reverted: 1 })
  })

  it('после решения — следующая, у последней — предыдущая', () => {
    expect(nextAfter(['a', 'b', 'c'], 'b')).toBe('c')
    expect(nextAfter(['a', 'b', 'c'], 'c')).toBe('b')
    expect(nextAfter(['a'], 'a')).toBeUndefined()
    expect(nextAfter(['a', 'b'], 'x')).toBe('a')
  })

  it('подписи как в макете', () => {
    const o = op('x', 6, 'pending', ['cut_in_speech'])
    expect(opTitle(o)).toBe('Убрано: ложный старт')
    expect(opPlace(o, assets)).toBe('A2 12:04.3 · 3.60 с')
    expect(opRange(o, assets)).toBe('A2 12:04.3–12:07.9 (3.60 с)')
    expect(riskTag(1)).toBe('риск: 1')
    // Перестановка: заголовок и куда встал кусок
    const m: Op = { ...op('m', 0, 'pending', ['reorder']), kind: 'move', reason: 'reorder',
      target: { asset_id: 'a2', src_us: 2_273_100_000, side: 'after' } }
    expect(opTitle(m)).toBe('Переставлено: по указанию автора')
    expect(opRange(m, assets)).toBe('A2 12:04.3–12:07.9 (3.60 с) → после 37:53.1')
    expect(riskTag(2)).toBe('риски: 2')
  })
})

describe('стыки рядом с правкой', () => {
  it('окно соседних: один до, два после, у краёв сдвигается', () => {
    expect(jointWindow(35, 5)).toEqual([4, 8])
    expect(jointWindow(35, 0)).toEqual([0, 4])
    expect(jointWindow(35, 34)).toEqual([31, 35])
    expect(jointWindow(2, 1)).toEqual([0, 2])
    expect(jointWindow(0, 0)).toEqual([0, 0])
  })

  it('ближайший стык к месту правки в том же файле', () => {
    const j = (asset: string, end: number, start: number): Joint =>
      ({ out_frame: 0, before_asset: asset, before_src_end_us: end, after_asset: asset, after_src_start_us: start })
    const joints = [j('a2', 100_000_000, 110_000_000), j('a2', 700_000_000, 720_000_000), j('a1', 724_000_000, 728_000_000)]
    expect(nearestJoint(joints, op('x', 0, 'reverted'))).toBe(1)
    expect(nearestJoint([], op('x', 0, 'reverted'))).toBe(-1)
  })
})

describe('множественное число', () => {
  it.each([
    [1, 'правка'], [2, 'правки'], [5, 'правок'], [11, 'правок'], [21, 'правка'], [38, 'правок'], [104, 'правки'],
  ])('%i %s', (n, word) => {
    expect(plural(n, ['правка', 'правки', 'правок'])).toBe(word)
  })
})
