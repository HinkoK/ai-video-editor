// Окна «До» и «После» на экране проверки (screens/review/FrameMonitor.tsx):
// где в версии показывать правку. Чистые функции; кадры — версии.
import { whereInVersion } from './edit'
import { usToFrame, type Rate } from './time'
import type { Revision } from './types'

/** Окно показа в кадрах версии: [from, to); mark — пунктир (вырезанное,
 *  участок кадра), joint — черта стыка */
export interface FrameWindow {
  from: number
  to: number
  mark?: [number, number]
  joint?: number
}

/** Участок кадра или графика, которые окно показывает без них (on: false —
 *  «До») или с ними (on: true — «После», даже если автор их уже вернул) */
export interface Change {
  kind: 'shot' | 'graphic'
  id: string
  on: boolean
}

/** Окно вокруг отрезка версии [a, b) с запасом padUs по краям (по умолчанию секунда). */
export function around(rev: Revision, rate: Rate, a: number, b: number, extra: Partial<FrameWindow> = {}, padUs = 1_000_000): FrameWindow {
  const pad = usToFrame(padUs, rate)
  return { from: Math.max(0, a - pad), to: Math.min(rev.total_frames, b + pad), ...extra }
}

/** Вырез в версии: вырезаемое есть — окно вокруг него, пунктиром; его нет
 *  (возвращённое в версии до правки, вырезанное — после) — окно вокруг стыка. */
export function cutWindow(rev: Revision, rate: Rate, assetId: string, startUs: number, endUs: number, padUs: number): FrameWindow | undefined {
  const r = framesOf(rev, rate, assetId, startUs, endUs)
  if (r) return around(rev, rate, r[0], r[1], { mark: r }, padUs)
  const j = whereInVersion(rev, rate, assetId, startUs)
  return j ? around(rev, rate, j.frame, j.frame, { joint: j.frame }, padUs) : undefined
}

/** Отрезок файла записи [startUs, endUs) в кадрах версии; undefined — его
 *  в версии нет. Части отрезка по кускам версии, соседние в ролике — одним
 *  отрезком. После перестановки части бывают в разных местах
 *  ролика — тогда та, где начало отрезка (самый ранний оставленный момент). */
export function framesOf(rev: Revision, rate: Rate, assetId: string, startUs: number, endUs: number): [number, number] | undefined {
  const parts: { a: number; b: number; from: number }[] = []
  for (const c of rev.clips) {
    if (c.asset_id !== assetId || c.src_start_us >= endUs || startUs >= c.src_end_us) continue
    const from = Math.max(startUs, c.src_start_us)
    const a = c.out_start_frame + usToFrame(from - c.src_start_us, rate)
    const b = c.out_start_frame + usToFrame(Math.min(endUs, c.src_end_us) - c.src_start_us, rate)
    if (b > a) parts.push({ a, b, from })
  }
  if (!parts.length) return undefined
  const glued: { a: number; b: number; from: number }[] = []
  for (const p of [...parts].sort((x, y) => x.a - y.a)) {
    const last = glued[glued.length - 1]
    if (last && last.b === p.a) {
      last.b = p.b
      last.from = Math.min(last.from, p.from)
    } else glued.push({ ...p })
  }
  const start = Math.min(...parts.map((p) => p.from))
  const best = glued.find((g) => g.from === start) ?? glued.reduce((x, y) => (y.b - y.a > x.b - x.a ? y : x))
  return [best.a, best.b]
}
