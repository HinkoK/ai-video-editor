import { describe, expect, it } from 'vitest'
import { DEFAULT_HEIGHTS, GAP, MARKS, RULER, clampHeight, edgeAt, laneAt, layoutLanes, neededHeight, parseHeights } from './tracks'

describe('дорожки таймлайна', () => {
  it('графика сверху, над камерой; без камеры — сразу над экраном', () => {
    const lanes = layoutLanes(DEFAULT_HEIGHTS, true)
    expect(lanes.map((l) => l.key)).toEqual(['graphics', 'camera', 'screen', 'audio'])
    expect(lanes[0].top).toBe(RULER + MARKS)
    expect(lanes[1].top).toBe(RULER + MARKS + DEFAULT_HEIGHTS.graphics + GAP)
    expect(layoutLanes(DEFAULT_HEIGHTS, false).map((l) => l.key)).toEqual(['graphics', 'screen', 'audio'])
  })

  it('высоты из настройки: испорченное и лишнее — по умолчанию, края — в пределах', () => {
    expect(parseHeights(null)).toEqual(DEFAULT_HEIGHTS)
    expect(parseHeights('не json')).toEqual(DEFAULT_HEIGHTS)
    expect(parseHeights('{"graphics": 90, "camera": "x"}')).toEqual({ ...DEFAULT_HEIGHTS, graphics: 90 })
    expect(parseHeights('{"graphics": 2, "screen": 9999}')).toEqual({ ...DEFAULT_HEIGHTS, graphics: 14, screen: 200 })
    expect(clampHeight('camera', 70.4)).toBe(70)
  })

  it('звук тянется до низа холста, но не меньше своей высоты', () => {
    const need = neededHeight(DEFAULT_HEIGHTS, true)
    expect(layoutLanes(DEFAULT_HEIGHTS, true, need).at(-1)?.height).toBe(DEFAULT_HEIGHTS.audio)
    expect(layoutLanes(DEFAULT_HEIGHTS, true, need + 100).at(-1)?.height).toBe(DEFAULT_HEIGHTS.audio + 100)
  })

  it('край дорожки под мышью — его можно тянуть; внутри дорожки — сама дорожка', () => {
    const lanes = layoutLanes(DEFAULT_HEIGHTS, true)
    const g = lanes[0]
    expect(edgeAt(lanes, g.top + g.height)).toBe('graphics')
    expect(edgeAt(lanes, g.top + g.height + 3)).toBe('graphics')
    expect(edgeAt(lanes, g.top + 5)).toBeUndefined()
    expect(laneAt(lanes, g.top + 5)).toBe('graphics')
    expect(laneAt(lanes, lanes[1].top + 1)).toBe('camera')
    const audio = lanes.at(-1)!
    expect(edgeAt(lanes, audio.top + audio.height)).toBeUndefined()
  })
})
