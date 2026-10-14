// Общее для экрана проверки без разметки: подписи строки состояния,
// пометки сервера с понятными A1, A2 и задачи пробных роликов «После».
import { useCallback, useMemo, useState } from 'react'
import { api } from '../../lib/api'
import { assetRef } from '../../lib/review'
import type { Rate } from '../../lib/time'
import type { Job, Project } from '../../lib/types'

/** Внутренние id файлов в пометках сервера -> понятные A1, A2. */
export function withRefs(text: string, project: Project): string {
  return project.assets.reduce((t, a) => t.split(a.id).join(assetRef(project.assets, a.id)), text)
}

export function modeSentence(mode: 'apply' | 'propose'): string {
  return mode === 'apply' ? 'Claude правит сразу, вы проверяете после.' : 'Claude только предлагает, вы утверждаете.'
}

/** «Предложение 3 ждёт решения.» / «Предложения 3, 4 ждут решения.» */
export function openSentence(numbers: number[]): string {
  if (numbers.length === 0) return ''
  return numbers.length === 1
    ? `Предложение ${numbers[0]} ждёт решения.`
    : `Предложения ${numbers.join(', ')} ждут решения.`
}

/** Пробный ролик — 3 с до и после стыка */
export const AROUND_US = 3_000_000

export function previewWindow(jointFrame: number, rate: Rate): [number, number] {
  const around = Math.round((AROUND_US * rate.num) / (rate.den * 1_000_000))
  return [Math.max(0, jointFrame - around), jointFrame + around]
}

export interface Previews {
  /** Задача пробного ролика для стыка версии, если уже есть */
  jobFor: (revisionId: string, jointFrame: number) => string | undefined
  /** Поставить сборку в очередь (по кнопке) */
  start: (revisionId: string, jointFrame: number) => Promise<void>
}

/** Задачи пробных роликов экрана: уже собранные (из списка задач) и
 *  запущенные отсюда. Живут, пока открыт экран, — при переходе между
 *  правками собранное не теряется. */
export function usePreviews(pid: string, rate: Rate | undefined, jobs: Job[] | undefined): Previews {
  const [started, setStarted] = useState<Record<string, string>>({})
  const known = useMemo(() => {
    const out: Record<string, Job> = {}
    for (const j of jobs ?? []) {
      if (j.type !== 'preview' || !j.revision_id) continue
      const key = `${j.revision_id}:${String(j.params.out_start_frame)}`
      // Последняя по времени задача на это окно
      if (!out[key] || out[key].created_at < j.created_at) out[key] = j
    }
    return out
  }, [jobs])
  const jobFor = useCallback((rid: string, joint: number) => {
    if (!rate) return undefined
    const [from, to] = previewWindow(joint, rate)
    const key = `${rid}:${joint}`
    if (started[key]) return started[key]
    const j = known[`${rid}:${from}`]
    // Сервер обрезает конец окна по концу ролика: конец мог стать меньше
    const end = Number(j?.params.out_end_frame)
    return j && end > joint && end <= to && j.state !== 'cancelled' ? j.id : undefined
  }, [rate, started, known])
  const start = useCallback(async (rid: string, joint: number) => {
    if (!rate) return
    const [from, to] = previewWindow(joint, rate)
    const j = await api.preview(pid, rid, from, to)
    setStarted((s) => ({ ...s, [`${rid}:${joint}`]: j.id }))
  }, [pid, rate])
  return { jobFor, start }
}
