// Ручной монтаж на таймлайне, как в Adobe Premiere Pro: ходить по дорожке, резать лезвием, удалять куски, обрезать
// до курсора (Q/W), вырезать отрезок между метками входа и выхода — теми же
// клавишами. В нашей модели дыр в ролике нет: любое удаление сдвигает всё
// следующее (ripple), поэтому Delete и Shift+Delete делают одно и то же.
// Перестановка: ⌘X берёт кусок (версия не меняется), ⌘⇧V
// вставляет его со сдвигом перед кадром под курсором.
// Здесь — без интерфейса: точки склеек, куски, перевод отрезка ролика в
// вырезы исходника, раскладка клавиш. Клавиши — по e.code: работают и на
// русской раскладке.
import { frameToUs, usToFrame, type Rate } from './time'
import type { Revision } from './types'

/** Точки, где можно резать и куда прыгать: начало, стыки, надрезы лезвием, конец. */
export function editPoints(total: number, joints: number[], marks: number[]): number[] {
  return [...new Set([0, ...joints, ...marks, total])].filter((f) => f >= 0 && f <= total).sort((a, b) => a - b)
}

/** Кусок между соседними точками, в котором стоит кадр: [начало, конец). */
export function pieceAt(points: number[], frame: number): [number, number] | undefined {
  for (let i = 0; i + 1 < points.length; i++) {
    if (points[i] <= frame && frame < points[i + 1]) return [points[i], points[i + 1]]
  }
  return undefined
}

/** Ближайшая точка строго до кадра (↑ в Premiere) и строго после (↓). */
export function prevPoint(points: number[], frame: number): number | undefined {
  return [...points].reverse().find((p) => p < frame)
}
export function nextPoint(points: number[], frame: number): number | undefined {
  return points.find((p) => p > frame)
}

/** Q — обрезать от предыдущей точки до курсора; W — от курсора до следующей. */
export function trimToPrev(points: number[], playhead: number): [number, number] | undefined {
  const start = prevPoint(points, playhead)
  return start !== undefined ? [start, playhead] : undefined
}
export function trimToNext(points: number[], playhead: number): [number, number] | undefined {
  const b = nextPoint(points, playhead)
  return b !== undefined && playhead < b ? [playhead, b] : undefined
}

/** Отрезки, склеенные там, где касаются или пересекаются. */
export function mergeRanges(ranges: [number, number][]): [number, number][] {
  const out: [number, number][] = []
  for (const [a, b] of [...ranges].filter(([x, y]) => y > x).sort((x, y) => x[0] - y[0])) {
    const last = out[out.length - 1]
    if (last && a <= last[1]) last[1] = Math.max(last[1], b)
    else out.push([a, b])
  }
  return out
}

export interface CutOp {
  kind: 'remove'
  asset_id: string
  src_start_us: number
  src_end_us: number
  reason: 'other'
  why: string
  precise: true
}

/** Отрезок ролика [a, b) кадров — вырезы исходника по кускам версии, которые
 *  он задевает. Граница — точная: если попала в звук, сервер сдвинет её в
 *  ближайший тихий момент рядом (до 60 мс), чтобы не было щелчка. */
export function rangeOps(rev: Revision, rate: Rate, a: number, b: number, why: string): CutOp[] {
  const out: CutOp[] = []
  for (const c of rev.clips) {
    const lo = Math.max(a, c.out_start_frame)
    const hi = Math.min(b, c.out_end_frame)
    if (lo >= hi) continue
    out.push({
      kind: 'remove', asset_id: c.asset_id, reason: 'other', why, precise: true,
      src_start_us: c.src_start_us + frameToUs(lo - c.out_start_frame, rate),
      src_end_us: c.src_start_us + frameToUs(hi - c.out_start_frame, rate),
    })
  }
  return out
}

/** Кусок записи для перестановки: один отрезок одного файла. */
export interface Block {
  asset_id: string
  src_start_us: number
  src_end_us: number
}

/** Отрезок ролика [a, b) кадров — один отрезок записи для перестановки (по
 *  образцу rangeOps). Куски, которые он задевает, должны быть из одного
 *  файла, идти в порядке записи, и между ними — только вырезанное: сервер
 *  переставляет все оставленные кадры отрезка записи. Иначе undefined
 *  (выделение из разных мест записи). */
export function blockOf(rev: Revision, rate: Rate, a: number, b: number): Block | undefined {
  const parts: Block[] = []
  for (const c of rev.clips) {
    const lo = Math.max(a, c.out_start_frame)
    const hi = Math.min(b, c.out_end_frame)
    if (lo >= hi) continue
    parts.push({
      asset_id: c.asset_id,
      src_start_us: c.src_start_us + frameToUs(lo - c.out_start_frame, rate),
      src_end_us: c.src_start_us + frameToUs(hi - c.out_start_frame, rate),
    })
  }
  if (!parts.length) return undefined
  const first = parts[0]
  const last = parts[parts.length - 1]
  for (let i = 1; i < parts.length; i++) {
    if (parts[i].asset_id !== first.asset_id || parts[i].src_start_us < parts[i - 1].src_end_us) return undefined
  }
  // Других оставленных кадров этого отрезка записи в версии нет
  const inside = rev.clips
    .filter((c) => c.asset_id === first.asset_id)
    .reduce((sum, c) => sum + Math.max(0,
      usToFrame(Math.min(c.src_end_us, last.src_end_us), rate) - usToFrame(Math.max(c.src_start_us, first.src_start_us), rate)), 0)
  if (inside !== b - a) return undefined
  return { asset_id: first.asset_id, src_start_us: first.src_start_us, src_end_us: last.src_end_us }
}

export interface MoveOp {
  kind: 'move'
  asset_id: string
  src_start_us: number
  src_end_us: number
  reason: 'reorder'
  why: string
  precise: true
  target_edited_frame: number
  target_side: 'before'
}

/** ⌘⇧V: взятый кусок — перед кадром под курсором (точная правка автора:
 *  граница — в ближайший тихий момент, огрызков у стыков сервер не оставит). */
export function moveOp(block: Block, playhead: number, why: string): MoveOp {
  return { kind: 'move', ...block, reason: 'reorder', why, precise: true, target_edited_frame: playhead, target_side: 'before' }
}

export type Action =
  | 'toggle' | 'reverse' | 'stop' | 'forward'
  | 'frameBack' | 'frameFwd' | 'frameBack5' | 'frameFwd5' | 'prevEdit' | 'nextEdit' | 'home' | 'end'
  | 'markIn' | 'markOut' | 'clearIn' | 'clearOut' | 'clearInOut' | 'extract' | 'lift'
  | 'addEdit' | 'trimPrev' | 'trimNext' | 'delete' | 'undo' | 'redo'
  | 'zoomIn' | 'zoomOut' | 'zoomFit' | 'select' | 'razor' | 'marker' | 'escape'
  | 'take' | 'insertShift' | 'paste'

type Key = Pick<KeyboardEvent, 'code' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>

/** Клавиша → действие, раскладка Premiere Pro по умолчанию (Mac; Ctrl вместо Cmd тоже). */
export function keyAction(e: Key): Action | undefined {
  const cmd = e.metaKey || e.ctrlKey
  if (cmd) {
    if (e.code === 'KeyZ') return e.shiftKey ? 'redo' : 'undo'
    if (e.code === 'KeyY') return 'redo'
    if (e.code === 'KeyK') return 'addEdit'
    // ⌘X — взять кусок, ⌘⇧V — вставить со сдвигом; ⌘C не трогаем
    if (e.code === 'KeyX') return 'take'
    if (e.code === 'KeyV') return e.shiftKey ? 'insertShift' : 'paste'
    return undefined
  }
  if (e.altKey) {
    if (e.code === 'KeyX') return 'clearInOut'
    if (e.code === 'KeyI') return 'clearIn'
    if (e.code === 'KeyO') return 'clearOut'
    if (e.code === 'Backspace' || e.code === 'Delete') return 'delete'
    return undefined
  }
  switch (e.code) {
    case 'Space': return 'toggle'
    case 'KeyJ': return 'reverse'
    case 'KeyK': return 'stop'
    case 'KeyL': return 'forward'
    case 'ArrowLeft': return e.shiftKey ? 'frameBack5' : 'frameBack'
    case 'ArrowRight': return e.shiftKey ? 'frameFwd5' : 'frameFwd'
    case 'ArrowUp': return 'prevEdit'
    case 'ArrowDown': return 'nextEdit'
    case 'Home': return 'home'
    case 'End': return 'end'
    case 'KeyI': return 'markIn'
    case 'KeyO': return 'markOut'
    case 'Quote': return 'extract'
    case 'Semicolon': return 'lift'
    case 'KeyQ': return 'trimPrev'
    case 'KeyW': return 'trimNext'
    case 'Backspace':
    case 'Delete': return 'delete'
    case 'Equal': return 'zoomIn'
    case 'Minus': return 'zoomOut'
    case 'Backslash': return 'zoomFit'
    case 'KeyV': return 'select'
    case 'KeyC': return 'razor'
    case 'KeyM': return 'marker'
    case 'Escape': return 'escape'
    default: return undefined
  }
}

/** Скорость прокрутки J/L: каждое нажатие в ту же сторону — вдвое быстрее (до ×8). */
export function shuttle(current: number, dir: 1 | -1): number {
  if (current * dir <= 0) return dir
  return dir * Math.min(8, Math.abs(current) * 2)
}

/** Где в версии момент записи: кадр ролика. Если это место вырезано — начало
 *  следующего оставленного куска той же записи (jumped), если дальше ничего нет —
 *  undefined. Нужно просмотру: вырезали кусок, где идёт видео, — продолжить
 *  со следующего оставленного, а не доигрывать вырезанное. */
export function whereInVersion(rev: Revision, rate: Rate, assetId: string, us: number):
  { frame: number; us: number; jumped: boolean } | undefined {
  for (const c of rev.clips) {
    if (c.asset_id === assetId && c.src_start_us <= us && us < c.src_end_us) {
      return { frame: c.out_start_frame + usToFrame(us - c.src_start_us, rate), us, jumped: false }
    }
  }
  // Следующий по записи, а не по ролику: после перестановки куски
  // идут не в порядке записи
  let next: Revision['clips'][number] | undefined
  for (const c of rev.clips) {
    if (c.asset_id === assetId && c.src_start_us >= us && (!next || c.src_start_us < next.src_start_us)) next = c
  }
  return next ? { frame: next.out_start_frame, us: next.src_start_us, jumped: true } : undefined
}

