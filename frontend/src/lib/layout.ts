// Раскладка кадра для монитора программы — те же размеры, что у сборки ролика
// (backend/src/longform/render/compose.py: side_geometry, pip_geometry), чтобы
// на мониторе было ровно то, что выйдет в ролике. Округление — как в Python
// (половина — к чётному), иначе рамки расходились бы на пиксель.
import type { GraphicItem, VisualLayer, VisualSegment } from './types'

const SIDE_REF = [2560, 1440] as const
const SIDE_BORDER = 9
const SIDE_RADIUS = 26
const SIDE_SCREEN = { card: [83, 87, 2250, 1266], face: [2061, 922, 463] } as const
const SIDE_WINDOW = { cardXyH: [32, 64, 1283], face: 711, faceGap: 4, faceDrop: 14 } as const
const PIP_MARGIN = 1 / 40

/** round() Python: половина — к чётному. */
export function pyRound(x: number): number {
  const r = Math.round(x)
  return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r
}
const even = (x: number) => Math.max(2, pyRound(x / 2) * 2)

export type Spec = { layout: VisualSegment['layout'] } & VisualSegment['params']
export type Box = [number, number, number, number] // x, y, ширина, высота — точки кадра

export interface SideGeometry {
  card: Box
  face: Box // квадрат: ширина = высота
  border: number
  radius: number
}

/** Экран в рамке и квадрат лица сбоку (side). */
export function sideGeometry(spec: Spec, w: number, h: number, scrW: number, scrH: number): SideGeometry {
  const kx = w / SIDE_REF[0]
  const ky = h / SIDE_REF[1]
  const r = spec.region ?? { x: 0, y: 0, w: 1, h: 1 }
  const aspect = (r.w * scrW) / (r.h * scrH)
  const border = Math.max(2, pyRound(SIDE_BORDER * kx))
  const radius = Math.max(2, pyRound(SIDE_RADIUS * kx))
  let card: Box
  let face: Box
  if (aspect >= 1.6) {
    const [x, y, cw, ch] = SIDE_SCREEN.card
    let fw = cw
    let fh = cw / aspect
    if (fh > ch) {
      fw = ch * aspect
      fh = ch
    }
    card = [even((x + (cw - fw) / 2) * kx), even((y + (ch - fh) / 2) * ky), even(fw * kx), even(fh * ky)]
    const [fx, fy, fs] = SIDE_SCREEN.face
    face = [even(fx * kx), even(fy * ky), even(fs * kx), even(fs * kx)]
  } else {
    const [x, y, ch0] = SIDE_WINDOW.cardXyH
    const fs = SIDE_WINDOW.face
    let ch = ch0
    let cw = ch * aspect
    const room = SIDE_REF[0] - x - SIDE_WINDOW.faceGap - fs - x
    if (cw > room) {
      cw = room
      ch = room / aspect
    }
    card = [even(x * kx), even(y * ky), even(cw * kx), even(ch * ky)]
    const faceY = y + ch + SIDE_WINDOW.faceDrop - fs
    face = [even((x + cw + SIDE_WINDOW.faceGap) * kx), even(faceY * ky), even(fs * kx), even(fs * kx)]
  }
  if (spec.card) {
    const cw = even(spec.card.w * w)
    card = [even(spec.card.x * w), even(spec.card.y * h), cw, even(cw / aspect)]
  }
  if (spec.face) {
    const s = even(spec.face.size * w)
    face = [even(spec.face.x * w), even(spec.face.y * h), s, s]
  }
  return { card, face, border, radius }
}

/** Лицо в углу поверх экрана (pip). */
export function pipGeometry(spec: Spec, w: number, h: number, camW: number, camH: number): Box {
  if (spec.face) {
    const pw = even(w * spec.face.size)
    return [even(spec.face.x * w), even(spec.face.y * h), pw, even((pw * camH) / camW)]
  }
  const pw = even(w * (spec.size ?? 0.22))
  const ph = even((pw * camH) / camW)
  const m = even(w * PIP_MARGIN)
  const slot = spec.slot ?? 'br'
  const x = slot === 'br' || slot === 'tr' ? w - pw - m : m
  const y = slot === 'br' || slot === 'bl' ? h - ph - m : m
  return [x, y, pw, ph]
}

/** Участок кадра в точке ролика: свой (не возвращённый автором) или раскладка проекта. */
export function specAt(layer: VisualLayer | undefined, frame: number):
  { spec: Spec; segment?: VisualSegment; range?: [number, number] } {
  for (const s of layer?.segments ?? []) {
    if (s.review === 'reverted') continue
    const range = s.edited_ranges.find(([a, b]) => a <= frame && frame < b)
    if (range) return { spec: { layout: s.layout, ...s.params }, segment: s, range }
  }
  return { spec: layer?.default ?? { layout: 'screen' } }
}

/** Части графики в ролике, как при сборке (graphics._joined): идущие встык
 *  (склейка внутри графики) — один рисунок, не встык (кусок переставлен,
 *  step-29) — каждая своим рисунком с начала. */
export function joinedRanges(ranges: [number, number][]): [number, number][] {
  const out: [number, number][] = []
  for (const [a, b] of [...ranges].sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1]
    if (last && last[1] === a) last[1] = b
    else out.push([a, b])
  }
  return out
}

/** Графика, видная в точке ролика, и сколько миллисекунд её рисунка прошло
 *  (кусок после склейки продолжает рисунок, часть после перестановки —
 *  отдельный рисунок, как при сборке). */
export function graphicsAt(items: GraphicItem[], frame: number, fps: number): { item: GraphicItem; ms: number; total: number }[] {
  const out = []
  for (const g of items) {
    if (g.review === 'reverted') continue
    const part = joinedRanges(g.edited_ranges).find(([a, b]) => a <= frame && frame < b)
    if (part) out.push({ item: g, ms: ((frame - part[0]) / fps) * 1000, total: ((part[1] - part[0]) / fps) * 1000 })
  }
  return out
}

/** Камера во весь кадр: масштаб в этот момент (укрупнение, смена плана на склейках). */
export function cameraScale(spec: Spec, range: [number, number] | undefined, joints: number[], frame: number, fps: number):
  { scale: number; cx: number; cy: number } {
  if (spec.alternate && range) {
    // Куски между склейками участка по очереди общий и крупнее; кусок короче секунды — как предыдущий
    const cuts = [range[0], ...joints.filter((j) => j > range[0] && j < range[1]), range[1]]
    let tight = false
    for (let k = 0; k + 1 < cuts.length; k++) {
      if (k && cuts[k + 1] - cuts[k] >= fps) tight = !tight
      if (frame < cuts[k + 1]) break
    }
    return { scale: tight ? 1 + spec.alternate.step : 1, cx: spec.alternate.cx, cy: spec.alternate.cy }
  }
  if (spec.punch) {
    const t = range ? (frame - range[0]) / fps : 0
    const s = spec.punch.scale
    const smooth = (p: number) => p * p * (3 - 2 * p)
    let scale = s
    if (spec.punch.release) scale = s - (s - 1) * smooth(Math.min(1, t / spec.punch.release))
    else if (spec.punch.attack) scale = 1 + (s - 1) * smooth(Math.min(1, t / spec.punch.attack))
    return { scale, cx: spec.punch.cx, cy: spec.punch.cy }
  }
  return { scale: 1, cx: 0.5, cy: 0.5 }
}

/** Источник, вписанный в рамку целиком (fill — с обрезкой краёв, как _fill в сборке):
 *  какую часть источника брать, чтобы закрыть рамку. */
export function coverCrop(srcW: number, srcH: number, boxW: number, boxH: number, scale = 1, cx = 0.5, cy = 0.5) {
  const k = Math.max(boxW / srcW, boxH / srcH) * scale
  const sw = boxW / k
  const sh = boxH / k
  const sx = Math.max(0, Math.min(srcW - sw, cx * srcW - sw / 2))
  const sy = Math.max(0, Math.min(srcH - sh, cy * srcH - sh / 2))
  return { sx, sy, sw, sh }
}
