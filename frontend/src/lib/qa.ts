// Отчёт качества экспорта для показа: названия проверок кода и подробности
// из того, что сервер записал в technical (backend/src/longform/worker.py,
// _check_export). Ничего сверх этих данных не выводим.
import type { QaCheck, QaReport } from './types'
import { groupDigits } from './units'

const TITLES: Record<string, string> = {
  frames: 'Число кадров',
  constant_frame_rate: 'Постоянная частота кадров',
  audio_samples: 'Длина звука до отсчёта',
  matches_revision: 'Сверка с исходником',
  protected_ranges: 'Защищённое не вырезано без причины',
  revision: 'Версия утверждена',
  agent_review: 'Правки Claude проверены',
  loudness: 'Громкость',
}

export function checkTitle(key: string): string {
  return TITLES[key] ?? key
}

/** Проверка без явного ok: false считается пройденной (как на сервере). */
export function checkPassed(check: QaCheck): boolean {
  return check.ok !== false
}

/** Для показа: пройдено, не пройдено или напоминание. Проверка правок
 *  Claude на сервере не ошибка (ok всегда true), а напоминание: если что-то
 *  не проверено или ждёт решения, писать «пройдено» нельзя. */
export function checkStatus(key: string, check: QaCheck): 'ok' | 'fail' | 'remind' {
  if (!checkPassed(check)) return 'fail'
  if (key === 'agent_review' && (num(check.unreviewed) ?? 0) + (num(check.held) ?? 0) > 0) return 'remind'
  // Громкость не дотянута до цели из-за пиков — не ошибка, но и не «пройдено»
  if (key === 'loudness' && check.capped_by_peak === true) return 'remind'
  return 'ok'
}

const num = (v: unknown): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)
const count = (v: unknown): number | undefined => (Array.isArray(v) ? v.length : num(v))

function compare(actual: number | undefined, expected: number | undefined, unit = ''): string {
  if (actual === undefined || expected === undefined) return ''
  const u = unit ? ` ${unit}` : ''
  return actual === expected
    ? `${groupDigits(actual)} = ${groupDigits(expected)}${u}`
    : `получено ${groupDigits(actual)}, нужно ${groupDigits(expected)}${u}`
}

/** Подробность под названием проверки; пустая строка, если данных нет. */
export function checkDetail(key: string, check: QaCheck): string {
  switch (key) {
    case 'frames':
      return compare(num(check.actual), num(check.expected))
    case 'audio_samples':
      if (check.actual === null) return 'длину звуковой дорожки прочитать не удалось'
      return compare(num(check.actual), num(check.expected), 'отсчётов')
    case 'matches_revision': {
      const parts: string[] = []
      const points = num(check.points)
      const audio = num(check.audio_checked)
      const ambiguous = num(check.video_ambiguous)
      const video = count(check.video_mismatches)
      const sound = count(check.audio_mismatches)
      if (points !== undefined) parts.push(`мест сверки: ${points}`)
      if (audio !== undefined) parts.push(`звук проверен в ${audio}`)
      if (ambiguous) parts.push(`неразличимо: ${ambiguous}`)
      if (video) parts.push(`расхождений в кадре: ${video}`)
      if (sound) parts.push(`расхождений в звуке: ${sound}`)
      return parts.join(' · ')
    }
    case 'protected_ranges': {
      const n = count(check.cut_without_reason)
      return n === undefined ? '' : `вырезов в защищённом без причины: ${n}`
    }
    case 'revision': {
      const n = num(check.number)
      return n === undefined ? '' : `версия ${n}`
    }
    case 'agent_review': {
      const unreviewed = num(check.unreviewed)
      const held = num(check.held)
      return [
        unreviewed !== undefined ? `не проверено: ${unreviewed}` : '',
        held !== undefined ? `ждут решения: ${held}` : '',
      ].filter(Boolean).join(' · ')
    }
    case 'loudness': {
      const db = (v: number) => v.toFixed(1).replace('.', ',').replace('-', '−')
      const before = num(check.before_lufs)
      const after = num(check.after_lufs)
      const target = num(check.target_lufs)
      const gain = num(check.gain_db)
      const peak = num(check.after_true_peak)
      return [
        before !== undefined && after !== undefined ? `${db(before)} → ${db(after)} LUFS` : '',
        target !== undefined ? `цель ${db(target)}` : '',
        gain !== undefined && check.applied ? `усиление ${gain > 0 ? '+' : ''}${db(gain)} дБ` : 'без усиления',
        peak !== undefined ? `пик ${db(peak)} dBTP` : '',
        check.capped_by_peak ? 'ограничено пиками' : '',
      ].filter(Boolean).join(' · ')
    }
    default:
      return ''
  }
}

export function checksSummary(report: Pick<QaReport, 'technical'>): { passed: number; total: number; reminders: number } {
  const all = Object.entries(report.technical).map(([key, check]) => checkStatus(key, check))
  return {
    passed: all.filter((s) => s === 'ok').length,
    total: all.length,
    reminders: all.filter((s) => s === 'remind').length,
  }
}

/** Номер версии, из которой собран ролик (записан в проверке «revision»). */
export function reportRevisionNumber(report: Pick<QaReport, 'technical'>): number | undefined {
  return num(report.technical.revision?.number)
}

/** Сколько кадров в готовом ролике (по раскодированному результату). */
export function reportFrames(report: Pick<QaReport, 'technical'>): number | undefined {
  return num(report.technical.frames?.actual)
}
