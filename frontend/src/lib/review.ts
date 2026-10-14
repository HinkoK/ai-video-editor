// Логика очереди проверки без интерфейса: подписи, фильтры и сборка
// запроса на утверждение. Сервер проверяет те же правила (store.approve);
// здесь они нужны, чтобы показать, чего не хватает, до нажатия кнопки.
import type { Asset, Op, RiskFlag } from './types'

export const REASON_LABEL: Record<string, string> = {
  failed_take: 'неудачный дубль',
  false_start: 'ложный старт',
  restart_gap: 'пауза перед повтором',
  technical_dead_air: 'техническая пауза',
  recording_setup: 'подготовка записи',
  waiting: 'сокращено ожидание',
  vocal_noise: 'кашель, «кхм»',
  other: 'другое',
  restore: 'возврат',
  reorder: 'перестановка',
}

export const RISK_LABEL: Record<RiskFlag, string> = {
  protected: 'задевает защищённое',
  high_impact: 'важные слова: отрицание, условие или число',
  reason_other: 'причина «другое»',
  long_cut: 'длинный вырез',
  cut_in_speech: 'возможно обрезано слово',
  reorder: 'кусок переставлен: посмотреть три стыка',
  splits_graphic: 'граница режет графику: части показываются отдельно',
}

export interface Decision {
  include: boolean
  acknowledged: boolean
  override: string
}

export function initialDecisions(ops: Op[]): Record<string, Decision> {
  return Object.fromEntries(ops.map((o) => [o.id, { include: true, acknowledged: false, override: '' }]))
}

export interface ApprovalRequest {
  body: { exclude_op_ids: string[]; acknowledge_op_ids: string[]; overrides: Record<string, string> }
  /** Чего не хватает: пусто — можно отправлять. */
  missing: string[]
}

export function buildApproval(ops: Op[], decisions: Record<string, Decision>): ApprovalRequest {
  const missing: string[] = []
  const body: ApprovalRequest['body'] = { exclude_op_ids: [], acknowledge_op_ids: [], overrides: {} }
  for (const op of ops) {
    const d = decisions[op.id] ?? { include: true, acknowledged: false, override: '' }
    if (!d.include) {
      body.exclude_op_ids.push(op.id)
      continue
    }
    if (op.risk_flags.length > 0) {
      if (d.acknowledged) body.acknowledge_op_ids.push(op.id)
      else missing.push(`правка ${op.position + 1}: рискованная, отметь, что проверил её отдельно`)
    }
    if (op.risk_flags.includes('protected')) {
      if (d.override.trim()) body.overrides[op.id] = d.override.trim()
      else missing.push(`правка ${op.position + 1}: задевает защищённое, нужна причина`)
    }
  }
  if (body.exclude_op_ids.length === ops.length) missing.push('исключены все правки — это отклонение, а не утверждение')
  return { body, missing }
}

export type RiskFilter = 'all' | 'risky' | 'clean'

export function filterOps(ops: Op[], reason: string, risk: RiskFilter): Op[] {
  return ops.filter(
    (o) =>
      (reason === 'all' || o.reason === reason) &&
      (risk === 'all' || (risk === 'risky' ? o.risk_flags.length > 0 : o.risk_flags.length === 0)),
  )
}

/** Рискованные — сначала: на них нужно внимание. */
export function sortForReview(ops: Op[]): Op[] {
  return [...ops].sort((a, b) => b.risk_flags.length - a.risk_flags.length || a.position - b.position)
}

export const REVIEW_LABEL: Record<Op['review'], string> = {
  pending: 'ещё не проверена',
  kept: 'оставлена',
  reverted: 'возвращена',
  held: 'не применена: задевает жёсткую защиту, ждёт решения',
}

/** Проверка после применения: сначала отложенные, потом непроверенные
 *  рискованные, потом непроверенные, в конце уже проверенные. */
export function sortApplied(ops: Op[]): Op[] {
  const rank = (o: Op) =>
    o.review === 'held' ? 0 : o.review === 'pending' ? (o.risk_flags.length ? 1 : 2) : 3
  return [...ops].sort((a, b) => rank(a) - rank(b) || a.position - b.position)
}

export function assetRef(assets: Asset[], id: string): string {
  const i = assets.findIndex((a) => a.id === id)
  return i >= 0 ? `A${i + 1}` : id
}
