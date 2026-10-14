import { describe, expect, it } from 'vitest'
import { cameraScale, graphicsAt, joinedRanges, pipGeometry, pyRound, sideGeometry, specAt } from './layout'
import type { GraphicItem, VisualLayer } from './types'

// Эталон — числа сборки ролика (render/compose.py), снятые для этих же входов
describe('монитор раскладывает кадр как сборка ролика', () => {
  it('округление как в Python', () => {
    expect([pyRound(0.5), pyRound(1.5), pyRound(2.5), pyRound(2.6)]).toEqual([0, 2, 2, 3])
  })
  it('экран в рамке и лицо сбоку', () => {
    const g = (spec: object, w: number, h: number, sw: number, sh: number) => {
      const r = sideGeometry({ layout: 'side', ...spec }, w, h, sw, sh)
      return [r.card, r.face.slice(0, 3), r.border, r.radius]
    }
    expect(g({}, 1920, 1080, 2560, 1440)).toEqual([[62, 66, 1688, 950], [1546, 692, 348], 7, 20])
    expect(g({ region: { x: 0, y: 0, w: 0.6, h: 1 } }, 1920, 1080, 2560, 1440)).toEqual([[24, 48, 1026, 962], [1054, 488, 534], 7, 20])
    expect(g({ region: { x: 0.1, y: 0.05, w: 0.5, h: 0.9 } }, 1280, 720, 1920, 1080)).toEqual([[16, 32, 634, 642], [652, 324, 356], 4, 13])
    // Сдвинуто автором мышью
    expect(g({ face: { x: 0.05, y: 0.6, size: 0.2 }, card: { x: 0.3, y: 0.05, w: 0.65 } }, 1920, 1080, 1920, 1080))
      .toEqual([[576, 54, 1248, 702], [96, 648, 384], 7, 20])
  })
  it('лицо в углу', () => {
    expect(pipGeometry({ layout: 'pip' }, 1920, 1080, 1920, 1080)).toEqual([1450, 794, 422, 238])
    expect(pipGeometry({ layout: 'pip', slot: 'tl', size: 0.3 }, 1280, 720, 1920, 1080)).toEqual([32, 32, 384, 216])
    expect(pipGeometry({ layout: 'pip', face: { x: 0.1, y: 0.1, size: 0.25 } }, 1920, 1080, 1920, 1080)).toEqual([192, 108, 480, 270])
  })
  it('участок под курсором, иначе раскладка проекта', () => {
    const layer = {
      default: { layout: 'side' },
      segments: [
        { id: 'a', layout: 'camera', params: { punch: { scale: 1.2, cx: 0.5, cy: 0.4 } }, review: 'kept', edited_ranges: [[100, 200]] },
        { id: 'b', layout: 'screen', params: {}, review: 'reverted', edited_ranges: [[300, 400]] },
      ],
    } as unknown as VisualLayer
    expect(specAt(layer, 150).segment?.id).toBe('a')
    expect(specAt(layer, 350).spec.layout).toBe('side')
    expect(specAt(layer, 50).segment).toBeUndefined()
  })
  it('смена плана на склейках и укрупнение', () => {
    const alt = { layout: 'camera' as const, alternate: { step: 0.08, cx: 0.5, cy: 0.42 } }
    // Склейки на 100 и 150 (кусок 100–150 короче секунды при 60 к/с — план не меняет)
    expect(cameraScale(alt, [0, 400], [100, 150, 300], 50, 60).scale).toBe(1)
    expect(cameraScale(alt, [0, 400], [100, 150, 300], 120, 60).scale).toBe(1)
    expect(cameraScale(alt, [0, 400], [100, 150, 300], 200, 60).scale).toBeCloseTo(1.08)
    expect(cameraScale(alt, [0, 400], [100, 150, 300], 350, 60).scale).toBe(1)
    const punch = { layout: 'camera' as const, punch: { scale: 1.2, cx: 0.5, cy: 0.4, release: 2 } }
    expect(cameraScale(punch, [0, 400], [], 0, 60).scale).toBeCloseTo(1.2)
    expect(cameraScale(punch, [0, 400], [], 240, 60).scale).toBeCloseTo(1)
  })
  it('графика: сколько рисунка прошло, через склейку — продолжение', () => {
    // Вырез внутри графики: части в ролике встык — один рисунок
    const g = { id: 'g', review: 'kept', edited_ranges: [[0, 30], [30, 60]] } as unknown as GraphicItem
    expect(graphicsAt([g], 40, 30)).toEqual([{ item: g, ms: 1333.3333333333333, total: 2000 }])
    expect(graphicsAt([g], 60, 30)).toEqual([])
  })
  it('графика, разрезанная перестановкой, — части не встык рисуются отдельно, как при сборке', () => {
    // Кусок с концом графики переставлен в начало ролика: [60, 90) раньше по записи, [0, 30) — в ролике раньше
    const g = { id: 'g', review: 'kept', edited_ranges: [[60, 90], [0, 30]] } as unknown as GraphicItem
    expect(graphicsAt([g], 70, 30)).toEqual([{ item: g, ms: 333.3333333333333, total: 1000 }])
    expect(graphicsAt([g], 0, 30)).toEqual([{ item: g, ms: 0, total: 1000 }])
    expect(graphicsAt([g], 40, 30)).toEqual([])
    expect(joinedRanges([[60, 90], [0, 30], [30, 45]])).toEqual([[0, 45], [60, 90]])
  })
})
