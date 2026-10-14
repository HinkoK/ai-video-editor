// Очередь проверки после применения (режим «сразу»): правки всех
// применённых предложений одним списком. Чистые функции без интерфейса;
// правило порядка — sortApplied из review.ts.
import { assetRef, REASON_LABEL, sortApplied } from './review'
import type { Asset, Joint, Op, Proposal } from './types'
import { fmtUs } from './time'

export type QueueGroup = 'held' | 'risky' | 'pending' | 'done'

export interface QueueItem {
  op: Op
  /** Предложение, в котором сделана правка: для api.reviewOp */
  proposalId: string
  proposalNumber: number
  /** Сквозной номер правки в очереди, с 1 */
  n: number
  group: QueueGroup
}

export function queueGroup(op: Op): QueueGroup {
  if (op.review === 'held') return 'held'
  if (op.review === 'pending') return op.risk_flags.length > 0 ? 'risky' : 'pending'
  return 'done'
}

/** Правки применённых предложений одной очередью. Номера сквозные: по
 *  номеру предложения, внутри — по порядку правок; порядок очереди — как
 *  в sortApplied (отложенные, рискованные, непроверенные, проверенные). */
export function buildQueue(proposals: Proposal[]): QueueItem[] {
  const items = new Map<string, QueueItem>()
  const renumbered: Op[] = []
  let n = 0
  for (const p of [...proposals].sort((a, b) => a.number - b.number)) {
    for (const op of [...p.ops].sort((a, b) => a.position - b.position)) {
      n += 1
      items.set(op.id, { op, proposalId: p.id, proposalNumber: p.number, n, group: queueGroup(op) })
      // sortApplied сравнивает position: подставляем сквозной номер, чтобы
      // правки разных предложений не перемешались
      renumbered.push({ ...op, position: n - 1 })
    }
  }
  return sortApplied(renumbered).map((o) => items.get(o.id)!)
}

export interface QueueCounts {
  held: number
  risky: number
  pending: number
  kept: number
  reverted: number
}

export function queueCounts(items: QueueItem[]): QueueCounts {
  const c: QueueCounts = { held: 0, risky: 0, pending: 0, kept: 0, reverted: 0 }
  for (const it of items) {
    if (it.group === 'done') c[it.op.review === 'reverted' ? 'reverted' : 'kept'] += 1
    else c[it.group] += 1
  }
  return c
}

/** Куда перейти после решения по правке: следующая в очереди, иначе
 *  предыдущая, иначе никуда. */
export function nextAfter(order: string[], id: string): string | undefined {
  const i = order.indexOf(id)
  if (i < 0) return order[0]
  return order[i + 1] ?? order[i - 1]
}

/** «Убрано: ложный старт»; перестановка — «Переставлено: по указанию автора» */
export function opTitle(op: Op): string {
  if (op.kind === 'move') return 'Переставлено: по указанию автора'
  if (op.kind === 'unmove') return 'Возвращено: порядок как в записи'
  return `${op.kind === 'remove' ? 'Убрано' : 'Возвращено'}: ${REASON_LABEL[op.reason] ?? op.reason}`
}

/** Длительность правки «3.60» (секунды, сотые). */
export function opSeconds(op: Op): string {
  return ((op.src_end_us - op.src_start_us) / 1e6).toFixed(2)
}

/** «A2 12:04.3 · 3.60 с» — место правки в строке очереди. */
export function opPlace(op: Op, assets: Asset[]): string {
  return `${assetRef(assets, op.asset_id)} ${fmtUs(op.src_start_us, 1)} · ${opSeconds(op)} с`
}

/** «A2 12:04.3–12:07.9 (3.60 с)» — место правки в карточке; у перестановки
 *  ещё куда: «→ после 37:53.1». */
export function opRange(op: Op, assets: Asset[]): string {
  const range = `${assetRef(assets, op.asset_id)} ${fmtUs(op.src_start_us, 1)}–${fmtUs(op.src_end_us, 1)} (${opSeconds(op)} с)`
  if (op.kind !== 'move' || !op.target) return range
  return `${range} → ${op.target.side === 'after' ? 'после' : 'перед'} ${fmtUs(op.target.src_us, 1)}`
}

/** «риск: 1», «риски: 2» — пометка в строке очереди. */
export function riskTag(count: number): string {
  return count > 1 ? `риски: ${count}` : `риск: ${count}`
}

/** Окно соседних стыков вокруг выбранного: [начало, конец). */
export function jointWindow(total: number, index: number, before = 1, after = 2): [number, number] {
  const size = Math.min(total, before + after + 1)
  const start = Math.max(0, Math.min(index - before, total - size))
  return [start, start + size]
}

/** Номер стыка, ближайшего к месту правки в исходнике: для правки, у которой
 *  своего стыка в версии нет (возвращена или не применена). -1 — стыков нет. */
export function nearestJoint(joints: Joint[], op: Op): number {
  let best = -1
  let dist = Infinity
  joints.forEach((j, i) => {
    const d = Math.min(
      j.before_asset === op.asset_id ? Math.abs(j.before_src_end_us - op.src_start_us) : Infinity,
      j.after_asset === op.asset_id ? Math.abs(j.after_src_start_us - op.src_end_us) : Infinity,
    )
    if (d < dist) {
      dist = d
      best = i
    }
  })
  return best >= 0 ? best : joints.length > 0 ? 0 : -1
}

/** Русское множественное число: plural(5, ['правка', 'правки', 'правок']). */
export function plural(n: number, forms: [string, string, string]): string {
  const a = Math.abs(n) % 100
  const b = a % 10
  if (a > 10 && a < 20) return forms[2]
  if (b === 1) return forms[0]
  if (b >= 2 && b <= 4) return forms[1]
  return forms[2]
}
