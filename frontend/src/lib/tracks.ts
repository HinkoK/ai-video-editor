// Дорожки таймлайна сверху вниз, как в Premiere: графика (V3), камера (V2),
// экран (V1), звук. Высоту каждой видеодорожки автор меняет, потянув за её
// нижний край; звук занимает остальное место (не меньше своей высоты).
// Чистые функции.

export type TrackKey = 'graphics' | 'camera' | 'screen' | 'audio'
export type TrackHeights = Record<TrackKey, number>

export const RULER = 24 // шкала времени
export const MARKS = 18 // полоса меток
export const GAP = 6 // между дорожками
export const BOTTOM_PAD = 8

export const DEFAULT_HEIGHTS: TrackHeights = { graphics: 32, camera: 48, screen: 48, audio: 60 }
const LIMITS: Record<TrackKey, [number, number]> = {
  graphics: [14, 160], camera: [16, 200], screen: [16, 200], audio: [24, 300],
}

export const clampHeight = (key: TrackKey, h: number): number =>
  Math.round(Math.min(LIMITS[key][1], Math.max(LIMITS[key][0], h)))

/** Высоты из настройки браузера (JSON); чего нет или что испорчено — по умолчанию. */
export function parseHeights(saved: string | null | undefined): TrackHeights {
  let raw: Partial<Record<TrackKey, unknown>> = {}
  try {
    const v = saved ? JSON.parse(saved) : {}
    if (v && typeof v === 'object') raw = v
  } catch {
    // испорченная настройка — просто по умолчанию
  }
  const out = { ...DEFAULT_HEIGHTS }
  for (const key of Object.keys(DEFAULT_HEIGHTS) as TrackKey[]) {
    const h = raw[key]
    if (typeof h === 'number' && Number.isFinite(h)) out[key] = clampHeight(key, h)
  }
  return out
}

export interface Lane {
  key: TrackKey
  top: number
  height: number
}

/** Дорожки сверху вниз. Камеры нет — её дорожки тоже нет. Звук тянется до
 *  низа холста, если холст выше суммы дорожек. */
export function layoutLanes(heights: TrackHeights, withCamera: boolean, canvasHeight = 0): Lane[] {
  const lanes: Lane[] = []
  let y = RULER + MARKS
  const order: TrackKey[] = withCamera ? ['graphics', 'camera', 'screen'] : ['graphics', 'screen']
  for (const key of order) {
    lanes.push({ key, top: y, height: heights[key] })
    y += heights[key] + GAP
  }
  lanes.push({ key: 'audio', top: y, height: Math.max(heights.audio, canvasHeight - BOTTOM_PAD - y) })
  return lanes
}

/** Сколько точек нужно холсту, чтобы все дорожки поместились целиком. */
export function neededHeight(heights: TrackHeights, withCamera: boolean): number {
  const audio = layoutLanes(heights, withCamera).at(-1) as Lane
  return audio.top + heights.audio + BOTTOM_PAD
}

/** Нижний край какой видеодорожки под мышью (±4 точки, вместе с зазором) —
 *  его можно тянуть. Звук тянется до низа холста — его край не берём. */
export function edgeAt(lanes: Lane[], y: number): TrackKey | undefined {
  for (const lane of lanes) {
    if (lane.key === 'audio') continue
    const edge = lane.top + lane.height
    if (y >= edge - 4 && y <= edge + GAP) return lane.key
  }
  return undefined
}

/** Дорожка под мышью. */
export function laneAt(lanes: Lane[], y: number): TrackKey | undefined {
  return lanes.find((l) => y >= l.top && y < l.top + l.height)?.key
}
